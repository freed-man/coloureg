"""
Paint fallback resolution.

When the initial VDG bundle call returns a vehicle but NO paint code, this
module tries to recover the paint code two ways IN PARALLEL and returns
whichever produces a code first:

  1. VDG bundle retry  -- an immediate second get_combined_lookup() call.
     Empirically this sometimes recovers paint that the first call missed
     (VDG's upstream paint source is intermittent/slow on the first hit).
     Fast (~a few seconds) and cheap-ish (~£0.15-0.50), so it's worth racing.

  2. pl24 scrape  -- call the pl24 service's /lookup-paint with the VIN we
     already got from the first VDG call. pl24 scrapes partslink24's catalogue,
     which carries codes VDG often doesn't (esp. commercial vehicles). Slower
     (~3-60s depending on routing) but a different data source, so it catches
     misses the VDG retry can't.

Design notes:
  - Both run on threads and we take the FIRST that returns a usable paint code.
    If the VDG retry hits, we use it immediately (faster, and the pl24 result
    is simply ignored when it lands). If the retry misses, we wait for pl24.
    If both miss, the caller falls through to the manual-lookup offer.
  - This function is SYNCHRONOUS and may block for up to ~PL24_TIMEOUT seconds.
    It is intended to be called from the background/status path (where the user
    is already looking at their vehicle data), NOT inline in the main lookup
    request.
  - Nothing here raises on a miss; failures degrade to "no paint found" so the
    caller always gets a clean result dict.
"""

import concurrent.futures
import os

import requests

from .vdg import get_combined_lookup, VdgError


# pl24 service base URL + auth. In production these point at the pl24 Railway
# service. PL24_BASE_URL can be the private address (http://pl24.railway.internal
# :PORT) once coloureg+pl24 are both on Railway, or the public URL for testing.
PL24_BASE_URL = os.environ.get(
    'PL24_BASE_URL', 'https://pl24-production.up.railway.app'
).rstrip('/')
PL24_API_KEY = os.environ.get('PL24_API_KEY', '')

# How long to wait on the pl24 scrape before giving up. pl24's own internal
# ceiling is ~60s (worst-case fallback chain); we allow a little more so a
# legitimately slow-but-successful scrape isn't cut off by our client. The
# VDG retry, being fast, effectively short-circuits this whenever it hits.
PL24_TIMEOUT = float(os.environ.get('PL24_CLIENT_TIMEOUT_S', '70'))


def _vdg_retry(registration):
    """Second VDG bundle call. Returns a paint dict if paint came back, else
    None. Never raises — VDG errors degrade to None (no recovery)."""
    try:
        data = get_combined_lookup(registration)
    except VdgError:
        return None
    if not data or not data.get('paint_returned'):
        return None
    return {
        'source': 'vdg_retry',
        'paint_code': data.get('paint_code', ''),
        'paint_description': data.get('paint_description', ''),
        'all_paint_codes': data.get('all_paint_codes', []),
        'balance': data.get('balance'),
    }


def _pl24_lookup(vin, make, category=None):
    """Call the pl24 service. Returns a paint dict if pl24 found a code, else
    None. Never raises — network/HTTP/timeout errors degrade to None."""
    if not vin or not make:
        return None
    params = {'vin': vin, 'make': make}
    if category:
        params['category'] = category
    headers = {'X-API-Key': PL24_API_KEY} if PL24_API_KEY else {}
    try:
        resp = requests.get(
            f'{PL24_BASE_URL}/lookup-paint',
            params=params, headers=headers, timeout=PL24_TIMEOUT,
        )
    except requests.exceptions.RequestException:
        return None
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    code = (data.get('paint_code') or '').strip()
    if not code:
        return None
    return {
        'source': 'pl24',
        'paint_code': code,
        'paint_description': data.get('paint_description', ''),
        # pl24 returns one code; keep the shape consistent with VDG's list form.
        'all_paint_codes': [{
            'code': code,
            'description': data.get('paint_description', ''),
        }],
        'via': data.get('via', ''),
    }


def resolve_paint(registration, vin, make, category=None):
    """Race the VDG bundle-retry and the pl24 scrape; return the first usable
    paint result, or None if neither recovers a code.

    Returns a dict on success:
        {'source': 'vdg_retry'|'pl24', 'paint_code', 'paint_description',
         'all_paint_codes', ...}
    or None if no paint could be recovered by either path.

    Both paths run concurrently. The VDG retry usually finishes first; if it
    yields paint we return immediately and let the pl24 future resolve and be
    discarded. If the retry misses, we wait for pl24. If both miss, None.
    """
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        f_vdg = ex.submit(_vdg_retry, registration)
        f_pl24 = ex.submit(_pl24_lookup, vin, make, category)
        futures = {f_vdg: 'vdg_retry', f_pl24: 'pl24'}

        pl24_result = None
        # Process whichever finishes first. Prefer the VDG retry when it hits
        # (faster path); fall back to pl24's result if the retry missed.
        for fut in concurrent.futures.as_completed(futures, timeout=PL24_TIMEOUT + 5):
            which = futures[fut]
            try:
                result = fut.result()
            except Exception:  # noqa: BLE001
                result = None
            if result is None:
                continue
            if which == 'vdg_retry':
                # VDG retry recovered paint — use it now; pl24 future (if still
                # running) is abandoned when the executor exits.
                return result
            else:
                # pl24 found paint. Hold it: if the VDG retry is still running
                # it might also hit and we'd prefer it, but as_completed gives us
                # whatever finished first. Since the retry is the faster path,
                # if pl24 finished first the retry has very likely already
                # missed (returned None above) — so just use pl24.
                pl24_result = result
                return pl24_result

    return None
