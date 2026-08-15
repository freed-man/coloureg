"""
Paint fallback resolution.

When the initial VDG bundle call returns a vehicle but NO paint code, this
module tries to recover the paint code two ways IN PARALLEL and returns
whichever produces a code first:

  1. VDG paint retry   -- an immediate second paint_lookup() call.
     Empirically this sometimes recovers paint that the first call missed
     (VDG's upstream paint source is intermittent/slow on the first hit).
     Fast (~a few seconds) and cheap-ish (~£0.15-0.50), so it's worth racing.

  2. pl24 scrape  -- call the pl24 service's /lookup-paint with the VIN we
     already got from the first VDG call. pl24 scrapes partslink24's catalogue,
     which carries codes VDG often doesn't (esp. commercial vehicles). Slower
     (~3-60s depending on routing) but a different data source, so it catches
     misses the VDG retry can't.

Design notes:
  - Both run on threads and we take the FIRST that returns a usable paint code,
    with a deterministic preference for the VDG retry when both produce a code
    (it's cheaper — already paid for — and faster). See resolve_paint for how
    the preference is enforced even when both finish in the same wait() batch.
  - This function is SYNCHRONOUS and may block for up to ~PL24_TIMEOUT seconds.
    It is intended to be called from the background/status path (where the user
    is already looking at their vehicle data), NOT inline in the main lookup
    request.
  - Nothing here raises on a miss; failures degrade to "no paint found" so the
    caller always gets a clean result dict (or None).
"""

import concurrent.futures
import logging
import os
import threading
import time

import requests

from .vdg import paint_lookup, VdgError
from . import oneauto
from .routing import should_call_oneauto


def _enrich_from_lookup(result, make, model=None, vdg_colour=None):
    """Fill gaps in a provider result from the PaintLookup table, BEHIND the
    live race (we only fill what VDG/pl24 didn't return).

    Two directions:
      - code but blank description  -> fill the colour name  (reliable: a code
        is 1:1 with a colour within a make)
      - colour name but blank code  -> fill the code, but ONLY if the name maps
        to exactly one code within the make (names are 1:many, so ambiguous
        names are left as-is — a wrong code is worse than none); this also
        clears the name_only flag so it counts as a full result

    Always attaches a hex swatch when one is available for the resolved code.
    Never raises; enrichment failure must never break the result. Returns the
    (possibly mutated) result dict, or the original unchanged on miss/None.
    """
    if not result or not make:
        return result
    try:
        # Import here to avoid a circular import at module load.
        from lookup.models import PaintLookup

        code = (result.get('paint_code') or '').strip()
        desc = (result.get('paint_description') or '').strip()

        if code and not desc:
            # code -> name (+ swatch)
            # vdg_colour lets a two-tone row order its halves BODY FIRST
            # (paint47). Without it "Z11 + Qab" expands black-first even though
            # DVLA reports the car White and QAB is the Pearl White.
            hex_val, name, _canon = PaintLookup.lookup_with_canonical(
                manufacturer=make, paint_code=code,
                # paint49: passed in, NOT read off the result. pl24 results have
                # no 'colour' key, and pl24 is where combination codes come from.
                vdg_colour=vdg_colour or result.get('colour') or '',
            )
            if name:
                result['paint_description'] = name
                # the NAME was supplied by our database, not the provider
                result['enriched_from'] = 'name'
            if hex_val and not result.get('paint_hex'):
                result['paint_hex'] = hex_val

        elif desc and not code:
            # name -> code (conservative: unique match only)
            found_code, hex_val, _canon_name = PaintLookup.code_from_name(
                manufacturer=make, colour_name=desc, model=model,
            )
            if found_code:
                result['paint_code'] = found_code
                # the CODE was supplied by our database, not the provider
                result['enriched_from'] = 'code'
                if hex_val and not result.get('paint_hex'):
                    result['paint_hex'] = hex_val
                # a code was found → no longer a name-only result
                if result.get('name_only'):
                    result['name_only'] = False
    except Exception:
        pass
    return result


# pl24 service base URL + auth. In production PL24_BASE_URL is set to the private
# Railway address (http://pl24.railway.internal:8080) — pl24's public domain has
# been removed, so the default below points at the private address too (a missing
# env var should fail toward the real, private service, not a dead public URL).
PL24_BASE_URL = os.environ.get(
    'PL24_BASE_URL', 'http://pl24.railway.internal:8080'
).rstrip('/')
PL24_API_KEY = os.environ.get('PL24_API_KEY', '')

