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

from django.http import HttpResponse, JsonResponse

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
# paint155: `time` was used by _tick and never imported, so every call raised
# NameError into a bare `except Exception: pass` and the breaker window was
# never written. The safety mechanism that backs the origin gate off when
# Cloudflare's headers stop arriving has therefore never recorded anything.
import time

from django.core.cache import caches
from django.utils import timezone

logger = logging.getLogger(__name__)

_ORIGIN_LOG_KEY = 'origin-gate:last-log'
_ORIGIN_STATS_KEY = 'origin-gate:stats'
_ORIGIN_LOG_EVERY_S = 300
_ORIGIN_STATS_TTL = 60 * 60 * 24 * 14
_ORIGIN_MAX_PATHS = 8
_ORIGIN_MAX_REMOTES = 5

# Breaker tuning. A window must carry a real sample before it can conclude
# anything, and the share must be overwhelming — a partial outage is not what
# this detects, a total one is.
_BREAKER_KEY = 'origin-gate:window'
_BREAKER_WINDOW_S = 120
_BREAKER_MIN_SAMPLE = 30
_BREAKER_THRESHOLD = 0.9

# paint186: THE WINDOW ABOVE CANNOT TRIP ON THIS SITE. It needs 30 requests
# inside 120 seconds on ONE worker, and coloureg sees a few an hour. Measured,
# with block mode on and the Transform Rule broken: at one request every five
# minutes, and at ten times that, every visitor was refused for six hours and
# the breaker never fired. Under block that is a total outage with no end, and
# the admin dashboard is refused with everything else, so its switch cannot end
# it either. Found by an external audit (N6), then reproduced.
#
# So under BLOCK there is a second trigger that needs no window: a run of
# requests REFUSED IN A ROW, with none getting through. It works at any volume,
# because it counts events rather than rates.
#
# Why a short run is safe here. The window's large sample guards against stray
# scanners hitting the origin directly being read as "Cloudflare is broken".
# The operator's dashboard showed ZERO requests bypassing Cloudflare in 14 days
# (23 Sep), so there are no such scanners to mistake. And the costs are not
# symmetric: a false trip drops block to observe, where the site works and the
# dashboard says so; a missed trip refuses every customer indefinitely.
#
# Counted at the moment a request is REFUSED, not in _tick. _tick sees every
# request, including the exempt Stripe webhook, and a run of those arriving
# without the header must not trip block mode when nobody was refused. Reset by
# any request that DID come through Cloudflare, so a working rule clears it
# with every real visitor. Held in the per-process cache like the window, so
# counting never touches the database.
_BLOCK_TRIP_REFUSED_IN_A_ROW = 5
_BLOCK_STREAK_KEY = 'origin-gate:block-refused-streak'
# Long, deliberately. At a few requests an hour a short expiry would forget the
# streak between refusals and it could never reach the threshold — the same
# starvation this exists to fix, reintroduced by a TTL.
_BLOCK_STREAK_TTL = 60 * 60 * 24

