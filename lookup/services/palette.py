"""paint398: THE CHINESE MAKES PALETTE ON THE MANUAL QUEUE CARD, FOR THE OPERATOR ONLY.

His word of 6 Nov 2026 ("Queue card only"). The paint_finder chat built a palette of the UK paints of BYD, MG,
Omoda, Jaecoo, Chery, Polestar, Smart, Maxus, Geely, Changan, Ora, Haval, Leapmotor, Xpeng and Skywell: each row a
model (by VIN prefix or by name), a registration window, the DVLA colour it registers as, a code and its UK name,
graded A to D by its evidence. It is NOT 100 percent: its own grades top out at 90 (A), and on his lookups to 6 Nov
its single answers matched the code served 86 times in 90 (a 2025 Atto 3 grey and a 2021 MG HS red among the rest).
So no customer sees it: the card of a car in his manual queue shows its answer, or its candidates, for him to
confirm. Nothing here is an answer; only the dashboard's queue reads it.

THE RULE is the paint_finder chat's palette_resolve.py, kept rule for rule (resolve, confidence and their helpers
below are its code): the make; the model by VIN prefix first, a DVLA model text naming another line joining as a
split; otherwise by the model text; the registration month inside the row's window, widened 6 and then 12 months
when nothing matches; the DVLA colour's first word in the row's colour or its alternative (silvers carry Grey); a row
not sold in the UK never; an answer only when exactly one code is left. A car with a year and no month is read as its
audit reads it: January, June and December, together.

ONE CHANGE FROM palette_resolve.py, and only one (mtext): a "1" or "2" is dropped as a generation qualifier only
after "gen" or "mk" ("HS gen 1" still reads "hs"). The reference drops it anywhere, so "Atto 2" read "atto" and
"Polestar 2" read "polestar", and their rows joined any Atto 3 or Polestar 3 and 4 whose VIN did not pick the model:
on his lookups to 6 Nov, Polestar 2's paints among a Polestar 3's or 4's candidates (3 lookups, the right code kept)
and, read without its VIN, an Atto 3 given the Atto 2's black (the right code with this change). Measured: on the
palette's 74 cases the same verdicts; on his 171 lookups of these makes, only those changed. The paint_finder chat
was told, so its resolver reads the same.

THE DATA is lookup/data/palette_chinese.csv: the palette's file with only the columns read here (COLUMNS). Its notes
and sources name customers' cars, and this repository is public, so they are never copied in (trap 359); a new
palette from the paint_finder chat is copied the same way, those columns left behind.
"""
import csv
import logging
import os
import re

logger = logging.getLogger(__name__)

PALETTE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data', 'palette_chinese.csv')

#: The columns the file holds and this module reads; nothing else of the palette is copied into the repository.
COLUMNS = ('make', 'model', 'vin_prefix', 'powertrain', 'from_yyyy_mm', 'to_yyyy_mm', 'dvla_bucket', 'alt_bucket',
           'code', 'name_uk', 'uk_sold', 'evidence')

#: The palette's make keys for the makes as DVLA writes them (the paint_finder chat's audit_lookup.py).
MAKE_KEYS = {'BYD': 'byd', 'MG': 'mg', 'OMODA': 'omoda', 'JAECOO': 'jaecoo', 'CHERY': 'chery', 'POLESTAR': 'polestar',
             'SMART': 'smart', 'MAXUS': 'maxus', 'LDV': 'maxus', 'GEELY': 'geely', 'CHANGAN': 'changan',
             'DEEPAL': 'changan', 'ORA': 'ora', 'GWM': 'ora', 'GWM ORA': 'ora', 'HAVAL': 'haval', 'GWM HAVAL': 'haval',
             'LEAPMOTOR': 'leapmotor', 'XPENG': 'xpeng', 'SKYWELL': 'skywell'}

GRADE_PCT = {'A': 90, 'B': 88, 'C': 80, 'D': 70}

QUAL = {'gen', '1', '2', 'mk1', 'mk2', 'longbridge', 'petrol', 'ev', 'hybrid+', 'hybrid', 'dm-i', 'em-i', 'phev', 'hev',
        'csh', 'shs', 'auto', 'manual'}

#: paint398: a 1 or 2 is dropped as a generation only after one of these words (mtext).
GEN_NUMBERS = {'1', '2'}
GEN_WORDS = {'gen', 'mk'}