# How long resolve_paint waits for pl24 before giving up. Set just ABOVE pl24's
# own internal ceiling (~60s worst case, when it walks the full fallback chain:
# catalog -> commercial sibling -> Classic sibling -> dashboard). Matching it
# this way means pl24 always gets to finish its own attempt before we abandon
# it — capping lower would truncate exactly the slow commercial-vehicle lookups
# pl24 exists to rescue, throwing away codes it would have found seconds later.
# The long wait is acceptable because resolve_paint runs in the background while
# the user already sees their vehicle data; the results page communicates the
# wait ("checking manufacturer database...") rather than appearing frozen.
logger = logging.getLogger(__name__)

PL24_TIMEOUT = float(os.environ.get('PL24_CLIENT_TIMEOUT_S', '65'))

# Concurrency cap on the recovery race (paint19).
#
# resolve_paint parks its calling thread for up to PL24_TIMEOUT seconds. Gunicorn
# runs 2 workers x 8 threads = 16 concurrent requests, so 16 simultaneous
# paint-miss lookups park EVERY thread — including the one that would answer
# Railway's healthcheck. Failing healthchecks get the container restarted, which
# kills whatever lookups are in flight; once payments are live that means a
# customer charged mid-fulfilment.
#
# A plain semaphore would not help: a thread blocked waiting on it is just as
# parked. So callers TRY to acquire and are told to come back if they cannot,
# leaving the thread free immediately. The default of 10 keeps 6 threads clear
# for ordinary traffic and the healthcheck.
MAX_CONCURRENT_RECOVERIES = int(os.environ.get('MAX_CONCURRENT_RECOVERIES', '10'))
_recovery_slots = threading.BoundedSemaphore(MAX_CONCURRENT_RECOVERIES)


def acquire_recovery_slot():
    """Non-blocking. True if a slot was taken (caller MUST release it)."""
    return _recovery_slots.acquire(blocking=False)


def release_recovery_slot():
    try:
        _recovery_slots.release()
    except ValueError:
        # BoundedSemaphore raises if released more times than acquired. Never
        # let bookkeeping take down a request that has already done its work.
        pass

# requests timeout as (connect, read): cap connection setup tightly (the pl24
# service is on the same platform/region, so a slow connect means trouble), and
# allow the read to run up to the overall budget for the scrape itself.
_PL24_HTTP_TIMEOUT = (5.0, PL24_TIMEOUT)


# Counter of retry-billing writes still in flight. Production does not need
# this — the write lands whenever it lands, and nothing reads the cost that
# quickly. It exists so tests can wait for the asynchronous write deterministically
# instead of racing it.
_pending_lock = threading.Lock()
_pending_count = 0


def _recovery_writes_pending():
    """True while a retry-billing write has not finished. Test support only."""
    with _pending_lock:
        return _pending_count > 0


def _record_worker_result(search_id, **fields):
    """Write a column straight to the Search row from a worker thread (paint26).

    Same problem and same shape as _record_retry_billing: resolve_paint returns
    as soon as one path wins, so the losing worker finishes AFTER the caller has
    read its telemetry and saved. Anything it learned has to be written by the
    worker itself or it is lost.

    An atomic UPDATE on named columns only, so it cannot lose a race against the
    caller's save and cannot clobber a column it does not own. Best-effort
    throughout — recording an observation must never break a lookup a customer
    is waiting on.
    """
    if search_id is None or not fields:
        return
    global _pending_count
    with _pending_lock:
        _pending_count += 1
    try:
        from lookup.models import Search
        Search.objects.filter(pk=search_id).update(**fields)
    except Exception:  # noqa: BLE001
        logger.warning('worker result not recorded for search=%s', search_id,
                       exc_info=True)
    finally:
        with _pending_lock:
            _pending_count -= 1
        # Worker threads get their own Django connection and nothing else
        # closes it; without this each recovery would strand one on Neon.
        try:
            from django.db import connections as _c
            _c.close_all()
        except Exception:  # noqa: BLE001
            pass


