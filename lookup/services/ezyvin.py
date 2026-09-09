"""Ezyvin build sheet — the reserve leg.

paint95. The last source asked, and only when VDG paint and pl24 have both
finished with nothing. Measured 8 Sep against 18 recorded FAILURES — lookups
where the live pipeline returned no code at all:

    15 answered, 3 returned 404 free, 0 charged-and-empty.  75 credits, £8.25.

That is the population this leg exists for, and it is the reason the leg is
worth having. An earlier test on 22 vehicles One Auto had ALREADY resolved was
reassuring and close to meaningless: it measured coverage on cars a supplier
demonstrably held data for, not on the ones that reach a reserve.

WHY IT IS NOT IN THE RACE. Ezyvin charges 5 credits for any 200, including one
carrying no colour. pl24 can afford to start on a drop-out because it is free;
this cannot. In 44% of deliveries one leg comes back empty while the other
still delivers — firing on a single drop-out would spend ~£188/month on answers
that were already arriving. So the trigger is BOTH legs finished with no code.

COSTS ARE A MEASURED CONSTANT, NOT A METERED READING. sources.py brackets each
call with /me/usage before and after, which is right for a diagnostic and wrong
here: it triples the HTTP calls on the slowest leg for a number that does not
change. It also misreports — a Tesla read 30 credits once and 5 on every
subsequent run, because anything else touching the balance in that window lands
in the delta.
"""

import logging
import os
import re
import time

from .http import get_session

logger = logging.getLogger(__name__)

BASE_URL = os.environ.get('EZYVIN_BASE_URL', 'https://ezyvin.com')
TOKEN = os.environ.get('EZYVIN_TOKEN', '')
USER_AGENT = os.environ.get('EZYVIN_UA', 'coloureg/1.0 (+https://coloureg.com)')

#: Measured across 40 build sheets on 8 Sep: every 200 cost 5, every 404 cost 0.
#: Tesla included — an early reading of 30 for a Tesla did not reproduce.
CREDITS_PER_HIT = 5
CREDITS_PER_MISS = 0

#: Wall-clock ceiling for the whole leg. Measured: a HIT lands in ~2.2s and a
#: MISS takes ~9s, because a 404 is decided after the job runs rather than at
#: submission. 20s is roughly double the slowest observed miss — generous
#: enough to absorb a slow day, tight enough that the leg cannot own the
#: 60s race deadline on its own.
TOTAL_BUDGET_S = float(os.environ.get('EZYVIN_BUDGET_S', '20'))
POLL_GAP_S = float(os.environ.get('EZYVIN_POLL_GAP_S', '0.7'))
_HTTP_TIMEOUT_S = 15

#: A code parked in brackets at the end of the exterior string:
#: "Imola yellow (1T)", "Cyber Orange 3c (Pn4jf)", "Steel Grey (279)".
#:
#: The leading character may be an UNDERSCORE. Jaguar returned '(_PJABZ)' on
#: Y607AOG — a field separator that leaked into the value. Requiring the first
#: character to be alphanumeric made that whole match fail, so the code was
#: dropped and only the name survived. _clean_code trims the underscore after.
_RE_PAREN = re.compile(r'\(([A-Z0-9_][A-Z0-9/_\- ]{0,14})\)\s*$', re.I)
#: A label prefixed onto the field rather than a colour: Land Rover sends
#: "Exterior Paint - Indus Silver (1AC)" and Peugeot "PAINT DARK GREY MICA 1E0".
#:
#: The separator is OPTIONAL because Peugeot omits it, and at least one word
#: must follow — so a colour genuinely called "Paint" on its own survives
#: rather than being stripped to nothing.
_RE_LABEL = re.compile(
    r'^\s*(?:exterior\s*paint|exterior|paint)\s*[-:]?\s+(?=\S)', re.I)
#: Ezyvin's null. /vehicle/ returned a placeholder row of dashes on an MX-5;
#: reading the dash as a colour reports a miss as a hit.
_NULLISH = {'', '-', '--', 'n/a', 'na', 'null', 'none'}


