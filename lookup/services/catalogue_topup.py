"""Top up the paint catalogue from a scraped source (paint236).

THE RULE: ADD WHAT THE CATALOGUE LACKS, NEVER CHANGE WHAT IT HAS.

The catalogue in the database is the real one. It carries what no file knows:
the operator's hand corrections, locked fields, hidden rows and operator names.
So a new source is compared with the live table and may only:
  * add a code the catalogue has no row for, and that the resolver cannot
    already find through another code (an L prefix, a slash, a hyphen), so no
    lookup that works today starts answering differently;
  * give a swatch to a code that has none, unless that swatch is locked or the
    row is hidden, and only when the source is plainly describing the SAME
    colour: its name for the code matches ours, or names the same colour
    family, and the swatch doesn't contradict the name we show. A code can be
    a different paint in another source (the triple check before paint236
    shipped found Alfa Romeo 241, "Verde" here, as "Bianco Pininfarina" in
    bsp, and 221 fills like it), so a fill we cannot confirm is not made.
    Two-character codes are never filled either: they are the codes reused
    across generations.
It never renames, never replaces a swatch, never unhides, and never adds names
to an existing code (extra names widen the name->code search and can make a
colour ambiguous that resolves today). For the same reason a NEW code whose
name another code of the make already carries is kept out of the name search:
it answers code->name, but a customer's colour name still finds exactly what
it finds today. (paint308: a name the operator answered by hand, in his own
table, counts as a name already carried.)

This replaces rebuilding the whole catalogue with paintscraper's merge, where
every source votes: measured on bsp, that would have dropped 822 of 5,455 short
codes and swapped about 7,470 swatches on tie-breaks. paintscraper still
scrapes; the adding happens here, against the live table, with a preview.

Every addition is marked with the source's name in `sources`, and every
import starts as a preview that writes nothing.

THE ONE EXCEPTION (paint237): `upgrade_swatches` may replace a swatch, but only
a GUESSED one (no committed catalogue file ever carried it) and only where the
row's own name says the source's swatch is right and ours is wrong or a stock
value. See plan_guess_upgrades below; it saves a backup before writing.
"""
import csv
import json
import os
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from django.db import transaction

from lookup.models import OperatorPaintCode, PaintLookup

# -- bsp-peinture.fr -----------------------------------------------------------
# US, Australian and British Leyland lists: other markets' code systems (Ford
# USA code 12 is black where UK Ford 12 is white), or a bucket spanning several
# marques. Measured: they would have named none of the codes customers were given.
BSP_EXCLUDED_MAKES = {'GM Amérique du Nord', 'Ford USA', 'GM Holden', 'Ford Australie',
                      'BLMC', 'American Motor'}
# bsp's longer company names for makes the catalogue already holds.
BSP_MAKE_ALIASES = {
    'tatamotors': 'tata', 'samsungmotors': 'samsung', 'mahindra&mahindra': 'mahindra',
    'jacmotors': 'jac', 'ineosautomotive': 'ineos', 'drautomobiles': 'dr',
    'fawgroup': 'faw', 'fotonmotor': 'foton', 'asia': 'asiamotors',
}

CODE_OK = re.compile(r'^[A-Z0-9][A-Z0-9/.\-]*$')
HEX_OK = re.compile(r'^#[0-9A-F]{6}$')
CODE_TOKEN = re.compile(r'^(?=.*\d)[A-Za-z0-9]{1,8}$')
SWATCH_SPREAD = 30            # two swatches for one code further apart than this, per channel, disagree

# paintscraper's classify_color, so a new row gets its colour group the same way
# the existing rows did (the matcher uses it to tell same-named colours apart).
COLOR_KEYWORDS = {
    "white":  ["white", "weiss", "weis", "bianco", "branco", "blanco", "blanc"],
    "black":  ["black", "schwarz", "nero", "preto", "negro", "noir"],
    "grey":   ["grey", "gray", "grau", "grigio", "cinza", "gris", "silver", "silber", "platinum",
               "graphite", "graphit"],
    "blue":   ["blue", "blau", "blu", "azul", "bleu", "turquoise", "turkis", "türkis", "tuerkis",
               "tanzanite"],
    "red":    ["red", "rot", "rosso", "vermelho", "rojo", "rouge", "infrared"],
    "green":  ["green", "gruen", "grün", "verde", "vert", "olivine", "olive"],
    "yellow": ["yellow", "gelb", "giallo", "amarelo", "amarillo", "jaune"],
    "orange": ["orange", "arancio", "laranja", "naranja", "sunset"],
    "brown":  ["brown", "braun", "marrone", "marrom", "sepia", "havanna", "mocca"],
    "purple": ["purple", "violet", "lila", "violett", "roxo"],
    "beige":  ["beige", "champagne", "cashmere", "sand", "ivory"],
    "gold":   ["gold"],
}


def classify_color(name):
    low = (name or '').lower()
    for group, words in COLOR_KEYWORDS.items():
        if any(w in low for w in words):
            return group
    return 'other'


def name_is_codes(name):
    """A "name" made only of codes, like "209-6X3" or "753 + 752": a two-tone
    formula or a paint-maker reference, not a colour a customer would know."""
    tokens = [t for t in re.split(r'[\s\-+/.]+', name or '') if t]
    return bool(tokens) and all(CODE_TOKEN.match(t) for t in tokens)


def _name_forms(norm):
    """The forms the name search also tries (it swaps grey and gray)."""
    return {norm, norm.replace('gray', 'grey'), norm.replace('grey', 'gray')}


def titlecase_if_upper(name):
    return name.title() if name.isupper() else name


def _fold(text):
    return ''.join(c for c in unicodedata.normalize('NFKD', text or '') if not unicodedata.combining(c))


def bsp_make_key(raw):
    key = PaintLookup.normalize_manufacturer(_fold(raw))
    return BSP_MAKE_ALIASES.get(key, key)


def _strict(name):
    return re.sub(r'\s+', ' ', re.sub(r'[^a-z0-9 ]', ' ', _fold(name).lower())).strip()


def _rgb(h):
    return tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))


def _lightness(h):
    r, g, b = _rgb(h)
    return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255


def contradicts(name, hexv):
    """A swatch plainly at odds with its own name: a white name on a dark
    swatch, or a black name on a light one. Shown to a customer, a wrong swatch
    is worse than none."""
    group = classify_color(name)
    if group == 'white' and _lightness(hexv) < 0.35:
        return True
    if group == 'black' and _lightness(hexv) > 0.65:
        return True
    return False


# -- reading -------------------------------------------------------------------

@dataclass
class Source:
    name: str
    rows_read: int = 0
    left_out: Counter = field(default_factory=Counter)
    codes: dict = field(default_factory=dict)      # (make, code) -> {'names', 'strict', 'hexes', 'two_tone'}
    # paint391: the name written into `sources` for what this source adds, when
    # it is not the source's own name (see TOPUP_MARK); the codes the reader
    # tried, for the left out check; the files it read.
    mark: str = ''
    attempts: int = 0
    files: list = field(default_factory=list)


