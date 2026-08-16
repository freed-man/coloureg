from pathlib import Path
from django.contrib.messages import constants as messages_constants
from django.core.exceptions import ImproperlyConfigured
import dj_database_url
import os
import sys

if os.path.isfile('env.py'):
    import env  # noqa

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

# Quick-start development settings - unsuitable for production
# See https://docs.djangoproject.com/en/6.0/howto/deployment/checklist/

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = os.environ.get('SECRET_KEY')

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = os.environ.get('DEVELOPMENT', '') == 'True'

ALLOWED_HOSTS = ['127.0.0.1', 'localhost', '.coloureg.com', '.coloureg.co.uk']

# REMOVED (F3): ALLOWED_HOSTS.append('.up.railway.app').
#
# The wildcard was here to stop DisallowedHost noise from direct hits to the
# Railway-assigned URL. Measured 16 Aug 2026: there is no such URL. Public
# Networking lists only coloureg.com and www.coloureg.com, and both CNAME
# targets return 404 with 'x-railway-fallback: true' when hit by name — Railway
# has no service registered under either, so nothing can arrive with a
# .up.railway.app Host. The wildcard was accepting a suffix that only ever
# resolves to other people's services.
#
# Nothing breaks if a Railway domain is generated later: the RAILWAY_PUBLIC_DOMAIN
# append immediately below covers whatever Railway considers canonical.

# Railway provides the service's current public domain in RAILWAY_PUBLIC_DOMAIN
# (the custom domain once attached, otherwise the *.up.railway.app URL). Append
# it too so whatever Railway considers canonical is always allowed. Absent
# outside Railway, in which case this is a no-op.
_railway_domain = os.environ.get('RAILWAY_PUBLIC_DOMAIN', '').strip()
if _railway_domain:
    ALLOWED_HOSTS.append(_railway_domain)

# Private service-to-service traffic uses the *.railway.internal domain (this is
# also what coloureg uses to call pl24 privately). Allow it. The healthcheck
# probe's own (internal) Host header is handled separately by
# HealthCheckMiddleware, which answers /health/ before host validation runs, so
# we don't need to enumerate the probe's host here.
ALLOWED_HOSTS.append('.railway.internal')
_railway_private = os.environ.get('RAILWAY_PRIVATE_DOMAIN', '').strip()
if _railway_private:
    ALLOWED_HOSTS.append(_railway_private)

# Rate limiting (django-ratelimit) — resolve the REAL visitor IP, not Cloudflare's.
# All traffic is Cloudflare-proxied, so request.META['REMOTE_ADDR'] (the library's
# default for key='ip') is a Cloudflare EDGE node IP, shared across many unrelated
# visitors. Keying limits on that would let strangers share one counter (innocent
# users blocked by others' activity) and weaken protection. CF-Connecting-IP is the
# single real client IP Cloudflare sets (unspoofable when proxied), matching what
# get_client_ip() uses for logging — so the limits and the logs agree on "who".
# Falls back to REMOTE_ADDR if the header is somehow absent (direct origin hit).
# NOTE (paint19): every candidate is VALIDATED before it is returned. This used
# to hand the raw header straight to django-ratelimit, which feeds it into
# ipaddress.ip_network(f'{ip}/{mask}') — that raises ValueError on anything that
# is not an address, so a junk CF-Connecting-IP produced an unhandled 500 on
# submit_email and submit_contact rather than a rate-limit decision. Not just an
# attacker's tool: a comma-separated value like '1.2.3.4, 5.6.7.8', which a
# proxy change could legitimately produce, crashed it too.
#
# paint17 added exactly this guard to views.get_client_ip() and missed the
# sibling here, which is why the comment below claimed the two agreed when they
# no longer did. They agree again now.
def RATELIMIT_IP_META_KEY(request):
    from django.core.exceptions import ValidationError
    from django.core.validators import validate_ipv46_address

    for candidate in (
        request.META.get('HTTP_CF_CONNECTING_IP'),
        request.META.get('REMOTE_ADDR'),
    ):
        candidate = (candidate or '').strip()
        if not candidate:
            continue
        try:
            validate_ipv46_address(candidate)
        except ValidationError:
            continue
        return candidate
    # Nothing usable. Return a constant rather than '' so callers still share a
    # single bucket instead of django-ratelimit seeing an empty key — anyone
    # arriving without a resolvable address is limited together, which is the
    # safe direction.
    return '0.0.0.0'

