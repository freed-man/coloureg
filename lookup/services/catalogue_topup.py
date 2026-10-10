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
    """history: {"make|code": [every hex any committed catalogue file had]}."""
    from lookup.services.paint_resolver import _colour_families, _hex_family
    plan = UpgradePlan(src.name)
    rows = list(PaintLookup.all_objects.exclude(hex='').values_list(
        'id', 'manufacturer', 'code', 'name', 'hex', 'locked_fields', 'sources', 'suppressed'))
    shared = Counter((r[4] or '').upper() for r in rows)
    for pk, mfr, code, name, hexv, locked, sources, hidden in rows:
        name, sources = name or '', list(sources or [])
        if hidden or 'hex' in (locked or []) or src.name in sources:
            continue
        if hexv.upper() in history.get(f'{mfr}|{code}', ()):
            plan.tally['kept: a swatch a catalogue file carried'] += 1
            continue
        entry = src.codes.get((mfr, code))
        if not entry:
            plan.tally['kept: bsp does not list the code'] += 1
            continue
        if len(entry['strict']) > 1 or len(code) <= 2:
            plan.tally['kept: bsp gives the code two colours, or a short code'] += 1
            continue
        their_name = sorted(entry['names'])[0]
        if not _same_colour(their_name, entry, name):
            plan.tally['kept: bsp may mean another colour'] += 1
            continue
        new_hex = _swatch(entry, their_name, Counter())
        if not new_hex or contradicts(name, new_hex):
            plan.tally['kept: bsp has no usable swatch'] += 1
            continue
        said = _colour_families(name)
        if not said:
            plan.tally['kept: the name states no colour'] += 1
            continue
        if _hex_family(new_hex) not in said:
            plan.tally["kept: bsp's swatch contradicts the name"] += 1
            continue
        ours_agrees = _hex_family(hexv) in said
        stock = shared[hexv.upper()] >= STOCK_SHARED
        if ours_agrees and not stock:
            plan.tally['kept: ours agrees with the name and is specific to a few codes'] += 1
            continue
        plan.changes.append({
            'id': pk, 'manufacturer': mfr, 'code': code, 'name': name, 'old_hex': hexv, 'new_hex': new_hex,
            'old_sources': sources, 'new_sources': sorted(set(sources) | {src.name}),
            'why': 'ours contradicts the name' if not ours_agrees else 'ours is a stock value',
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
