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

logger = logging.getLogger(__name__)

_ORIGIN_LOG_KEY = 'origin-gate:last-log'
_ORIGIN_COUNT_KEY = 'origin-gate:direct-count'
_ORIGIN_LOG_EVERY_S = 300


class OriginGateObserverMiddleware:
    """Count requests that did not come through Cloudflare, and say so.

    This is the evidence stage. It changes nothing about how a request is
    handled — it only records how many arrive without the Transform Rule's
    header, so ORIGIN_GATE_MODE can be moved to 'enforce' on data rather than
    on hope. If effectively all traffic carries the header, enforcing is safe.
    If it does not, enforcing would put real visitors on a shared rate-limit
    bucket, and this log is what stops that being discovered in production.

    THROTTLED. A bot hitting the origin directly would otherwise emit a log line
    per request; instead the count accumulates and is reported at most once
    every few minutes, so the signal survives contact with a flood.

    Placed after HealthCheckMiddleware, which short-circuits before this runs —
    Railway's internal probe never reaches here and so is never counted as a
    direct hit.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        try:
            self._observe(request)
        except Exception:
            # Observation must never affect a response. If the cache is missing
            # (see F6) or anything else misbehaves, the request continues.
            pass
        return self.get_response(request)

    def _observe(self, request):
        from lookup.views import ORIGIN_SECRET, via_cloudflare
        if not ORIGIN_SECRET or via_cloudflare(request):
            return
        cache = caches['local']
        count = (cache.get(_ORIGIN_COUNT_KEY) or 0) + 1
        cache.set(_ORIGIN_COUNT_KEY, count, 3600)
        if cache.add(_ORIGIN_LOG_KEY, 1, _ORIGIN_LOG_EVERY_S):
            logger.warning(
                'Origin gate: %d request(s) since last report arrived without a '
                'valid Cloudflare header (latest path=%s). Cloudflare is being '
                'bypassed, or the Transform Rule is not firing.',
                count, request.path,
            )
            cache.set(_ORIGIN_COUNT_KEY, 0, 3600)