# CSRF_TRUSTED_ORIGINS: Django 4+ requires the request's Origin to be trusted
# for any POST over HTTPS (the reg-lookup submit, email submit, admin manual
# -lookup actions). Behind Railway's proxy on a new domain, POSTs would 403
# without this. We trust the real domains always, plus the Railway domain when
# present. Scheme is required in this setting (unlike ALLOWED_HOSTS).
# NOTE (paint17): all Heroku entries removed, from here AND from ALLOWED_HOSTS
# above. They were residue from the original Heroku deploy that this project
# migrated off; nothing runs there. The CSRF one mattered more than the hosts
# one: 'https://*.herokuapp.com' trusted the Origin of EVERY app on a shared
# third-party domain for state-changing POSTs here. The CSRF token check still
# stood behind it so it was not a live hole, but a wildcard over a domain
# anyone can deploy to has no business in this list.
CSRF_TRUSTED_ORIGINS = [
    'https://*.coloureg.com',
    'https://*.coloureg.co.uk',
]
if _railway_domain:
    CSRF_TRUSTED_ORIGINS.append(f'https://{_railway_domain}')

# Application definition

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django.contrib.sites',
    'django.contrib.sitemaps',
    'lookup',
]

MIDDLEWARE = [
    # First: answer /health/ before host validation / SSL redirect run, so
    # Railway's internal healthcheck probe (which uses an internal Host header
    # not in ALLOWED_HOSTS) gets a clean 200 instead of a 400 DisallowedHost.
    'lookup.middleware.HealthCheckMiddleware',
    # Counts requests arriving without Cloudflare's Transform Rule header (F2).
    # After the healthcheck short-circuit, so Railway's internal probe is never
    # miscounted as a direct hit. Observation only — see the middleware.
    'lookup.middleware.OriginGateObserverMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'coloureg.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'lookup.context_processors.analytics',
            ],
        },
    },
]

WSGI_APPLICATION = 'coloureg.wsgi.application'

# Database
# https://docs.djangoproject.com/en/6.0/ref/settings/#databases
#
# Neon's free tier auto-suspends inactive databases after ~5 minutes,
# which kills any pooled connection from Django's side. Two settings
# work together to handle this gracefully:
#   conn_max_age=200      — recycle connections after ~3.3 min, comfortably
#                           below Neon's ~5-min idle suspend window
#   conn_health_checks    — run a fast SELECT 1 before each query; if the
#                           connection is dead, transparently open a new
#                           one instead of crashing with an SSL error

DATABASES = {
    'default': dj_database_url.config(
        default=f'sqlite:///{BASE_DIR}/db.sqlite3',
        conn_max_age=200,
        conn_health_checks=True,
    )
}

# FAIL FAST (F14). The sqlite default above is what makes a fresh clone work
# without configuration, but in production it is a trap: an unset or misspelled
# DATABASE_URL would start the site perfectly happily against an empty local
# sqlite file inside an ephemeral container. Every lookup would succeed, every
# row would be written, and all of it would vanish on the next deploy — with no
# error anywhere to say so. Refusing to boot is enormously preferable to
# silently serving from the wrong database.
#
# EXCEPT under collectstatic. The Dockerfile runs it at BUILD time with
# DEVELOPMENT=False and deliberately no DATABASE_URL, because the step only
# writes to the filesystem — that contract is stated in the Dockerfile itself.
# The first version of this check did not exempt it and failed the Railway
# build. Keyed on sys.argv rather than an opt-out env var on purpose: gunicorn's
# argv can never contain 'collectstatic', so the guard cannot be switched off in
# production by setting the wrong variable.
_BUILD_ONLY = len(sys.argv) > 1 and sys.argv[1] == 'collectstatic'
if not DEBUG and not _BUILD_ONLY and not os.environ.get('DATABASE_URL'):
    raise ImproperlyConfigured(
        'DATABASE_URL is not set and DEBUG is off. Refusing to start against '
        'the sqlite fallback — set DATABASE_URL, or set DEVELOPMENT=True if '
        'this really is a development machine.'
    )

# Password validation
# https://docs.djangoproject.com/en/6.0/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]


# Internationalization
# https://docs.djangoproject.com/en/6.0/topics/i18n/

LANGUAGE_CODE = 'en-us'

TIME_ZONE = 'Europe/London'