_YM = re.compile(r'^\d{4}-\d{2}$')
_PALETTE = None


def parse(lines):
    """The palette's rows from its CSV text (an open file or any iterable of lines), as palette_resolve.load reads
    them."""
    rows = list(csv.DictReader(lines))
    for r in rows:
        r['_prefixes'] = [p.strip().upper() for p in (r.get('vin_prefix') or '').split('|') if p.strip()]
        r['_buckets'] = {b.strip() for b in [(r.get('dvla_bucket') or '').upper()] + (r.get('alt_bucket') or '').upper().split('|') if b.strip()}
    return rows


def load(path=PALETTE_PATH):
    with open(path, encoding='utf-8', newline='') as f:
        return parse(f)


def palette():
    """The repository's palette, read once a process."""
    global _PALETTE
    if _PALETTE is None:
        _PALETTE = load()
    return _PALETTE


def in_window(ym, frm, to, slack_months=0):
    """Registration month inside [frm, to], widened by slack_months at both ends: a car can be
    registered a few months before the official on-sale month (press and demo cars) and stock cars
    are registered months after a colour was dropped."""
    def n(s):
        y, m = s.split('-'); return int(y) * 12 + int(m) - 1
    v = n(ym)
    if frm and v + slack_months < n(frm): return False
    if to and v - slack_months > n(to): return False
    return True


def bucket_match(word, buckets):
    # dvla_bucket or alt_bucket must carry the DVLA word itself. Silvers carry alt GREY
    # (a silver is often registered Grey); a grey carries alt SILVER only when it is
    # known to register Silver (Rembrandt Grey). So a Grey record can resolve to a
    # silver paint, a Silver record only to a silver paint or a known silver grey.
    return word.upper() in buckets


def mtext(s):
    """Model text for matching: lower case, generation and powertrain qualifiers dropped; DVLA's
    "MG 3" / "MG 4" / "MG 5" spellings collapse to mg3 / mg4 / mg5. (paint398: a 1 or 2 only after gen or mk.)"""
    low = re.sub(r'\bmg\s+([3-9])\b', r'mg\1', (s or '').lower())
    raw = [t for t in re.split(r'[\s/]+', low.replace('(', ' ').replace(')', ' ')) if t]
    # paint398: a 1 or 2 is a generation only after "gen" or "mk"; anywhere else it is the model's own number
    # ("Atto 2", "Polestar 2"), the one change from palette_resolve.py (see the module's notes).
    toks = [t for i, t in enumerate(raw) if t not in QUAL or (t in GEN_NUMBERS and not (i and raw[i - 1] in GEN_WORDS))]
    return ' ' + ' '.join(toks) + ' '


def resolve(pal, make, vehicle, vin, ym, dvla):
    make = make.lower(); vin = (vin or '').upper(); vt = mtext(vehicle)
    word = re.split(r'\s+and\s+', dvla.strip(), flags=re.I)[0].strip()
    cands = [r for r in pal if r['make'].lower() == make and (r.get('uk_sold') or 'yes') != 'no']
    # powertrain filter: a row marked petrol only never fits a PHEV, a PHEV only row never fits a petrol car
    vraw = (vehicle or '').lower()
    v_phev = bool(re.search(r'phev|plug.?in', vraw))
    v_ev = bool(re.search(r'\bev\b|electric|\bbev\b', vraw)) and not v_phev

    def pt_ok(r):
        pt = (r.get('powertrain') or '').lower()
        if pt in ('', 'any'): return True
        if pt == 'petrol' and (v_phev or v_ev): return False
        if pt == 'phev' and not v_phev: return False
        if pt == 'ev' and not v_ev: return False
        return True
    cands = [r for r in cands if pt_ok(r)]
    # model by VIN prefix first
    by_vin = [r for r in cands if r['_prefixes'] and any(vin.startswith(p) for p in r['_prefixes'])]
    if by_vin:
        # The VIN prefix selects the model line. When the DVLA model text names a different line (a Sealion 7
        # prefix on a record that says "Atto 3 Evo"), the conflict is a split, not a choice: the text-named line's
        # rows join the candidates. Lines whose text is the same as, or a parent of, a VIN-selected line ("Seal"
        # beside "Seal U", "HS gen 1" beside "HS gen 2") are not conflicts.
        vin_texts = {mtext(r['model']) for r in by_vin}
        conflict = [r for r in cands if r not in by_vin and mtext(r['model']) in vt
                    and not any(mtext(r['model']) in t for t in vin_texts)]
        mrows = by_vin + conflict
    else:
        mrows = [r for r in cands if not r['_prefixes'] and mtext(r['model']) in vt]
        if not mrows:
            # fall back to model text even for rows with prefixes (VIN missing or unknown prefix)
            mrows = [r for r in cands if mtext(r['model']) in vt]
    out = []
    for slack in (0, 6, 12):
        drows = [r for r in mrows if in_window(ym, r['from_yyyy_mm'], r['to_yyyy_mm'], slack)] if ym else mrows
        brows = [r for r in drows if bucket_match(word, r['_buckets'])]
        if brows:
            out = brows; break
    codes = sorted({r['code'] for r in out if r['code']})
    named_blank = [r for r in out if not r['code']]
    status = 'single' if len(codes) == 1 and not named_blank else ('none' if not out else 'multi')
    return status, codes, out