def _record_retry_billing(search_id, cost, balance, retry_code):
    """Write the retry's own spend straight to its Search row (paint21).

    The retry used to hand its cost back through the telemetry dict, which the
    caller read AFTER resolve_paint returned. That works only if the retry
    finishes first. When pl24 wins the race, resolve_paint returns immediately
    and the retry is still in flight — cancel_futures cannot stop it, because
    with max_workers=2 it already started — so it completes, VDG bills us, and
    the cost lands in a dict nobody reads again.

    Measured on real traffic: every partslink24 row recording 0.08 was followed
    by exactly the abandoned retry's charge appearing on the NEXT balance
    reading. 5 of 5, no false positives, about GBP1/day and always under.

    Writing from here fixes it regardless of timing: whenever this worker
    finishes, it adds its own cost to the row. An atomic UPDATE, so it cannot
    lose against the caller's save, and Coalesce because the column is nullable
    and NULL + x is NULL in SQL.

    Best-effort by design: bookkeeping must never break a lookup the customer
    is waiting on.
    """
    if search_id is None:
        return
    global _pending_count
    with _pending_lock:
        _pending_count += 1
    try:
        from decimal import Decimal
        from django.db.models import DecimalField, F, Value
        from django.db.models.functions import Coalesce
        from lookup.models import Search

        updates = {}
        if cost is not None:
            updates['vdg_transaction_cost'] = Coalesce(
                F('vdg_transaction_cost'), Value(Decimal('0')),
                output_field=DecimalField(max_digits=10, decimal_places=2),
            ) + Decimal(str(cost))
        if balance is not None:
            # The retry is the newer call, so its balance is the fresher truth.
            updates['vdg_balance_after_call'] = Decimal(str(balance))
        if retry_code is not None:
            updates['vdg_retry_code'] = (retry_code or '')[:100]
        if updates:
            Search.objects.filter(pk=search_id).update(**updates)
    except Exception:  # noqa: BLE001 — never let bookkeeping break a lookup
        logger.warning('retry billing not recorded for search=%s', search_id,
                       exc_info=True)
    finally:
        with _pending_lock:
            _pending_count -= 1
        # This runs on a pool thread. Django opens a connection per thread and
        # nothing here closes it, so without this each recovery would strand one
        # on Neon (conn_max_age=200 keeps it alive well past the thread's life).
        try:
            from django.db import connections as _c
            _c.close_all()
        except Exception:  # noqa: BLE001
            pass


