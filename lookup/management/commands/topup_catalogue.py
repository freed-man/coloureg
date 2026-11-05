"""Top up the paint catalogue from a scraped source (paint236).

Adds only what the catalogue lacks and never changes what it has: see
lookup/services/catalogue_topup.py for the rules. A preview by default; nothing
is written without --apply.

    python manage.py topup_catalogue bsp etc\\bsp-paint-codes-2026-09-28.csv
    python manage.py topup_catalogue bsp etc\\bsp-paint-codes-2026-09-28.csv --apply

paint391: THE SITES PAINTSCRAPER SCRAPES, READ TOGETHER from its data folder
(chipex, colorndrive, peinturevoiture, AutomotiveTouchup and PaintScratch,
the newest file of each): every file is checked first, a new code goes in only
where two of the sites give it the same paint, and an empty swatch is filled
only where the swatch is the colour the name states. The backup lists what was
written, so --restore can take it out again.

    python manage.py topup_catalogue scraped "C:\\path\\to\\paintscraper\\data"             preview
    python manage.py topup_catalogue scraped "C:\\path\\to\\paintscraper\\data" --apply     write, backup in etc\\
    python manage.py topup_catalogue --restore etc\\BACKUP.json                            preview of the undo
    python manage.py topup_catalogue --restore etc\\BACKUP.json --apply                    undo

Run it where the file is: from the PC, env.py points it at the live catalogue.
Source files belong in etc/, which .gitignore keeps out of the (public) repo.
"""
import os
from datetime import datetime

from django.core.management.base import BaseCommand, CommandError

from lookup.services import catalogue_topup as ct

READERS = {'bsp': ct.read_bsp}


class Command(BaseCommand):
    help = "Top up the paint catalogue from a scraped source: add what it lacks, change nothing it has"

    def add_arguments(self, parser):
        parser.add_argument('source', nargs='?', choices=sorted(READERS) + ['scraped'])
        parser.add_argument('path', nargs='?', help="the source's file, or for scraped the folder holding the files")
        parser.add_argument('--apply', action='store_true', help='write; without it, only a preview')
        parser.add_argument('--restore', metavar='BACKUP',
                            help="take out what a scraped top up wrote; only a preview unless --apply is given")

    def handle(self, *args, **opts):
        if opts['restore']:
            return self._restore(opts)
        if not (opts['source'] and opts['path']):
            raise CommandError('Give the source and its file (for scraped, the folder), or --restore BACKUP.')
        if opts['source'] == 'scraped':
            return self._scraped(opts)
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

    def _scraped(self, opts):
        """paint391: every scraped site in the folder, planned together."""
        w = self.stdout.write
        folder = opts['path']
        if not os.path.isdir(folder):
            raise CommandError(f'No such folder: {folder}')
        files = ct.find_scraped(folder)
        if not files:
            raise CommandError(f'No scraped files in {folder}: they are named like chipex_2026-10-28.jsonl.')
        try:
            sources = [ct.SCRAPED_READERS[site](path) for site, path in files.items()]
            plan = ct.plan_scraped(sources)
        except ct.SourceChanged as exc:
            raise CommandError(str(exc)) from exc
        w(('SCRAPED TOP-UP, WRITING' if opts['apply'] else
           'SCRAPED TOP-UP PREVIEW, nothing written (add --apply to write)') + f': {folder}')
        for src in sources:
            out, held, agree = plan.checks[src.name]
            base = ct.SCRAPED_BASELINE[src.name]
            w(f'\n{src.name}: {", ".join(os.path.basename(p) for p in src.files)}, {src.rows_read:,} rows read')
            w(f'  checked: {out:.0%} of its codes left out (28 Oct 2026: {base["left_out"]:.0%}); '
              f'of the {held:,} codes we hold, {agree:.0%} the same colour as ours (28 Oct 2026: {base["agree"]:.0%})')
            for why, n in src.left_out.most_common(4):
                w(f'  {n:>8,}  left out: {why}')
        if len(sources) < 2:
            w('\nOnly one site found: a new code needs two sites that agree, so only swatches can be filled.')
        n_new = len(plan.new_rows)
        w(f'\nNew codes two sites agree on:     {n_new:>8,}  ({sum(1 for r in plan.new_rows if r["hex"]):,} with a swatch)')
        w(f'Empty swatches filled:            {len(plan.fills):>8,}')
        w('\nLeft alone:')
        for why, n in sorted(plan.tally.items()):
            w(f'  {n:>8,}  {why}')
        if plan.new_by_make:
            w('\nNew codes by make: ' + ', '.join(f'{m} {n:,}' for m, n in plan.new_by_make.most_common(20)))
        if plan.fill_by_make:
            w('Swatches filled by make: ' + ', '.join(f'{m} {n:,}' for m, n in plan.fill_by_make.most_common(12)))
        for r in plan.new_rows[:8]:
            w(f'  e.g. new: {r["manufacturer"]} {r["code"]} "{r["name"]}" {r["hex"] or "(no swatch)"} '
              f'({", ".join(r["sources"])})')
        for c in plan.fills[:5]:
            w(f'  e.g. swatch: {c["manufacturer"]} {c["code"]} "{c["name"]}" {c["new_hex"]} ({c["site"]})')
        if opts['apply'] and (plan.new_rows or plan.fills):
            os.makedirs('etc', exist_ok=True)
            backup = os.path.abspath(os.path.join('etc', f'topup_scraped_backup_{datetime.now():%Y%m%d_%H%M%S}.json'))
            ct.apply_scraped(plan, backup)
            w(f'\nWritten: {n_new:,} codes added, {len(plan.fills):,} swatches filled.')
            w(f'Backup of what was written: {backup}')
            w(f'To undo: python manage.py topup_catalogue --restore "{backup}" --apply   (without --apply, a preview)')

    def _restore(self, opts):
        w = self.stdout.write
        path = opts['restore']
        if not os.path.exists(path):
            raise CommandError(f'No such file: {path}')
        removed, kept_rows, emptied, kept_fills = ct.restore_scraped(path, apply=opts['apply'])
        if not opts['apply']:
            w(f'TOP-UP RESTORE PREVIEW, nothing written (add --apply to write): {path}')
            w(f'\nCodes to take out:                {len(removed):>8,}')
            w(f'Swatches to empty again:          {len(emptied):>8,}')
            w(f'Left alone, changed since:        {kept_rows + kept_fills:>8,}')
            for a in removed[:5]:
                w(f'  e.g. {a["manufacturer"]} {a["code"]} "{a["name"]}"')
            return
        w(f'Restored: {len(removed):,} codes taken out, {len(emptied):,} swatches emptied'
          + (f'; {kept_rows + kept_fills:,} left alone because they changed since' if kept_rows + kept_fills else '')
          + '.')