def read_bsp(path):
    """The bsp-peinture CSV (make, code, name, finish, hex, swatch_css, url)."""
    src = Source('bsp')
    with open(path, encoding='utf-8-sig', newline='') as f:
        for row in csv.DictReader(f):
            src.rows_read += 1
            make = (row.get('make') or '').strip()
            if make in BSP_EXCLUDED_MAKES:
                src.left_out['a US, Australian or British Leyland list'] += 1
                continue
            code = (row.get('code') or '').strip().upper()
            if not code or not CODE_OK.match(code):
                src.left_out['a code with spaces or brackets'] += 1
                continue
            if len(code) == 4 and code[:2] == code[2:]:
                src.left_out['a doubled code such as F1F1'] += 1
                continue
            name = (row.get('name') or '').strip()
            if not name or name_is_codes(name):
                src.left_out['a name that is only codes (a formula or reference)'] += 1
                continue
            if _strict(name) in ('multi ton', 'multiton'):
                src.left_out['a finish label in the name column'] += 1
                continue
            name = titlecase_if_upper(name)
            hexv = (row.get('hex') or '').strip().upper()
            entry = src.codes.setdefault((bsp_make_key(make), code),
                                         {'names': set(), 'strict': set(), 'hexes': set(), 'two_tone': False})
            entry['names'].add(name)
            entry['strict'].add(_strict(name))
            if (row.get('finish') or '').strip().lower() == 'multi-ton' or \
                    (row.get('swatch_css') or '').strip().lower().startswith('linear'):
                entry['two_tone'] = True
            elif HEX_OK.match(hexv):
                entry['hexes'].add(hexv)
    return src


# -- paint391: the scraped sites, as paintscraper writes them ------------------
#
# paintscraper scrapes chipex, colorndrive, peinturevoiture, AutomotiveTouchup
# (atu) and PaintScratch every few months. Measured on 5 Nov 2026 against the
# live catalogue (155,276 rows) with the files of 28 Oct, what they can add is
# small, and it has to be certain:
#   * A NEW CODE ONLY WHERE TWO SITES GIVE IT THE SAME COLOUR. Of the 27,257
#     codes the catalogue lacks that these sites list, 25,492 are on one site
#     only. One shop's list is not proof; two sites that agree are.
#   * AN EMPTY SWATCH FILLED ONLY WHERE THE SITE'S NAME FOR THE CODE IS OURS
#     AND THE SWATCH IS THE COLOUR OUR NAME STATES. Under paint236's rule
#     alone, 29 of chipex's 399 fills were a different colour from the name.
#     A name that states no colour gets none: nothing could check it.
#   * EACH FILE CHECKED BEFORE ANYTHING IS PLANNED (check_scraped): the fields
#     its reader needs, how much of it is left out, and how often the codes
#     we already hold get the same colour from it. A site that changed its
#     layout, or a code field that now means something else, stops the run
#     with the reason instead of planning on it.
# Everything a site adds is marked with its TOPUP_MARK, every one of them in
# PaintLookup.TOPUP_SOURCES: a new row answers only when no established row
# would (paint236), and a fill leaves an established row established.
# paint395: and autocolor7, a sixth site (its notes are above read_autocolor7).

class SourceChanged(Exception):
    """A scraped file that no longer looks like the file its reader was written for."""


SCRAPED_SITES = ('chipex', 'colorndrive', 'peinturevoiture', 'atu', 'paintscratch', 'autocolor7')
TOPUP_MARK = {'chipex': 'chipex_topup', 'colorndrive': 'colorndrive_topup',
              'peinturevoiture': 'peinturevoiture_topup', 'atu': 'atu', 'paintscratch': 'paintscratch',
              'autocolor7': 'autocolor7'}
# Whose name a new row shows, first to last: colorndrive flags the UK name,
# chipex is a UK shop, PaintScratch and AutomotiveTouchup write US English,
# peinturevoiture the maker's own language. paint395: autocolor7 last, as it
# writes its names in capitals and in the maker's own language too: a new code
# needs another site that agrees, so the name shown is always that site's.
NAME_ORDER = ('colorndrive', 'chipex', 'paintscratch', 'atu', 'peinturevoiture', 'autocolor7')
# The sites whose swatches are the paint's own (the others have none).
SWATCH_SITES = ('chipex', 'colorndrive')
# AutomotiveTouchup and PaintScratch draw on one upstream list. Of the 10,997
# codes both list, half carry the very same wording, typos included
# ("Casablance Beige Metallic", "Pommegranate Red Metallic"), against 11 to 15
# per cent between any other two of these sites. When they agree that is one
# voice, not two: a new code needs another site with them.
# paint395: peinturevoiture and autocolor7 both write the maker's own names
# (Delftweiss, Garmish, Autriche), and of the codes both list, 35 in 1,000
# share a word no other site uses, against 15 at most for any other two of the
# six (measured 6 Nov 2026). Not shown to be one list, but counted as one voice
# to be safe: a new code needs a third site with them.
VOICE = {'atu': 'us lists', 'paintscratch': 'us lists',
         'peinturevoiture': 'maker language lists', 'autocolor7': 'maker language lists'}
# colorndrive's ten colour family stand ins (its notes of 28 Oct 2026): one
# value per family on thousands of different paints, never a measured colour.
# Read as no swatch, and paint237's upgrade treats them as guesses.
COLORNDRIVE_STAND_INS = frozenset({'#0E4BEF', '#909090', '#49B675', '#E71837', '#A37063',
                                   '#FCE903', '#F2E7BF', '#FCFCFC', '#FC9303', '#A83CC3'})
# The fields each reader needs, as paintscraper wrote them on 28 Oct 2026.
SCRAPED_FIELDS = {
    'chipex': ('manufacturer', 'model', 'year', 'color_name', 'color_codes', 'color_hex'),
    'colorndrive': ('manufacturer', 'color_name', 'color_code', 'aux_code', 'color_hex', 'alternative_names'),
    'peinturevoiture': ('manufacturer', 'color_codes', 'color_name', 'details'),
    'atu': ('manufacturer', 'model', 'color_name', 'color_codes'),
    'paintscratch': ('manufacturer', 'color_name', 'color_codes'),
    'autocolor7': ('manufacturer', 'color_codes', 'color_name', 'vehicle_type', 'title'),   # its file of 3 Nov 2026
}
# What each file looked like on 5 Nov 2026 against the live catalogue: the
# share of the codes its reader tried that were left out, and the share of the
# codes we already hold that it gives the same colour. A file left out more
# than LEFT_OUT_MARGIN above, or agreeing less than AGREE_MARGIN below, is not
# planned on.
SCRAPED_BASELINE = {
    'chipex': {'left_out': 0.032, 'agree': 0.952},            # 41,273 codes, 32,059 held
    'colorndrive': {'left_out': 0.029, 'agree': 0.978},       # 219,627 codes, 59,649 held
    'peinturevoiture': {'left_out': 0.176, 'agree': 0.948},   # 56,918 codes, 42,770 held
    'atu': {'left_out': 0.050, 'agree': 0.879},               # 301,497 codes, 21,876 held
    'paintscratch': {'left_out': 0.085, 'agree': 0.810},      # 39,073 codes, 13,763 held
    'autocolor7': {'left_out': 0.161, 'agree': 0.890},        # 127,987 codes, 29,163 held (its file of 3 Nov)
}
# paint395: the date of the file each reader and its baseline were made for,
# as the checks and the top up's preview name it.
SCRAPED_FILE_DATE = {'autocolor7': '3 Nov 2026'}
LEFT_OUT_MARGIN = 0.15
AGREE_MARGIN = 0.10
AGREE_MIN_HELD = 20           # fewer codes in common than this, and the file cannot be judged
# Make names the sites write their own way, as measured on 5 Nov 2026.
SCRAPED_MAKE_ALIASES = {
    'chipex': {'Rover Group': 'rover'},
    'colorndrive': {'Ford Europe': 'ford', 'Byd Auto': 'byd'},
    'peinturevoiture': {'Scania-Vabis': 'scania', 'Ssangyong-Kgm': 'ssangyong', 'Tata Motors': 'tata',
                        'CHERY AUTOMOBILE': 'chery', 'Changan Automobile': 'changan', 'Byd Auto': 'byd',
                        'Hanomag-Henschel': 'hanomag', 'Mahindra & Mahindra': 'mahindra',
                        'Samsung Motors': 'samsung', 'Polestar Racing': 'polestar',
                        'Ineos Automotive': 'ineos', 'Bürstner': 'bürstner', 'Jac Motors': 'jac'},
    'paintscratch': {'Rover-Sterling': 'sterling'},
}
# Lists left out whole (each site's notes of 28 Oct 2026): AutomotiveTouchup
# files US and European Ford under one Ford, and Austin Rover is a make we do
# not hold; PaintScratch's Ford is US Ford, its AMG is AM General (Hummer),
# Fleet and International are fleet and truck lists, and Merkur sold Ford
# Europe's cars in the US.
SCRAPED_SKIPPED_MAKES = {
    'atu': {'Ford', 'Austin Rover'},
    'paintscratch': {'Ford', 'AMG', 'Fleet', 'International', 'Merkur'},
}
PAINT_MAKER_REF = re.compile(r'PPG|DUPONT|BASF|R-M', re.I)
_SCRAPED_FILE = re.compile(r'^(' + '|'.join(SCRAPED_SITES) + r')_(\d{4}-\d{2}-\d{2})\.jsonl$')


