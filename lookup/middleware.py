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