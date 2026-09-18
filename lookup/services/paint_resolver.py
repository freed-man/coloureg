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

import colorsys
import concurrent.futures
import logging
import os
import re
import threading
import time

import requests

from .http import get_session

from .vdg import paint_lookup, VdgError, _log_reg
# paint95: One Auto is NO LONGER SUBMITTED as a race leg, but the import and
# _oneauto_leg below are deliberately kept. Re-enabling it is then one
# ex.submit line rather than a rebuild, and its battery coverage — the coverage
# skip list, the second chance, the billing sink — stays live rather than
# rotting. The decision to drop it rests on 24 days of data; that is enough to
# act on and not enough to burn the bridge.
from . import ezyvin
from . import oneauto


def _enrich_from_lookup(result, make, model=None, vdg_colour=None,
                        telemetry=None):
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
        from lookup.models import PaintLookup, OperatorPaintCode

        code = (result.get('paint_code') or '').strip()
        # paint162: a slash-joined answer is resolved to its paint code HERE,
        # where the provider's answer arrives, so the corrected code flows to
        # the row, the page and the email alike.
        _slashed = resolve_slashed_code(make, code)
        if _slashed != code:
            logger.info('slashed code %s resolved to %s for %s',
                        code, _slashed, (make or '')[:30])
            code = _slashed
            result['paint_code'] = code
        if is_special_order_code(code):
            # paint159: never name a special-order code. Whatever the catalogue
            # holds against it is somebody else's bespoke car, picked up by a
            # scraper that found the name sitting next to the placeholder.
            # The CODE is kept: it really is on the sticker.
            result['paint_description'] = ''
            result['special_order'] = True
            return result
        # A provider can return a real code that no retailer sells (paint85).
        # Rewritten HERE, where the provider's answer arrives, so the mapped
        # code flows to the row, the page and the email alike — mapping later
        # would leave the customer looking at the unpurchasable one.
        mapped = PaintLookup.map_code(make, code)
        if mapped != code:
            logger.info('code %s mapped to %s for %s', code, mapped, make)
            code = mapped
            result['paint_code'] = mapped
        desc = (result.get('paint_description') or '').strip()

        # A NAME THAT IS JUST THE CODE IS NOT A NAME. One Auto returned
        # "(MG2)" as the colour of a Hyundai i20 on 21 Aug, so the customer saw
        # "MG2 (MG2)" and learned nothing — the catalogue had MG2 as Mangrove
        # Green Metallic all along. Same shape as the SEAT "(M6M6)".
        # Treated as blank so the fill below runs; if the catalogue has no name
        # either, we are no worse off than before.
        if desc and code and desc.strip('()').strip().upper() == code.upper():
            logger.info('description %r was just the code for %s; refilling',
                        desc, make)
            desc = ''
            result['paint_description'] = ''

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
            # paint92: the operator's own table, consulted ONLY on a catalogue
            # miss. Additive by construction — it can fill a blank, never
            # overwrite an answer 120,594 merged rows already produced.
            if not name:
                name = OperatorPaintCode.name_for_code(make, code)
            if not name:
                # paint156: LAST RESORT — trim one trailing LETTER off an
                # alphanumeric base and try again.
                #
                # Three Mitsubishis in three days arrived with a code the
                # catalogue holds one character shorter, and every one lost its
                # name for it:
                #
                #   C06A -> C06  Quartz Brown Metallic   DVLA Brown
                #   A67B -> A67  Dark Grey Pearl         DVLA Grey
                #   A39A -> A39  Graphite Grey Pearl     DVLA Grey
                #
                # MEASURED, AND THE SHAPE IS THE GUARD. Trimming a trailing
                # LETTER from a base that holds both letters and digits agrees
                # on colour 167 times of 172 — 97%. Trimming a DIGIT from an
                # all-numeric code agrees 18% of the time, because Mitsubishi
                # and Volvo number colours sequentially and unrelated shades sit
                # next to each other (6054 Regatta Blue beside 605 Gris
                # Espumante). So digits are excluded outright.
                #
                # This was built into mmw's gate on 13 Sep and never into this
                # path, so a pl24 answer got no trim at all.
                _stem = code[:-1] if len(code) > 2 else ''
                if (_stem and code[-1].isalpha()
                        and any(ch.isdigit() for ch in _stem)
                        and any(ch.isalpha() for ch in _stem)):
                    _h2, _n2, _c2 = PaintLookup.lookup_with_canonical(
                        manufacturer=make, paint_code=_stem,
                        vdg_colour=vdg_colour or result.get('colour') or '',
                    )
                    # CORROBORATE IT. 97% is a good rate, not a certainty, and
                    # the 3% would put a wrong NAME beside a correct code. The
                    # registered colour is free and already to hand, so the trim
                    # only stands if the trimmed row describes the same kind of
                    # colour — the same test paint143 applies to mmw.
                    #
                    # Silent when the colour cannot decide: no registered
                    # colour, or a name and hex that name no family. Unknown is
                    # not approval, so the name stays blank rather than being
                    # taken on the trim alone.
                    if _n2:
                        _want = _colour_families(
                            vdg_colour or result.get('colour') or '')
                        _got = _colour_families(_n2) or {
                            _hex_family(_h2)} - {None}
                        if _want and _got and (_want & _got):
                            name, hex_val = _n2, hex_val or _h2
            if name:
                result['paint_description'] = name
                # the NAME was supplied by our database, not the provider
                result['enriched_from'] = 'name'
            if hex_val and not result.get('paint_hex'):
                result['paint_hex'] = hex_val

        elif desc and not code:
            # name -> code (conservative: unique match only)
            # paint136: clear this THREAD's slot first, so a decline recorded
            # on an earlier lookup on the same thread is not read as belonging
            # to this one. Thread-local, so a CONCURRENT lookup in another
            # thread can no longer leak into it — see models.py.
            PaintLookup.take_last_ambiguity()
            found_code, hex_val, _canon_name = PaintLookup.code_from_name(
                manufacturer=make, colour_name=desc, model=model,
            )
            # ONLY WHEN THE LOOKUP ACTUALLY DECLINED.
            #
            # _collapse_to_single_code runs more than once per resolution — the
            # decorated name is tried before the stripped one — so an early
            # decline leaves the stash set even when a later attempt wins. Ford
            # 'Ink Blue (Metallic)' resolves cleanly to 3CYCWWA and was being
            # recorded as a 2-code ambiguity, which is exactly the kind of
            # false positive that would make the whole measurement worthless.
            # Read-and-clear in one call, so the slot cannot be left set for
            # the next lookup on this thread.
            _amb = PaintLookup.take_last_ambiguity()
            if _amb and len(_amb) > 1 and not found_code and telemetry is not None:
                # Into TELEMETRY, not the result dict. _apply_recovery_telemetry
                # is handed telemetry only, so anything written to `result` here
                # never reaches the Search row — it would look recorded and
                # persist nothing.
                telemetry['name_match_count'] = len(_amb)
                telemetry['name_match_codes'] = ', '.join(_amb)[:200]
            # paint92: same fallback, other direction. This is the one that
            # pays — a hand-researched code exists precisely BECAUSE the
            # catalogue could not resolve that name the first time.
            if not found_code:
                found_code = OperatorPaintCode.code_for_name(make, desc)
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
        # paint158: LOG IT. This wraps the whole enrichment block, so anything
        # raised here silently costs the customer a name or a code, and nothing
        # anywhere would say so. paint155 found the origin-gate breaker swallowing
        # a NameError on every request for weeks in exactly this shape.
        # Still swallowed: enrichment is an improvement on the provider answer,
        # never a precondition for it.
        logger.exception('enrichment failed for %s', (make or '')[:40])
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