def _compact(code):
    """A code with its punctuation taken out: NH-737M and NH737M are one code."""
    return re.sub(r'[^A-Z0-9]', '', (code or '').upper())


def _file_date(site):
    return SCRAPED_FILE_DATE.get(site, '28 Oct 2026')


def _scraped_make(site, raw):
    if raw in SCRAPED_SKIPPED_MAKES.get(site, ()):
        return None
    return SCRAPED_MAKE_ALIASES.get(site, {}).get(raw) or PaintLookup.normalize_manufacturer(_fold(raw or ''))


def _scraped_rows(site, path):
    name = os.path.basename(path)
    with open(path, encoding='utf-8') as f:
        for n, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError as exc:
                raise SourceChanged(f'{site}: line {n} of {name} is not JSON. Nothing was planned.') from exc
            missing = [k for k in SCRAPED_FIELDS[site] if k not in row]
            if missing:
                raise SourceChanged(
                    f'{site}: line {n} of {name} has no field "{missing[0]}", which its reader needs. The file\'s '
                    f'layout has changed since {_file_date(site)}: check paintscraper\'s notes for it. '
                    f'Nothing was planned.')
            yield row


def _add_code(src, mk, code, name, hexv='', two_tone=False):
    """read_bsp's rules for one code of a scraped row. True when the code was kept."""
    src.attempts += 1
    code = (code or '').strip().upper()
    if not code or not CODE_OK.match(code):
        src.left_out['a code with spaces, brackets or other marks'] += 1
        return False
    if len(code) == 4 and code[:2] == code[2:]:
        src.left_out['a doubled code such as F1F1'] += 1
        return False
    name = re.sub(r'\s+', ' ', (name or '').strip())
    if not name or name_is_codes(name):
        src.left_out['a name that is only codes (a formula or reference)'] += 1
        return False
    if _strict(name) in ('multi ton', 'multiton'):
        src.left_out['a finish label in the name column'] += 1
        return False
    name = titlecase_if_upper(name)
    if name.islower():
        name = name.title()
    entry = src.codes.setdefault((mk, code), {'names': set(), 'strict': set(), 'hexes': set(), 'two_tone': False})
    entry['names'].add(name)
    entry['strict'].add(_strict(name))
    hexv = (hexv or '').strip().upper()
    if two_tone:
        entry['two_tone'] = True
    elif HEX_OK.match(hexv) and hexv not in COLORNDRIVE_STAND_INS:
        entry['hexes'].add(hexv)
    return True


def _names(text, sep=', '):
    """A site's several names for one paint, as one name read_bsp's way ("A/B").
    A slash inside one name becomes a space, so "Silver (matte/satin)" stays one."""
    return '/'.join(p.strip().replace('/', ' ') for p in (text or '').split(sep) if p.strip())


def read_chipex(path):
    """chipex: names, codes and swatches from the All Models pages (no model, no
    year), which hold what the model and year pages repeat."""
    src = Source('chipex', mark=TOPUP_MARK['chipex'], files=[path])
    for row in _scraped_rows('chipex', path):
        src.rows_read += 1
        if row['model'] or row['year'] is not None:
            continue
        mk = _scraped_make('chipex', row['manufacturer'])
        for code in (row['color_codes'] or '').split(','):
            _add_code(src, mk, code, _names(row['color_name']), row['color_hex'])
    return src


def read_colorndrive(path):
    """Color N Drive: the colour page's whole COLOR CODE (a Fiat style 293/A
    whole, where the list splits it) and its AUX CODE split at "/"; the name
    the page flags as the UK's, else its first; the list swatch unless it is
    one of the ten stand ins. The file of pages no list shows
    (<name>_sitemap.jsonl) is read with it when it is there."""
    src = Source('colorndrive', mark=TOPUP_MARK['colorndrive'], files=[path])
    side = path[:-len('.jsonl')] + '_sitemap.jsonl' if path.endswith('.jsonl') else ''
    if side and os.path.exists(side):
        src.files.append(side)
    for p in src.files:
        for row in _scraped_rows('colorndrive', p):
            src.rows_read += 1
            mk = _scraped_make('colorndrive', row['manufacturer'])
            uk = [a.get('name') for a in (row['alternative_names'] or []) if a.get('flag') == 'uk' and a.get('name')]
            name = uk[0] if uk else (row['color_name'] or '').split(', ')[0]
            codes = [row['color_code']] + (row['aux_code'] or '').split('/')
            for code in codes:
                if (code or '').strip():
                    _add_code(src, mk, code, name, row['color_hex'])
    return src


def read_peinturevoiture(path):
    """peinturevoiture: the colour page's Code (two codes "84P - 84Q" are two)
    and Nom; a Bi-Tons paint is a two tone. No swatches (they are pictures)."""
    src = Source('peinturevoiture', mark=TOPUP_MARK['peinturevoiture'], files=[path])
    for row in _scraped_rows('peinturevoiture', path):
        src.rows_read += 1
        mk = _scraped_make('peinturevoiture', row['manufacturer'])
        det = row['details'] or {}
        two = det.get('Type de peinture') == 'Bi-Tons'
        for code in re.split(r'\s+-\s+', det.get('Code') or row['color_codes'] or ''):
            _add_code(src, mk, code, det.get('Nom') or row['color_name'], two_tone=two)
    return src


def read_atu(path):
    """AutomotiveTouchup: codes and names from the All Models pages (each
    colour's full code list, cross references included). No swatches."""
    src = Source('atu', mark=TOPUP_MARK['atu'], files=[path])
    for row in _scraped_rows('atu', path):
        src.rows_read += 1
        if row['model']:
            continue
        mk = _scraped_make('atu', row['manufacturer'])
        if mk is None:
            src.left_out['a list left out whole (Ford, Austin Rover)'] += 1
            continue
        if 'delete usage' in (row['color_name'] or '').lower():
            src.left_out["a name carrying the site's Delete Usage tag"] += 1
            continue
        for code in (row['color_codes'] or '').split(','):
            _add_code(src, mk, code, _names(row['color_name']))
    return src


