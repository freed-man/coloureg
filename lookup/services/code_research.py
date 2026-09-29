"""Research paint codes whose names disagree, with Claude and web search (paint238).

READ-ONLY FOR THE CATALOGUE. Nothing here writes to PaintLookup: findings go to
a review file, and anything worth applying later goes through its own preview,
backup and lock rules, like upgrade_swatches.

Which codes: those whose listed names state different colours (measured on
28 Sep: 2,081 of 120,594; e.g. VW 0B is Timiano Green and Moon Rock Silver,
Audi 2J Enzian Blue and Dark Bronze). A code whose extra names are only other
languages or spellings ("Acid Green" / "Vert Acide") is not one of them.

The agent: one Messages API request per code with the web search server tool
(web_search_20250305, max_uses capped), asked to rely on what it finds, not on
memory, and to answer in JSON: one paint (which name is right), a code shared
by different paints (which paint on which models and years), or unsure, with
the pages it relied on.

Tested before trusting: codes customers were actually given come with the name
the PROVIDER gave for that car (pl24, VDG, Ezyvin, mmw, or the operator by
hand), never a name our catalogue supplied (enriched_from 'name'). Those are
researched first, and each answer is scored against them.

Prices (platform.claude.com/docs/en/about-claude/pricing, read 29 Sep 2026):
web search $10 per 1,000 searches plus tokens; results count as input tokens.
"""
import json
import re
from collections import Counter, defaultdict
from difflib import SequenceMatcher

import requests

from lookup.models import PaintLookup, Search
from lookup.services.http import get_session

API_URL = 'https://api.anthropic.com/v1/messages'
DEFAULT_MODEL = 'claude-sonnet-5-5'
PRICES = {                                   # dollars per million tokens: input, output
    'claude-sonnet-5-5': (2.0, 10.0),
    'claude-haiku-4-5-20251001': (1.0, 5.0),
    'claude-opus-5-5': (4.0, 20.0),
}
SEARCH_PRICE = 0.01                          # $10 per 1,000 searches
MAX_ROUNDS = 4                               # pause_turn continuations
TIMEOUT = 180
KNOWN_PROVIDERS = {Search.PROVIDER_VDG, Search.PROVIDER_VDG_RETRY, Search.PROVIDER_PARTSLINK24,
                   Search.PROVIDER_EZYVIN, Search.PROVIDER_MMW, Search.PROVIDER_MANUAL,
                   Search.PROVIDER_ONEAUTO}
MAKE_NAMES = {'landrover': 'Land Rover', 'mercedes': 'Mercedes-Benz', 'alfaromeo': 'Alfa Romeo',
              'astonmartin': 'Aston Martin', 'rollsroyce': 'Rolls-Royce', 'bmw': 'BMW', 'mg': 'MG',
              'ds': 'DS', 'vw': 'Volkswagen', 'seat': 'SEAT', 'mini': 'MINI'}

SYSTEM = (
    "You research car paint codes for a UK paint code lookup service. Use web search to find what "
    "the given manufacturer paint code denotes. Rely ONLY on what the search results show, never on "
    "memory; if the results do not settle it, or sources disagree, the verdict is \"unsure\". A code "
    "can be reused by a manufacturer for different paints on different models or model years; if "
    "so, the verdict is \"shared\" and each paint is listed with its models and years. Reply with "
    "ONLY a JSON object, no prose, with these keys: \"verdict\" (\"one\", \"shared\" or \"unsure\"), "
    "\"correct_name\" (the paint's name when the verdict is \"one\", else \"\"), \"paints\" (a list of "
    "objects with \"name\", \"models\" and \"years\"), \"sources\" (the URLs you relied on) and "
    "\"note\" (one short sentence)."
)


def display_make(key):
    return MAKE_NAMES.get(key, key.title())


