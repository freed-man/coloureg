"""Top up the paint catalogue from a scraped source (paint236).

Adds only what the catalogue lacks and never changes what it has: see
lookup/services/catalogue_topup.py for the rules. A preview by default; nothing
is written without --apply.

    python manage.py topup_catalogue bsp etc\\bsp-paint-codes-2026-09-28.csv
    python manage.py topup_catalogue bsp etc\\bsp-paint-codes-2026-09-28.csv --apply

Run it where the file is: from the PC, env.py points it at the live catalogue.
Source files belong in etc/, which .gitignore keeps out of the (public) repo.
"""
from django.core.management.base import BaseCommand, CommandError

from lookup.services import catalogue_topup as ct

READERS = {'bsp': ct.read_bsp}


class Command(BaseCommand):
    help = "Top up the paint catalogue from a scraped source: add what it lacks, change nothing it has"

    def add_arguments(self, parser):
        parser.add_argument('source', choices=sorted(READERS))
        parser.add_argument('path')
        parser.add_argument('--apply', action='store_true', help='write; without it, only a preview')

    def handle(self, *args, **opts):
        try:
            src = READERS[opts['source']](opts['path'])
        except FileNotFoundError as exc:
            raise CommandError(f'No such file: {opts["path"]}') from exc
        plan = ct.plan_topup(src)
        w = self.stdout.write
        w(('TOP-UP, WRITING' if opts['apply'] else 'TOP-UP PREVIEW, nothing written (add --apply to write)')
          + f': {src.name}, {src.rows_read:,} rows read')
        if src.left_out:
            w('\nLeft out while reading:')
            for why, n in src.left_out.most_common():
                w(f'  {n:>8,}  {why}')
        n_new = len(plan.new_rows)
        no_hex = sum(1 for r in plan.new_rows if not r['hex'])
        w(f'\nNew codes to add:                 {n_new:>8,}  ({n_new - no_hex:,} with a swatch)')
        w(f'Existing codes given a swatch:    {len(plan.fills):>8,}')
        w('\nOther outcomes:')
        for why, n in sorted(plan.tally.items()):
            w(f'  {n:>8,}  {why}')
        if plan.new_by_make:
            w('\nMost new codes by make: ' + ', '.join(f'{m} {n:,}' for m, n in plan.new_by_make.most_common(12)))
        if plan.new_makes:
            w('Makes new to the catalogue: ' + ', '.join(f'{m} {n:,}' for m, n in plan.new_makes.most_common(20))
              + (f' and {len(plan.new_makes) - 20} more' if len(plan.new_makes) > 20 else ''))
        if plan.fill_by_make:
            w('Most swatches filled by make: ' + ', '.join(f'{m} {n:,}' for m, n in plan.fill_by_make.most_common(12)))
        for r in plan.new_rows[:5]:
            w(f'  e.g. new: {r["manufacturer"]} {r["code"]} "{r["name"]}" {r["hex"] or "(no swatch)"}')
        if opts['apply']:
            ct.apply_plan(plan)
            w(f'\nWritten: {n_new:,} codes added, {len(plan.fills):,} swatches filled, all marked "{src.name}".')
