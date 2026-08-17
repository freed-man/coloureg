"""
Health-check middleware.

Railway's deploy-time healthcheck probe hits the service from inside Railway's
network with an internal Host header that isn't (and shouldn't need to be) in
ALLOWED_HOSTS. Django validates the Host header early in request handling and
returns 400 DisallowedHost for anything unrecognised — which makes the probe
fail and blocks the deploy, even though the app is perfectly healthy.

Rather than try to enumerate every internal Host Railway might use (which is
brittle and undocumented), this middleware short-circuits the health endpoint
*before* host validation or the HTTPS redirect run. It is placed FIRST in
MIDDLEWARE, so a request to the health path returns a plain 200 immediately,
regardless of Host header or scheme. All other requests pass straight through
untouched, so normal host validation and security behaviour are unaffected.

This is the standard pattern for liveness probes behind a platform proxy.
"""

from django.http import HttpResponse

HEALTH_PATHS = frozenset({'/health', '/health/'})


class HealthCheckMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path in HEALTH_PATHS:
            # Plain 200, no DB touch, no host check, no redirect. Reflects
            # "the web process is up and serving" — exactly what a liveness
            # probe needs.
            return HttpResponse('ok', content_type='text/plain')
        return self.get_response(request)

# ---------------------------------------------------------------------------
# Origin gate observation (F2, stage 1)
# ---------------------------------------------------------------------------

import logging

from django.core.cache import caches
from django.utils import timezone

logger = logging.getLogger(__name__)

_ORIGIN_LOG_KEY = 'origin-gate:last-log'
_ORIGIN_STATS_KEY = 'origin-gate:stats'
_ORIGIN_LOG_EVERY_S = 300
_ORIGIN_STATS_TTL = 60 * 60 * 24 * 14
_ORIGIN_MAX_PATHS = 8


def origin_gate_stats():
    """Direct-hit stats for the dashboard: {'count': int, 'paths': [...], 'since': str}.

    Never raises — the dashboard must render even if the cache table is missing.
    """
    try:
        return caches['default'].get(_ORIGIN_STATS_KEY) or {}
    except Exception:
        return {}


class OriginGateObserverMiddleware:
    """Count requests that did not come through Cloudflare, and record their paths.

    This is the evidence stage. It changes nothing about how a request is
    handled — it records what arrives without the Transform Rule's header so the
    gate can be moved to 'enforce' on data rather than on hope.

    THE PATHS ARE THE POINT, not the count. A number cannot tell you whether
    enforcing is safe. The paths can: '/', '/wp-admin/', '/.env' is scanner noise
    hitting Railway's edge directly and enforcing is safe, whereas
    '/stripe/webhook/' appearing here would mean Stripe is not coming through
    Cloudflare and enforcing would break payments silently.

    Written to the DEFAULT (database) cache, not 'local'. There are two gunicorn
    workers with separate in-memory caches, so a local counter would show
    roughly half the picture and reset on every deploy. The database cache is the
    only store both workers see — the same reasoning the CACHES comment gives for
    keeping rate limits there.

    Honest limitation: DatabaseCache has no atomic increment across processes, so
    under a flood the count will run low. It is an indicator, not an accountant.
    The path list does not have that problem.

    Placed after HealthCheckMiddleware, which short-circuits before this runs, so
    Railway's internal probe is never counted as a direct hit.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        try:
            self._observe(request)
        except Exception:
            # Observation must never affect a response. If the cache table is
            # missing (see F6) or anything else misbehaves, the request continues.
            pass
        return self.get_response(request)

    def _observe(self, request):
        from lookup.views import ORIGIN_SECRET, via_cloudflare
        if not ORIGIN_SECRET or via_cloudflare(request):
            return

        cache = caches['default']
        stats = cache.get(_ORIGIN_STATS_KEY) or {}
        stats['count'] = (stats.get('count') or 0) + 1
        stats.setdefault('since', timezone.now().isoformat())

        # Most recent distinct paths, newest first, capped so a scanner walking
        # a wordlist cannot bloat the cache row.
        paths = [p for p in (stats.get('paths') or []) if p != request.path]
        stats['paths'] = ([request.path] + paths)[:_ORIGIN_MAX_PATHS]
        cache.set(_ORIGIN_STATS_KEY, stats, _ORIGIN_STATS_TTL)

        # Throttled so a flood cannot drown Sentry in identical lines.
        if caches['local'].add(_ORIGIN_LOG_KEY, 1, _ORIGIN_LOG_EVERY_S):
            logger.warning(
                'Origin gate: %d request(s) have arrived without a valid '
                'Cloudflare header (latest path=%s). Cloudflare is being '
                'bypassed, or the Transform Rule is not firing.',
                stats['count'], request.path,
            )