def _clean_code(code):
    """Trim a code to what a paint counter would recognise.

    Jaguar returned '_PJABZ' with a leading underscore on Y607AOG (8 Sep) —
    a field separator that leaked into the value. Strip the padding, keep the
    inside: 'Z2Z2/H5X' must survive intact because the slash is meaningful.
    """
    return (code or '').strip().strip('_-/ ').upper()


def _walk_exterior(node, depth=0):
    """Find `exterior` at any depth, case-insensitively.

    It sits at three depths depending on endpoint, and the CASE DIFFERS from
    the spec — documented as `buildSheet`, returned as `BuildSheet`. Matching
    the spec exactly reported a perfectly good "734" as no result on a Fiat
    Panda (20 Aug), so this walks rather than indexes.
    """
    if depth > 6 or not isinstance(node, dict):
        return ''
    for key, val in node.items():
        if key.lower() == 'exterior' and isinstance(val, str):
            if val.strip().lower() not in _NULLISH:
                return val.strip()
    for val in node.values():
        if isinstance(val, dict):
            found = _walk_exterior(val, depth + 1)
            if found:
                return found
    return ''


def _designations(node, out=None, depth=0):
    """Every {code, name} entry, wherever the payload keeps them."""
    out = [] if out is None else out
    if depth > 6 or not isinstance(node, dict):
        return out
    for key, val in node.items():
        if key.lower() == 'designations' and isinstance(val, list):
            out.extend(d for d in val if isinstance(d, dict) and d.get('code'))
        elif isinstance(val, dict):
            _designations(val, out, depth + 1)
    return out


def extract(body):
    """(code, name) from a build sheet payload. Either may be ''.

    ORDER MATTERS. Strongest evidence first, and getting this wrong is how the
    diagnostic tool shipped a mirror-housing code as a paint code:

      1. A designation whose NAME IS the exterior string. Tesla returns
         exterior "Pearl White Paint" and a designation `ppsw` named exactly
         that — unambiguous, and it needs no guessing.
      2. The code the exterior string carries IN BRACKETS. VAG, Ford, Renault,
         Kia, Peugeot, Jaguar and Land Rover all do this, and for those
         families the designation list holds no paint entry at all.
      3. A bare token that IS the code — Fiat returns "734" alone.

    There is deliberately NO keyword scan of the designation list. sources.py
    had one, matching 'lack' for German Lack, and "Black exterior mirror
    housings" contains b-LACK — so a 2004 Audi A2 whose exterior read
    "Imola yellow (1T)" reported 6FJ, the mirror housings. It produced a wrong
    code on 20 of 22 vehicles measured. A guess that looks like an answer is
    worse here than no answer at all, because the customer buys paint with it.
    """
    ext = _walk_exterior(body)
    if not ext:
        return '', ''

    for opt in _designations(body):
        if (opt.get('name') or '').strip().lower() == ext.lower():
            return _clean_code(opt['code']), ext

    name = _RE_LABEL.sub('', ext).strip()
    match = _RE_PAREN.search(name)
    if match:
        code = _clean_code(match.group(1))
        # The name is the string minus its code, so the customer is shown a
        # colour rather than a colour with a code jammed on the end.
        return code, (name[:match.start()].strip() or name)

    # A BARE TOKEN IS A CODE ONLY IF IT CONTAINS A DIGIT. Fiat returns "734"
    # with no name around it, which is genuinely the code — but the same rule
    # without the digit test turns a one-word colour into one: "Silver" would
    # be served as paint code SILVER, and "Paint" as PAINT. Every bare code
    # observed is numeric or alphanumeric; every bare NAME is letters only.
    if len(name.split()) == 1 and len(name) <= 8 and any(c.isdigit() for c in name):
        return _clean_code(name), name
    return '', name


def _poll(job_id, headers, deadline):
    """Wait for an async job. Returns the job dict, or None on timeout."""
    while time.monotonic() < deadline:
        try:
            resp = get_session().get(f'{BASE_URL}/api/v1/job/{job_id}',
                                     headers=headers, timeout=_HTTP_TIMEOUT_S)
            if resp.status_code != 200:
                return {'status': 'error', 'statusCode': resp.status_code}
            job = resp.json()
        except Exception as e:  # noqa: BLE001 — a broken poll is a miss
            return {'status': 'error', 'errorMessage': f'{type(e).__name__}: {e}'}
        if str(job.get('status', '')).lower() in ('completed', 'failed', 'error'):
            return job
        time.sleep(POLL_GAP_S)
    return None