def confidence(status, rows):
    """Suggested 'this car wears it' percentage for a single answer: the best evidence grade among
    the matching rows sets it (A 90, B 88, C 80, D 70, never 100); a D-grade single answer is a
    flagged best guess, not a definitive answer, and stays blank on a pencil pass."""
    if status != 'single':
        return 0, ''
    best = min((r.get('evidence') or 'D') for r in rows if r['code'])
    return GRADE_PCT.get(best, 70), best


def palette_answer(make, vehicle, vin, reg_month='', year=None, colour='', pal=None):
    """The palette's answer for a car, or None when it has none (a make it does not cover, no row, no colour).

    {'status': 'single' or 'multi', 'paints': [{'code', 'name'}], 'grade', 'pct', 'two_tone'}: one paint for a
    single answer, each candidate for a split (a candidate the palette names without a code has code ''). With a
    registration month the car is read in that month; with only a year, in its January, June and December
    together (as the paint_finder chat's audit reads it); with neither, in any month. 'two_tone' is True when DVLA
    registered two colours ("White And Black"): the palette then read the first, the body, alone (trap 357)."""
    pal = palette() if pal is None else pal
    key = MAKE_KEYS.get((make or '').strip().upper())
    if not key:
        return None
    colour = (colour or '').strip()
    reg_month = (reg_month or '').strip()
    if reg_month and _YM.match(reg_month):
        status, codes, rows = resolve(pal, key, vehicle, vin, reg_month, colour)
    elif year:
        rows, found = [], set()
        for m in ('01', '06', '12'):
            st, cs, out = resolve(pal, key, vehicle, vin, f'{int(year):04d}-{m}', colour)
            rows += [o for o in out if o not in rows]
            found |= set(cs)
        codes = sorted(found)
        status = 'single' if len(codes) == 1 and not [o for o in rows if not o['code']] else ('none' if not rows else 'multi')
    else:
        status, codes, rows = resolve(pal, key, vehicle, vin, '', colour)
    if status == 'none':
        return None
    paints = [{'code': c, 'name': next((r['name_uk'] for r in rows if r['code'] == c), '')} for c in codes]
    for name in sorted({r['name_uk'] for r in rows if not r['code']}):
        paints.append({'code': '', 'name': name})
    pct, grade = confidence(status, rows)
    return {'status': status, 'paints': paints, 'grade': grade, 'pct': pct,
            'two_tone': bool(re.search(r'\s+and\s+', colour, flags=re.I))}


def queue_hint(search):
    """The palette's answer for a lookup in the manual queue (a Search row), or None. Read from the row as the
    paint_finder chat's audit reads it: the make, the vehicle title (or year, make and model), the VIN, DVLA's
    registration month (or the year) and the registered colour. Never raises: the dashboard must not break."""
    try:
        details = search.details if isinstance(search.details, dict) else {}
        dvla = details.get('dvla') if isinstance(details.get('dvla'), dict) else {}
        vehicle = search.vehicle_title or f"{search.year or ''} {search.make} {search.model or ''}"
        return palette_answer(search.make, vehicle, (search.vin or '').strip().upper(),
                              str(dvla.get('reg_month') or ''), search.year or details.get('year'), search.colour or '')
    except Exception:  # noqa: BLE001 - a hint that fails is no hint
        logger.warning('palette hint failed for lookup %s', getattr(search, 'pk', None), exc_info=True)
        return None