def read_paintscratch(path):
    """PaintScratch (a US shop): codes split at "/", a trailing "(1973)" or
    "(USA)" dropped, paint maker references left out. Its swatches are not used
    (most pages came without them, and a check found a quarter right)."""
    src = Source('paintscratch', mark=TOPUP_MARK['paintscratch'], files=[path])
    for row in _scraped_rows('paintscratch', path):
        src.rows_read += 1
        mk = _scraped_make('paintscratch', row['manufacturer'])
        if mk is None:
            src.left_out['a list left out whole (US Ford, fleet, AM General, Merkur)'] += 1
            continue
        for code in (row['color_codes'] or '').split('/'):
            code = re.sub(r'\s*\([^)]*\)\s*$', '', code).strip()
            if PAINT_MAKER_REF.search(code):
                src.left_out['a paint maker reference'] += 1
                continue
            if code:
                _add_code(src, mk, code, row['color_name'])
    return src


# -- paint395: autocolor7 ----------------------------------------------------
#
# autocolor7.com, a paint shop in Kyiv, has a page for every colour it mixes:
# the code, the name (in capitals, in the maker's own language, "-MET." for
# metallic), years, a colour group, the vehicle type and the market; no
# swatches. paintscraper's file of 3 Nov 2026 holds 143,465 pages (its notes
# beside it). Measured on 6 Nov 2026 against the live catalogue: of the codes
# it shares with us it names 89 in 100 the same colour (its colour group field
# far less often, so only its names are read), and with the five sites above it
# adds 329 codes two sites name the same paint (301 of them with
# AutomotiveTouchup), none of them a code a customer was given to 26 Oct.
# Read as its notes describe it:
#   * the make of each list as the site writes it; the lists written another way
#     than ours are in AUTOCOLOR7_MAKES, three of them two makes at once. Its
#     truck lists of car makers (MERCEDES TRUCKS, VOLVO TRUCKS), the Chinese
#     joint ventures (VW SHANGHAI) and the lists with no make are not ours;
#   * cars and vans only: fleet paints, motorcycles, buses and colour standards
#     are left out;
#   * a code only where it is the code in the page's title: the site's own
#     table lost the zeros of a few hundred codes (Toyota "41" for 041) and wrote
#     some as spreadsheet numbers ("1,00E+00" for 1E0). A "." in place of a code
#     is left out, and a Honda style bracket letter dropped ("B504P (A)");
#   * "-MET." read as Metallic and "-MAT" as Matt, "(2C)" and "-AC" dropped;
#   * every name a code is given is kept, so a code it sells under several names
#     (another market's, another era's) is no voice, as on the other sites.
AUTOCOLOR7_MAKES = {
    'FORD EUROPE': ('ford',), 'FORD USA': ('fordamerica',), 'GENERAL MOTORS-GM': ('generalmotors',),
    'VOLKSWAGEN-VW': ('volkswagen',), 'FIAT - LANCIA': ('fiat', 'lancia'), 'SEAT-CUPRA': ('seat', 'cupra'),
    'SSANGYONG - KG MOBILITY': ('ssangyong', 'kgmobility'), 'LADA - VAZ': ('lada',), 'DAF TRUCKS': ('daf',),
    'SCANIA TRUCKS': ('scania',), 'CHEVROLET EUROPE': ('chevrolet',), 'VAUXHALL VANS': ('vauxhall',),
    'GREATWALL AUTO': ('greatwall',), 'BYD AUTO': ('byd',), 'FAW CAR CO LTD': ('faw',), 'JAC MOTORS': ('jac',),
    'SAIC MAXUS': ('maxus',), 'XPENG MOTORS': ('xpeng',), 'FOTON MOTORS': ('foton',), 'XIAOMI AUTO': ('xiaomi',),
    'DONGFENG AUTO': ('dongfeng',), 'BRILLIANCE AUTO': ('brilliance',), 'GAC AION': ('gacmotor',),
    'LYNK & CO': ('lynkandco',), 'TESLA MOTORS': ('tesla',), 'MCLAREN AUTOMOTIVE': ('mclaren',),
    'CATERHAM CARS': ('caterham',), 'KARMA AUTOMOTIVE': ('karma',), 'INEOS AUTOMOTIVE': ('ineos',),
    'HUMMER - AM GENERAL': ('hummer',), 'MAHINDRA AND MAHINDRA': ('mahindra',),
    'MAHINDRA & MAHINDRA': ('mahindra',), 'ZAPOROZEC-ZAZ': ('zaz',), 'HANOMAG-HENSCHEL TRUCKS': ('hanomag',),
    'MACK TRUCK': ('mack',), 'ASIA': ('asiamotors',),
}
# The vehicle types kept, as the site writes them (Ukrainian, or Russian on a
# few pages): passenger cars, and goods vehicles (vans, and the lorry makes the
# catalogue holds, DAF and Scania).
AUTOCOLOR7_VEHICLES = frozenset({'легкові автомобілі', 'легковые автомобили', 'вантажний транспорт',
                                 'грузовой транспорт'})
_AC7_SUFFIX = re.compile(r'\s*(-MET\.?|-MAT|\(2C\)|-AC)\s*$', re.I)
_AC7_FINISH = {'-met': 'Metallic', '-met.': 'Metallic', '-mat': 'Matt'}


def _autocolor7_name(name):
    """ "PHANTOM BLACK-MET." as "Phantom Black Metallic", "DIGITAL GREEN-MET. -MAT" as
    "Digital Green Metallic Matt"; "(2C)" and "-AC" dropped."""
    n = re.sub(r'\s+', ' ', (name or '').strip())
    finish = []
    while True:
        m = _AC7_SUFFIX.search(n)
        if not m:
            break
        word = _AC7_FINISH.get(m.group(1).lower())
        if word and word not in finish:
            finish.insert(0, word)
        n = n[:m.start()].rstrip(' -')
    return ' '.join([titlecase_if_upper(n)] + finish).strip()


def read_autocolor7(path):
    """autocolor7 (paint395): codes and names, cars and vans only, each code
    only as its page's title shows it (see the notes above). No swatches."""
    src = Source('autocolor7', mark=TOPUP_MARK['autocolor7'], files=[path])
    for row in _scraped_rows('autocolor7', path):
        src.rows_read += 1
        raw_make = (row['manufacturer'] or '').strip()
        if not raw_make:
            src.left_out['a list with no make (mobicodes, mobifleet, xpeng)'] += 1
            continue
        if (row['vehicle_type'] or '').strip() not in AUTOCOLOR7_VEHICLES:
            src.left_out['not a car or a van (fleet paints, motorcycles, buses, colour standards)'] += 1
            continue
        raw = ' '.join((row['color_codes'] or '').upper().split())
        if raw in ('', '.'):
            src.left_out['no code, or a "." in its place'] += 1
            continue
        if f' {raw} ' not in f" {' '.join((row['title'] or '').upper().split())} ":
            src.left_out["a code that is not the one in its page's title (zeros lost, a spreadsheet's number)"] += 1
            continue
        makes = AUTOCOLOR7_MAKES.get(raw_make.upper()) or (_scraped_make('autocolor7', raw_make),)
        name = _autocolor7_name(row['color_name'])
        for part in re.split(r'[;,]', raw):
            code = re.sub(r'\s*\([^)]*\)\s*$', '', part).strip()
            if code:
                for mk in makes:
                    _add_code(src, mk, code, name)
    return src