def lookup(vin, cost_sink=None, race_over=None, budget=None):
    """Ask Ezyvin's build sheet for one VIN.

    Returns {'code', 'description', 'source'} on a usable answer, or None.
    Never raises: a reserve leg that throws takes the whole recovery with it.

    `cost_sink` is populated in place with what the call cost and what it did,
    so the Search row records it even when this leg loses or is superseded.
    """
    sink = cost_sink if cost_sink is not None else {}
    sink.setdefault('credits', 0)
    sink.setdefault('outcome', '')

    if not TOKEN:
        sink['outcome'] = 'no_token'
        return None
    if not vin:
        sink['outcome'] = 'no_vin'
        return None
    # CHECK BEFORE SPENDING. This leg starts last, so by the time it runs the
    # race may already be won by something still in flight when it was
    # submitted. Nothing can cancel an HTTP call already made — but this one
    # has not been made yet, and skipping it is 5 credits saved.
    if race_over is not None and race_over.is_set():
        sink['outcome'] = 'skipped_race_over'
        return None

    headers = {'Authorization': f'Bearer {TOKEN}',
               'User-Agent': USER_AGENT,
               'Accept': 'application/json',
               'X-Async': 'true'}
    deadline = time.monotonic() + (budget if budget is not None else TOTAL_BUDGET_S)
    url = f'{BASE_URL}/api/v1/report/buildsheet/{vin}'

    try:
        resp = get_session().get(url, headers=headers, timeout=_HTTP_TIMEOUT_S)
    except Exception as e:  # noqa: BLE001
        logger.warning('Ezyvin transport failure: %s', type(e).__name__)
        sink['outcome'] = 'transport_error'
        return None

    status, body = resp.status_code, None
    try:
        body = resp.json()
    except ValueError:
        body = None

    # 202 ACCEPTED is the normal path, not an error. X-Async means the work is
    # queued and the answer arrives on the job — and the REAL status code lives
    # on the job, not on this response. Reading resp.status_code alone made
    # every 404 look like a success with no colour.
    if status == 202 and isinstance(body, dict):
        job_id = body.get('jobId') or body.get('id')
        if not job_id:
            sink['outcome'] = 'no_job_id'
            return None
        job = _poll(job_id, headers, deadline)
        if job is None:
            # Budget spent. The job may still complete and still be charged —
            # we simply are not waiting for it, and cannot know which.
            sink['outcome'] = 'timeout'
            sink['credits'] = CREDITS_PER_HIT
            return None
        status = job.get('statusCode') or (200 if job.get('result') else status)
        body = job.get('result')

    # 404 IS FREE. Measured on two Hondas and a US-built Ford Probe: no vehicle,
    # no charge. That is what makes this leg affordable on the marques with no
    # coverage at all — trying costs nothing when there is nothing to find.
    if status == 404:
        sink['outcome'] = 'not_found'
        sink['credits'] = CREDITS_PER_MISS
        return None
    if status != 200 or not isinstance(body, dict):
        sink['outcome'] = f'http_{status}'
        # Anything that is not a clean 404 may well have been billed. Recording
        # the charge on a failure is the safe error: it overstates spend rather
        # than hiding it.
        sink['credits'] = CREDITS_PER_HIT if status not in (401, 403) else 0
        return None

    sink['credits'] = CREDITS_PER_HIT
    code, name = extract(body)
    if not code and not name:
        # THE PAID MISS. A 200 with a build sheet carrying no exterior — seen
        # once, on a Corsa EV. Distinct from a 404 and worth its own outcome,
        # because the two must not be counted together when judging whether
        # this source earns its place.
        sink['outcome'] = 'empty_charged'
        return None

    sink['outcome'] = 'code' if code else 'name_only'
    return {'code': code, 'description': name, 'source': 'ezyvin'}
