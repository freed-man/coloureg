"""Anti-bleed protection helpers (paint15).

Three independent layers that all sit in front of the paid VDG call, so an
abuser — or a bug — cannot run up unbounded spend:

  1. Blocklists (reg / IP / user-agent)      -> lives on SiteConfig (models.py)
  2. VRM result cache (repeat regs cost £0)  -> this module
  3. Daily budget breaker (hard £ ceiling)   -> this module

Layer 1 is on the model because it's pure data + trivial matching. Layers 2 and
3 need to query the Search table, so they live here.

Design notes
------------
* The budget breaker reads VDG's own money trail: it sums `vdg_transaction_cost`
  over today's rows. That column is now stored NET of refunds (see vdg.py
  `_extract_transaction_cost`), so the sum is what VDG actually kept — the same
  quantity the account balance moves by — not the inflated gross. This is why
  the earlier gross-vs-net bug mattered: a breaker on the gross figure would
  trip early and lock out real users before the true budget was reached.

* "Today" is London-local (Europe/London) to match how a human reads "per day",
  computed from the aware UTC now via timezone.localtime.

* The VRM cache serves a previously stored successful result for the same
  registration within a TTL window. A registration's factory paint code is
  effectively immutable, so this is safe — the one edge case is a cherished-
  plate transfer moving a reg to a different vehicle, which the 30-day TTL
  bounds. Repeat lookups of the same reg (the exact abuse pattern seen) then
  cost £0 because no VDG call is made at all.
"""

from datetime import timedelta
from decimal import Decimal

import logging

from django.db.models import F, Sum
from django.utils import timezone
from .http import get_session


# How long a cached VRM result stays fresh.
#
# Paint codes are factory-fixed, so this TTL isn't about the code going out of
# date — it bounds two other things: a cherished-plate transfer moving a reg to
# a different vehicle, and the delay before a corrected entry (a new curated
# override, a fixed upstream record) reaches customers.
#
# Set from the real repeat-lookup distribution rather than a guess. Of 88
# repeat lookups of an already-cached reg: 56% came back within an hour, 78%
# within a day, 95.5% within 7 days, 98.9% within 30. Dropping 30 -> 7 gives up
# 3 cache hits (about 75p across the whole dataset) and cuts the staleness
# window by three quarters. Nearly all the value is in the first day anyway —
# someone checking a reg twice in an afternoon, or a body shop returning to the
# same car — so the long tail was buying almost nothing.
logger = logging.getLogger(__name__)

VRM_CACHE_TTL_DAYS = 7

# How long a FAILED lookup is remembered. Much shorter than a success: a miss
# might only be a miss because VDG flaked or was slow that second (the data shows
# VDG is genuinely non-deterministic), so we must let a reg retry soon. But
# within this window, a repeat of a known-dud reg is served instantly with no VDG
# spend — which is what stops a proxy pool from re-running the same unanswerable
# reg at full price over and over (the observed abuse). One hour is the balance:
# long enough to absorb a burst, short enough that a real retry gets a fresh shot.
VRM_NEGATIVE_TTL_SECONDS = 60 * 60


def london_day_start(now=None):
    """Return the start (00:00) of the current London day as an aware datetime.

    Used by the budget breaker so "spend today" means a UK calendar day, not a
    UTC one — the two diverge by up to an hour under BST.
    """
    now = now or timezone.now()
    local = timezone.localtime(now)  # -> Europe/London per settings.TIME_ZONE
    local_midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return local_midnight


def spend_today(now=None):
    """Sum of ALL provider spend since London midnight, as a Decimal.

    VDG (`vdg_transaction_cost`, net of refunds) plus One Auto
    (`oneauto_cost`). Both, because the breaker exists to stop a runaway day and
    a runaway day does not care which supplier caused it — £30 of One Auto with
    VDG at zero is exactly as bad. Kept in separate COLUMNS so the dashboard can
    still show which provider spent what.

    Rows without a cost (DVLA-fallback lookups that never called a paid
    provider, a One Auto 206, or pre-field legacy rows) contribute nothing.
    Returns Decimal('0.00') when there has been no spend.
    """
    # Imported here to avoid a circular import at module load (models imports
    # nothing from services, but services importing models at top level plus
    # views importing both can tangle during app startup).
    from lookup.models import Search

    start = london_day_start(now)
    agg = (
        Search.objects
        .filter(timestamp__gte=start)
        .aggregate(vdg=Sum('vdg_transaction_cost'), oneauto=Sum('oneauto_cost'))
    )
    return (agg['vdg'] or Decimal('0.00')) + (agg['oneauto'] or Decimal('0.00'))