USE_I18N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/6.0/howto/static-files/

STATIC_URL = 'static/'
STATICFILES_DIRS = [BASE_DIR / 'static']
STATIC_ROOT = BASE_DIR / 'staticfiles'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Production security settings
if not DEBUG:
    SECURE_SSL_REDIRECT = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

# Google Analytics 4 — Measurement ID is set via environment variable.
# Leave unset (or empty) to disable GA on a given environment.
# When DEBUG=True (local development), GA is suppressed regardless of this value.
GA_MEASUREMENT_ID = os.environ.get('GA_MEASUREMENT_ID', '')

# Whitenoise compression + cache-busting manifest for static files.
# Django 5.1 removed the old STATICFILES_STORAGE setting, so it must be
# configured via STORAGES (the 'default' entry keeps Django's normal file
# storage; only 'staticfiles' is overridden).
STORAGES = {
    'default': {
        'BACKEND': 'django.core.files.storage.FileSystemStorage',
    },
    'staticfiles': {
        'BACKEND': 'whitenoise.storage.CompressedManifestStaticFilesStorage',
    },
}

# Email configuration (Resend)
RESEND_API_KEY = os.environ.get('RESEND_API_KEY', '')
DEFAULT_FROM_EMAIL = os.environ.get('DEFAULT_FROM_EMAIL', 'hello@coloureg.com')
ADMIN_EMAIL = os.environ.get('ADMIN_EMAIL', 'hello@coloureg.com')

# Upload ceiling. Manual-lookup photos are validated and capped at 10 MB in
# lookup/services/uploads.py (MAX_UPLOAD_BYTES — this comment said 8 MB and was
# wrong); Django rejects the request outright if it exceeds this first, so it
# must be at least as large. Nothing is written to disk —
# files are held in memory for the request and attached to the outgoing email.
DATA_UPLOAD_MAX_MEMORY_SIZE = 12 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 12 * 1024 * 1024

# --- Cloudflare Turnstile (E, paint15) --------------------------------------
# Invisible CAPTCHA embedded in the lookup form, verified server-side BEFORE
# any spend (Stripe or VDG). Both keys come from the Cloudflare dashboard
# (Turnstile -> Add site). SAFE-BY-DEFAULT: when either key is unset, the
# widget doesn't render and verification is skipped — so this deploy changes
# nothing until the keys are added to Railway. Once they're set, scripts that
# POST without a valid token are rejected for free.
TURNSTILE_SITE_KEY = os.environ.get('TURNSTILE_SITE_KEY', '')
TURNSTILE_SECRET_KEY = os.environ.get('TURNSTILE_SECRET_KEY', '')
# Optional hostname pinning (paint17). Cloudflare's siteverify response reports
# the hostname the token was issued for; when this list is non-empty,
# verify_turnstile() rejects tokens from anything else. Comma-separated, e.g.
# 'coloureg.com,www.coloureg.com'. Left EMPTY by default and therefore inert —
# an incorrect list would reject every lookup, so populate it deliberately, not
# as part of a deploy that changes other things.
TURNSTILE_ALLOWED_HOSTNAMES = [
    h.strip() for h in os.environ.get('TURNSTILE_ALLOWED_HOSTNAMES', '').split(',')
    if h.strip()
]

# --- Stripe (F, paint15) ------------------------------------------------------
# Payment scaffolding for the paid-lookup flow (see LOOKUP_PRICE_PENCE below). Fully built and testable
# against Stripe TEST keys, but gated twice: these env vars must be set AND
# SiteConfig.payments_enabled must be flipped in /admin-stats/ (defaults False).
# Until both are true the site behaves exactly as today (free lookups).
# STRIPE_WEBHOOK_SECRET comes from the webhook endpoint you register in the
# Stripe dashboard (use the Railway hostname directly, not the Cloudflare-
# proxied domain, so bot protection can never challenge Stripe's POSTs).
STRIPE_PUBLISHABLE_KEY = os.environ.get('STRIPE_PUBLISHABLE_KEY', '')
STRIPE_SECRET_KEY = os.environ.get('STRIPE_SECRET_KEY', '')
STRIPE_WEBHOOK_SECRET = os.environ.get('STRIPE_WEBHOOK_SECRET', '')
# Price per lookup in pence (GBP). 100 = £1.00, so the 200 default is £2.00 —
# which is the live price. Neighbouring comments used to describe a "£1-per-
# lookup flow"; that was stale and is corrected here and in payments.py.
LOOKUP_PRICE_PENCE = int(os.environ.get('LOOKUP_PRICE_PENCE', '200'))