# paint140: the mmw service. Same shape as pl24 — private Railway hostname in
# production, X-API-Key, /lookup-paint. Unset key means the service has no key
# either, which is only true in local development.
MMW_BASE_URL = os.environ.get(
    'MMW_BASE_URL', 'http://mmw.railway.internal:8080'
).rstrip('/')
MMW_API_KEY = os.environ.get('MMW_API_KEY', '')

#: mmw answers in about a second warm (measured 14 Sep: 1.5s cold opening the
#: session, then 1.2s and 1.0s). This is a hang detector, not a patience
#: setting — it starts at t=0 and nothing waits on it, so a generous value
#: costs nothing and a tight one throws away answers.
_MMW_HTTP_TIMEOUT = (5.0, 12.0)

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

# The ceiling on how long a customer waits. pl24 is unaffected by the exact
# value: the backstop guarantees it STARTS by 10s, and its worst observed run is
# 29s (p50 1.0s, p90 8.0s), so it has ample room either way.
PL24_TIMEOUT = float(os.environ.get('PL24_CLIENT_TIMEOUT_S', '60'))

# How long the two PAID legs get before pl24 is brought in regardless (paint68).
# 10s: One Auto's quick answers land at ~6s (6.06-6.38 on 42 of 45 calls) and a
# VDG paint MISS refunds fast, so by 10s either leg that was going to fail has
# usually said so — while a VDG paint HIT can take 10-26s, which is why this is
# a backstop and not a deadline. pl24 joins the race; it does not end it.
#
# Worth being honest that the earlier "~6s" reading was optimistic: the same
# coverage run had vehicles still returning 202 at 21-31s. The backstop exists
# precisely because a leg can be slow rather than failed.
PL24_BACKSTOP_S = float(os.environ.get('PL24_BACKSTOP_S', '10'))
#: paint105. How long the reserve waits before firing on its own.
#:
#: A SAFETY NET FOR A HUNG LEG, NOT A COMPETITOR. It can only ever pre-empt a
#: FREE answer: it fires while a leg is still running, and a running leg usually
#: still answers, from VDG or pl24, at no cost. Every second earlier costs money
#: and buys time only for a leg that has genuinely stopped.
#:
#: Shipped at 15s and moved to 20s on 10 Sep, on the first evidence from the
#: pipeline as it now runs rather than as it used to:
#:
#:   SO71HKE completed in 16.7s. The backstop fired at 15, pl24 answered
#:   shortly after with the same C3Y, and 5 credits bought 1.7 seconds. At 20s
#:   that call never happens. It was the first backstop firing in production
#:   and it was pure waste.
#:
#: The distribution moved too, because paint96 starts pl24 at zero instead of
#: summoning it when VDG drops out. Deliveries since: p50 9.8s (was 11.8s), and
#: NOTHING past 20s against 10.3% before. So 20s now sits above the whole
#: observed tail while 15s sits inside it.
#:
#: Six deliveries is a thin sample and a slow day will produce a 25s outlier —
#: which is fine, because that is the case this exists for. THE NUMBER TO WATCH
#: is ezyvin_started_because: if 'backstop' is more than a rarity against
#: 'both_empty', the answer is not to move this again, it is to find out which
#: leg is hanging.
EZYVIN_BACKSTOP_S = float(os.environ.get('EZYVIN_BACKSTOP_S', '20'))

# SECOND-CHANCE STAGE (paint73). When a paid leg finishes with nothing, ask it
# once more — but only briefly.
#
# The two are second chances for different reasons:
#   VDG      — a failed first call has WARMED their upstream cache, so the
#              second read is sub-second. GL68VPN timed out twice at 40s and
#              then returned B85 in 0.74s. Before the vehicle/paint split this
#              mechanism supplied 214 of 866 answers (25%); the split removed it
#              because nothing warms the paint route any more.
#   One Auto — not a retry at all but a COLLECTION. 'still_fetching' means their
#              job was running server-side when we stopped polling, and results
#              are held 24 hours. PF68MYJ recorded still_fetching in coloureg
#              and then answered in 685ms on the next call.
#
# 5s, because the call should be fast OR NOT AT ALL: a warm read is sub-second,
# so anything slower is a cold fetch that will not finish inside the ceiling
# anyway. Giving up at 5s rather than 30 means a FAILED lookup — where the
# customer is waiting on the last leg to finish — resolves that much sooner.
SECOND_CHANCE_S = float(os.environ.get('SECOND_CHANCE_S', '5'))

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


def _record_retry_billing(search_id, cost, balance, retry_code, retry_name=''):
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
        # Only write a NAME we actually have. A blank must not clobber a name
        # already on the row: the first pass and the retry both write here, and
        # the second one arriving empty should leave the first one's answer
        # alone rather than erase it.
        if retry_name:
            updates['vdg_paint_name'] = retry_name[:120]
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