SCRAPED_READERS = {'chipex': read_chipex, 'colorndrive': read_colorndrive,
                   'peinturevoiture': read_peinturevoiture, 'atu': read_atu,
                   'paintscratch': read_paintscratch, 'autocolor7': read_autocolor7}


def find_scraped(folder):
    """The newest file of each site under `folder` (paintscraper's data folder,
    or any folder they were copied into): {site: path}. Only a file named as
    paintscraper names them, <site>_YYYY-MM-DD.jsonl, is read; logs, notes and
    other sites are passed over."""
    found = {}
    for root, _dirs, files in os.walk(folder):
        for fn in files:
            m = _SCRAPED_FILE.match(fn)
            if m and (m.group(1) not in found or m.group(2) > found[m.group(1)][0]):
                found[m.group(1)] = (m.group(2), os.path.join(root, fn))
    return {site: path for site, (_d, path) in sorted(found.items())}


def check_scraped(src, existing=None):
    """Raise SourceChanged unless the file looks like the one its reader was
    written for (see SCRAPED_BASELINE). `existing`: plan_scraped's view of the
    catalogue, {(make, code): (id, hex, hidden, locked, sources, name)}.
    Returns (left out share, codes held in common, share agreeing)."""
    if existing is None:
        existing = _catalogue_view()[0]
    base = SCRAPED_BASELINE[src.name]
    files = ', '.join(os.path.basename(p) for p in src.files)
    if not src.codes:
        raise SourceChanged(f'{src.name}: {files} gave no codes at all. Nothing was planned.')
    out = sum(src.left_out.values()) / max(src.attempts, 1)
    if out > base['left_out'] + LEFT_OUT_MARGIN:
        raise SourceChanged(
            f'{src.name}: {files} leaves out {out:.0%} of the codes it holds (the file of {_file_date(src.name)}: '
            f'{base["left_out"]:.0%}). Its codes may be written another way now: check paintscraper\'s notes '
            f'for it. Nothing was planned.')
    held = same = 0
    for key, entry in src.codes.items():
        row = existing.get(key)
        if not row or row[2] or len(entry['strict']) > 1:
            continue
        held += 1
        same += _same_colour(sorted(entry['names'])[0], entry, row[5])
    if held < AGREE_MIN_HELD:
        raise SourceChanged(f'{src.name}: only {held} of the codes in {files} are in our catalogue, too few to '
                            f'check the file against. Nothing was planned.')
    agree = same / held
    if agree < base['agree'] - AGREE_MARGIN:
        raise SourceChanged(
            f'{src.name}: of the {held:,} codes in {files} that our catalogue holds, it gives only {agree:.0%} '
            f'the same colour (the file of {_file_date(src.name)}: {base["agree"]:.0%}). Its codes may mean something '
            f'else now: check paintscraper\'s notes for it. Nothing was planned.')
    return out, held, agree


# -- planning ------------------------------------------------------------------

@dataclass
class Plan:
    source: str
    new_rows: list = field(default_factory=list)       # PaintLookup kwargs
    fills: list = field(default_factory=list)          # (row id, hex, sources)
    tally: Counter = field(default_factory=Counter)
    new_by_make: Counter = field(default_factory=Counter)
    fill_by_make: Counter = field(default_factory=Counter)
    new_makes: Counter = field(default_factory=Counter)


def _swatch(entry, name, tally):
    if entry['two_tone']:
        tally['new code without a swatch: a two-tone'] += 1
        return ''
    hexes = sorted(entry['hexes'])
    if not hexes:
        tally['new code without a swatch: none given'] += 1
        return ''
    if len(hexes) > 1 and max(max(a) - min(a) for a in zip(*(_rgb(h) for h in hexes))) > SWATCH_SPREAD:
        tally['new code without a swatch: its swatches disagree'] += 1
        return ''
    if contradicts(name, hexes[0]):
        tally['new code without a swatch: it contradicts the name'] += 1
        return ''
    return hexes[0]


def _parts(names):
    """Each name, and each part of a name listing several ("Blue Silver/Nebula Blue")."""
    out = set()
    for n in names:
        out.update(p.strip() for p in [n] + n.split('/') if p.strip())
    return out


def _same_colour(name, entry, our_name):
    """Is the source plainly describing the colour we SHOW? One of its names
    (or one part of a name listing several) matches our displayed name, closely
    enough to allow a spelling variant (Bijirim / Bijarim Khaki), or both name
    the same colour family. Measured against the displayed name, not every
    alias the row carries: some rows conflate two paints (BMW WC7T is shown as
    "Black Coral" but also lists "Polarized Grey"), and a swatch for the other
    one would sit under the wrong name."""
    theirs = {PaintLookup.normalize_name(p) for p in _parts(entry['names'])} - {''}
    ours = {PaintLookup.normalize_name(p) for p in _parts([our_name])} - {''}
    if theirs & ours:
        return True
    if any(SequenceMatcher(None, a, b).ratio() >= 0.9 for a in theirs for b in ours):
        return True
    ours_groups = {classify_color(p) for p in _parts([our_name])} - {'other'}
    their_groups = {classify_color(p) for p in _parts(entry['names'])} - {'other'}
    return bool(ours_groups) and ours_groups == their_groups


# paint259: MAKES WHOSE OWN CODES ARE TWO CHARACTERS. Short codes are left out
# because for most makes they are short forms that clash with real codes (VW's
# 2T is LC9X). Chery, Omoda and Jaecoo number their paints in two characters
# (Omoda BW Selenite White, GV, SK, CL), so for them a two-character code is the
# real code; one character never is. Measured on bsp's list of 28 Sep against the
# repository catalogue: it lists 24 Omoda, 20 Jaecoo and 129 Chery two-character
# codes, of which 20, 15 and 99 were missing. Every other rule still applies.
TWO_CHARACTER_MAKES = {'chery', 'omoda', 'jaecoo'}


def _too_short(mfr, code):
    return len(code) <= (1 if mfr in TWO_CHARACTER_MAKES else 2)


def _two_character_pair(name):
    """paint259: a name made only of two-character codes, like "Kx-Cl" (Omoda X4,
    KX body with a CL roof): a two-tone formula, which the top-up keeps out like
    any other two-colour code. name_is_codes() misses it, because its tokens
    have no digits; for these makes two characters ARE a code. Measured: 8 of the
    128 codes the change would otherwise add were exactly this, all with no
    swatch."""
    tokens = [t for t in re.split(r'[\s\-+/.]+', name or '') if t]
    return len(tokens) > 1 and all(len(t) == 2 and t.isalnum() for t in tokens)


