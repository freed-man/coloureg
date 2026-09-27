"""Where the AI car pictures are kept: Cloudflare R2 (paint218).

R2 speaks Amazon S3's API, so boto3 does the talking. Pictures are public
through the bucket's custom domain (R2_PUBLIC_URL, e.g. https://img.coloureg.com)
under random names, never a registration. The settings live in Railway's
variables and, locally, in env.py:

    R2_ENDPOINT            https://<account id>.r2.cloudflarestorage.com
    R2_ACCESS_KEY_ID       from the bucket-scoped Object Read & Write token
    R2_SECRET_ACCESS_KEY
    R2_BUCKET              coloureg-cars
    R2_PUBLIC_URL          https://img.coloureg.com

boto3 keeps its own connections and retries (at most 3 tries here); an upload
is idempotent under its random name, so a retry cannot double anything, and
R2's first million writes a month are free.
"""
import os

SETTINGS = ('R2_ENDPOINT', 'R2_ACCESS_KEY_ID', 'R2_SECRET_ACCESS_KEY', 'R2_BUCKET', 'R2_PUBLIC_URL')
# A picture's name never changes, so browsers and Cloudflare may keep it for a year.
CACHE_CONTROL = 'public, max-age=31536000, immutable'


def setting(name):
    return os.environ.get(name, '').strip()


def configured():
    """True when every R2 setting is present."""
    return all(setting(n) for n in SETTINGS)


def _client():
    import boto3
    from botocore.config import Config
    return boto3.client(
        's3', endpoint_url=setting('R2_ENDPOINT'), region_name='auto',
        aws_access_key_id=setting('R2_ACCESS_KEY_ID'),
        aws_secret_access_key=setting('R2_SECRET_ACCESS_KEY'),
        config=Config(retries={'max_attempts': 3, 'mode': 'standard'},
                      connect_timeout=10, read_timeout=60))


def upload(key, data, content_type):
    """Store `data` under `key`. Raises on failure; the caller records why."""
    _client().put_object(Bucket=setting('R2_BUCKET'), Key=key, Body=data,
                         ContentType=content_type, CacheControl=CACHE_CONTROL)


def delete(key):
    _client().delete_object(Bucket=setting('R2_BUCKET'), Key=key)


def public_url(key):
    return f"{setting('R2_PUBLIC_URL').rstrip('/')}/{key}" if key else ''
