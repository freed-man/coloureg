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
  - Both run on threads and we take the FIRST that returns a usable paint code,
    with a deterministic preference for the VDG retry when both produce a code
    (it's cheaper — already paid for — and faster). See resolve_paint for how
    the preference is enforced even when both finish in the same wait() batch.
  - This function is SYNCHRONOUS and may block for up to ~PL24_TIMEOUT seconds.
    It is intended to be called from the background/status path (where the user
    is already looking at their vehicle data), NOT inline in the main lookup
    request.
  - Nothing here raises on a miss; failures degrade to "no paint found" so the
    caller always gets a clean result dict (or None).
"""

import concurrent.futures
import os
import time

import requests

from .vdg import get_combined_lookup, VdgError


# pl24 service base URL + auth. In production these point at the pl24 Railway
# service. PL24_BASE_URL can be the private address (http://pl24.railway.internal
# :PORT) once coloureg+pl24 are both on Railway, or the public URL for testing.
PL24_BASE_URL = os.environ.get(
    'PL24_BASE_URL', 'https://pl24-production.up.railway.app'
).rstrip('/')
PL24_API_KEY = os.environ.get('PL24_API_KEY', '')

# How long resolve_paint waits for pl24 before giving up. Set just ABOVE pl24's
# own internal ceiling (~60s worst case, when it walks the full fallback chain:
# catalog -> commercial sibling -> Classic sibling -> dashboard). Matching it
# this way means pl24 always gets to finish its own attempt before we abandon
# it — capping lower would truncate exactly the slow commercial-vehicle lookups
# pl24 exists to rescue, throwing away codes it would have found seconds later.
# The long wait is acceptable because resolve_paint runs in the background while
# the user already sees their vehicle data; the results page communicates the
# wait ("checking manufacturer database...") rather than appearing frozen.
PL24_TIMEOUT = float(os.environ.get('PL24_CLIENT_TIMEOUT_S', '65'))

# requests timeout as (connect, read): cap connection setup tightly (the pl24
# service is on the same platform/region, so a slow connect means trouble), and
# allow the read to run up to the overall budget for the scrape itself.
_PL24_HTTP_TIMEOUT = (5.0, PL24_TIMEOUT)


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
            params=params, headers=headers, timeout=_PL24_HTTP_TIMEOUT,
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

    Preference: when BOTH paths produce a code, the VDG-retry result wins (it's
    cheaper and already paid for). This holds even if both futures complete in
    the same wait() batch — we explicitly inspect the VDG future before the pl24
    future within a batch, rather than relying on set-iteration order.

    Timeout: the total wait is hard-bounded by PL24_TIMEOUT. We deliberately do
    NOT use the ThreadPoolExecutor as a context manager, because its __exit__
    blocks until all worker threads finish — which would let a slow/hung pl24
    thread make this function hang far past the timeout. Instead we wait on the
    futures with an explicit deadline, then shut the executor down WITHOUT
    waiting (wait=False, cancel_futures=True), abandoning any straggler. The
    abandoned pl24 thread's HTTP request has its own timeout and ends on its own.
    """
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=2)
    try:
        f_vdg = ex.submit(_vdg_retry, registration)
        f_pl24 = ex.submit(_pl24_lookup, vin, make, category)

        deadline = time.monotonic() + PL24_TIMEOUT
        pending = {f_vdg, f_pl24}
        pl24_result = None

        def _result_or_none(fut):
            try:
                return fut.result()
            except Exception:  # noqa: BLE001  (any worker failure -> no paint)
                return None

        while pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break  # deadline hit — stop waiting, abandon stragglers
            done, pending = concurrent.futures.wait(
                pending, timeout=remaining,
                return_when=concurrent.futures.FIRST_COMPLETED,
            )
            if not done:
                break  # wait timed out with nothing newly completed

            # Enforce the VDG-over-pl24 preference within this batch: if the VDG
            # future is among the just-completed ones and produced paint, that
            # wins outright — regardless of whether pl24 also completed here.
            if f_vdg in done:
                vdg_result = _result_or_none(f_vdg)
                if vdg_result is not None:
                    return vdg_result

            # VDG didn't (yet) yield paint. If pl24 completed in this batch with
            # a code, hold it; we return it below unless a later batch produces
            # a VDG hit first (it won't, since VDG either completed here without
            # paint or is still pending and is the faster path anyway).
            if f_pl24 in done:
                p = _result_or_none(f_pl24)
                if p is not None:
                    pl24_result = p

            if pl24_result is not None:
                return pl24_result

        return pl24_result  # None unless pl24 produced a code before the deadline
    finally:
        # Do NOT block on stragglers. wait=False means we don't join running
        # threads; cancel_futures cancels any not-yet-started work. A pl24 thread
        # still mid-request is abandoned and ends when its own HTTP timeout fires.
        ex.shutdown(wait=False, cancel_futures=True)