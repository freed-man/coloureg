"""One Auto API — paint codes from a VIN (paint67).

WHAT THIS IS
    One Auto resell Ezyvin's OE data. Their `OE VIN Lookup (Europe)` endpoint
    is the cheapest field-bearing product they have at 30p, and the field
    mapping to Ezyvin's own API is one-to-one: Ezyvin's `exterior` is One Auto's
    `oem_colour_desc`.

WHY IT IS IN THE POOL
    It covers ~79% of coloureg's traffic and — the number that matters — it
    answers 57% of the lookups that currently FAIL. It is also the only source
    that reliably resolves BMW, which is coloureg's worst marque: 5 of 5 in
    testing (B39, A61, A83, A22, B45) against a VDG paint route that runs to a
    502 from their own gateway at ~60 seconds on cold BMW vehicles.

    It is BOUNDED, which VDG is not. Measured at 6.06-6.38s on 42 of 45 calls,
    hit or miss. VDG paint ranges from 0.45s warm to 60s-and-an-error. For a
    page with a spinner, predictable beats fast-on-average.

BILLING — the parts that are not obvious
    * Billed PER VIN, not per HTTP call. Polling a 202 is free; the Golf control
      took 3 requests and was charged once.
    * A 206 ("no data available for this VIN") is FREE. Their own message says
      so. So a miss on a make they do not cover costs nothing but time.
    * A 200 WITH A NULL COLOUR STILL BILLS. This is the expensive failure:
      pre-2022 Mazda and Mercedes vans return a vehicle with `oem_colour_desc`
      null and charge 30p for it. That is why the routing table exists.
    * Build Decode (Tesla) is billed PER RESULT at £1.50. A VIN returning three
      results would be £4.50. Both Teslas tested returned one.

THE INTERFACE
    `lookup()` returns the same shape every provider in the pool returns, so
    paint_resolver does not need to know which provider it is talking to:

        {'code', 'description', 'all_codes', 'source', 'cost', 'outcome'}

    or None when there is nothing. Never raises — network, HTTP and timeout
    failures all degrade to None with an outcome recorded, exactly like
    _pl24_lookup, because a provider falling over must not take the lookup with
    it.
"""

import logging
import os
import time

import requests

logger = logging.getLogger(__name__)

BASE_URL = 'https://api.oneautoapi.com'

# The 30p endpoint. Everything except Tesla.
VIN_LOOKUP_PATH = '/ezyvin/vinlookup/'
VIN_LOOKUP_COST = 0.30

# Tesla only. Ezyvin's VIN lookup returns 206 for every Tesla; Build Decode is
# the only route that has them (ppsb, pr01 — both matching codes coloureg
# already held). Five times the price and billed per RESULT, so it is never the
# default.
BUILD_DECODE_PATH = '/oneauto/oebuilddecode/v2'
BUILD_DECODE_COST = 1.50

# Total wall-clock budget for one lookup INCLUDING polls.
#
# 20s WAS WRONG and was measured to be so: PF68MYJ, a cold BMW, recorded
# oneauto_outcome 'still_fetching' — polled to the budget and cut off, not
# failed. The "~6s" figure that produced 20 came from vehicles that happened to
# answer fast; the same coverage run had Nissan and Fiat still returning 202 at
# 21-31s and a Mercedes still fetching at 60s.
#
# So One Auto is bounded in the sense that it always answers EVENTUALLY, not
# that it answers quickly.
#
# 30s, not 45, because this budget decides how long a FAILED lookup takes:
# resolve_paint returns when every leg has finished, so once the others have
# given up the customer is waiting on this one alone. Fifteen seconds of extra
# spinner for an answer that is not coming is the worse trade.
#
# What that costs is small and measured: the vehicles that sat at 202 for
# 21-31s in the coverage run were Nissan and Fiat, and when they finally
# resolved they returned 206 — NO DATA. A longer budget bought nothing there.
# One Mercedes in fifty was still fetching at 60s and did eventually carry a
# code, and that one is lost — but its answer is held by One Auto for 24 hours,
# so a repeat lookup gets it instantly.
HTTP_TIMEOUT = 12.0
TOTAL_BUDGET_S = float(os.environ.get('ONEAUTO_BUDGET_S', '30'))

# Poll gap. Measured: their data is ready at ~6s, and every earlier timing of
# "9.5s" was an artefact of polling every 4 seconds — the numbers clustered on
# the poll schedule, not on their latency. Polling is free (billed per VIN), so
# the only limit is their 5-calls-per-second account cap; 0.4s uses 2.5/s for a
# single lookup and leaves room for a concurrent one.
POLL_GAP_S = 0.4


def _api_key():
    return os.environ.get('ONEAUTO_API_KEY', '')


def _pick_endpoint(make):
    """(path, cost) for this make, or (None, None) if we should not call.

    Tesla is the only make that needs the expensive endpoint, and the only one
    where the cheap endpoint is guaranteed to waste a call.
    """
    m = (make or '').strip().lower()
    if m == 'tesla':
        return BUILD_DECODE_PATH, BUILD_DECODE_COST
    return VIN_LOOKUP_PATH, VIN_LOOKUP_COST