def spend_today_by_provider(now=None):
    """Same window, split by provider — for the admin panel.

    Returns {'vdg': Decimal, 'oneauto': Decimal, 'total': Decimal}.
    """
    from lookup.models import Search

    start = london_day_start(now)
    agg = (
        Search.objects
        .filter(timestamp__gte=start)
        .aggregate(vdg=Sum('vdg_transaction_cost'), oneauto=Sum('oneauto_cost'))
    )
    vdg = agg['vdg'] or Decimal('0.00')
    oneauto = agg['oneauto'] or Decimal('0.00')
    return {'vdg': vdg, 'oneauto': oneauto, 'total': vdg + oneauto}


def budget_exceeded(config, now=None):
    """True if today's spend has reached the configured daily budget.

    `config` is a SiteConfig instance (already fetched by the caller, so this
    adds no config query). A budget of 0 disables the breaker (always False).
    """
    budget = config.daily_budget_gbp or Decimal('0')
    if budget <= 0:
        return False
    return spend_today(now) >= budget


def get_cached_vrm_payload(registration, now=None, count_hit=True):
    """Return a fresh cached results payload (dict) for this reg, or None.

    Freshness is enforced here by comparing updated_at against the TTL, so stale
    entries are simply ignored (and overwritten on the next live lookup) — no
    purge job needed. On a hit, increments hit_count for observability and
    returns a COPY of the stored payload so the caller can add per-request keys
    (search_id, paint_pending) without mutating the cache.
    """
    from lookup.models import VrmCache

    now = now or timezone.now()
    cutoff = now - timedelta(days=VRM_CACHE_TTL_DAYS)
    try:
        entry = VrmCache.objects.get(registration=registration)
    except VrmCache.DoesNotExist:
        return None
    if entry.updated_at < cutoff:
        return None  # stale — treat as miss; next live lookup refreshes it
    # Count the hit without racing on the whole row — but ONLY when this read is
    # actually serving a lookup. The /vehicle-make/ endpoint also reads the cache
    # (to name the manufacturer in the loading message), and counting that would
    # double every figure: one real lookup fired both paths, so "free repeats
    # served" advanced by 2 each time.
    if count_hit:
        # F() expression, not entry.hit_count + 1 (paint17): the Python form is
        # a read-modify-write and two concurrent hits on the same reg both
        # compute the same successor, so one increment is silently lost. F()
        # increments in the database. Only observability, but the figure feeds
        # the "free repeats served" card and a wrong number there is worse than
        # no number.
        VrmCache.objects.filter(pk=entry.pk).update(hit_count=F('hit_count') + 1)
    return dict(entry.payload or {})


