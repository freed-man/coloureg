"""paint396: the years the paint sites list each paint for, for the manual
queue's paint hints (paint329) and nothing else.

WHY. A model's paints in one colour run long: the card of a silver Fiesta
listed eight and "30 more". The sites paintscraper reads say which years each
paint was offered: chipex filters every model page and every brand's All Models
page by year, and colorndrive gives each colour page a FOR YEARS range.
Measured on 6 Nov 2026 on the 3,836 cars of his export of 26 Oct whose code is
known, with the paints listed for the car's year first: the right code is among
the card's eight for 1,409 cars, against 1,083 before, and a short list (one to
five) holds it for 797, against 385.

WHY NOTHING IS LEFT OUT. Leaving out the paints not listed for the car's year
would have hidden the right code on 15 of those cars: the sites' years have
gaps (a BMW i3s colour listed only to 2018, Fiat codes reused thirty years
apart, a range that starts two years after the car). So queue_hints lists the
year's paints first and the rest after them, named as not listed for that year.

WHY THESE TWO SITES. Adding AutomotiveTouchup's, autocolor7's or
peinturevoiture's years made the lists longer, not better (US model years, and
a code's whole span over every model), on the same cars.

A YEAR IS NO ANSWER (trap 323): nothing that names a code reads these years,
only queue_hints. The field is replaced whole by every run of `paint_years`
from the newest files, and every write keeps a backup it can be undone from.
"""
import json
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from django.db import transaction

from lookup.models import PaintLookup
from lookup.services import catalogue_topup as ct

YEAR_SITES = ('chipex', 'colorndrive')
# The fields each reader needs, as paintscraper wrote them on 28 Oct 2026.
YEAR_FIELDS = {
    'chipex': ('manufacturer', 'year', 'color_codes'),
    'colorndrive': ('manufacturer', 'color_code', 'aux_code', 'color_codes', 'for_years'),
}
FIRST_YEAR, LAST_YEAR = 1900, 2030
# What each file gave on 6 Nov 2026 (the files of 28 Oct, read by the readers
# below): the share of its rows that gave years, and how many codes. A file
# giving years on far fewer of its rows, or for far fewer codes, is refused:
# its layout or its scrape has changed.
YEAR_BASELINE = {
    'chipex': {'with_years': 0.816, 'codes': 37346},          # 398,480 of 488,638 rows
    'colorndrive': {'with_years': 0.940, 'codes': 65352},     # 162,840 of 173,264 rows
}
YEAR_FILE_DATE = '28 Oct 2026'
WITH_YEARS_MARGIN = 0.15
CODES_MIN_SHARE = 0.5
BACKUP_KIND = 'paint396 years'
_FOR_YEARS = re.compile(r'^from\s*(\d*)\s*to\s*(\d*)$')    # "from to" and "from 2004 to" have no space to spare


class SourceChanged(Exception):
    """A file that does not look like the one its reader was written for."""


@dataclass
class YearSource:
    name: str
    files: list
    rows_read: int = 0
    rows_with_years: int = 0
    left_out: Counter = field(default_factory=Counter)
    codes: dict = field(default_factory=lambda: defaultdict(set))   # (make, code) -> {(first, last)}


def _rows(site, path):
    name = os.path.basename(path)
    with open(path, encoding='utf-8') as f:
        for n, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError as exc:
                raise SourceChanged(f'{site}: line {n} of {name} is not JSON. Nothing was planned.') from exc
            missing = [k for k in YEAR_FIELDS[site] if k not in row]
            if missing:
                raise SourceChanged(
                    f'{site}: line {n} of {name} has no field "{missing[0]}", which its years need. The file\'s '
                    f'layout has changed since {YEAR_FILE_DATE}: check paintscraper\'s notes for it. '
                    f'Nothing was planned.')
            yield row


def _year(value):
    """A year as a site writes it: an int from 1900 to 2030, None for no year
    (blank or 0), and False for anything else."""
    text = '' if value is None else str(value).strip()
    if text in ('', '0'):
        return None
    try:
        y = int(text)
    except ValueError:
        return False
    return y if FIRST_YEAR <= y <= LAST_YEAR else False


def _codes(items):
    """The codes of a row, as the catalogue keys them (upper case, no spaces round)."""
    return {c.strip().upper() for c in items if c and c.strip()}