def _extract(payload):
    """Pull (code, description, all_codes) out of a One Auto response.

    The shape varies BY MANUFACTURER, which is why this walks rather than
    reading one field:
      * VAG and most makes put it in `oem_colour_desc` as "Name (CODE)" —
        'Urano grey (5K)', 'MINERALGRAU METALLIC (B39)'.
      * Stellantis often returns a NAME ONLY with no bracket —
        'BANQUISE WHITE PAINT', 'Race Red'. Still useful: coloureg can resolve a
        name to a code through its own table, so the description is returned
        even when the code is empty.
      * Tesla puts it in the `options` array as a lowercase `factory_code`
        ('ppsb'), with no oem_colour_desc at all.
    """
    result = (payload or {}).get('result') or {}
    if isinstance(result, list):
        result = result[0] if result else {}

    desc = (result.get('oem_colour_desc') or '').strip()
    code = ''
    if desc:
        # "Urano grey (5K)" -> ("Urano grey", "5K"). The bracket is the code.
        if desc.endswith(')') and '(' in desc:
            head, _, tail = desc.rpartition('(')
            candidate = tail[:-1].strip()
            # A bracketed value is only a code if it looks like one. Some
            # descriptions carry a bracketed WORD ('(metallic finish)') which
            # must not become a paint code.
            if candidate and len(candidate) <= 16 and ' ' not in candidate:
                code = candidate.upper()
                desc = head.strip() or desc

    all_codes = []
    if code:
        all_codes.append({'code': code, 'description': desc})

    # Tesla and some build-decode responses: the code lives in options.
    if not code:
        for opt in (result.get('options') or [])[:200]:
            if not isinstance(opt, dict):
                continue
            fc = (opt.get('factory_code') or '').strip()
            fd = ' '.join(str(x) for x in (opt.get('factory_desc'),
                                           opt.get('additional_desc')) if x)
            if fc and 'paint' in fd.lower():
                code = fc.upper()
                desc = desc or fd.strip()
                all_codes.append({'code': code, 'description': desc})
                break

    return code, desc, all_codes


def lookup(vin=None, reg=None, make=None, model=None, year=None,
           search_id=None, cost_sink=None, budget=None):
    """The pool interface. Returns a result dict, or None.

    Never raises. `cost_sink`, if given, is populated with what this call cost
    even on the paths that return None — an unrecorded charge is invisible to
    the daily budget breaker, which is the same reasoning as vdg.py's
    billing_sink.
    """
    sink = cost_sink if cost_sink is not None else {}
    sink.setdefault('cost', None)
    sink.setdefault('outcome', '')

    if not vin:
        # VIN-keyed API. `reg` is accepted in the signature because Ezyvin's
        # direct API takes a registration and will slot in here unchanged, but
        # One Auto's resale of it does not.
        sink['outcome'] = 'no_vin'
        return None

    key = _api_key()
    if not key:
        sink['outcome'] = 'not_configured'
        return None

    path, cost = _pick_endpoint(make)
    url = BASE_URL + path
    headers = {'x-api-key': key, 'Accept': 'application/json'}
    params = {'vehicle_identification_number': vin}

    started = time.monotonic()
    attempts = 0
    while True:
        attempts += 1
        try:
            resp = requests.get(url, params=params, headers=headers,
                                timeout=HTTP_TIMEOUT)
        except requests.exceptions.RequestException as e:
            logger.warning('One Auto transport failure: %s', type(e).__name__)
            sink['outcome'] = 'transport_error'
            return None

        # 206 = "no data available for this VIN. You have not been charged."
        # Free, and the single most common outcome on makes they do not cover.
        if resp.status_code == 206:
            sink['outcome'] = 'no_data'
            return None

        if resp.status_code == 202:
            # Accepted, still fetching. Polling does NOT bill.
            if time.monotonic() - started >= (TOTAL_BUDGET_S if budget is None
                                              else budget):
                sink['outcome'] = 'still_fetching'
                return None
            time.sleep(POLL_GAP_S)
            continue

        if resp.status_code != 200:
            logger.warning('One Auto HTTP %s for a lookup', resp.status_code)
            sink['outcome'] = f'http_{resp.status_code}'
            return None
        break

    try:
        payload = resp.json()
    except ValueError:
        sink['outcome'] = 'unparseable'
        return None

    code, desc, all_codes = _extract(payload)

    # BUILD DECODE IS BILLED PER RESULT (F4). This module's own header says so
    # and the probe script warns it at run time, but the adapter recorded a flat
    # £1.50 however many results came back, because _extract collapses a list
    # payload to result[0]. A VIN returning three results is £4.50 and would
    # have been counted as £1.50 — under-reporting spend to the daily budget
    # breaker, the same invisible-charge class as paint18/paint21/paint26 and
    # paint67. Both Teslas tested returned one result, so nothing has been lost
    # yet; the count is taken here so it stays right when one does not.
    #
    # VIN Lookup is billed per CALL, so its cost is untouched by the count.
    if path == BUILD_DECODE_PATH:
        _results = (payload or {}).get('result')
        if isinstance(_results, list) and len(_results) > 1:
            cost = round(BUILD_DECODE_COST * len(_results), 2)
            logger.warning(
                'One Auto Build Decode returned %d results: charged %.2f',
                len(_results), cost)

    # A 200 BILLS whether or not it carried a colour. Record the cost before
    # deciding there is nothing to return, or the spend vanishes.
    sink['cost'] = cost

    if not code and not desc:
        # Vehicle found, no colour on it. The expensive failure — pre-2022
        # Mazda, Mercedes vans. Charged 30p for nothing, which is why the
        # routing table keeps those makes away from here.
        sink['outcome'] = 'null_colour'
        return None

    sink['outcome'] = 'success' if code else 'name_only'
    return {
        'code': code,
        'description': desc,
        'all_codes': all_codes,
        'source': 'oneauto',
        'cost': cost,
        'outcome': sink['outcome'],
        'attempts': attempts,
    }