def plan_topup(src):
    plan = Plan(src.name)
    existing, active, makes, searched = {}, defaultdict(set), set(), defaultdict(set)
    for pk, mfr, code, hexv, suppressed, locked, sources, norms, our_name in PaintLookup.all_objects.values_list(
            'id', 'manufacturer', 'code', 'hex', 'suppressed', 'locked_fields', 'sources', 'normalized_names', 'name'):
        existing[(mfr, code)] = (pk, hexv, suppressed, locked or [], sources or [], our_name or '')
        makes.add(mfr)
        if not suppressed:
            active[mfr].add(code)
            searched[mfr].update(norms or [])
    # paint308: THE OPERATOR'S OWN TABLE HOLDS NAMES TOO. The rule further down
    # keeps a new code out of the name search when another code of the make
    # already carries its name, so that a customer's colour name still finds
    # what it finds today. It read only the catalogue's rows. A name the
    # operator answered by hand is found through his own table, and only when
    # the catalogue finds nothing (paint92), so a new row carrying that name
    # took the answer over. Reproduced on a scratch copy: "Harbour Mauve",
    # answered by hand with one code, gave the source's different code after
    # a top-up, and the source's "Storm Gray" took over his "Storm Grey". On
    # 25 Sep, 27 of his 133 entries could not be folded into a catalogue row
    # (26 with no row for the code, 1 refused), so his table is the only place
    # those names lead to his codes. His names now count as names already
    # searched.
    for mfr, norm in OperatorPaintCode.objects.values_list('manufacturer', 'normalized_name'):
        searched[mfr].add(norm)
    for (mfr, code), entry in sorted(src.codes.items()):
        if len(entry['strict']) > 1:
            plan.tally['left alone: more than one colour under this code'] += 1
            continue
        name = sorted(entry['names'])[0]
        row = existing.get((mfr, code))
        if row:
            pk, hexv, suppressed, locked, sources, our_name = row
            if suppressed:
                plan.tally['existing code left alone: hidden'] += 1
            elif hexv:
                plan.tally['existing code left alone: already has a swatch'] += 1
            elif 'hex' in locked:
                plan.tally['existing code left alone: swatch locked'] += 1
            elif _too_short(mfr, code):
                plan.tally['existing code left alone: a short code (two characters or fewer)'] += 1
            elif not _same_colour(name, entry, our_name):
                plan.tally['existing code left alone: bsp may mean another colour'] += 1
            else:
                new_hex = _swatch(entry, name, Counter())
                if new_hex and not contradicts(our_name, new_hex):
                    plan.fills.append((pk, new_hex, sorted(set(sources) | {src.name})))
                    plan.fill_by_make[mfr] += 1
                else:
                    plan.tally['existing code left alone: no usable swatch'] += 1
            continue
        if _too_short(mfr, code):
            plan.tally['left alone: a short code (two characters or fewer)'] += 1
            continue
        if mfr in TWO_CHARACTER_MAKES and _two_character_pair(name):
            plan.tally['left alone: a two-tone pair named only by its codes'] += 1
            continue
        variants = PaintLookup.normalize_code_variants(code)
        found = any(v in active[mfr] for v in variants) or (
            mfr in PaintLookup.LEADING_L_MAKES and any('L' + v in active[mfr] for v in variants))
        if found:
            plan.tally['left alone: already found through an existing code'] += 1
            continue
        hexv = _swatch(entry, name, plan.tally)
        norm = PaintLookup.normalize_name(name)
        if norm and not _name_forms(norm).isdisjoint(searched[mfr]):
            plan.tally['new code kept out of the name search: its name is already another code\'s'] += 1
            norm = ''
        plan.new_rows.append({
            'manufacturer': mfr, 'code': code, 'name': name, 'all_names': [name],
            'normalized_names': [norm] if norm else [], 'hex': hexv,
            'color_group': classify_color(name), 'models_list': [], 'sources': [src.name],
        })
        plan.new_by_make[mfr] += 1
        if mfr not in makes:
            plan.new_makes[mfr] += 1
    return plan


def apply_plan(plan, batch_size=1000):
    """Write the plan in one transaction. Re-running finds nothing left to do."""
    with transaction.atomic():
        PaintLookup.all_objects.bulk_create([PaintLookup(**kw) for kw in plan.new_rows],
                                            batch_size=batch_size)
        rows = {r.id: r for r in PaintLookup.all_objects.filter(id__in=[pk for pk, _h, _s in plan.fills])}
        for pk, hexv, sources in plan.fills:
            rows[pk].hex = hexv
            rows[pk].sources = sources
        PaintLookup.all_objects.bulk_update(list(rows.values()), ['hex', 'sources'], batch_size=batch_size)


# -- paint391: planning the scraped sites together -----------------------------

@dataclass
class ScrapedPlan:
    new_rows: list = field(default_factory=list)       # PaintLookup kwargs
    fills: list = field(default_factory=list)          # dicts: id, manufacturer, code, name, new_hex, old_sources, new_sources, site
    tally: Counter = field(default_factory=Counter)
    new_by_make: Counter = field(default_factory=Counter)
    fill_by_make: Counter = field(default_factory=Counter)
    checks: dict = field(default_factory=dict)         # site -> (left out share, held, agreeing share)


def _catalogue_view():
    existing, active, compact, searched = {}, defaultdict(set), defaultdict(set), defaultdict(set)
    for pk, mfr, code, hexv, suppressed, locked, sources, norms, our_name in PaintLookup.all_objects.values_list(
            'id', 'manufacturer', 'code', 'hex', 'suppressed', 'locked_fields', 'sources', 'normalized_names', 'name'):
        existing[(mfr, code)] = (pk, hexv or '', suppressed, locked or [], sources or [], our_name or '')
        if not suppressed:
            active[mfr].add(code)
            compact[mfr].add(_compact(code))
            searched[mfr].update(norms or [])
    for mfr, norm in OperatorPaintCode.objects.values_list('manufacturer', 'normalized_name'):
        searched[mfr].add(norm)            # paint308: his own names count as names carried
    return existing, active, compact, searched


_ENGLISH_COLOUR = re.compile(r'\b(black|white|silver|grey|gray|blue|red|green|yellow|orange|brown|beige|gold|'
                             r'purple|violet|pink|bronze|copper|maroon|burgundy|turquoise|cream|ivory|graphite)\b', re.I)


def _same_paint(entry, names):
    """One of the site's names for the code IS one of `names`: the same words
    once finish words are dropped, or a spelling variant (ratio 0.9). Not
    merely the same colour family, which two different blues share; so a
    translation (Mythos Black, Mythosschwarz) does not count either."""
    theirs = {PaintLookup.normalize_name(p) for p in _parts(entry['names'])} - {''}
    ours = {PaintLookup.normalize_name(p) for p in _parts(names)} - {''}
    return bool(theirs & ours) or any(SequenceMatcher(None, a, b).ratio() >= 0.9 for a in theirs for b in ours)


def _display_name(entry):
    """The name a new row shows: of a site's several names for the paint, the
    first that is plain English with a colour word in it, else the first."""
    parts = [p.strip() for n in sorted(entry['names']) for p in n.split('/') if p.strip()] or sorted(entry['names'])
    for p in parts:
        if p.isascii() and _ENGLISH_COLOUR.search(p):
            return p
    return parts[0]