CACHES = {
    # DEFAULT stays the database cache. django-ratelimit reads/writes here, and
    # the rate limit MUST be shared across all gunicorn workers — the database is
    # the only store every worker sees. Do not point the default at locmem, or
    # each worker would count requests separately and the 3/h limit would leak.
    'default': {
        'BACKEND': 'django.core.cache.backends.db.DatabaseCache',
        'LOCATION': 'rate_limit_cache',
    },
    # LOCAL is a per-process in-memory cache. It is intentionally NOT shared
    # between workers — each worker keeps its own copy. That is fine (and
    # desirable) for small, read-mostly values where a few seconds of
    # per-worker staleness is acceptable and we want ZERO database round-trips.
    # Its first use is SiteConfig.get() (see models.py): caching the singleton
    # config row here means a homepage GET makes no DB query at all, so Neon's
    # compute can actually reach its 5-minute idle threshold and suspend. Thread-
    # safe (LocMemCache is), so it's safe under gunicorn's threaded workers.
    'local': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'coloureg-local',
        'TIMEOUT': 60,  # default TTL; SiteConfig.get() sets its own explicitly
    },
}

MESSAGE_TAGS = {
    messages_constants.ERROR: 'alert-danger',
    messages_constants.SUCCESS: 'alert-success',
    messages_constants.WARNING: 'alert-warning',
    messages_constants.INFO: 'alert-info',
}

SITE_ID = 1

# ---------------------------------------------------------------------------
# Sentry error tracking
# ---------------------------------------------------------------------------
# Catches unhandled exceptions in production and sends them to sentry.io for
# triage. Only initialised when DEBUG=False AND the SENTRY_DSN env var is set,
# so local development errors stay local.
# --- Logging ------------------------------------------------------------------
# Django's default config attaches a console handler ONLY to the `django` logger,
# and only when DEBUG is on — so in production our own module loggers fall
# through to Python's lastResort handler, which emits WARNING and above and
# drops INFO entirely. That is fine for errors and useless for diagnostics.
#
# This attaches the `lookup` package to stdout at INFO so the per-call VDG
# billing line (services/vdg.py) is visible in Railway logs. There are no other
# INFO callers in the package, so this adds one line per VDG call and nothing
# else. Level is env-controlled: set LOG_LEVEL=WARNING to silence it again once
# the billing ledger question is settled, without a deploy.
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'simple': {'format': '[%(levelname)s] %(name)s: %(message)s'},
    },
    'handlers': {
        'stdout': {
            'class': 'logging.StreamHandler',
            'stream': sys.stdout,
            'formatter': 'simple',
        },
    },
    'loggers': {
        'lookup': {
            'handlers': ['stdout'],
            'level': os.environ.get('LOG_LEVEL', 'INFO'),
            'propagate': False,
        },
    },
}

SENTRY_DSN = os.environ.get('SENTRY_DSN', '')

if not DEBUG and SENTRY_DSN:
    import sentry_sdk

    def _before_send(event, hint):
        """Filter out events we don't want to log to Sentry.

        - 404 errors: noisy bots scanning for /wp-admin/, /.env, etc.
        - Admin path crashes: usually the developer breaking something on
          purpose, not a real production error.
        """
        # Drop events for paths under /admin/ or /admin-stats/
        request = event.get('request') or {}
        url = request.get('url') or ''
        if '/admin/' in url or '/admin-stats/' in url:
            return None

        # Drop 404s — Django raises Http404 which becomes a logger event
        exc_info = hint.get('exc_info')
        if exc_info:
            exc_type = exc_info[0]
            if exc_type is not None:
                # Match Http404 by name to avoid importing django.http here
                if exc_type.__name__ == 'Http404':
                    return None

        return event

    sentry_sdk.init(
        dsn=SENTRY_DSN,
        # Don't capture IPs, request headers, or user info — keeps the data
        # we send to Sentry minimal and consistent with our privacy notice.
        send_default_pii=False,
        # No performance tracing for now (saves event quota; can enable later)
        traces_sample_rate=0.0,
        # Tag events so multiple deploys can be told apart in Sentry's UI
        environment='production',
        # Filter out noise (404s, admin path crashes)
        before_send=_before_send,
    )