def colour_conflict(names):
    """True when the listed names state different colours: at least two name a
    colour and no colour is common to them all."""
    from lookup.services.paint_resolver import _colour_families
    fams = [f for f in (_colour_families(n) for n in set(names or [])) if f]
    return len(fams) > 1 and not set.intersection(*fams)


def conflicting_codes():
    out = []
    for pk, mfr, code, name, names, models in PaintLookup.objects.values_list(
            'id', 'manufacturer', 'code', 'name', 'all_names', 'models_list'):
        if colour_conflict(names):
            out.append({'id': pk, 'make_key': mfr, 'code': code, 'shown_name': name or '',
                        'all_names': sorted(set(names or [])), 'models': (models or [])[:12]})
    return out


def known_answers(codes=None):
    """(make key, code) -> Counter of names providers gave for real cars; never
    a name the catalogue itself supplied, and never a cache replay. Matched in
    memory against the codes being researched (their exact form, the resolver's
    variants, and the VW/Audi L prefix): one database lookup per past search
    would take minutes over the network from a PC."""
    wanted = {(c['make_key'], c['code']) for c in (codes if codes is not None else conflicting_codes())}
    known = defaultdict(Counter)
    rows = (Search.objects.exclude(paint_code='').exclude(paint_description='')
            .filter(provider__in=KNOWN_PROVIDERS)
            .exclude(enriched_from=Search.ENRICHED_NAME)
            .values_list('make', 'paint_code', 'paint_description'))
    for make, code, name in rows:
        hit = _match(make, code, wanted)
        if hit:
            known[hit][name.strip()] += 1
    return known


def _match(make, code, wanted):
    """The (make key, code) among `wanted` that a lookup's make and code reach:
    the exact form, the resolver's variants, or the VW/Audi L prefix."""
    mk = PaintLookup.normalize_manufacturer(make)
    variants = PaintLookup.normalize_code_variants(code)
    if mk in PaintLookup.LEADING_L_MAKES:
        variants = variants + ['L' + v for v in variants]
    return next(((mk, v) for v in variants if (mk, v) in wanted), None)


def customer_codes(codes):
    """paint240: (make key, code) -> when a customer last landed on it, for the
    codes given. Research on demand spends only on codes customers actually get
    (46 of the 2,081 over all the months before 29 Sep)."""
    wanted = {(c['make_key'], c['code']) for c in codes}
    seen = {}
    for make, code, when in Search.objects.exclude(paint_code='').values_list('make', 'paint_code', 'timestamp'):
        hit = _match(make, code, wanted)
        if hit and (hit not in seen or when > seen[hit]):
            seen[hit] = when
    return seen


def choose(codes, known, limit):
    """Codes with known answers first (they are the test), then the rest in a
    fixed, varied order."""
    tested = sorted((c for c in codes if (c['make_key'], c['code']) in known),
                    key=lambda c: (c['make_key'], c['code']))
    rest = sorted((c for c in codes if (c['make_key'], c['code']) not in known),
                  key=lambda c: (hash_key(c), c['make_key'], c['code']))
    return (tested + rest)[:limit]


def hash_key(c):
    return sum(ord(ch) * (i + 1) for i, ch in enumerate(f"{c['make_key']}|{c['code']}")) % 997


def question(c):
    return (f"Manufacturer: {display_make(c['make_key'])}\n"
            f"Paint code: {c['code']}\n"
            f"Names our catalogue lists for this code: {', '.join(c['all_names'])}\n"
            f"Models it appears on in our data: {', '.join(c['models']) or 'unknown'}\n"
            "Which paint does this code denote? If it denotes different paints on different models "
            "or years, list each one.")


def _post(payload, key):
    """(status, json or None, error text). Kept separate so the battery can fake it."""
    try:
        r = get_session().post(API_URL, json=payload, timeout=TIMEOUT, headers={   # the shared session (F12)
            'x-api-key': key, 'anthropic-version': '2023-06-01', 'content-type': 'application/json'})
    except requests.RequestException as exc:
        return None, None, f'{type(exc).__name__}: {exc}'[:200]
    try:
        body = r.json()
    except ValueError:
        body = None
    if r.status_code != 200:
        msg = ((body or {}).get('error') or {}).get('message', '') if isinstance(body, dict) else ''
        return r.status_code, body, f'HTTP {r.status_code} {msg}'.strip()[:200]
    return 200, body, ''