def plan_scraped(sources):
    """What the scraped sites add together: new codes two sites agree on, and
    empty swatches filled where the swatch is the name's colour. Checks every
    source first (check_scraped). Writes nothing."""
    from lookup.services.paint_resolver import _colour_families, _hex_family
    existing, active, compact, searched = _catalogue_view()
    makes = {m for m, _c in existing}
    plan = ScrapedPlan()
    for src in sources:
        plan.checks[src.name] = check_scraped(src, existing)
    by_site = {s.name: s for s in sources}
    for key in sorted(set().union(*(s.codes for s in sources))):
        mfr, code = key
        votes = {n: s.codes[key] for n, s in by_site.items() if key in s.codes and len(s.codes[key]['strict']) == 1}
        row = existing.get(key)
        if row:
            _plan_fill(plan, key, row, votes, by_site, _colour_families, _hex_family)
            continue
        if not votes:
            plan.tally['left alone: each site that lists it gives it more than one colour'] += 1
            continue
        if mfr not in makes:
            plan.tally['left alone: a make the catalogue does not hold'] += 1
            continue
        if _too_short(mfr, code):
            plan.tally['left alone: a short code (two characters or fewer)'] += 1
            continue
        if any(e['two_tone'] for e in votes.values()):
            plan.tally['left alone: a two tone'] += 1
            continue
        variants = PaintLookup.normalize_code_variants(code)
        if any(v in active[mfr] for v in variants) or (
                mfr in PaintLookup.LEADING_L_MAKES and any('L' + v in active[mfr] for v in variants)):
            plan.tally['left alone: already found through an existing code'] += 1
            continue
        if _compact(code) in compact[mfr]:
            plan.tally['left alone: a code we hold, written another way (NH-737M, NH737M)'] += 1
            continue
        agreeing = name = None
        for anchor in (n for n in NAME_ORDER if n in votes):
            name = _display_name(votes[anchor])
            agreeing = [anchor] + [n for n in NAME_ORDER if n in votes and n != anchor
                                   and _same_paint(votes[n], votes[anchor]['names'])]
            if len({VOICE.get(n, n) for n in agreeing}) >= 2:
                break
        if not agreeing or len({VOICE.get(n, n) for n in agreeing}) < 2:
            plan.tally['left alone: on one site only' if len(votes) == 1 else
                       'left alone: only AutomotiveTouchup and PaintScratch give it' if
                       {VOICE.get(n, n) for n in votes} == {'us lists'} else
                       'left alone: only peinturevoiture and autocolor7 give it' if
                       {VOICE.get(n, n) for n in votes} == {'maker language lists'} else
                       'left alone: the sites give it different colours'] += 1
            continue
        if mfr in TWO_CHARACTER_MAKES and _two_character_pair(name):
            plan.tally['left alone: a two tone pair named only by its codes'] += 1
            continue
        hexv = ''
        said = _colour_families(name)
        for n in (s for s in SWATCH_SITES if s in agreeing):
            h = _swatch(votes[n], name, Counter())
            if h and not contradicts(name, h) and said and _hex_family(h) in said:
                hexv = h
                break
        norm = PaintLookup.normalize_name(name)
        if norm and not _name_forms(norm).isdisjoint(searched[mfr]):
            plan.tally["new code kept out of the name search: its name is already another code's"] += 1
            norm = ''
        plan.new_rows.append({
            'manufacturer': mfr, 'code': code, 'name': name, 'all_names': [name],
            'normalized_names': [norm] if norm else [], 'hex': hexv,
            'color_group': classify_color(name), 'models_list': [],
            'sources': sorted({by_site[n].mark for n in agreeing}),
        })
        plan.new_by_make[mfr] += 1
    # paint395: TWO NEW CODES WITH ONE NAME are both kept out of the name search
    # too. They are two codes for one paint (Land Rover 364, 524 and MCH, all
    # Astral Silver), and a customer's colour name must not land on one of them
    # by chance: each answers its own code, as a code whose name the catalogue
    # already carries does. Measured with autocolor7: 23 of the 329 new codes.
    shared = Counter((r['manufacturer'], r['normalized_names'][0].replace('gray', 'grey'))
                     for r in plan.new_rows if r['normalized_names'])
    for r in plan.new_rows:
        if r['normalized_names'] and shared[(r['manufacturer'], r['normalized_names'][0].replace('gray', 'grey'))] > 1:
            r['normalized_names'] = []
            plan.tally['new code kept out of the name search: another new code has its name'] += 1
    return plan


def _plan_fill(plan, key, row, votes, by_site, _colour_families, _hex_family):
    pk, hexv, hidden, locked, sources, our_name = row
    offers = [n for n in SWATCH_SITES if n in votes and votes[n]['hexes'] and not votes[n]['two_tone']]
    if hexv or not offers:
        return
    if hidden:
        plan.tally['swatch left empty: the code is hidden'] += 1
        return
    if 'hex' in locked:
        plan.tally['swatch left empty: locked'] += 1
        return
    if _too_short(key[0], key[1]):
        plan.tally['swatch left empty: a short code (two characters or fewer)'] += 1
        return
    said = _colour_families(our_name)
    if not said:
        # A swatch on a name that states no colour cannot be checked against
        # it, and it would become the only colour the gates read (paint143).
        plan.tally['swatch left empty: our name states no colour to check it against'] += 1
        return
    why = ''
    for n in offers:
        entry = votes[n]
        new_hex = _swatch(entry, sorted(entry['names'])[0], Counter())
        if not _same_paint(entry, [our_name]):
            why = why or "swatch left empty: the site's name for the code is not ours"
        elif not new_hex or contradicts(our_name, new_hex):
            why = why or 'swatch left empty: no usable swatch'
        elif _hex_family(new_hex) not in said:
            why = 'swatch left empty: the swatch is not the colour our name states'
        else:
            mark = by_site[n].mark
            plan.fills.append({'id': pk, 'manufacturer': key[0], 'code': key[1], 'name': our_name,
                               'new_hex': new_hex, 'old_sources': list(sources),
                               'new_sources': sorted(set(sources) | {mark}), 'site': n})
            plan.fill_by_make[key[0]] += 1
            return
    plan.tally[why] += 1


def apply_scraped(plan, backup_path, batch_size=1000):
    """Save what is about to be written FIRST, then write it in one transaction."""
    with open(backup_path, 'w', encoding='utf-8') as f:
        json.dump({'added': [{k: r[k] for k in ('manufacturer', 'code', 'name', 'hex', 'sources')}
                             for r in plan.new_rows],
                   'filled': plan.fills}, f, ensure_ascii=False, indent=0)
    with transaction.atomic():
        PaintLookup.all_objects.bulk_create([PaintLookup(**kw) for kw in plan.new_rows], batch_size=batch_size)
        rows = {r.id: r for r in PaintLookup.all_objects.filter(id__in=[c['id'] for c in plan.fills])}
        for c in plan.fills:
            rows[c['id']].hex = c['new_hex']
            rows[c['id']].sources = c['new_sources']
        PaintLookup.all_objects.bulk_update(list(rows.values()), ['hex', 'sources'], batch_size=batch_size)


def restore_scraped(backup_path, apply=False):
    """Take out what a scraped top up added, from its backup: a row it added is
    removed, and a swatch it filled emptied again, only while nothing has
    changed them since (and never one locked or given his own names since).
    Writes only with apply=True. Returns (rows to remove, rows left, swatches
    to empty, swatches left)."""
    with open(backup_path, encoding='utf-8') as f:
        data = json.load(f)
    removed, kept_rows, emptied, kept_fills = [], 0, [], 0
    with transaction.atomic():
        for a in data.get('added', []):
            row = PaintLookup.all_objects.filter(manufacturer=a['manufacturer'], code=a['code']).first()
            if row is None or row.name != a['name'] or row.hex != a['hex'] or list(row.sources or []) != a['sources'] \
                    or row.locked_fields or row.operator_names:
                kept_rows += 1
                continue
            if apply:
                row.delete()
            removed.append(a)
        for c in data.get('filled', []):
            row = PaintLookup.all_objects.filter(id=c['id']).first()
            if row is None or row.hex != c['new_hex'] or 'hex' in (row.locked_fields or []):
                kept_fills += 1
                continue
            if apply:
                row.hex, row.sources = '', c['old_sources']
                row.save(update_fields=['hex', 'sources'])
            emptied.append(c)
    return removed, kept_rows, emptied, kept_fills