def _vdg_retry(registration, telemetry=None, search_id=None):
    """Second VDG bundle call. Returns a paint dict if paint came back, else
    None. Never raises — VDG errors degrade to None (no recovery).

    COST (paint15): VDG bills this call whether or not it returns paint — a
    paint-less call is partially refunded and nets ~£0.12, a hit costs the full
    ~£0.45. That spend used to vanish entirely: this function only surfaced a
    value on a hit, so the retry's cost never reached the Search row. Since ~58%
    of lookups trigger a retry, every downstream total undercounted badly — and
    the daily budget breaker (which sums vdg_transaction_cost) would have seen
    only ~60% of real spend, letting a £30 budget run to ~£50.

    So we now stash the NET cost and the latest balance into the telemetry dict
    on EVERY outcome — hit, miss, or error — and the caller adds them to the row.
    Writing two keys into a plain dict from this worker thread is safe (single
    assignments under the GIL, and the caller only reads them after the future
    resolves or the deadline passes).
    """
    _t = telemetry if telemetry is not None else {}
    # billing_sink is populated by vdg.py on EVERY call that reached VDG,
    # including ones that then raise or report not-found (paint18). Without it
    # a retry that came back empty was billed and recorded nothing — which is
    # exactly the common case here, since we only retry when the first call
    # found no paint. That is why partslink24 rows were storing £0.08 (one
    # call) when the account had actually been charged £0.16 (two).
    sink = {}
    data = None
    try:
        # PAINT package only (paint66). The retry never needed the vehicle
        # half — it exists because a cold first call warms VDG's upstream cache,
        # so the second read is fast. Asking for the vehicle documents again
        # would pay for identity we already hold.
        data = paint_lookup(registration, billing_sink=sink)
    except VdgError:
        pass  # cost below is still recorded — VDG charged us either way
    # Take the cost from whichever source has it. The sink is the only source
    # on the not-found and error paths (where `data` is None), but `data`
    # carries it on the success path — and reading BOTH means this keeps
    # working if either mechanism changes, rather than silently recording
    # nothing. Losing this figure is not a visible failure: it just makes the
    # budget breaker read low, which is precisely how the original bug went
    # unnoticed.
    retry_cost = sink.get('transaction_cost')
    if retry_cost is None and data:
        retry_cost = data.get('transaction_cost')
    if retry_cost is not None:
        _t['vdg_retry_cost'] = retry_cost

    retry_balance = sink.get('balance')
    if retry_balance is None and data:
        retry_balance = data.get('balance')
    if retry_balance is not None:
        _t['vdg_retry_balance'] = retry_balance

    # Record straight to the row, so this survives the caller having already
    # returned and saved (paint21). The telemetry keys above are kept for the
    # existing tests and for the case where the caller is still waiting, but
    # they are no longer what the cost DEPENDS on — see _record_retry_billing.
    retry_code = ''
    if data:
        retry_code = (data.get('paint_code') or '')
    _t['vdg_retry_code'] = retry_code
    _record_retry_billing(search_id, retry_cost, retry_balance, retry_code)
    if data is None:
        return None
    if not data or not data.get('paint_returned'):
        return None
    return {
        'source': 'vdg_retry',
        'paint_code': data.get('paint_code', ''),
        'paint_description': data.get('paint_description', ''),
        'all_paint_codes': data.get('all_paint_codes', []),
        'balance': data.get('balance'),
        # The retry calls the SAME bundle endpoint as the first pass, so `data`
        # carries the vehicle identity too — VIN included. This used to be
        # dropped: only the paint fields were surfaced (paint61).
        #
        # It matters when the FIRST call returned nothing at all. A VDG timeout
        # yields no vehicle and no VIN, but the retry (which needs only the
        # registration) comes back with the full bundle. YF23KRN on 12 Aug is
        # the case: first pass died at 46s with nothing, retry returned C31 —
        # so a complete response was in hand, and the row still shows vin=''.
        # The VIN then reads blank on the results page and in the email.
        #
        # DOES NOT change which lookups succeed, and it is worth being exact
        # about why: pl24 is submitted to the executor at the SAME instant as
        # this retry, with the `vin` variable as it stands then — empty. It
        # no-ops at its own `if not vin` guard before this value could exist.
        # So this is data completeness and honest telemetry, not a recovery
        # improvement. It is also the precondition for sequencing the recovery
        # (retry first behind a short fuse, then pl24 with the VIN it produced),
        # which is where it WOULD change outcomes.
        'vin': data.get('vin', ''),
    }


# VW model lines whose paint data lives in partslink24's COMMERCIAL catalogue
# regardless of the EU type-approval class VDG reports. The Caddy is the
# canonical case: a Caddy Life is type-approved M1 (passenger MPV), so VDG's
# category is "correct" — but partslink24 files every Caddy under Volkswagen
# Commercial Vehicles, so an M1 routing sends pl24 to a catalogue that cannot
# resolve it. Matched as a prefix of the model string ("Caddy Maxi C20 Life"
# starts with "caddy"). Model name is the primary signal; the WV1 VIN prefix
# (VW Commercial Vehicles' WMI — data-confirmed 16/16 commercial in our
# traffic) is a belt-and-braces catch for commercial VWs with unusual model
# strings. WV2/WV3 are deliberately NOT used: WV2 is ambiguous (car-derived
# vans) and WV3 is unverified.
_VW_COMMERCIAL_MODELS = (
    'transporter', 'caddy', 'crafter', 'amarok', 'caravelle', 'multivan',
)


