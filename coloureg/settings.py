from pathlib import Path
from django.contrib.messages import constants as messages_constants
import dj_database_url
import os

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

ALLOWED_HOSTS = ['127.0.0.1', 'localhost', '.herokuapp.com', '.coloureg.com', '.coloureg.co.uk']

# The Railway-assigned public URL (e.g. 'coloureg-production.up.railway.app').
# Allow the whole '.up.railway.app' suffix so direct hits to that URL are
# accepted. We can't rely on RAILWAY_PUBLIC_DOMAIN for this: once a custom
# domain (coloureg.com) is attached, Railway sets RAILWAY_PUBLIC_DOMAIN to the
# custom domain, so the original *.up.railway.app URL would otherwise fall out
# of ALLOWED_HOSTS and throw DisallowedHost (noise in Sentry, and the URL would
# 400). Harmless on Heroku (just an extra never-matched suffix there).
ALLOWED_HOSTS.append('.up.railway.app')

# Railway provides the service's current public domain in RAILWAY_PUBLIC_DOMAIN
# (the custom domain once attached, otherwise the *.up.railway.app URL). Append
# it too so whatever Railway considers canonical is always allowed. Harmless on
# Heroku (the var is simply absent there).
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
def RATELIMIT_IP_META_KEY(request):
    return (
        request.META.get('HTTP_CF_CONNECTING_IP')
        or request.META.get('REMOTE_ADDR', '')
    )

# CSRF_TRUSTED_ORIGINS: Django 4+ requires the request's Origin to be trusted
# for any POST over HTTPS (the reg-lookup submit, email submit, admin manual
# -lookup actions). Behind Railway's proxy on a new domain, POSTs would 403
# without this. We trust the real domains always, plus the Railway domain when
# present. Scheme is required in this setting (unlike ALLOWED_HOSTS).
CSRF_TRUSTED_ORIGINS = [
    'https://*.coloureg.com',
    'https://*.coloureg.co.uk',
    'https://*.herokuapp.com',
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

# Whitenoise compression for static files
STATICFILES_STORAGE = 'whitenoise.storage.CompressedManifestStaticFilesStorage'

# Email configuration (Resend)
RESEND_API_KEY = os.environ.get('RESEND_API_KEY', '')
DEFAULT_FROM_EMAIL = os.environ.get('DEFAULT_FROM_EMAIL', 'hello@coloureg.com')
ADMIN_EMAIL = os.environ.get('ADMIN_EMAIL', 'hello@coloureg.com')

CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.db.DatabaseCache',
        'LOCATION': 'rate_limit_cache',
    }
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