# -- replacing guessed swatches (paint237) -------------------------------------
#
# THE ONE PLACE A SOURCE MAY REPLACE A SWATCH, and only a swatch that is a GUESS.
#
# A swatch is taken to be a guess when no committed version of the catalogue file
# ever carried it for that code (the history file lists every hex all 11 versions
# had): it was generated by propose_hexes, copied by copy_prefix_hexes, or came
# in some other way after loading. propose_hexes says a generated hex should give
# way to a real one, but the top-up only fills gaps, so it never could.
#
# Measured on production (28 Sep) before this was written: 6,089 guesses where
# bsp has the same colour. Only two kinds are replaced, judged by the colour the
# row's own NAME states, with the gate propose_hexes and the mmw check use:
#   * ours contradicts the name and bsp's agrees                     (76)
#   * both agree, but ours is a stock value on STOCK_SHARED+ codes   (2,550)
#     (a generic silver like #C0C0C0 on many different paints)
# Left alone: ours agrees and is specific to a few codes (1,789, perhaps real data
# from a load that was never committed), bsp's contradicts the name (1,257),
# neither agrees (415), the name states no colour (2). Locked, hidden and short
# codes are never touched. Every replacement is marked with the source and saved
# to a backup file first, so `--restore` can put it back.
STOCK_SHARED = 10


@dataclass
class UpgradePlan:
    source: str
    changes: list = field(default_factory=list)   # dicts: id, manufacturer, code, name, old_hex, new_hex, old_sources, new_sources, why
    tally: Counter = field(default_factory=Counter)


def plan_guess_upgrades(src, history):
    """history: {"make|code": [every hex any committed catalogue file had]}.

    paint391: COLORNDRIVE'S TEN STAND INS ARE GUESSES TOO, though the
    committed files carried them: one value per colour family on thousands of
    paints (COLORNDRIVE_STAND_INS). Measured on the live catalogue of 5 Nov
    2026: 16,278 rows showed one; bsp, chipex and colorndrive's own real
    swatches can replace 806 of them under the rules below, which are
    unchanged. A row whose name states no colour keeps its stand in, which is
    then the only colour the gates can read for it (paint143).
    And a swatch a top up site gave a row (bsp's, or one of TOPUP_MARK's) is
    that site's own: while it is the colour the name states, another site's
    never replaces it, so one real swatch is not swapped for another (bsp's
    near black #060607 is on 284 black paints: a stock value by count, and
    real)."""
    from lookup.services.paint_resolver import _colour_families, _hex_family
    plan = UpgradePlan(src.name)
    mark = src.mark or src.name
    rows = list(PaintLookup.all_objects.exclude(hex='').values_list(
        'id', 'manufacturer', 'code', 'name', 'hex', 'locked_fields', 'sources', 'suppressed'))
    shared = Counter((r[4] or '').upper() for r in rows)
    for pk, mfr, code, name, hexv, locked, sources, hidden in rows:
        name, sources = name or '', list(sources or [])
        if hidden or 'hex' in (locked or []) or mark in sources:
            continue
        stand_in = hexv.upper() in COLORNDRIVE_STAND_INS
        if hexv.upper() in history.get(f'{mfr}|{code}', ()) and not stand_in:
            plan.tally['kept: a swatch a catalogue file carried'] += 1
            continue
        entry = src.codes.get((mfr, code))
        if not entry:
            plan.tally[f'kept: {src.name} does not list the code'] += 1
            continue
        if len(entry['strict']) > 1 or len(code) <= 2:
            plan.tally[f'kept: {src.name} gives the code two colours, or a short code'] += 1
            continue
        their_name = sorted(entry['names'])[0]
        if not _same_colour(their_name, entry, name):
            plan.tally[f'kept: {src.name} may mean another colour'] += 1
            continue
        new_hex = _swatch(entry, their_name, Counter())
        if not new_hex or contradicts(name, new_hex):
            plan.tally[f'kept: {src.name} has no usable swatch'] += 1
            continue
        said = _colour_families(name)
        if not said:
            plan.tally['kept: the name states no colour'] += 1
            continue
        if _hex_family(new_hex) not in said:
            plan.tally[f"kept: {src.name}'s swatch contradicts the name"] += 1
            continue
        ours_agrees = _hex_family(hexv) in said
        if ours_agrees and not stand_in and set(sources) & PaintLookup.TOPUP_SOURCES:
            plan.tally['kept: a swatch a top up site gave, the colour the name states'] += 1
            continue
        stock = shared[hexv.upper()] >= STOCK_SHARED
        if ours_agrees and not stock:
            plan.tally['kept: ours agrees with the name and is specific to a few codes'] += 1
            continue
        plan.changes.append({
            'id': pk, 'manufacturer': mfr, 'code': code, 'name': name, 'old_hex': hexv, 'new_hex': new_hex,
            'old_sources': sources, 'new_sources': sorted(set(sources) | {mark}),
            'why': ("ours is colorndrive's stand in for the colour" if stand_in else
                    'ours contradicts the name' if not ours_agrees else 'ours is a stock value'),
        })
    return plan


def apply_upgrades(plan, backup_path, batch_size=1000):
    """Save every old value to backup_path FIRST, then write in one transaction."""
    import json
    with open(backup_path, 'w', encoding='utf-8') as f:
        json.dump(plan.changes, f, ensure_ascii=False, indent=0)
    with transaction.atomic():
        rows = {r.id: r for r in PaintLookup.all_objects.filter(id__in=[c['id'] for c in plan.changes])}
        for c in plan.changes:
            rows[c['id']].hex = c['new_hex']
            rows[c['id']].sources = c['new_sources']
        PaintLookup.all_objects.bulk_update(list(rows.values()), ['hex', 'sources'], batch_size=batch_size)


def restore_upgrades(backup_path, apply=False):
    """Put back every swatch in a backup, unless the row has changed since or
    its swatch has been locked. Writes only with apply=True; without it the
    same rows are worked out and nothing is saved.
    Returns (the backup's entries put back, left because changed since, left
    because locked)."""
    import json
    with open(backup_path, encoding='utf-8') as f:
        changes = json.load(f)
    restored, changed, locked = [], 0, 0
    with transaction.atomic():
        for c in changes:
            row = PaintLookup.all_objects.filter(id=c['id']).first()
            if row is None or row.hex != c['new_hex']:
                changed += 1
                continue
            # paint308: A LOCKED SWATCH IS A DECISION, AND AN UNDO MUST NOT UNDO IT.
            # The upgrade never touches a locked swatch, but its undo did: a row
            # upgraded and THEN locked (the operator looked at the new swatch and
            # kept it) still holds the swatch the backup expects, so the test
            # above let it through and the old guess went back over the lock.
            # Reproduced on a scratch copy: of two upgraded rows, one locked
            # since, both were put back. The same rule as the loader (paint160).
            if 'hex' in (row.locked_fields or []):
                locked += 1
                continue
            # paint308: AND NOTHING IS WRITTEN UNLESS ASKED. --restore wrote at
            # once, the only mode of this command with no preview.
            if apply:
                row.hex, row.sources = c['old_hex'], c['old_sources']
                row.save(update_fields=['hex', 'sources'])
            restored.append(c)
    return restored, changed, locked