# paint187: CHECK BEFORE SWITCHING OFF. From the server's side, "Cloudflare's tag
# stopped arriving" and "someone is hitting the origin directly" look identical,
# so a run of refusals alone could be caused on purpose: five direct requests
# and block mode switches itself off. The operator spotted it. So before acting
# on a run, the site asks ITSELF, through Cloudflare, whether the tag arrives.
_ORIGIN_CHECK_PATH = '/origin-check/'
# The refused request waits for this, so it is short; and the shared session has
# no default timeout, so without one a hung check would hold the worker.
_SELF_CHECK_TIMEOUT_S = 5
# One check per ten minutes per worker. Without it, an attacker's thousand
# requests would make this server send two hundred requests of its own. Held
# (blocking stays on) for the length of it, so a genuine break during a
# cooldown waits at most this long before the next check switches off.
_SELF_CHECK_COOLDOWN_KEY = 'origin-gate:self-check-cooldown'
_SELF_CHECK_COOLDOWN_S = 600
# An attack that keeps coming must not flood the operator's inbox.
_HELD_ALERT_KEY = 'origin-gate:held-alert'
_HELD_ALERT_THROTTLE_S = 3600


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

        # paint187: the self-check's probe. AFTER observation, so the
        # dashboard's count of direct hits stays honest, and BEFORE the block
        # decision, so it answers even when the tag is missing, which is the
        # one moment it has to: a broken rule must be reported, not refused.
        if request.path == _ORIGIN_CHECK_PATH:
            return self._answer_origin_check(request)

        # BLOCK MODE. Everything enforce does, plus refusing the request
        # outright — which is what restores Cloudflare's WAF and bot protection,
        # since neither can act on traffic that goes round them.
        #
        # Wrapped and fail-open: if deciding the mode raises for any reason, the
        # request is served. A gate that takes the site down when its own
        # config is unreadable is worse than the hole it closes.
        try:
            if self._should_block(request):
                logger.warning(
                    'Origin gate BLOCKED a request that skipped Cloudflare '
                    '(path=%s agent=%s)', request.path,
                    (request.META.get('HTTP_USER_AGENT') or '')[:60],
                )
                # 403, not 404: this is not a scanner probe being told nothing,
                # it is a real path refused for a reason. A monitor of ours
                # pointed at the wrong address should see a clear refusal in its
                # own logs rather than think the page has gone.
                self._note_refused()
                return HttpResponse(b'Forbidden', status=403,
                                    content_type='text/plain; charset=utf-8')
        except Exception:
            pass
        return self.get_response(request)

    #: Paths that must answer even when they did not come through Cloudflare.
    #: /health/ never reaches here — HealthCheckMiddleware short-circuits above
    #: this in the stack — but it is listed because that ordering is a fact
    #: about another file, and a future reorder must not silently break deploys.
    _BLOCK_EXEMPT = ('/health', '/stripe/webhook/')

    def _should_block(self, request):
        from lookup.views import ORIGIN_SECRET, via_cloudflare, origin_gate_mode
        if not ORIGIN_SECRET:
            return False          # inert until configured — never fail closed
        if origin_gate_mode() != 'block':
            return False
        if request.path.startswith(self._BLOCK_EXEMPT):
            return False
        return not via_cloudflare(request)

    def _observe(self, request):
        from lookup.views import ORIGIN_SECRET, via_cloudflare
        if not ORIGIN_SECRET:
            return

        ok = via_cloudflare(request)
        self._tick(ok)
        if ok:
            return

        cache = caches['default']
        stats = cache.get(_ORIGIN_STATS_KEY) or {}
        stats['count'] = (stats.get('count') or 0) + 1
        stats.setdefault('since', timezone.now().isoformat())

        # Most recent distinct paths, newest first, capped so a scanner walking
        # a wordlist cannot bloat the cache row.
        paths = [p for p in (stats.get('paths') or []) if p != request.path]
        stats['paths'] = ([request.path] + paths)[:_ORIGIN_MAX_PATHS]

        # THE SOURCE ADDRESS RAILWAY REPORTS for a connection that skipped
        # Cloudflare. This is the open question about enforcing: if it is
        # Railway's own proxy, every direct caller shares one rate-limit bucket;
        # if it is the caller's real address, they get one each but keyed to
        # something they cannot forge. Both defeat the attack, so it does not
        # change whether to enforce — but it decides what the limit actually
        # does, and it is answerable by looking rather than reasoning.
        remote = (request.META.get('REMOTE_ADDR') or '?').strip()
        seen = [a for a in (stats.get('remotes') or []) if a != remote]
        stats['remotes'] = ([remote] + seen)[:_ORIGIN_MAX_REMOTES]

        # WHO, not just where and what. The paths and the source address say a
        # request skipped Cloudflare; they do not say whether it was a scanner
        # or something of ours. An uptime monitor pointed at the wrong URL
        # produces a steady 1,440 hits a day that look exactly like an attack
        # until you read the agent — and that ambiguity is the only thing
        # standing between here and turning blocking on.
        ua = (request.META.get('HTTP_USER_AGENT') or '(none)').strip()[:60]
        seen_ua = [u for u in (stats.get('agents') or []) if u != ua]
        stats['agents'] = ([ua] + seen_ua)[:_ORIGIN_MAX_REMOTES]
        cache.set(_ORIGIN_STATS_KEY, stats, _ORIGIN_STATS_TTL)

        # Throttled so a flood cannot drown Sentry in identical lines.
        if caches['local'].add(_ORIGIN_LOG_KEY, 1, _ORIGIN_LOG_EVERY_S):
            logger.warning(
                'Origin gate: %d request(s) have arrived without a valid '
                'Cloudflare header (latest path=%s). Cloudflare is being '
                'bypassed, or the Transform Rule is not firing.',
                stats['count'], request.path,
            )

    # -- breaker ----------------------------------------------------------
    def _tick(self, ok):
        """Track the share of traffic arriving without the header, and if
        enforcement is on and almost NONE of it has one, drop back to observe.

        The failure this exists for: the Cloudflare Transform Rule is deleted,
        disabled, or its secret rotated on one side only. Enforcement then keys
        every genuine visitor to Railway's proxy address, so they all share one
        rate-limit bucket and the fourth caller of the hour is refused — with
        nothing broken, nothing logged, and no visible cause. Same shape as the
        daily budget breaker: stop automatically rather than quietly do harm.

        Counted in the PER-PROCESS cache, never the database. Counting every
        request in the shared cache would mean a query per request, which would
        hold Neon's compute awake permanently — the exact regression SiteConfig's
        cache exists to prevent. Each worker therefore evaluates its own window,
        which is fine: the state it writes on tripping IS shared.
        """
        try:
            cache = caches['local']
            now = time.time()
            w = cache.get(_BREAKER_KEY)
            if not w or (now - w['start']) > _BREAKER_WINDOW_S:
                if w:
                    self._evaluate(w)
                w = {'start': now, 'total': 0, 'missing': 0}
            w['total'] += 1
            if not ok:
                w['missing'] += 1
            cache.set(_BREAKER_KEY, w, _BREAKER_WINDOW_S * 3)
            if ok:
                # paint186: one request through Cloudflare proves the rule is
                # firing, so a run of refusals so far was not an outage.
                cache.delete(_BLOCK_STREAK_KEY)
        except Exception:
            # paint155: LOG IT. This swallowed a NameError on every request for
            # as long as the breaker has existed, and nothing anywhere said so —
            # a safety mechanism that cannot trip looks identical to one that
            # never needed to. Still swallowed, because a broken counter must
            # not break the site, but no longer silent.
            logger.exception('origin gate breaker tick failed')

    def _evaluate(self, w):
        from lookup.views import origin_gate_mode
        # Needs a real sample. On a quiet site three stray scanner hits must not
        # be read as "Cloudflare is broken".
        if w['total'] < _BREAKER_MIN_SAMPLE:
            return
        if w['missing'] / w['total'] < _BREAKER_THRESHOLD:
            return
        if origin_gate_mode() not in ('enforce', 'block'):
            return

        from lookup.models import SiteConfig
        cfg = SiteConfig.get()
        # FROM BLOCK TOO, and this is where it matters most: under enforce a
        # Transform Rule that stops firing skews rate limits, under block it
        # refuses every visitor. Reverting to observe restores service.
        if cfg.origin_gate_mode not in (SiteConfig.ORIGIN_GATE_ENFORCE,
                                        SiteConfig.ORIGIN_GATE_BLOCK):
            return
        self._revert(cfg, '%d of %d recent requests arrived without the '
                          'Cloudflare header' % (w['missing'], w['total']))

    def _note_refused(self):
        """paint186: count a refusal; under block, enough in a row reverts.

        Never raises. It runs on the refusal path, where an exception would be
        swallowed by the caller's fail-open handler and the request SERVED —
        so a broken counter must fail quietly and let the 403 stand.
        """
        try:
            cache = caches['local']
            streak = (cache.get(_BLOCK_STREAK_KEY) or 0) + 1
            if streak >= _BLOCK_TRIP_REFUSED_IN_A_ROW and self._trip_block(streak):
                cache.delete(_BLOCK_STREAK_KEY)
            else:
                cache.set(_BLOCK_STREAK_KEY, streak, _BLOCK_STREAK_TTL)
        except Exception:
            logger.exception('origin gate refusal streak failed')

    def _trip_block(self, streak):
        """Under BLOCK, act on a run of refusals, after checking why.

        paint187: it used to revert straight away, which meant anyone who knew
        the origin's address could switch block mode off with five requests.
        Now it asks, through Cloudflare, whether the tag arrives: if it does,
        the run was an attack and blocking stays on; if not, or if the check
        cannot tell, the rule is taken as broken and it reverts.

        Block only: under enforce nothing is refused, so this trigger has
        nothing to say. Returns True when the run was dealt with, so the caller
        clears the streak.
        """
        from lookup.views import origin_gate_mode
        if origin_gate_mode() != 'block':
            return False
        from lookup.models import SiteConfig
        cfg = SiteConfig.get()
        if cfg.origin_gate_mode != SiteConfig.ORIGIN_GATE_BLOCK:
            return False
        cache = caches['local']
        # Set BEFORE the check, so concurrent refusals on this worker cannot
        # each fire one of their own while the first is still waiting.
        if cache.get(_SELF_CHECK_COOLDOWN_KEY):
            return False
        cache.set(_SELF_CHECK_COOLDOWN_KEY, True, _SELF_CHECK_COOLDOWN_S)
        tagged = self._tag_arrives_through_cloudflare()
        if tagged is True:
            # The rule works, so these came from someone going round
            # Cloudflare. Switching off now would hand them exactly what they
            # were trying to get.
            logger.warning(
                'ORIGIN GATE HELD: %d requests in a row were refused, but a '
                'check through Cloudflare confirmed the tag is arriving, so they '
                'came from a direct connection. Blocking stays on.', streak)
            if not cache.get(_HELD_ALERT_KEY):
                cache.set(_HELD_ALERT_KEY, True, _HELD_ALERT_THROTTLE_S)
                self._alert('held', streak, tagged)
            return True
        # LEAVING block, so clear the cooldown: re-enabling block within ten
        # minutes would otherwise find it still set and hold, refusing every
        # customer, for up to that long.
        cache.delete(_SELF_CHECK_COOLDOWN_KEY)
        if tagged is False:
            why = ('%d requests in a row were refused, and a check through '
                   'Cloudflare found the tag is not arriving' % streak)
        else:
            why = ('%d requests in a row were refused, and a check through '
                   'Cloudflare could not complete' % streak)
        self._revert(cfg, why)
        self._alert('reverted', streak, tagged)
        return True

    def _tag_arrives_through_cloudflare(self):
        """Ask the site, through Cloudflare, whether Cloudflare's tag arrives.

        True   the tag arrived: the rule works.
        False  no tag: the rule is broken.
        None   could not tell: no URL, a timeout, an error, or an answer that
               was not the probe's. The caller treats it like False, because
               the costly mistake is staying blocked while customers are
               refused. That also makes an unreachable check fall back to the
               plain run-of-five rule rather than to never switching off.

        An attacker cannot fake the answer: going round Cloudflare is what they
        are doing, and they cannot stop Cloudflare adding the tag to a request
        that goes through it.
        """
        from django.conf import settings
        from lookup.services.http import get_session
        url = getattr(settings, 'ORIGIN_CHECK_URL', '') or ''
        if not url:
            return None
        try:
            # A unique query string as well as no-store on the answer, so no
            # cache between here and the origin can hand back an old verdict.
            resp = get_session().get(url, params={'t': int(time.time() * 1000)},
                                     timeout=_SELF_CHECK_TIMEOUT_S)
            if resp.status_code != 200:
                return None
            body = resp.json()
        except Exception:
            logger.warning('origin gate self-check could not complete',
                           exc_info=True)
            return None
        # Only an explicit boolean counts. A 200 from something else, such as a
        # Cloudflare error page or a maintenance page, is "could not tell", not
        # "the rule is broken".
        if not isinstance(body, dict) or not isinstance(body.get('tagged'), bool):
            return None
        return body['tagged']

    def _answer_origin_check(self, request):
        """Report whether THIS request carried Cloudflare's tag.

        No database. And never cached: a cached "tagged" served after the rule
        broke would tell the self-check all was well while block mode refused
        every customer, the outage this exists to end, now with a check
        vouching for it.
        """
        from lookup.views import via_cloudflare
        try:
            tagged = bool(via_cloudflare(request))
        except Exception:
            tagged = False
        resp = JsonResponse({'tagged': tagged})
        resp['Cache-Control'] = 'no-store, max-age=0'
        return resp

    def _alert(self, kind, streak, tagged):
        """Email the operator. Never raises, as a SECOND layer.

        What actually keeps a failing email from turning a refusal into a
        served request is _note_refused's own guard (paint186): it catches an
        exception from here before the caller's fail-open handler ever sees it.
        This guard changes nothing the customer sees, and a break test proved
        it — removing it left every refusal refused. It is kept for whatever
        calls this in future WITHOUT that outer guard, and because _safe_send
        builds its email client outside its own try, so it can raise before it
        gets to protect itself.
        """
        try:
            from lookup.services.email import send_admin_origin_gate_alert
            send_admin_origin_gate_alert(kind, streak, tagged)
        except Exception:
            logger.exception('origin gate alert could not be sent')

    def _revert(self, cfg, why):
        """Drop to observe. ONE copy, shared by both triggers, so the thing
        that actually restores the site cannot drift between them."""
        from lookup.models import SiteConfig
        cfg.origin_gate_mode = SiteConfig.ORIGIN_GATE_OBSERVE
        cfg.origin_gate_auto_reverted_at = timezone.now()
        cfg.save(update_fields=['origin_gate_mode',
                                'origin_gate_auto_reverted_at', 'updated_at'])
        logger.error(
            'ORIGIN GATE AUTO-REVERTED to observe: %s. Either the Transform Rule '
            'has stopped firing (check it) or the origin is being flooded '
            'directly. Enforcement is now OFF and must be re-enabled by hand.',
            why,
        )