def store_vrm_payload(registration, payload):
    """Upsert the cache entry for a successful lookup.

    `payload` should be the results 'vehicle_data' dict WITHOUT request-specific
    keys (search_id, paint_pending) — those are recomputed on each serve. Never
    raises into the caller: a cache write failure must not break a live lookup
    that already succeeded.
    """
    from lookup.models import VrmCache

    clean = dict(payload or {})
    clean.pop('search_id', None)
    clean.pop('paint_pending', None)
    try:
        VrmCache.objects.update_or_create(
            registration=registration,
            defaults={'payload': clean},
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Cloudflare Turnstile (E)
# ---------------------------------------------------------------------------

TURNSTILE_VERIFY_URL = 'https://challenges.cloudflare.com/turnstile/v0/siteverify'


def turnstile_configured():
    """True when both Turnstile keys are present in settings.

    The whole feature is safe-by-default: unconfigured -> the form renders no
    widget and the server skips verification, so shipping this code changes
    nothing until the keys are added to the environment.
    """
    from django.conf import settings
    return bool(settings.TURNSTILE_SITE_KEY and settings.TURNSTILE_SECRET_KEY)


def verify_turnstile(token, remote_ip=None, timeout=5):
    """Server-side verification of a Turnstile token. Returns True/False.

    Called BEFORE anything costs money: a failed or missing token rejects the
    lookup with zero Stripe and zero VDG spend. Fails CLOSED on a bad/missing
    token but fails OPEN on a Cloudflare outage (network error / non-JSON):
    if Cloudflare's verify endpoint itself is down we'd rather serve real
    users unprotected for a few minutes than take the whole site down with it —
    the budget breaker still bounds the worst case underneath.
    """
    import requests
    from django.conf import settings

    if not turnstile_configured():
        return True  # feature off -> pass-through
    if not token:
        return False  # feature on + no token -> reject (this is the bot case)
    try:
        resp = get_session().post(
            TURNSTILE_VERIFY_URL,
            data={
                'secret': settings.TURNSTILE_SECRET_KEY,
                'response': token,
                **({'remoteip': remote_ip} if remote_ip else {}),
            },
            timeout=timeout,
        )
        data = resp.json()
        if not data.get('success'):
            return False
        # Hostname pinning (paint17), OPT-IN and off by default. Cloudflare
        # returns the hostname the token was issued for; without checking it we
        # accept any valid token, including one solved on a different site
        # someone controls. Sitekeys are already hostname-bound in the
        # dashboard, so this is defence in depth — it matters most because the
        # origin answers directly on its Railway hostname, outside Cloudflare.
        #
        # Empty setting -> skipped, same safe-by-default posture as the keys
        # themselves. Getting this list wrong would reject EVERY lookup (bad
        # token fails closed), so it stays inert until deliberately populated
        # via TURNSTILE_ALLOWED_HOSTNAMES.
        allowed = getattr(settings, 'TURNSTILE_ALLOWED_HOSTNAMES', None) or []
        if allowed:
            hostname = data.get('hostname') or ''
            if hostname not in allowed:
                # Logged, not silent. A rejection here and a genuine bot block
                # produce the SAME message for the user ("We could not verify
                # your browser"), so without this line a misconfigured
                # allowlist is indistinguishable from the protection working —
                # and it would reject every lookup on the site. This says
                # exactly which hostname Cloudflare reported and what was
                # expected, so the fix is a one-line answer in the logs.
                logger.warning(
                    'Turnstile hostname rejected: Cloudflare reported %r, '
                    'TURNSTILE_ALLOWED_HOSTNAMES=%r. If %r is legitimate, add '
                    'it to the env var or unset the var to disable this check.',
                    hostname, allowed, hostname,
                )
                return False
        return True
    except Exception:
        # Cloudflare verify unreachable/broken -> fail open (see docstring).
        return True


# ---------------------------------------------------------------------------
# Negative cache (repeated failed lookups)
# ---------------------------------------------------------------------------
# Stored in the DEFAULT (database) cache, not VrmCache: misses are short-lived
# and high-churn, so a TTL cache is the right tool, and the DB cache is shared
# across workers (unlike locmem). Keyed by registration.

def _neg_key(registration):
    return f'lookup_miss:{registration}'


def is_recent_miss(registration):
    """True if this reg failed a lookup within VRM_NEGATIVE_TTL_SECONDS.

    Lets the caller short-circuit a repeat of a known-dud reg without spending on
    VDG. Never raises — a cache hiccup just means we do the live lookup.
    """
    from django.core.cache import caches

    try:
        return caches['default'].get(_neg_key(registration)) is not None
    except Exception:
        return False


def record_miss(registration):
    """Remember that this reg just failed, for VRM_NEGATIVE_TTL_SECONDS.

    Called after a live lookup returns no paint code. Best-effort.
    """
    from django.core.cache import caches
    try:
        caches['default'].set(
            _neg_key(registration), True, VRM_NEGATIVE_TTL_SECONDS
        )
    except Exception:
        pass


def clear_miss(registration):
    """Forget a recorded miss — called when a lookup later succeeds, so a reg
    that starts returning paint isn't held back by a stale negative entry."""
    from django.core.cache import caches
    try:
        caches['default'].delete(_neg_key(registration))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Sliding-window rate limit (paint16)
# ---------------------------------------------------------------------------
# django-ratelimit uses FIXED windows: '3/h' buckets time into clock hours, so
# the counter resets at the top of every hour. That let a real IP make 4 lookups
# in 13 minutes (3 at 22:52-22:53, a 4th at 23:05 once the bucket rolled over),
# and in the worst case allows 6 back-to-back across a boundary. Observed in
# production on 31 Jul.
#
# This is a true sliding window: it keeps the timestamps of recent requests and
# counts how many fall inside the last N seconds, so the limit holds no matter
# where the clock is. Stored in the shared DB cache so it works across workers.

SLIDING_WINDOW_SECONDS = 3600
SLIDING_WINDOW_LIMIT = 3


def _sw_key(scope, ident):
    return f'sw:{scope}:{ident}'


def credit_sliding_allowance(scope, ident, window=SLIDING_WINDOW_SECONDS, now=None):
    """Give one allowance back to `ident` (paint22).

    Called when a lookup has been PAID for. The free-tier limit exists to bound
    what non-paying traffic can cost us; a lookup someone paid for is not that,
    so it should not count against them. This restores the intent of the old
    paid path — which deliberately had no rate limit — without reopening the
    hole, because the exemption now has to be bought.

    It cannot be farmed. One credit costs the customer the full lookup price and
    buys them a lookup that costs us a fraction of it, so anyone "abusing" this
    is handing us the difference. There is no version of this where the attacker
    comes out ahead.

    Drops the OLDEST timestamp rather than clearing the list: it returns exactly
    one slot, so a payer cannot bank an unlimited burst by paying once.

    Silent no-op when `ident` is falsy — a lookup whose IP failed validation
    (paint19 stores those as NULL) simply cannot be credited, and the payment
    still stands. Fails silently on cache errors for the same reason
    sliding_rate_limited fails open: bookkeeping must never break a paid
    transaction.
    """
    if not ident:
        return False
    import time as _time
    from django.core.cache import caches

    now = now or _time.time()
    cutoff = now - window
    key = _sw_key(scope, ident)
    try:
        cache = caches['default']
        hits = sorted(t for t in (cache.get(key) or []) if t > cutoff)
        if not hits:
            return False
        hits.pop(0)
        cache.set(key, hits, window)
        return True
    except Exception:  # noqa: BLE001
        return False


def sliding_rate_limited(scope, ident, limit=SLIDING_WINDOW_LIMIT,
                         window=SLIDING_WINDOW_SECONDS, now=None):
    """Return True if `ident` has already used its allowance in the last `window`
    seconds, else record this request and return False.

    Fails OPEN on any cache error: if the cache is unavailable we would rather
    serve a request than take the site down, and the daily budget breaker still
    bounds the worst case underneath.
    """
    import time as _time
    from django.core.cache import caches

    now = now or _time.time()
    cutoff = now - window
    key = _sw_key(scope, ident)
    lock_key = key + ':lock'
    try:
        cache = caches['default']

        # paint51: the read-modify-write below is NOT atomic, and the race is far
        # worse than the "+1 tolerance" previously documented. Measured with 12
        # simultaneous requests from one IP against a limit of 3, five trials:
        # 11, 12, 4, 10, 8 allowed. Every thread reads the list before any writes
        # it, so a genuinely simultaneous burst passes almost entirely.
        #
        # Serialised with cache.add(), which is atomic on DatabaseCache — the
        # same primitive already trusted for the payment-capture lock, where
        # getting it wrong would double-charge. Cost is two extra cache ops on a
        # path that also makes a multi-second external call.
        #
        # Whoever loses the race spins briefly rather than proceeding: the
        # critical section is two cache ops, so the wait is sub-millisecond in
        # practice. After ~50ms we give up and fall through UNLOCKED, preserving
        # the old fail-open behaviour rather than blocking a real customer
        # because the cache is slow.
        # Read FIRST, unlocked. If the caller is already over the limit the
        # answer cannot change by taking a lock — nobody removes hits from the
        # window — so the common blocked case costs one query and no lock at
        # all. Only the borderline case, where we are about to APPEND, needs
        # serialising.
        hits = [t for t in (cache.get(key) or []) if t > cutoff]
        if len(hits) >= limit:
            cache.set(key, hits, window)
            return True

        _held = False
        _deadline = _time.time() + 0.05
        while _time.time() < _deadline:
            if cache.add(lock_key, 1, 5):
                _held = True
                break
            _time.sleep(0.002)

        try:
            # RE-READ inside the lock. The unlocked read above is only a cheap
            # rejection for callers already over the limit; by the time this
            # thread acquired the lock another may have appended, so appending
            # on the strength of the earlier read reintroduces the race it was
            # meant to close.
            hits = [t for t in (cache.get(key) or []) if t > cutoff]
            if len(hits) >= limit:
                # Re-store the pruned list so it can't grow without bound while a
                # client keeps hammering a blocked endpoint.
                cache.set(key, hits, window)
                return True
            hits.append(now)
            cache.set(key, hits, window)
            return False
        finally:
            if _held:
                try:
                    cache.delete(lock_key)
                except Exception:  # noqa: BLE001 — the 5s TTL is the backstop
                    pass
    except Exception:
        return False
