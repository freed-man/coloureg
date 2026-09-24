"""Fold the operator's own paint names into the catalogue (paint202).

Each OperatorPaintCode entry is a name the operator typed while fulfilling a
lookup. paint161 gave catalogue rows an `operator_names` field for exactly
these, merged into the names every lookup matches against, but nothing ever
moved the entries across. This does, through PaintLookup.fold_operator_name,
the same method every new fulfilment now uses.

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
        for e in qs:
            if e.needs_review:
                outcome = 'needs review'
            else:
                outcome = PaintLookup.fold_operator_name(
                    e.manufacturer, e.code, e.colour_name, apply=opt['apply'])
            out[outcome].append(e)
        head = 'WRITTEN' if opt['apply'] else 'PREVIEW, nothing written'
        self.stdout.write(f'{head}: {sum(len(v) for v in out.values())} operator entries')
        for k in ORDER:
            if out.get(k):
                label = 'would fold' if (k == 'folded' and not opt['apply']) else k
                self.stdout.write(f'  {label:<18}{len(out[k]):>5}')
        for k in ('folded', 'contradicts', 'not in catalogue', 'needs review', 'suppressed'):
            for e in out.get(k, []):
                note = ''
                if k == 'contradicts':
                    row = PaintLookup.all_objects.filter(manufacturer=e.manufacturer,
                                                         code__iexact=e.code).first()
                    note = f'   catalogue says {row.name!r}' if row else ''
                label = 'would fold' if (k == 'folded' and not opt['apply']) else k
                self.stdout.write(f'    {label}: {e.manufacturer}/{e.code} {e.colour_name!r}{note}')