def read_chipex_years(path):
    """chipex: every page filtered to a year (a model's, or a brand's All
    Models page), that year for each code the page lists."""
    src = YearSource('chipex', [path])
    for row in _rows('chipex', path):
        src.rows_read += 1
        y = _year(row['year'])
        if y is None:
            src.left_out['a page not filtered to a year'] += 1
            continue
        if y is False:
            src.left_out['a year outside 1900 to 2030'] += 1
            continue
        mk = ct._scraped_make('chipex', row['manufacturer'])
        if not mk:
            src.left_out['a make left out'] += 1
            continue
        codes = _codes((row['color_codes'] or '').split(','))
        if not codes:
            src.left_out['no code'] += 1
            continue
        src.rows_with_years += 1
        for c in codes:
            src.codes[(mk, c)].add((y, y))
    return src


def read_colorndrive_years(path):
    """Color N Drive: the colour page's FOR YEARS ("from 2004 to 2025"; "from
    2004 to" runs on; "from to" and "from 0 to 0" give none) for its COLOR CODE
    whole, its AUX CODE split at "/" and the list's split codes. The file of
    pages no list shows (<name>_sitemap.jsonl) is read with it when it is there."""
    src = YearSource('colorndrive', [path])
    side = path[:-len('.jsonl')] + '_sitemap.jsonl' if path.endswith('.jsonl') else ''
    if side and os.path.exists(side):
        src.files.append(side)
    for p in src.files:
        for row in _rows('colorndrive', p):
            src.rows_read += 1
            m = _FOR_YEARS.match((row['for_years'] or '').strip())
            if not m:
                src.left_out['FOR YEARS written another way'] += 1
                continue
            a, b = _year(m.group(1)), _year(m.group(2))
            if a is False or b is False:
                src.left_out['a year outside 1900 to 2030'] += 1
                continue
            if a is None and b is None:
                src.left_out['a page with no years'] += 1
                continue
            if a is not None and b is not None and b < a:
                src.left_out['a range that ends before it starts'] += 1
                continue
            mk = ct._scraped_make('colorndrive', row['manufacturer'])
            if not mk:
                src.left_out['a make left out'] += 1
                continue
            codes = _codes([row['color_code'] or ''] + (row['aux_code'] or '').split('/')
                           + (row['color_codes'] or '').split(','))
            if not codes:
                src.left_out['no code'] += 1
                continue
            src.rows_with_years += 1
            for c in codes:
                src.codes[(mk, c)].add((a, b))
    return src


YEAR_READERS = {'chipex': read_chipex_years, 'colorndrive': read_colorndrive_years}


def find_year_files(folder):
    """The newest chipex and colorndrive files under `folder`: {site: path}."""
    found = ct.find_scraped(folder)
    return {site: found[site] for site in YEAR_SITES if site in found}


def check_years(src):
    """Raise SourceChanged unless the file looks like the one its reader was
    written for (YEAR_BASELINE). Returns the share of its rows that gave years."""
    base = YEAR_BASELINE[src.name]
    files = ', '.join(os.path.basename(p) for p in src.files)
    if not src.codes:
        raise SourceChanged(f'{src.name}: {files} gave no years at all. Nothing was planned.')
    share = src.rows_with_years / max(src.rows_read, 1)
    if share < base['with_years'] - WITH_YEARS_MARGIN:
        raise SourceChanged(
            f'{src.name}: {files} gives years on {share:.0%} of its rows (the file of {YEAR_FILE_DATE}: '
            f'{base["with_years"]:.0%}). Its years may be written another way now: check paintscraper\'s notes '
            f'for it. Nothing was planned.')
    if len(src.codes) < base['codes'] * CODES_MIN_SHARE:
        raise SourceChanged(
            f'{src.name}: {files} gives years for {len(src.codes):,} codes (the file of {YEAR_FILE_DATE}: '
            f'{base["codes"]:,}). The scrape may have stopped early: check its log. Nothing was planned.')
    return share


def merge_ranges(ranges):
    """Ranges as [first, last] pairs (None at an open end), merged where they
    overlap or meet (2004 to 2008 and 2009 to 2012 are one), in order."""
    lo, hi = -10 ** 9, 10 ** 9
    spans = sorted((lo if a is None else a, hi if b is None else b) for a, b in ranges)
    merged = []
    for a, b in spans:
        if merged and a <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return [[None if a == lo else a, None if b == hi else b] for a, b in merged]


