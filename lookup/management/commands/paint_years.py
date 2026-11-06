"""Load the years the paint sites list each paint for (paint396), for the
manual queue's paint hints only: see lookup/services/catalogue_years.py.

It reads the newest chipex and colorndrive files in paintscraper's data folder
(chipex_YYYY-MM-DD.jsonl, colorndrive_YYYY-MM-DD.jsonl and its _sitemap file),
checks each against the file of 28 Oct 2026, and replaces each paint's years
with what they list now. A preview by default; nothing is written without
--apply, and the backup it writes first lists every change, so --restore can
put the years back.

    python manage.py paint_years "C:\\path\\to\\paintscraper\\data"             preview
    python manage.py paint_years "C:\\path\\to\\paintscraper\\data" --apply     write, backup in etc\\
    python manage.py paint_years --restore etc\\BACKUP.json                     preview of the undo
    python manage.py paint_years --restore etc\\BACKUP.json --apply             undo

Run it where the files are: from the PC, env.py points it at the live catalogue.
"""
import os
from datetime import datetime

from django.core.management.base import BaseCommand, CommandError

from lookup.services import catalogue_years as cy


class Command(BaseCommand):
    help = "Load the years chipex and colorndrive list each paint for, for the manual queue's paint hints"

    def add_arguments(self, parser):
        parser.add_argument('folder', nargs='?', help="paintscraper's data folder (or any folder holding the files)")
        parser.add_argument('--apply', action='store_true', help='write; without it, only a preview')
        parser.add_argument('--restore', metavar='BACKUP',
                            help='put back the years a run wrote over; only a preview unless --apply is given')

    def handle(self, *args, **opts):
        if opts['restore']:
            return self._restore(opts)
        folder = opts['folder']
        if not folder:
            raise CommandError('Give the folder holding the scraped files, or --restore BACKUP.')
        if not os.path.isdir(folder):
            raise CommandError(f'No such folder: {folder}')
        files = cy.find_year_files(folder)
        missing = [s for s in cy.YEAR_SITES if s not in files]
        if missing:
            raise CommandError(f'No {" or ".join(missing)} file in {folder}: the years need both chipex and '
                               f'colorndrive (named like chipex_2026-10-28.jsonl). Nothing was planned.')
        try:
            sources = [cy.YEAR_READERS[site](path) for site, path in files.items()]
            shares = {src.name: cy.check_years(src) for src in sources}
        except cy.SourceChanged as exc:
            raise CommandError(str(exc)) from exc
        plan = cy.plan_years(sources)
        w = self.stdout.write
        w(('PAINT YEARS, WRITING' if opts['apply'] else
           'PAINT YEARS PREVIEW, nothing written (add --apply to write)') + f': {folder}')
        for src in sources:
            base = cy.YEAR_BASELINE[src.name]
            w(f'\n{src.name}: {", ".join(os.path.basename(p) for p in src.files)}, {src.rows_read:,} rows read')
            w(f'  checked: years on {shares[src.name]:.0%} of its rows ({cy.YEAR_FILE_DATE}: {base["with_years"]:.0%}); '
              f'{len(src.codes):,} codes with years ({cy.YEAR_FILE_DATE}: {base["codes"]:,})')
            for why, n in src.left_out.most_common(4):
                w(f'  {n:>8,}  left out: {why}')
        w('')
        for kind, label in (('given years', 'Paints given years:'),
                            ('years changed', 'Paints whose years change:'),
                            ('years taken away (no site gives them now)', 'Paints whose years go (no site gives them now):'),
                            ('already the same', 'Paints already the same:'),
                            ('no site gives years', 'Paints no site gives years for:')):
            w(f'{label:<50}{plan.tally[kind]:>8,}')
        if plan.given_by_make:
            w('\nGiven years, by make: ' + ', '.join(f'{m} {n:,}' for m, n in plan.given_by_make.most_common(12)))
        for c in plan.changes[:6]:
            w(f'  e.g. {c["manufacturer"]} {c["code"]}: {cy.years_text(c["new"]) or "(none)"}'
              + (f' (was {cy.years_text(c["old"])})' if c['old'] else ''))
        if opts['apply'] and plan.changes:
            os.makedirs('etc', exist_ok=True)
            backup = os.path.abspath(os.path.join('etc', f'paint_years_backup_{datetime.now():%Y%m%d_%H%M%S}.json'))
            written, left = cy.apply_years(plan, backup)
            w(f'\nWritten: years on {written:,} paints' + (f'; {left:,} left alone, changed meanwhile' if left else '')
              + '.')
            w(f'Backup of what was written: {backup}')
            w(f'To undo: python manage.py paint_years --restore "{backup}" --apply   (without --apply, a preview)')
        elif opts['apply']:
            w('\nNothing to write.')

    def _restore(self, opts):
        w = self.stdout.write
        path = opts['restore']
        if not os.path.exists(path):
            raise CommandError(f'No such file: {path}')
        try:
            back, left = cy.restore_years(path, apply=opts['apply'])
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        if not opts['apply']:
            w(f'PAINT YEARS RESTORE PREVIEW, nothing written (add --apply to write): {path}')
            w(f'\nPaints whose years go back:       {len(back):>8,}')
            w(f'Left alone, changed since:        {left:>8,}')
            for c in back[:5]:
                w(f'  e.g. {c["manufacturer"]} {c["code"]}: back to {cy.years_text(c["old"]) or "(none)"}')
            return
        w(f'Restored: years put back on {len(back):,} paints'
          + (f'; {left:,} left alone because they changed since' if left else '') + '.')