# ---------------------------------------------------------------------------
# Junk-path short circuit
# ---------------------------------------------------------------------------

_JUNK_SUFFIXES = ('.php', '.asp', '.aspx', '.jsp', '.cgi', '.env', '.sql',
                  '.bak', '.old', '.swp', '.git', '.yml', '.ini', '.conf')
_JUNK_PREFIXES = ('/wp-', '/wordpress', '/vendor/', '/.git', '/.env',
                  '/phpmyadmin', '/cgi-bin', '/.aws', '/.ssh')

_JUNK_BODY = b'Not Found'


class JunkPathMiddleware:
    """Answer obvious scanner probes with nine bytes instead of 13,720.

    A webshell scanner walked the site on 18 Aug 2026 — /file.php, /dex.php,
    /wsomini.php, /wp_motu_4r80b.php, /.admin.php and some fifty more. Every one
    correctly 404d, but Django rendered templates/404.html, which extends
    base.html: the whole navigation, the stylesheet links, the Turnstile script.
    13,720 bytes to tell a bot a path does not exist, roughly 800KB in the four
    minutes that log covers.

    This site serves no PHP, no ASP and no dotfiles, so a request for one is
    never a customer who mistyped — it is always a probe. Genuine 404s (a real
    path typed wrong, an old link) still get the styled page, because those DO
    reach a person who benefits from a way back.

    Placed AFTER HealthCheckMiddleware and BEFORE the origin gate observer: the
    gate exists to count requests that skipped Cloudflare, and counting scanner
    noise there would bury the signal it is meant to surface.

    Returns 404, not 403 or 444: a scanner reading 403 learns the path is
    defended and therefore interesting. 404 says nothing at all.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = (request.path or '').lower()
        if path.endswith(_JUNK_SUFFIXES) or path.startswith(_JUNK_PREFIXES):
            return HttpResponse(_JUNK_BODY, status=404,
                                content_type='text/plain; charset=utf-8')
        return self.get_response(request)
