# coloureg — Django web app container for Railway.
#
# Mirrors the Heroku setup (gunicorn + whitenoise + dj-database-url) so nothing
# about how the app runs changes — only where it runs. The same Neon database
# is used via the DATABASE_URL env var (do NOT provision a Railway Postgres;
# copy the existing Neon connection string so all current data is retained).
#
# Migrations are NOT run here. On Heroku the Procfile `release:` phase ran them;
# the Railway equivalent is a Pre-Deploy Command on the service:
#     python manage.py migrate
# set that in the Railway service settings (Deploy -> Pre-Deploy Command), so
# migrations run once per deploy, separate from the web process — exactly like
# Heroku's release phase. Running them in the container start command instead
# would re-run on every restart and can race if scaled to >1 instance.

# Match runtime.txt (python-3.12.7).
FROM python:3.12-slim

# - PYTHONUNBUFFERED: logs stream to Railway live, unbuffered.
# - PYTHONDONTWRITEBYTECODE: no .pyc clutter in the image.
# - PORT: Railway overrides this at runtime; 8000 is the local default.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000

WORKDIR /app

# psycopg2-binary ships its own libpq, so no system postgres dev headers are
# needed; the slim image plus the binary wheel is enough. Install deps first
# for layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code.
COPY . .

# Collect static files at BUILD time so whitenoise can serve them at runtime.
# This needs Django importable but must NOT require the database or real
# secrets — collectstatic only touches the filesystem. We pass a throwaway
# SECRET_KEY and DEVELOPMENT=False so settings imports cleanly; STATIC_ROOT
# (BASE_DIR/staticfiles) is populated and baked into the image. The real
# SECRET_KEY/DATABASE_URL are injected at runtime by Railway and are not
# needed for this step.
RUN SECRET_KEY=build-time-only-not-used DEVELOPMENT=False \
    python manage.py collectstatic --noinput

EXPOSE 8000

# Run gunicorn, binding Railway's $PORT. Shell form so $PORT expands.
# Bind to [::] (IPv6 dual-stack) for Railway's internal network — see the
# healthcheck notes above. --timeout 90: the /lookup-status endpoint runs the
# paint recovery (parallel VDG-retry + pl24) synchronously, which can take up to
# ~65s on a slow/commercial vehicle. Gunicorn's DEFAULT worker timeout is 30s, so
# without this it kills the worker mid-request (SystemExit / worker abort) before
# the recovery finishes. 90s sits comfortably above the ~65s ceiling. (Railway
# itself has no 30s router limit — this is purely gunicorn's own worker timeout,
# which is why it bit us even after leaving Heroku.) Tune workers via the
# WEB_CONCURRENCY env var if needed (gunicorn reads it automatically).
CMD ["sh", "-c", "gunicorn coloureg.wsgi --bind [::]:${PORT} --workers ${WEB_CONCURRENCY:-2} --timeout 90 --access-logfile - --error-logfile -"]