# VDG make strings that pl24's MAKE_TO_BRAND has no key for, mapped to one it
# does (paint65).
#
# pl24 turns make strings into catalogues — that IS its job, and its map already
# carries 46 make strings. The reason this translation lives HERE rather than
# there is narrower: "Mercedes-AMG" is what VDG calls the car, and VDG's
# vocabulary is coloureg's business. Put it in both places and two systems are
# each half-responsible for the same rewrite, with the one that knows why not
# doing it. Same boundary, same reasoning, same file as _route_category.
#
# EVIDENCE, not guesswork: pl24's resolve_brand('Mercedes-AMG') returns
# "unknown make", so the lookup dies at its routing gate before a browser
# opens. Mercedes-Benz resolves and returns paint (6 of 54 lookups); every
# Mercedes-AMG lookup has failed (3 of 3), and partslink24 was confirmed by
# hand to hold the code for WF70WZR.
#
# DELIBERATELY NOT HERE — checked, and the ownership guess was wrong:
#   Cupra   pl24 has its OWN Cupra catalogue, not SEAT. 10/10 resolved anyway.
#   Dacia   pl24 has its OWN Dacia catalogue, not Renault. 9/9 resolved.
#   Alpine, smart  both already keys in pl24's map.
# Mapping those to a parent would break routing that works. A shared corporate
# owner is not evidence of a shared catalogue.
#
# Renault is a different problem and NOT an alias: it routes cleanly and still
# never returns (0 of 25 attempted). That belongs to pl24's extractor, and
# diagnosing it needs a debug dump, not an entry here.
_PL24_MAKE_ALIASES = {
    'mercedes-amg': 'Mercedes-Benz',
}


def route_make(make):
    """The make string pl24 should receive. Only rewrites known-unroutable ones.

    Returns `make` untouched when there is no alias, so an unknown string still
    reaches pl24 and fails visibly rather than being silently swallowed here.
    """
    return _PL24_MAKE_ALIASES.get((make or '').strip().lower(), make)


def _route_category(make, model, vin, category):
    """The category pl24 should receive for this vehicle.

    Fixes the one known misroute: VW commercial lines that VDG classes as M1
    (or leaves unclassed), which sends pl24 to the passenger catalogue where
    their paint doesn't exist. Only ever upgrades ''/M1 to N1, and only for
    Volkswagen: an explicit non-passenger class (N1/N2/N3) from VDG is trusted
    as-is, and other makes are untouched. The Search row keeps VDG's raw
    category — this routing applies solely at the pl24 boundary.
    """
    cat = (category or '').strip().upper()
    if cat and cat != 'M1':
        return category
    mk = (make or '').strip().lower()
    if not (mk.startswith('volkswagen') or mk == 'vw'):
        return category
    m = (model or '').strip().lower()
    if any(m.startswith(t) for t in _VW_COMMERCIAL_MODELS) \
            or (vin or '').strip().upper().startswith('WV1'):
        return 'N1'
    return category


def _pl24_lookup(vin, make, category=None, search_id=None):
    """Call the pl24 service. Returns a paint dict if pl24 found a code, else
    None. Never raises — network/HTTP/timeout errors degrade to None."""
    if not vin or not make:
        return None
    params = {'vin': vin, 'make': make}
    if category:
        params['category'] = category
    headers = {'X-API-Key': PL24_API_KEY} if PL24_API_KEY else {}
    try:
        resp = requests.get(
            f'{PL24_BASE_URL}/lookup-paint',
            params=params, headers=headers, timeout=_PL24_HTTP_TIMEOUT,
        )
    except requests.exceptions.RequestException:
        return None
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    code = (data.get('paint_code') or '').strip()
    desc = (data.get('paint_description') or '').strip()

    # Record what pl24 found regardless of whether this answer gets used
    # (paint26). When the VDG retry wins the race, resolve_paint has already
    # returned by the time we get here and nothing would otherwise keep this.
    # Compare against paint_code afterwards to see whether the two sources
    # agree; vdg_retry_code covers the mirror case.
    # Record the outcome REGARDLESS of whether a code came back — a failure's
    # reason is the whole point of storing it (paint65). Capped at the column
    # width because _record_worker_result writes via .update(), which bypasses
    # Model.save() and its truncation guard.
    outcome = (data.get('outcome') or '').strip()[:40]
    if search_id is not None and (code or outcome):
        _record_worker_result(search_id,
                              **({'pl24_code': code[:100]} if code else {}),
                              **({'pl24_outcome': outcome} if outcome else {}))
    # Keep the result if pl24 returned EITHER a code OR a colour name. The
    # name-only case (code == '' but desc set) covers brands partslink24 carries
    # a colour name but no code for (Ford passenger, Jaguar, older Land Rover,
    # some Kia) — pl24's `name_only` outcome. It's a confident match (pl24 read
    # the right vehicle's colour row; the brand just has no code field), so the
    # name is worth surfacing even without a code. Only a result with neither is
    # a true miss.
    if not code and not desc:
        return None
    name_only = (code == '' and bool(desc))
    return {
        'source': 'pl24',
        'paint_code': code,                 # may be '' for name-only
        'paint_description': desc,
        'name_only': name_only,
        # pl24 returns one code; keep the shape consistent with VDG's list form.
        # A name-only result has no code, so it contributes no code block.
        'all_paint_codes': [{
            'code': code,
            'description': desc,
        }] if code else [],
        'via': data.get('via', ''),
    }