def years_cover(years, year):
    """Whether a paint's stored years take in a car's year: True or False, or
    None when either is not known (no years stored, or no year for the car),
    which the hints read as listed. A car's year of 0, or outside 1900 to
    2030, is no year."""
    try:
        y = int(year)
    except (TypeError, ValueError):
        return None
    if not FIRST_YEAR <= y <= LAST_YEAR:
        return None
    known = False
    for pair in years or ():
        try:
            a, b = pair
        except (TypeError, ValueError):
            continue
        if not (a is None or isinstance(a, int)) or not (b is None or isinstance(b, int)):
            continue
        known = True
        if (a is None or a <= y) and (b is None or y <= b):
            return True
    return False if known else None


def years_text(years):
    """A paint's years as the preview prints them: 2004 to 2008, 2011, 2015 on."""
    out = []
    for a, b in years or ():
        if a is None and b is None:
            out.append('every year')
        elif a is None:
            out.append(f'to {b}')
        elif b is None:
            out.append(f'{a} on')
        else:
            out.append(str(a) if a == b else f'{a} to {b}')
    return ', '.join(out)


@dataclass
class YearPlan:
    changes: list = field(default_factory=list)      # {'id', 'manufacturer', 'code', 'old', 'new'}
    tally: Counter = field(default_factory=Counter)
    given_by_make: Counter = field(default_factory=Counter)


def plan_years(sources):
    """Every catalogue row's years from the sites, hidden rows too (one shown
    again later has them): what would change. The years replace what the row
    holds, so a paint the newest files no longer give years for loses them."""
    want = defaultdict(set)
    for src in sources:
        for key, ranges in src.codes.items():
            want[key] |= ranges
    plan = YearPlan()
    # Not .iterator(): on PostgreSQL that is a server side cursor, which breaks
    # behind Neon's pooler (models.py, code_from_name). The rows are small.
    rows = PaintLookup.all_objects.values_list('id', 'manufacturer', 'code', 'years').order_by('id')
    for pk, mk, code, old in rows:
        new = merge_ranges(want.get((mk, (code or '').strip().upper()), ()))
        old = old or []
        if new == old:
            plan.tally['already the same' if new else 'no site gives years'] += 1
            continue
        if not old:
            kind = 'given years'
            plan.given_by_make[mk] += 1
        elif not new:
            kind = 'years taken away (no site gives them now)'
        else:
            kind = 'years changed'
        plan.tally[kind] += 1
        plan.changes.append({'id': pk, 'manufacturer': mk, 'code': code, 'old': old, 'new': new})
    return plan


def apply_years(plan, backup_path, batch_size=1000):
    """Save every change to backup_path FIRST, then write in one transaction.
    A row whose years are no longer those the plan read is left alone.
    Returns (written, left alone)."""
    with open(backup_path, 'w', encoding='utf-8') as f:
        json.dump({'kind': BACKUP_KIND, 'changes': plan.changes}, f, ensure_ascii=False, indent=0)
    with transaction.atomic():
        now = dict(PaintLookup.all_objects.values_list('id', 'years'))
        rows, left = [], 0
        for c in plan.changes:
            if c['id'] not in now or (now[c['id']] or []) != c['old']:
                left += 1
                continue
            rows.append(PaintLookup(id=c['id'], years=c['new']))
        PaintLookup.all_objects.bulk_update(rows, ['years'], batch_size=batch_size)
    return len(rows), left


def restore_years(backup_path, apply=False):
    """Put back the years a backup holds, on every row that still has the years
    that run wrote. Writes only with apply=True. Returns (put back, left alone
    because changed since)."""
    try:
        with open(backup_path, encoding='utf-8') as f:
            data = json.load(f)
    except ValueError:
        data = None
    if not isinstance(data, dict) or data.get('kind') != BACKUP_KIND:
        raise ValueError(f'{backup_path} is not a backup paint_years wrote.')
    with transaction.atomic():
        now = dict(PaintLookup.all_objects.values_list('id', 'years'))
        back, left = [], 0
        for c in data['changes']:
            if c['id'] not in now or (now[c['id']] or []) != c['new']:
                left += 1
                continue
            back.append(c)
        if apply:
            PaintLookup.all_objects.bulk_update([PaintLookup(id=c['id'], years=c['old']) for c in back], ['years'],
                                                batch_size=1000)
    return back, left