def _vdg_retry(registration, telemetry=None, search_id=None, race_over=None):
    """Second VDG bundle call. Returns a paint dict if paint came back, else
    None. Never raises — VDG errors degrade to None (no recovery).

    COST. Measured 8 Sep across 473 paint-less lookups since paint66 split the
    packages. A refund is CONDITIONAL, and the condition is what the paint
    document contained rather than whether we could use it:

        genuinely empty PaintCodeList -> refunded, ~£0.06 (vehicle only)   73%
        a colour NAME but no code     -> charged in full, ~£0.33           26%

    The discriminator is stark: of the 343 refunded, ZERO carried a colour name
    from VDG; of the 125 charged, 117 did. VDG refunds an empty document, not a
    disappointing one — a list with a name in it is not empty, so we got data,
    it just was not a code.

    This replaces the paint15 note, which read "VDG bills this call whether or
    not it returns paint — a paint-less call is partially refunded and nets
    ~£0.12, a hit costs the full ~£0.45". Those were BUNDLE-era figures, true
    until 15 Aug and wrong every day since, and they overstate the retry's cost
    by roughly 4x. They were quoted as current on 8 Sep and produced a wrong
    answer about the pipeline; the numbers above carry their measurement date
    for that reason.

    WHY THE SPEND IS RECORDED AT ALL (paint15, still true). It used to vanish:
    this function only surfaced a value on a hit, so the retry's cost never
    reached the Search row. Since ~58% of lookups trigger a retry, every
    downstream total undercounted — and the daily budget breaker, which sums
    vdg_transaction_cost, would have seen only ~60% of real spend, letting a
    £30 budget run to ~£50.

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
    except Exception as e:  # noqa: BLE001 — see below; this must never escape
        # A TIMEOUT LANDS HERE, and the second chance below must still run
        # (paint84). It previously sat inside this try, so an exception jumped
        # straight past it — leaving the stage unreachable on the one case that
        # justified building it. GL68VPN timed out twice at 40s and then
        # returned B85 in 0.74s; that is a timeout followed by a warm read, and
        # the code could not reach the warm read.
        #
        # CATCHES EVERYTHING, not just VdgError (F5). `sink` is populated by
        # _make_request the moment VDG answers, BEFORE the response is parsed —
        # so a parse-side failure (_parse_paint_fields meeting an unexpected
        # shape, e.g. a list of strings in PaintCodeList) arrives with the
        # charge ALREADY INCURRED. Catching only VdgError let that escape past
        # the cost read and _record_retry_billing below, losing a real charge:
        # the exact blind spot paint18 exists to close, and invisible spend is
        # what lets the daily budget breaker read low. views.py added the same
        # trust-boundary catch on the first pass; the retry lacked it.
        if not isinstance(e, VdgError):
            logger.exception('VDG paint call failed unexpectedly for %s',
                             _log_reg(registration))
        data = None

    # SECOND CHANCE (paint73). The first call has warmed VDG's upstream cache
    # whether it succeeded, came back empty, or timed out — that is the whole
    # mechanism behind the 214 answers the old bundle-retry supplied. A short
    # timeout because a warm read is sub-second; anything slower is a cold fetch
    # that will not finish inside the ceiling anyway.
    #
    # Only when the first call produced NO PAINT. A hit needs no second call,
    # and a hit is the only outcome that has already cost the full price — a
    # paint-less call is refunded.
    if not (data and data.get('paint_returned')):
        # RECORDED, not just done. Until now `data = second` overwrote silently,
        # so a row won on the second attempt looked identical to one won on the
        # first — and the question "does this £0.27 earn its keep" had no answer
        # in the data. Captured BEFORE the call so a raise still leaves a trace.
        from lookup.models import Search   # lazy: circular import at module load
        second_chance = Search.SECOND_CHANCE_EMPTY
        # Was the race already decided when this fired? Every True here is spend
        # a race-over flag would have prevented.
        after_race = bool(race_over is not None and race_over.is_set())
        try:
            second = paint_lookup(registration, billing_sink=sink,
                                  timeout=SECOND_CHANCE_S)
        except Exception:  # noqa: BLE001 — a second chance must never raise
            second = None
        if second and second.get('paint_returned'):
            logger.info('VDG second chance recovered paint for %s',
                        _log_reg(registration))
            data = second
            second_chance = Search.SECOND_CHANCE_WON
        if search_id is not None:
            _record_worker_result(search_id, vdg_second_chance=second_chance,
                                  second_chance_after_race=after_race)
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
    retry_name = ''
    if data:
        retry_code = (data.get('paint_code') or '')
        # The NAME too (paint69). VDG can return a colour name with no code, and
        # until now that was thrown away — so a row could not show that VDG had
        # said anything at all. It is also what makes source comparison
        # possible: 18 of 27 observed disagreements were the same paint at
        # different completeness, which only the names reveal.
        retry_name = (data.get('paint_description') or '')
    _t['vdg_retry_code'] = retry_code
    _t['vdg_paint_name'] = retry_name
    _record_retry_billing(search_id, retry_cost, retry_balance, retry_code,
                          retry_name=retry_name)
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


#: paint140. Colour words to families, for the mmw validation gate.
#:
#: DELIBERATELY COARSE. It answers one question — is the catalogue's name for
#: this code the same KIND of colour as the car's registered one — and a
#: finer-grained map would reject honest answers over shade. Measured at a 1.4%
#: false-reject rate across 1,547 known-good paid answers.
#:
#: Multilingual because the catalogue is: it holds Phantomschwarz beside
#: Phantom Black and Ljusbla beside Light Blue, from three scraped sources.
_COLOUR_FAMILY = {
    'red': 'red', 'rosso': 'red', 'rouge': 'red', 'rot': 'red',
    'infrared': 'red', 'rood': 'red', 'rojo': 'red',
    'blue': 'blue', 'bleu': 'blue', 'blau': 'blue', 'blu': 'blue',
    'azul': 'blue', 'bla': 'blue', 'turquoise': 'blue', 'teal': 'blue',
    'green': 'green', 'vert': 'green', 'verde': 'green', 'grun': 'green',
    'gruen': 'green', 'moss': 'green', 'olive': 'green',
    'yellow': 'yellow', 'jaune': 'yellow', 'gelb': 'yellow',
    'amarillo': 'yellow',
    'black': 'black', 'noir': 'black', 'nero': 'black', 'schwarz': 'black',
    'preto': 'black', 'negro': 'black', 'svart': 'black',
    'white': 'white', 'blanc': 'white', 'bianco': 'white', 'weiss': 'white',
    'weis': 'white', 'branco': 'white', 'blanco': 'white', 'vit': 'white',
    # Silver and grey are ONE family. DVLA registers many metallic greys as
    # Silver and the catalogue names them Grey, or the reverse; splitting them
    # would reject correct answers on a naming convention.
    'grey': 'grey', 'gray': 'grey', 'gris': 'grey', 'grigio': 'grey',
    'grau': 'grey', 'silver': 'grey', 'silber': 'grey', 'cinza': 'grey',
    'plata': 'grey', 'titanium': 'grey', 'graphite': 'grey', 'anthracite': 'grey',
    'orange': 'orange', 'arancio': 'orange',
    'purple': 'purple', 'violet': 'purple', 'viola': 'purple', 'lilac': 'purple',
    'brown': 'brown', 'braun': 'brown', 'marron': 'brown', 'beige': 'brown',
    'bronze': 'brown', 'sand': 'brown', 'tan': 'brown',
    'gold': 'gold', 'or': 'gold',
    'pink': 'pink', 'rose': 'pink',
}


#: Codes that are a placeholder rather than an answer. Shape only — whether one
#: is REJECTED depends on evidence, not on matching this (see is_placeholder_code).
_PLACEHOLDER_CODE = re.compile(
    r'^(X{2,}|N\.?/?A\.?|NONE|UNKNOWN|TBC|TBA|\?+|-+)$', re.I)


#: Codes that mean "painted to special order", not a colour. The code IS on the
#: car's sticker, so it is kept and shown — it just does not identify a paint.
_SPECIAL_ORDER_CODES = {'999', 'L999', '0999'}


def resolve_slashed_code(make, code):
    """Pick the paint code out of a slash-joined string, or return it unchanged.

    paint162. 283 delivered codes have contained a slash, and a customer cannot
    order paint from any of them. They are four different conventions joined by
    the same character:

        Ford        2431C/2PJE/ZJNC  -> 2PJE   cross-references
        Chrysler    PS3/QS3S         -> PS3    body / trim
        Jeep        PW6/QW6S         -> PW6
        Audi        T9/Y9C           -> Y9C    interior / exterior
        Volkswagen  0Q0Q/C9A         -> C9A

    THE GUARD IS THE CATALOGUE, not the shape. Exactly one part must resolve as
    a code for that make. If several do, we cannot tell which is the paint and
    it is left alone; if none does, we know nothing and it is left alone.

    Measured over the full history: 266 of 283 have exactly one resolving part,
    13 have several, 4 have none. No delivered slashed code was itself a
    catalogue entry, but the whole string is still tried FIRST — abarth 103/B
    and its 4,565 siblings carry a slash inside a legitimate code, and must
    never be split.
    """
    code = (code or '').strip()
    if '/' not in code:
        return code
    from lookup.models import PaintLookup
    # The whole string wins if it is ITSELF a row. Checked against the raw
    # table, NOT through lookup(), because lookup() already splits on '/'
    # internally — so asking it would always say yes and nothing would ever be
    # resolved. That is exactly what happened on the first attempt here.
    #
    # Ordering matters: this is what protects abarth 103/B and its 4,565
    # siblings, where the slash is part of a legitimate code.
    mfr = PaintLookup.normalize_manufacturer(make)
    if PaintLookup.objects.filter(manufacturer=mfr, code__iexact=code).exists():
        return code
    # Each part is tried as sent AND `L`-prefixed, because VDG returns the VAG
    # exterior code without the L that the catalogue carries: `T9/Y9C` is Ibis
    # White under `LY9C`, and testing `Y9C` alone finds nothing. The interior
    # halves — T9, 0E, 2T2T — resolve under neither form, which is what makes
    # the pair separable at all.
    #
    # The part is returned AS SENT, not as the variant that matched: the
    # customer's sticker says Y9C, and the L is our catalogue's notation.
    hits = []
    for part in (x.strip() for x in code.split('/')):
        if not part:
            continue
        if PaintLookup.objects.filter(
                manufacturer=mfr,
                code__iexact=part).exists() or PaintLookup.objects.filter(
                manufacturer=mfr, code__iexact='L' + part).exists():
            hits.append(part)
    return hits[0] if len(hits) == 1 else code


def is_special_order_code(code):
    """True for a code that marks bespoke paint rather than naming one.

    paint159. DIFFERENT FROM is_placeholder_code. `XXX` is a wildcard a source
    returns when it has nothing, and is worthless to the customer. `999` is
    genuinely stamped on the car: it means the paint was ordered outside the
    catalogue, and the colour lives only on the build record.

    So the code is KEPT and shown. Only the name is suppressed, because every
    name attached to it is a different car's bespoke colour that a scraper
    happened to find next to the placeholder. The catalogue proves it — eight
    makes carry `999` with eight unrelated colours:

        bmw Medium Grey · dodge Orange · plymouth Orange · porsche Dunkelgrau
        renault Vert Tyrol Metallic · isuzu, mazda, nissan Trans Blue

    and mazda's Trans Blue ALSO exists under its real code, A6A.

    Two groups use it the same way: VW Group as `L999` (Bentley has used VW
    paint codes since 1998, and the L is often dropped on the sticker), and
    Chrysler as plain `999` on 1969-70 Dodge and Plymouth.

    NOT evidence-based, unlike is_placeholder_code. `renault/999` has a hex and
    six models, so an evidence test would keep it — but six models is six cars
    that had special-order paint, not six cars sharing a colour.
    """
    return (code or '').strip().upper() in _SPECIAL_ORDER_CODES


def is_placeholder_code(make, code):
    """True when a code is a scraper artefact rather than a paint code.

    paint146. `BO55LDP`, a 2013 Audi A8 registered GREY, was delivered paint
    code `XXX` with the description `Blue` — twice, once via One Auto in
    September and again via pl24 tonight. `XXX` is a wildcard the source uses
    where it has no answer, and the catalogue carries a junk row for it.

    A code is worthless to a customer whatever its name: `XXX` sends them to a
    paint counter with nothing, while a failed lookup at least offers them a
    free manual one.

    EVIDENCE, NOT A BLACKLIST. Shape alone would be wrong — three makes carry
    `NA` with a hex and a model list:

        fordamerica  NA  Dark Tourmaline Pearl   #003339  8 models
        mazda        NA  Dark Tourmaline Mica    #003339  1 model
        nissan       NA  Midnight Teal Metallic  #003339  1 model

    All the same colour, on makes that genuinely shared platforms and paints.
    `bedford XX` is Brilliant Ochre at #FCE903 with a model. So a placeholder is
    only refused when its catalogue row has NO hex AND NO models — nothing to
    suggest it is real. That keeps 7 of the 18 placeholder-shaped rows and
    refuses 11.

    Measured over four months: blocks 1 of 1,943 delivered answers, and loses
    ZERO whose description matched the registered colour.

    NOT APPLIED TO MANUAL FULFILMENTS — see the caller. `YD70XAA` is an AJS
    motorcycle where the operator entered `N/A` with 'Metallic Blue': no code
    exists for that bike, the colour does, and that is a real answer.
    """
    code = (code or '').strip()
    if not code or not _PLACEHOLDER_CODE.match(code):
        return False
    from lookup.models import PaintLookup
    row = PaintLookup.lookup(make, code)
    if row and (row.hex or (row.models_list or [])):
        return False
    return True


def _hex_family(hex_value):
    """The colour family a hex sits in, or None when it cannot be read.

    paint143. The gate matched on COLOUR WORDS IN THE NAME, so a row whose name
    does not happen to say its colour was refused however obviously right it
    was. SG13VEW, 14 Sep: a Honda CR-V registered Red, mmw returned R-539P, the
    catalogue holds R539P as 'Molten Lava Pearl' at #8E1F13. Plainly red, and
    invisible — molten, lava and pearl are not colour words. That is 21,645
    rows, 18% of the catalogue, carrying a hex and no colour word.

    MEASURED, NOT GUESSED. Against 1,674 known-good delivered answers this
    accepts 151 of the 183 the name cannot judge, and against deliberately
    wrong codes it wrongly accepts 13.1% — slightly BETTER than the name gate's
    15.5%, so reach improves without the gate getting looser.

    AN EARLIER VERSION TREATED NEIGHBOURING FAMILIES AS COMPATIBLE and was
    dropped: it raised true-accept to 95% and false-accept to 44.8%, three
    times worse than the gate it was meant to help. A false reject costs an
    opportunity; a false accept sends someone the wrong paint.

    Neutrals are decided FIRST and generously, because that is where the
    boundaries bite: a bluish silver must read grey, not blue.
    """
    raw = (hex_value or '').lstrip('#')
    if len(raw) != 6:
        return None
    try:
        r, g, b = (int(raw[i:i + 2], 16) / 255 for i in (0, 2, 4))
    except ValueError:
        return None
    h, sat, val = colorsys.rgb_to_hsv(r, g, b)
    h *= 360
    if val < 0.10:
        return 'black'
    # 0.25, not 0.15. Metallic silvers carry a real blue cast — Meteor Silver
    # #97B0BF is 0.21 saturated — and DVLA records those cars as Silver or
    # Grey, so a lower threshold reads them blue and refuses a correct answer.
    # Measured across 1,674 known-good answers the choice is a WASH (86.2/11.8
    # against 86.1/12.1), so this is settled on being comprehensible rather
    # than on a difference that is inside the noise.
    if sat < 0.25:
        return 'black' if val < 0.18 else ('white' if val > 0.82 else 'grey')
    # Dark and washed out reads grey whatever the hue — 'Magnetic' #383838 is
    # a grey to DVLA, not a black.
    if sat < 0.30 and val < 0.45:
        return 'grey'
    if h < 18 or h >= 330:
        return 'red'
    if h < 42:
        return 'brown' if val < 0.55 else 'orange'
    if h < 70:
        return 'yellow'
    if h < 180:
        return 'green'
    if h < 260:
        return 'blue'
    return 'purple'


def _colour_families(text):
    """Every colour family named in a string. Empty set when it names none."""
    words = re.sub(r'[^a-z]+', ' ', (text or '').lower()).split()
    return {_COLOUR_FAMILY[w] for w in words if w in _COLOUR_FAMILY}


def mmw_code_validates(make, code, dvla_colour):
    """Should mmw's code be trusted enough to serve?

    paint140. mmw scrapes a third-party site of unknown provenance, so its
    answer is never served unverified. The check: does OUR catalogue's name for
    that code describe the same KIND of colour the car is registered as.

    `WP09UOU` on 14 Sep is why this exists. mmw returned Z9Y for an Audi A3
    registered BLACK:

        Z9Y   Dark Grey Matt        no hex, no models, 1 source
        LZ9Y  Phantom Black Pearl   #0D0F13, 3 sources, 51 models

    The bare code is a GREY on a BLACK car. The prefixed one is black. So the
    registered colour picks the right row, and this returns the code that
    actually matched rather than the one mmw sent.

    NOTE mmw's own colour field is NOT used. It returns the DVLA-style word
    (GREY, BLACK), confirmed live across three vehicles, so comparing it to the
    registered colour would compare DVLA against DVLA and prove nothing.

    Returns the code to use, or None. UNKNOWN IS NOT APPROVAL: a code absent
    from the catalogue, or a colour naming no family, returns None — the whole
    point is corroboration, and there is none.
    """
    from lookup.models import PaintLookup
    if not code or not make:
        return None
    want = _colour_families(dvla_colour)
    if not want:
        return None

    # Try the code as sent, then the notations the catalogue uses. mmw returns
    # whatever the SITE holds, and that differs from the catalogue in at least
    # two ways — refusing to look further rejects correct answers on
    # punctuation.
    #
    #   PREFIX   mmw sends A7N;      the catalogue has LA7N
    #   HYPHEN   mmw sends NH-731P;  the catalogue has NH731P
    #
    # The hyphen case cost a real answer on 14 Sep: SA10RXD, a Honda CR-V this
    # pipeline had failed three times, came back NH-731P and was refused as
    # unknown. NH731P is in the catalogue as Crystal Black Pearl (#030405) on a
    # car registered BLACK — it would have validated. The catalogue is
    # inconsistent about this by nature: 778 of 2,080 Honda codes carry a
    # hyphen and the rest do not.
    #
    # Order matters: as-sent first, so an exact match is never passed over for
    # a variant.
    _bare = code.replace('-', '').replace(' ', '')
    # paint159: B0N is mmw's notation for the Stellantis E codes. Three live
    # deliveries agree — B0NPR twice, B0NZR once, all answered Exx by pl24 —
    # and EWP, EZR and EPR each exist across Vauxhall, Opel, Peugeot and
    # Citroen with consistent colours, against 847 E+2 codes in total.
    #
    # WEAKER EVIDENCE THAN THE OTHERS, AND DELIBERATELY SO. `TE` was confirmed
    # at 69 of 69 inside the catalogue; B0N appears in ZERO catalogue rows, so
    # it cannot be corroborated that way, and one counter-example exists
    # (B0N9V answered KTV, where neither E9V nor 9V is in the catalogue). What
    # makes it safe is that the colour check still has to pass afterwards: the
    # prefix only earns a row a hearing, never an answer.
    _variants = [code, _bare, f'L{code}', f'L{_bare}']
    if _bare.upper().startswith('B0N') and len(_bare) > 3:
        _variants.append('E' + _bare[3:])
    for candidate in _variants:
        row = PaintLookup.lookup(make, candidate)
        if not row:
            continue
        # THE NAME FIRST, ALWAYS. A colour word the manufacturer wrote is
        # better evidence than a hex we classified, so the hex never overrides
        # it and never gets a vote when the name has one.
        named = _colour_families(row.name) if row.name else set()
        if named:
            if named & want:
                return candidate
            # The name spoke and disagreed. Do NOT then ask the hex for a
            # second opinion — that is how a gate turns into a search for any
            # reason to say yes.
            continue
        # paint143: the name says nothing. Fall back to the hex, which is what
        # made SG13VEW's 'Molten Lava Pearl' refusable despite being #8E1F13 on
        # a car registered Red.
        if _hex_family(row.hex) in want:
            return candidate
    return None


def _mmw_settle(f_mmw, make, vdg_colour, telemetry, delivered_code=None):
    """Read mmw's held answer. Records agreement; returns a usable code or None.

    paint140. Called in BOTH places mmw matters:

      * when a paid leg won, with `delivered_code` set — then this only records
        whether mmw agreed, and returns nothing. That is how the ordering gets
        tested without changing any customer's answer.
      * at the end, with `delivered_code` None — then a validated code may be
        returned and served.

    NEVER BLOCKS. mmw started at t=0 and everything else has already finished
    by the time this runs, so the future is done or it hung; either way waiting
    on it would make a free leg cost time.
    """
    if f_mmw is None or not f_mmw.done():
        return None
    try:
        row = f_mmw.result()
    except Exception:  # noqa: BLE001 — a free leg cannot be allowed to raise
        return None
    if not row or not row.get('code'):
        return None

    validated = mmw_code_validates(make, row['code'], vdg_colour)

    if delivered_code is not None:
        # Compare against what the customer actually got. Compared on the
        # VALIDATED form where there is one, because mmw sends the short code
        # (A7N) and the pipeline may deliver the catalogue's (LA7N) — counting
        # that as disagreement would understate mmw badly.
        if telemetry is not None:
            got = (delivered_code or '').strip().upper()
            telemetry['mmw_agreed'] = got in {
                (row['code'] or '').upper(), (validated or '').upper()}
        return None

    if validated and telemetry is not None:
        telemetry['mmw_used'] = True
    return validated


def _mmw_lookup(registration, search_id=None):
    """Call the mmw service. Returns its row dict, or None on any failure.

    paint140. UNLIKE EVERY OTHER LEG, THIS NEEDS ONLY THE REGISTRATION. pl24
    and Ezyvin both need the VIN, which arrives from the £0.06 VDG vehicle
    call, so they cannot start until it returns. mmw can start at t=0.

    Free, so there is no spend guard and no budget check: the only cost of
    calling it is someone else's bandwidth, which the service paces.

    Never raises. A leg that cannot make things worse must not be able to break
    a lookup either.
    """
    if not registration:
        return None
    headers = {'X-API-Key': MMW_API_KEY} if MMW_API_KEY else {}
    try:
        resp = get_session().get(
            f'{MMW_BASE_URL}/lookup-paint',
            params={'reg': registration}, headers=headers,
            timeout=_MMW_HTTP_TIMEOUT,
        )
    except requests.exceptions.RequestException:
        return None
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None

    row = {
        'code': (data.get('paint_code') or '').strip()[:100],
        'colour': (data.get('paint_description') or '').strip()[:60],
        'outcome': (data.get('outcome') or '').strip()[:40],
        'ms': int((data.get('elapsed_s') or 0) * 1000),
    }
    # Recorded whether or not the answer is ever used — that is the entire
    # reason the columns exist. A leg used last still teaches you its accuracy.
    if search_id is not None and (row['code'] or row['outcome']):
        _record_worker_result(
            search_id,
            mmw_attempted=True,
            **({'mmw_code': row['code']} if row['code'] else {}),
            **({'mmw_colour': row['colour']} if row['colour'] else {}),
            **({'mmw_outcome': row['outcome']} if row['outcome'] else {}),
            mmw_ms=row['ms'],
        )
    return row


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
        resp = get_session().get(
            f'{PL24_BASE_URL}/lookup-paint',
            params=params, headers=headers, timeout=_PL24_HTTP_TIMEOUT,
        )
    except requests.exceptions.RequestException:
        return None
    # paint144: READ THE BODY BEFORE GIVING UP ON THE STATUS. pl24 puts `slot`
    # on its 502 and 504 bodies too, and that is where it is most informative —
    # a failure tells you nothing until you know WHICH session failed. Bailing
    # on the status code alone threw exactly that away.
    try:
        data = resp.json()
    except ValueError:
        data = {}
    _slot = data.get('slot')
    _via = (data.get('via') or '').strip()[:40]
    # `is not None`, NOT truthiness: slot 0 is the first account, and `if
    # _slot:` would silently drop every answer from it.
    if search_id is not None and (_slot is not None or _via):
        _record_worker_result(
            search_id,
            **({'pl24_slot': _slot} if isinstance(_slot, int)
               and not isinstance(_slot, bool) else {}),
            **({'pl24_via': _via} if _via else {}),
        )
    if resp.status_code != 200 or not data:
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
    name = (data.get('paint_description') or '').strip()[:120]
    if search_id is not None and (code or outcome or name):
        _record_worker_result(search_id,
                              **({'pl24_code': code[:100]} if code else {}),
                              **({'pl24_name': name} if name else {}),
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


def _oneauto_leg(vin, make, model, year, search_id, sink, race_over=None):
    """Run the One Auto call and WRITE ITS OWN COST, whoever wins the race.

    Same problem and same fix as _record_worker_result for pl24 (paint26):
    resolve_paint returns the instant any path produces a code, so a leg that is
    still in flight finishes AFTER the caller has read its telemetry and saved.
    Anything it learned — including what it SPENT — is lost unless the worker
    writes it itself.

    That mattered immediately. GY12CYO, the first BMW through the new pool, had
    pl24 answer in 1.64s while One Auto needs ~6s; the row recorded
    oneauto_cost NULL even though the call was made. An unrecorded charge is
    invisible to the daily budget breaker, which is the one thing standing
    between a bug and a £30 day.

    Best-effort throughout: recording an observation must never break a lookup
    a customer is waiting on.
    """
    result = oneauto.lookup(
        vin=vin, make=make, model=model, year=year,
        search_id=search_id, cost_sink=sink,
    )
    # SECOND CHANCE (paint73) — a COLLECTION rather than a retry. 'still_fetching'
    # means their job was running server-side when we stopped polling, and One
    # Auto hold a result for 24 hours. PF68MYJ recorded still_fetching in
    # coloureg and then answered in 685ms on the very next call.
    #
    # ONLY on still_fetching. A 206 is a settled "no data" and a 200 has already
    # answered; asking again would spend time on a question already decided.
    # Measured to be free: billing is per VIN, not per call — a repeat call on
    # WBAJA92070BV21477 moved the balance not at all.
    if result is None and sink.get('outcome') == 'still_fetching':
        # Recorded for the same reason as VDG's, though this one is FREE —
        # One Auto bills per VIN, not per call. Worth measuring anyway: if it
        # rarely collects anything, the wait it adds is not buying much.
        from lookup.models import Search   # lazy: circular import at module load
        oa_second = Search.SECOND_CHANCE_EMPTY
        oa_after_race = bool(race_over is not None and race_over.is_set())
        try:
            again = oneauto.lookup(
                vin=vin, make=make, model=model, year=year,
                search_id=search_id, cost_sink=sink,
                budget=SECOND_CHANCE_S,
            )
        except Exception:  # noqa: BLE001 — a second chance must never raise
            again = None
        if again:
            logger.info('One Auto second chance collected a result')
            result = again
            oa_second = Search.SECOND_CHANCE_WON
        if search_id is not None:
            _record_worker_result(search_id, oneauto_second_chance=oa_second)
            if oa_after_race:
                _record_worker_result(search_id, second_chance_after_race=True)
    if search_id is not None:
        fields = {}
        if sink.get('cost') is not None:
            fields['oneauto_cost'] = sink['cost']
        if sink.get('outcome'):
            fields['oneauto_outcome'] = str(sink['outcome'])[:40]
        # What it SAID, win or lose. A losing answer is what makes source
        # comparison possible, and a name we cannot resolve today may resolve
        # once the table grows.
        if result:
            if result.get('code'):
                fields['oneauto_code'] = str(result['code'])[:100]
            if result.get('description'):
                fields['oneauto_name'] = str(result['description'])[:120]
        if fields:
            _record_worker_result(search_id, **fields)
    return result


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
    # THREE workers for three legs (paint83). It was 2 when the pool held only
    # the VDG retry and pl24; a third leg made it three, and a third leg in a
    # two-worker pool does not run — it QUEUES.
    #
    # paint95 replaced One Auto with Ezyvin as that third leg, so the COUNT is
    # unchanged and the RELATIONSHIP is what matters: max_workers >= the number
    # of ex.submit sites below. Asserting the literal 3 would pass while a
    # fourth leg reintroduced the starvation this fixed.
    #
    # LF73YMU showed exactly that: One Auto polled to its 30s budget while pl24,
    # submitted by the backstop at 10s, sat waiting for a free worker. It only
    # started once One Auto released one, and the lookup took 36.7s to return a
    # code pl24 could have supplied in about one. The backstop had fired
    # correctly; there was simply nothing to run it on.
    # paint140: 4, not 3 — mmw is a fourth leg. The RELATIONSHIP is what
    # matters (see above): max_workers >= the number of legs, or a leg silently
    # queues behind another and its timing measurements become fiction.
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=4)
    _t = telemetry if telemetry is not None else {}
    _start = time.monotonic()
    # Recovery telemetry, finalised in the `finally` block so it is written no
    # matter which return path (or timeout) we take. pl24 and vdg-retry are both
    # always submitted, so "attempted" is True for both once we get here.
    _t['recovery_attempted'] = True
    _t['vdg_retry_returned'] = False
    # pl24 is no longer started unconditionally, so 'attempted' is set when it
    # actually is (paint68).
    _t['pl24_attempted'] = False
    _t['pl24_started_because'] = ''
    _t['pl24_returned'] = False
    _t['pl24_name_only'] = False
    # paint95: Ezyvin replaces One Auto here. The oneauto_* keys are still
    # written, as False/None/'' — the Search columns and every admin chart read
    # them, and dropping them mid-flight would blank historical comparisons
    # rather than showing the leg stopping.
    _t['oneauto_attempted'] = False
    _t['oneauto_returned'] = False
    _t['oneauto_name_only'] = False
    _t['oneauto_cost'] = None
    _t['oneauto_outcome'] = ''
    _t['ezyvin_attempted'] = False
    _t['ezyvin_returned'] = False
    _t['ezyvin_name_only'] = False
    _t['ezyvin_credits'] = None
    _t['ezyvin_outcome'] = ''
    _t['ezyvin_started_because'] = ''
    # RACE FLAG. Set the moment a usable code is found, so a second chance that
    # fires afterwards can record that it did. Measurement only right now — it
    # cancels nothing, because nothing here CAN be cancelled: an HTTP call
    # already in flight completes and is billed whatever a flag says (paint21).
    #
    # What it makes answerable is whether a race-over flag is worth building at
    # all. The only call it could ever prevent is a second chance not yet
    # started, and until this field exists there is no way to know how often
    # that happens.
    race_over = threading.Event()
    try:
        # paint140: mmw starts HERE, at t=0, before anything waits on the VIN.
        #
        # It is the only leg that needs just the registration — pl24 and Ezyvin
        # both need the VIN from the £0.06 vehicle call. It is free, so there
        # is nothing to weigh against starting it early, and it answers in
        # about a second.
        #
        # ITS ANSWER IS HELD, NOT RACED. Everything below can beat it: the
        # order of preference is VDG, pl24, Ezyvin, then mmw. That is a trust
        # ordering, not an economic one — mmw is free and Ezyvin costs 45p, so
        # every lookup where mmw was right and Ezyvin was called is money spent
        # to avoid a scraper. Whether that ordering is correct is exactly what
        # mmw_agreed is recorded to find out.
        # Set on the MAIN THREAD at submit time, exactly as pl24 does in
        # _start_pl24. _mmw_lookup also records it via _record_worker_result,
        # but that is a direct DB write from a worker thread — it never reaches
        # this dict, so a caller reading telemetry would see the leg as never
        # attempted. Two channels reporting the same fact must agree.
        _t['mmw_attempted'] = True
        f_mmw = ex.submit(_mmw_lookup, registration, search_id)

        f_vdg = ex.submit(_vdg_retry, registration, _t, search_id, race_over)
        # Category is routed (not raw): VW commercial lines misfiled as M1 by
        # VDG are sent to pl24 as N1 so the lookup hits the right catalogue
        # first time. See _route_category.
        # pl24 is NOT started here (paint68). It is the one source we do not own
        # — a partslink24 subscription with a browser session that can be locked
        # out, as it was on 13 Aug when amended T&Cs blocked the login and four
        # lookups failed until a human logged in by hand. So it is held back as
        # REINFORCEMENT and started only when a paid leg has already dropped
        # out, which keeps it on a fraction of lookups instead of all of them.
        #
        # Holding it back costs nothing in the common case because the two paid
        # legs start immediately and failure is FAST while success is slow: a
        # One Auto 206 lands in ~6s and a VDG paint miss refunds quickly, while
        # a VDG paint HIT can take 10-26s. So "one has dropped out" is a signal
        # that arrives early — exactly when reinforcement is useful.
        f_pl24 = None

        def _start_pl24(reason):
            """Bring pl24 into the race. Idempotent."""
            nonlocal f_pl24
            if f_pl24 is not None:
                return None
            _t['pl24_attempted'] = True
            _t['pl24_started_because'] = reason
            # Make AND category are both routed (not raw) at this boundary. The
            # Search row keeps VDG's originals either way — this rewrite applies
            # solely to what pl24 receives.
            f_pl24 = ex.submit(
                _pl24_lookup, vin, route_make(make),
                _route_category(make, model, vin, category),
                search_id,
            )
            return f_pl24

        # paint96: STARTED IMMEDIATELY, alongside VDG.
        #
        # paint68 held it back as reinforcement — summoned by a paid leg
        # dropping out, or a 10s backstop — because it is the one source we do
        # not own: a partslink24 subscription whose session can be locked out,
        # as it was on 13 Aug when amended T&Cs blocked the login and four
        # lookups failed until a human logged in by hand. Holding it back kept
        # it on roughly 73% of lookups instead of all of them.
        #
        # That is now traded the other way. pl24 wins 52% of non-VAG
        # deliveries and is FREE, so every second it spends waiting for VDG to
        # fail is a second the customer waits for nothing. Starting it at zero
        # also pulls the reserve's both-empty trigger forward by about VDG's
        # median 4.5s, which is worth more than any backstop tuning.
        #
        # THE COST IS LOAD: ~37% more calls on the supplier we control least.
        # If partslink24 ever rate-limits or locks out, this is the change that
        # caused it. Reverting is one line — delete this call and the drop-out
        # triggers below come back to life on their own.
        _start_pl24('immediate')

        # THIRD LEG (paint95) — EZYVIN, AND IT IS NOT IN THE RACE.
        #
        # One Auto used to sit here, started unconditionally alongside VDG. It
        # is gone. Measured over 24 days: it won 27 of 621 deliveries, 16 of
        # those were answers another leg also produced, and on 208 VAG lookups
        # it was the only source ZERO times. £115.20 spent, £102.30 of it on
        # calls whose answers were discarded.
        #
        # Ezyvin is the replacement and it is HELD BACK, for a different reason
        # than pl24 is. pl24 can start on a single drop-out because it is free.
        # Ezyvin charges 5 credits for any 200 — including one carrying no
        # colour — and in 44% of deliveries one leg comes back empty while the
        # other still delivers. Starting it on one drop-out would spend roughly
        # £188/month on answers that were already arriving.
        #
        # So the trigger is BOTH legs finished with no code. Measured on 18
        # recorded failures — the population that actually reaches here — it
        # answered 15, returned a free 404 on 3, and was charged-and-empty on
        # none: 75 credits, £8.25, for lookups the pipeline currently loses
        # outright.
        f_ezyvin = None
        def _start_ezyvin(reason):
            """Bring Ezyvin in. Idempotent."""
            nonlocal f_ezyvin
            if f_ezyvin is not None or not vin:
                return None
            _t['ezyvin_attempted'] = True
            _t['ezyvin_started_because'] = reason
            # paint112: BUDGET = WHATEVER THE RACE HAS LEFT, not a fixed 20s.
            #
            # Ezyvin bills on SUBMISSION — the job runs on their side whether
            # or not we wait — so abandoning it early is pure loss: full price,
            # no code, and the customer gets the manual-lookup offer for a car
            # we already paid to identify.
            #
            # Y288SCT, a 2001 Fiat Punto on 10 Sep, is exactly that. The job
            # finished and returned a code when run by hand; the pipeline gave
            # up at 20s with roughly 20s of race deadline still unused, and was
            # charged 5 credits for nothing.
            #
            # The reserve starts LAST, so whatever remains is its natural
            # budget — there is nothing waiting behind it to protect. The floor
            # keeps a near-expired race from submitting a job it cannot
            # possibly hear back from, which would be the same waste again.
            _ez_budget = deadline - time.monotonic() - 0.5
            if _ez_budget < 3.0:
                _t['ezyvin_started_because'] = ''
                _t['ezyvin_attempted'] = False
                _t['ezyvin_outcome'] = 'skipped_no_time'
                f_ezyvin = None
                return None
            f_ezyvin = ex.submit(ezyvin.lookup, vin, _ez_sink, race_over,
                                 _ez_budget)
            return f_ezyvin
        _ez_sink = {}
        # A LATE BACKSTOP, deliberately. Its job is to catch a HUNG leg, not to
        # race: a leg that is merely slow will usually still answer, and buying
        # its answer from Ezyvin instead is money for nothing. Measured against
        # 24 days of deliveries, a backstop at 15s pre-empts 32% of them
        # (~£125/month) while one at 25s pre-empts 4% (~£15/month). Ezyvin adds
        # ~2.2s on a hit, so a 25s backstop still answers inside the 60s
        # deadline with room to spare.
        ezyvin_backstop_at = time.monotonic() + EZYVIN_BACKSTOP_S
        ezyvin_name_only_result = None
        deadline = time.monotonic() + PL24_TIMEOUT
        pending = {f_vdg, f_pl24}
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
            # And for Ezyvin's, so a pair of slow-but-alive legs cannot leave
            # the reserve unstarted either.
            if f_ezyvin is None:
                remaining = min(remaining,
                                max(0.05, ezyvin_backstop_at - time.monotonic()))
            done, pending = concurrent.futures.wait(
                pending, timeout=remaining,
                return_when=concurrent.futures.FIRST_COMPLETED,
            )
            if not done:
                # Nothing completed in this slice. If that was the backstop
                # waking us, bring pl24 in and keep waiting; otherwise the real
                # deadline has passed.
                if f_ezyvin is None and time.monotonic() >= ezyvin_backstop_at:
                    started = _start_ezyvin('backstop')
                    if started is not None:
                        pending = pending | {started}
                        continue
                break

            # Enforce the VDG-over-pl24 preference within this batch: if the VDG
            # future is among the just-completed ones and produced paint, that
            # wins outright — regardless of whether pl24 also completed here.
            if f_vdg in done:
                vdg_result = _result_or_none(f_vdg)
                if vdg_result is not None:
                    _t['vdg_retry_returned'] = True
                    race_over.set()   # a usable code exists from here on
                    return _enrich_from_lookup(vdg_result, make, model,
                                               vdg_colour=vdg_colour, telemetry=_t)

            # VDG didn't (yet) yield paint. Inspect pl24 if it completed in this
            # batch. A real CODE wins immediately (subject only to a VDG code,
            # already handled above). A name-only result (colour name, no code)
            # is held aside as a FALLBACK — we do NOT return it here, because a
            # real code from a still-pending VDG-retry must be able to beat it.
            if f_pl24 is not None and f_pl24 in done:
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
                race_over.set()   # a usable code exists from here on
                return _enrich_from_lookup(pl24_code_result, make, model,
                                           vdg_colour=vdg_colour, telemetry=_t)
            # THE RESERVE. Started only once BOTH paid-and-free legs have
            # finished with nothing, so by the time it is inspected there is
            # nothing left to beat it — but it is read last regardless, because
            # a leg that costs credits should never pre-empt one that does not.
            if f_ezyvin is not None and f_ezyvin in done:
                _t['ezyvin_credits'] = _ez_sink.get('credits')
                _t['ezyvin_outcome'] = _ez_sink.get('outcome', '')
                ez = _result_or_none(f_ezyvin)
                if ez is not None and ez.get('code'):
                    _t['ezyvin_returned'] = True
                    race_over.set()
                    return _enrich_from_lookup(
                        {'paint_code': ez['code'],
                         'paint_description': ez['description'],
                         'all_paint_codes': [],
                         'source': 'ezyvin'},
                        make, model, vdg_colour=vdg_colour, telemetry=_t,
                    )
                # A NAME with no code is still real manufacturer data, and four
                # Mazdas measured on 8 Sep came back exactly that way —
                # 'MARINER BLUE', 'DEEP CRYSTAL BLUE MICA'. Held as a fallback
                # so _enrich_from_lookup can try to resolve it through our own
                # table, same as pl24's.
                if ez is not None and ez.get('description'):
                    _t['ezyvin_name_only'] = True
                    ezyvin_name_only_result = ez
            # BOTH LEGS DONE, NEITHER HAD A CODE — the trigger. Checked here
            # rather than on a single drop-out because Ezyvin is charged on any
            # 200: firing when one leg is empty while the other still delivers
            # would spend on 44% of deliveries that never needed it.
            if (f_ezyvin is None and f_pl24 is not None
                    and f_vdg not in pending and f_pl24 not in pending):
                started = _start_ezyvin('both_empty')
                if started is not None:
                    pending = pending | {started}

        # No real code from any path. Fall back to a colour NAME if one was
        # offered — a partial answer, but real manufacturer data and often
        # enough for someone at a paint counter.
        #
        # pl24's name is preferred over One Auto's because pl24 reads the
        # manufacturer's own catalogue for THIS vehicle, while One Auto's
        # Stellantis names arrive stripped of their code ('OKENITE WHITE PAINT-')
        # and are a marketing name rather than a catalogue entry.
        #
        # Either way _enrich_from_lookup may upgrade it to a full code: it runs
        # the name through code_from_name, which returns a code ONLY when the
        # candidates collapse to a single paint. Names are 1:many with codes
        # ('Race Red' matches 13, 'Black Pearl' 481), so it declines far more
        # often than it resolves — deliberately, because a wrong code is worse
        # than none when the customer is about to buy paint.
        # paint140: mmw is the LAST code source, after VDG, pl24 and Ezyvin have
        # all failed to produce one. Free, already answered, and only served if
        # the catalogue corroborates it against the registered colour.
        #
        # Placed above the name-only fallback deliberately: a validated CODE is
        # worth more to the customer than a colour name with no code, which is
        # what that fallback delivers.
        _mmw_code = _mmw_settle(f_mmw, make, vdg_colour, _t)
        if _mmw_code:
            return _enrich_from_lookup(
                {'paint_code': _mmw_code, 'paint_description': '',
                 'all_paint_codes': [], 'source': 'mmw'},
                make, model, vdg_colour=vdg_colour, telemetry=_t)

        fallback = pl24_name_only_result
        if fallback is None and ezyvin_name_only_result is not None:
            ez = ezyvin_name_only_result
            fallback = {
                'paint_code': '',
                'paint_description': ez.get('description', ''),
                'all_paint_codes': [],
                'source': 'ezyvin',
                'name_only': True,
            }
        return _enrich_from_lookup(fallback, make, model, vdg_colour=vdg_colour,
                                   telemetry=_t)
    finally:
        # Do NOT block on stragglers. wait=False means we don't join running
        # threads; cancel_futures cancels any not-yet-started work. A pl24 thread
        # still mid-request is abandoned and ends when its own HTTP timeout fires.
        ex.shutdown(wait=False, cancel_futures=True)
        _t['duration_ms'] = int((time.monotonic() - _start) * 1000)