def resolve_paint(registration, vin, make, category=None, telemetry=None, model=None,
                  search_id=None, vdg_colour=None, year=None):
    """Race the VDG bundle-retry and the pl24 scrape; return the first usable
    paint result, or None if neither recovers a code.

    Returns a dict on success:
        {'source': 'vdg_retry'|'pl24', 'paint_code', 'paint_description',
         'all_paint_codes', ...}
    or None if no paint could be recovered by either path.

    Optional telemetry: if a dict is passed, it is populated (in place) with what
    each path did, for logging on the Search row:
        {'recovery_attempted': True,
         'vdg_retry_returned': bool,   # did the 2nd VDG call return paint?
         'pl24_attempted': True,       # pl24 is always queried in the race
         'pl24_returned': bool,        # did pl24 return a usable CODE?
         'pl24_name_only': bool,       # did pl24 return a name but NO code?
         'duration_ms': int}           # wall-clock time of the recovery
    The return value is unchanged whether or not telemetry is supplied.

    Preference / ordering of results (strongest first):
      1. A real CODE wins. When both paths produce a code, the VDG-retry result
         wins (cheaper, already paid for). This holds even if both futures
         complete in the same wait() batch — we inspect the VDG future before the
         pl24 future within a batch, not relying on set-iteration order.
      2. A pl24 name-only result (colour name, no code) is a FALLBACK: it is held
         aside and returned ONLY if neither path produces a real code before the
         deadline. A late real code must still be able to beat it, so name-only
         never short-circuits the wait.
      3. Otherwise None (a true miss).

    Timeout: the total wait is hard-bounded by PL24_TIMEOUT. We deliberately do
    NOT use the ThreadPoolExecutor as a context manager, because its __exit__
    blocks until all worker threads finish — which would let a slow/hung pl24
    thread make this function hang far past the timeout. Instead we wait on the
    futures with an explicit deadline, then shut the executor down WITHOUT
    waiting (wait=False, cancel_futures=True), abandoning any straggler. The
    abandoned pl24 thread's HTTP request has its own timeout and ends on its own.
    """
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=2)
    _t = telemetry if telemetry is not None else {}
    _start = time.monotonic()
    # Recovery telemetry, finalised in the `finally` block so it is written no
    # matter which return path (or timeout) we take. pl24 and vdg-retry are both
    # always submitted, so "attempted" is True for both once we get here.
    _t['recovery_attempted'] = True
    _t['vdg_retry_returned'] = False
    _t['pl24_attempted'] = True
    _t['pl24_returned'] = False
    _t['pl24_name_only'] = False
    _t['oneauto_attempted'] = False
    _t['oneauto_returned'] = False
    _t['oneauto_cost'] = None
    _t['oneauto_outcome'] = ''
    try:
        f_vdg = ex.submit(_vdg_retry, registration, _t, search_id)
        # Category is routed (not raw): VW commercial lines misfiled as M1 by
        # VDG are sent to pl24 as N1 so the lookup hits the right catalogue
        # first time. See _route_category.
        f_pl24 = ex.submit(
            # Make AND category are both routed (not raw) at this boundary. The
            # Search row keeps VDG's originals either way — this rewrite applies
            # solely to what pl24 receives.
            _pl24_lookup, vin, route_make(make),
            _route_category(make, model, vin, category),
            search_id,
        )

        # THIRD LEG (paint67). Gated by measured coverage, not by hope: see
        # routing.py, where every make was established by calling it.
        #
        # It earns a place because it is BOUNDED — ~6s whether it answers or
        # not — while the VDG leg above is now the FIRST paint call rather than
        # a warm retry, so it is cold: 10-26s typically and up to a 60s gateway
        # 502 on BMW. One Auto answers 5 of 5 BMWs. Before the vehicle/paint
        # split this leg would have lost every race to a warm VDG read; now it
        # frequently wins.
        f_oneauto = None
        if should_call_oneauto(make, model, year):
            _t['oneauto_attempted'] = True
            _oa_sink = {}
            f_oneauto = ex.submit(
                oneauto.lookup, vin=vin, make=make, model=model, year=year,
                search_id=search_id, cost_sink=_oa_sink,
            )

        deadline = time.monotonic() + PL24_TIMEOUT
        pending = {f_vdg, f_pl24} | ({f_oneauto} if f_oneauto else set())
        pl24_code_result = None      # pl24 returned a real CODE (short-circuits)
        pl24_name_only_result = None  # pl24 returned a name but NO code (fallback)

        def _result_or_none(fut):
            try:
                return fut.result()
            except Exception:  # noqa: BLE001  (any worker failure -> no paint)
                return None

        while pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break  # deadline hit — stop waiting, abandon stragglers
            done, pending = concurrent.futures.wait(
                pending, timeout=remaining,
                return_when=concurrent.futures.FIRST_COMPLETED,
            )
            if not done:
                break  # wait timed out with nothing newly completed

            # Enforce the VDG-over-pl24 preference within this batch: if the VDG
            # future is among the just-completed ones and produced paint, that
            # wins outright — regardless of whether pl24 also completed here.
            if f_vdg in done:
                vdg_result = _result_or_none(f_vdg)
                if vdg_result is not None:
                    _t['vdg_retry_returned'] = True
                    return _enrich_from_lookup(vdg_result, make, model, vdg_colour=vdg_colour)

            # Then One Auto, ahead of pl24. Ordering rationale: both cost money
            # only when they answer, but One Auto returns a code AND a colour
            # name together ('MINERALGRAU METALLIC (B39)') where pl24 often
            # returns one or the other — and 18 of 27 observed disagreements
            # between sources were completeness rather than conflict.
            if f_oneauto is not None and f_oneauto in done:
                _t['oneauto_cost'] = _oa_sink.get('cost')
                _t['oneauto_outcome'] = _oa_sink.get('outcome', '')
                oa = _result_or_none(f_oneauto)
                if oa is not None and oa.get('code'):
                    _t['oneauto_returned'] = True
                    return _enrich_from_lookup(
                        {'paint_code': oa['code'],
                         'paint_description': oa['description'],
                         'all_paint_codes': oa['all_codes'],
                         'source': 'oneauto'},
                        make, model, vdg_colour=vdg_colour,
                    )

            # VDG didn't (yet) yield paint. Inspect pl24 if it completed in this
            # batch. A real CODE wins immediately (subject only to a VDG code,
            # already handled above). A name-only result (colour name, no code)
            # is held aside as a FALLBACK — we do NOT return it here, because a
            # real code from a still-pending VDG-retry must be able to beat it.
            if f_pl24 in done:
                p = _result_or_none(f_pl24)
                if p is not None:
                    if p.get('name_only'):
                        _t['pl24_name_only'] = True
                        pl24_name_only_result = p
                    else:
                        _t['pl24_returned'] = True
                        pl24_code_result = p

            # A real pl24 code is good enough to stop on (VDG had its chance above
            # in this batch). A name-only result is NOT — keep waiting for a code
            # while anything is still pending; the loop exits naturally when
            # nothing remains and we fall through to the name-only fallback.
            if pl24_code_result is not None:
                return _enrich_from_lookup(pl24_code_result, make, model, vdg_colour=vdg_colour)

        # No real code from either path. Surface the pl24 name-only result if we
        # got one (a partial but useful answer), else None (a true miss).
        # Enrichment may upgrade a name-only result to a full code if the colour
        # name maps unambiguously to a single code in our table.
        return _enrich_from_lookup(pl24_name_only_result, make, model, vdg_colour=vdg_colour)
    finally:
        # Do NOT block on stragglers. wait=False means we don't join running
        # threads; cancel_futures cancels any not-yet-started work. A pl24 thread
        # still mid-request is abandoned and ends when its own HTTP timeout fires.
        ex.shutdown(wait=False, cancel_futures=True)
        _t['duration_ms'] = int((time.monotonic() - _start) * 1000)