"""Image upload handling for manual lookups (paint16).

Design decision: uploads are NEVER persisted server-side. A photo arrives in the
POST, is validated here, base64-encoded, attached to the outgoing email, and
discarded when the request ends. That avoids:

  * Railway's ephemeral filesystem (anything written vanishes on redeploy),
  * paying Neon per GB to store binaries in Postgres (an antipattern that bloats
    backups and slows every query on the table),
  * a GDPR retention question — a V5C or paint-label photo is personal data, and
    the cleanest retention policy is "we don't retain it".

The photo ends up in the inbox of whoever needs it: ours for a customer request,
theirs (plus our BCC copy) for a reply. That is the same place it would have
ended up anyway, with no storage layer in between.

Validation is by MAGIC BYTES, not filename extension: an attacker can name a
file anything, so `.jpg` proves nothing. We only accept real image formats.
"""

import base64

# Cap per file. Resend accepts up to 40 MB per email AFTER Base64 encoding, so
# 10 MB (which encodes to ~13.5 MB) is comfortably inside their limit.
#
# The binding constraint is the RECIPIENT, not Resend: Gmail caps at 25 MB,
# Outlook and Apple Mail at 20 MB, and the smallest limit in the chain wins. A
# ~13.5 MB message clears all of those, but some corporate gateways cap incoming
# mail at 10 MB — so a trade customer on a locked-down Exchange server could
# still bounce. That is the trade-off of allowing 10 MB rather than something
# smaller; in practice a legible photo of a paint label is under 1 MB, so the
# cap is headroom for people who don't resize, not a target.
#
# DATA_UPLOAD_MAX_MEMORY_SIZE in settings must be at least this large, or Django
# rejects the request before this validator ever runs.
MAX_UPLOAD_BYTES = 10 * 1024 * 1024

# Magic-byte signatures for the formats we accept. Keys are the leading bytes;
# values are the MIME type reported to the email client.
#   JPEG  FF D8 FF
#   PNG   89 50 4E 47 0D 0A 1A 0A
#   GIF   47 49 46 38
#   WEBP  RIFF....WEBP  (checked specially — bytes 8-12)
#   HEIC  ....ftypheic / ftypheix / ftypmif1 (iPhone default; checked specially)
_SIGNATURES = [
    (b'\xff\xd8\xff', 'image/jpeg', 'jpg'),
    (b'\x89PNG\r\n\x1a\n', 'image/png', 'png'),
    (b'GIF8', 'image/gif', 'gif'),
]


def _sniff(head):
    """Identify an image from its leading bytes. Returns (mime, ext) or None."""
    for sig, mime, ext in _SIGNATURES:
        if head.startswith(sig):
            return mime, ext
    # WEBP: 'RIFF' at 0, 'WEBP' at 8
    if head[:4] == b'RIFF' and head[8:12] == b'WEBP':
        return 'image/webp', 'webp'
    # HEIC/HEIF (what an iPhone produces unless Safari transcodes on upload):
    # box header at offset 4 is 'ftyp', brand follows.
    if head[4:8] == b'ftyp':
        brand = head[8:12]
        if brand in (b'heic', b'heix', b'hevc', b'mif1', b'msf1'):
            return 'image/heic', 'heic'
    return None


def process_image_upload(uploaded_file, filename_prefix='photo'):
    """Validate an uploaded image and return a Resend attachment dict, or None.

    Returns None (rather than raising) for anything not acceptable — a missing
    file, an oversized one, or something that isn't actually an image. The
    caller treats None as "no photo" and carries on: a bad upload must never
    break the manual-lookup request itself, because the request is the thing
    that matters and the photo is a bonus.

    Returns:
        {"filename": ..., "content": <base64 str>, "content_type": ...}
    """
    if not uploaded_file:
        return None

    size = getattr(uploaded_file, 'size', None)
    if size is not None and size > MAX_UPLOAD_BYTES:
        return None

    try:
        uploaded_file.seek(0)
        data = uploaded_file.read(MAX_UPLOAD_BYTES + 1)
    except Exception:
        return None

    # Re-check after reading, in case `size` was absent or lied.
    if not data or len(data) > MAX_UPLOAD_BYTES:
        return None

    sniffed = _sniff(data[:16])
    if not sniffed:
        return None  # not a real image, whatever it claims to be
    mime, ext = sniffed

    try:
        content = base64.b64encode(data).decode('ascii')
    except Exception:
        return None

    # Deliberately ignore the client-supplied filename: it is untrusted and can
    # carry path separators or misleading extensions. Build our own from the
    # sniffed type.
    return {
        'filename': f'{filename_prefix}.{ext}',
        'content': content,
        'content_type': mime,
    }