def extract_json(text):
    """The last JSON object in the reply that has a verdict, however it arrives
    (fences, prose, citations around it)."""
    best = None
    for start in [m.start() for m in re.finditer(r'\{', text or '')]:
        depth = 0
        for end in range(start, len(text)):
            if text[end] == '{':
                depth += 1
            elif text[end] == '}':
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start:end + 1])
                    except ValueError:
                        break
                    if isinstance(obj, dict) and 'verdict' in obj:
                        best = obj
                    break
    return best


def research(c, key, model=DEFAULT_MODEL, max_searches=3):
    """One code: returns a result dict (never raises for API trouble)."""
    tools = [{'type': 'web_search_20250305', 'name': 'web_search', 'max_uses': max_searches}]
    messages = [{'role': 'user', 'content': question(c)}]
    used = Counter()
    text, error = '', ''
    for _round in range(MAX_ROUNDS):
        status, body, error = _post({'model': model, 'max_tokens': 2000, 'system': SYSTEM,
                                     'tools': tools, 'messages': messages}, key)
        if status != 200 or not isinstance(body, dict):
            break
        u = body.get('usage') or {}
        used['input'] += int(u.get('input_tokens') or 0) + int(u.get('cache_creation_input_tokens') or 0) \
            + int(u.get('cache_read_input_tokens') or 0)
        used['output'] += int(u.get('output_tokens') or 0)
        used['searches'] += int((u.get('server_tool_use') or {}).get('web_search_requests') or 0)
        blocks = body.get('content') or []
        text += ''.join(b.get('text', '') for b in blocks if b.get('type') == 'text')
        if body.get('stop_reason') != 'pause_turn':
            break
        messages = messages[:1] + [{'role': 'assistant', 'content': blocks}]
    inp, outp = PRICES[model]
    cost = round(used['input'] * inp / 1e6 + used['output'] * outp / 1e6 + used['searches'] * SEARCH_PRICE, 5)
    found = extract_json(text) if not error else None
    result = {'verdict': 'error', 'correct_name': '', 'paints': [], 'sources': [], 'note': '',
              'searches': used['searches'], 'input_tokens': used['input'], 'output_tokens': used['output'],
              'cost': cost, 'error': error, 'model': model,
              'api_error': bool(error)}     # the API itself failed: not recorded, so a rerun tries again
    if found:
        verdict = str(found.get('verdict', '')).strip().lower()
        result.update({
            'verdict': verdict if verdict in ('one', 'shared', 'unsure') else 'unsure',
            'correct_name': str(found.get('correct_name') or '').strip(),
            'paints': [p for p in (found.get('paints') or []) if isinstance(p, dict)][:8],
            'sources': [str(s) for s in (found.get('sources') or [])][:8],
            'note': str(found.get('note') or '')[:300],
        })
    elif not error:
        result['error'] = 'no JSON answer in the reply'
    return result


def _same_name(a, b):
    na, nb = PaintLookup.normalize_name(a or ''), PaintLookup.normalize_name(b or '')
    if not na or not nb:
        return False
    return na == nb or na in nb or nb in na or SequenceMatcher(None, na, nb).ratio() >= 0.85


def score(result, known_names):
    """right / wrong / unsure / error / untested, against names providers gave."""
    if not known_names:
        return 'untested'
    if result['verdict'] in ('error', 'unsure'):
        return result['verdict']
    offered = [result['correct_name']] if result['verdict'] == 'one' else [p.get('name', '') for p in result['paints']]
    return 'right' if any(_same_name(o, k) for o in offered for k in known_names) else 'wrong'
