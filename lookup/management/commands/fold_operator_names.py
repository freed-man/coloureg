"""Fold the operator's own paint names into the catalogue (paint202).

Each OperatorPaintCode entry is a name the operator typed while fulfilling a
lookup. paint161 gave catalogue rows an `operator_names` field for exactly
these, merged into the names every lookup matches against, but nothing ever
moved the entries across. This does, through PaintLookup.fold_operator_name,
the same method every new fulfilment now uses.

paint203: an entry finds its row the way a lookup does (the slash rule, the
Mercedes suffix, the Honda hyphen, the VW/Audi L prefix), through
PaintLookup.resolve_operator_code. Every entry that got there by a rule is
listed with the row it landed on, so the preview shows exactly what the rules
changed before anything is written.

PREVIEWS BY DEFAULT: nothing is written without --apply.

    manage.py fold_operator_names                                    # preview all
    manage.py fold_operator_names --make vauxhall --code KKJ          # preview one
    manage.py fold_operator_names --make vauxhall --code KKJ --apply  # write one
    manage.py fold_operator_names --apply                             # write all

Not to be confused with backfill_operator_codes, which must never run
without --dry-run.
"""
from collections import defaultdict

from django.core.management.base import BaseCommand

from lookup.models import OperatorPaintCode, PaintLookup

ORDER = ('folded', 'already known', 'not in catalogue', 'contradicts',
         'needs review', 'suppressed', 'no name')


def _finds_today(make, name):
    """The code a provider's name-only answer resolves to in the catalogue now."""
    code = PaintLookup.code_from_name(make, name)[0]
    PaintLookup.take_last_ambiguity()   # read-and-clear: never leave the stash set
    return code


class Command(BaseCommand):
    help = "Fold the operator's paint names into the catalogue. Previews unless --apply."

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Write. Without it, nothing is changed.')
        parser.add_argument('--make', default='', help='Only this make.')
        parser.add_argument('--code', default='', help='Only this code.')

    def handle(self, *args, **opt):
        qs = OperatorPaintCode.objects.all().order_by('manufacturer', 'code')
        if opt['make']:
            qs = qs.filter(manufacturer=PaintLookup.normalize_manufacturer(opt['make']))
        if opt['code']:
            qs = qs.filter(code__iexact=opt['code'].strip())
        out = defaultdict(list)
        notes = {}
        for e in qs:
            row, rule = PaintLookup.resolve_operator_code(e.manufacturer, e.code)
            note = ''
            if row is not None and rule != 'exact':
                note = f'   -> {row.code}, by the {rule} rule'
            if e.needs_review:
                outcome = 'needs review'
            else:
                # Read BEFORE the fold: once written, the name finds the new row.
                before = (_finds_today(e.manufacturer, e.colour_name)
                          if row is not None and e.colour_name else None)
                outcome = PaintLookup.fold_operator_name(
                    e.manufacturer, e.code, e.colour_name, apply=opt['apply'])
                # EXISTS IS NOT SAME, and a fold can take an answer away: if the
                # name already finds a DIFFERENT code, adding it to this row
                # gives the matcher two, and it declines rather than guess.
                if outcome == 'folded' and before and before.upper() != row.code.upper():
                    note += (f'   NOTE: this name finds {before} today; with this fold '
                             f'it may find two codes and decline')
            if outcome == 'contradicts' and row is not None:
                note += f'   catalogue says {row.name!r}'
            notes[e.pk] = note
            out[outcome].append(e)
        head = 'WRITTEN' if opt['apply'] else 'PREVIEW, nothing written'
        self.stdout.write(f'{head}: {sum(len(v) for v in out.values())} operator entries')
        for k in ORDER:
            if out.get(k):
                label = 'would fold' if (k == 'folded' and not opt['apply']) else k
                self.stdout.write(f'  {label:<18}{len(out[k]):>5}')
        for k in ('folded', 'contradicts', 'already known', 'not in catalogue',
                  'needs review', 'suppressed'):
            for e in out.get(k, []):
                # 'already known' is listed only where a rule found the row:
                # the plain ones were already right under paint202.
                if k == 'already known' and not notes[e.pk]:
                    continue
                label = 'would fold' if (k == 'folded' and not opt['apply']) else k
                self.stdout.write(f'    {label}: {e.manufacturer}/{e.code} {e.colour_name!r}{notes[e.pk]}')
