"""Replace GUESSED swatches with a source's own (paint237).

Only swatches that no committed catalogue file ever carried, and only where the
row's name says the source's swatch is right and ours is wrong or a stock value:
see plan_guess_upgrades in lookup/services/catalogue_topup.py. A preview by
default; --apply saves every old value to a backup file first, and --restore
puts a backup back.

    python manage.py upgrade_swatches bsp BSP.csv HISTORY.json.gz              preview
    python manage.py upgrade_swatches bsp BSP.csv HISTORY.json.gz --apply      write, with a backup
    python manage.py upgrade_swatches --restore BACKUP.json                    undo
"""
import gzip
import json
import os
from collections import Counter
from datetime import datetime

from django.core.management.base import BaseCommand, CommandError

from lookup.services import catalogue_topup as ct

READERS = {'bsp': ct.read_bsp}


class Command(BaseCommand):
    help = "Replace guessed swatches with a source's own, where the name says it is right"

    def add_arguments(self, parser):
        parser.add_argument('source', nargs='?', choices=sorted(READERS))
        parser.add_argument('path', nargs='?')
        parser.add_argument('history', nargs='?')
        parser.add_argument('--apply', action='store_true', help='write, after saving a backup')
        parser.add_argument('--restore', metavar='BACKUP', help='put back every swatch in this backup')

    def handle(self, *args, **opts):
        w = self.stdout.write
        if opts['restore']:
            if not os.path.exists(opts['restore']):
                raise CommandError(f'No such file: {opts["restore"]}')
            restored, changed = ct.restore_upgrades(opts['restore'])
            w(f'Restored {restored:,} swatches' + (f'; {changed:,} left alone because they changed since' if changed else '') + '.')
            return
        if not (opts['source'] and opts['path'] and opts['history']):
            raise CommandError('Give the source, its file and the history file, or --restore BACKUP.')
        for p in (opts['path'], opts['history']):
            if not os.path.exists(p):
                raise CommandError(f'No such file: {p}')
        with gzip.open(opts['history'], 'rt', encoding='utf-8') as f:
            history = json.load(f)
        src = READERS[opts['source']](opts['path'])
        plan = ct.plan_guess_upgrades(src, history)
        w(('SWATCH UPGRADE, WRITING' if opts['apply'] else 'SWATCH UPGRADE PREVIEW, nothing written (add --apply to write)')
          + f': {src.name}')
        w(f'\nGuessed swatches to replace:      {len(plan.changes):>8,}')
        for why, n in Counter(c['why'] for c in plan.changes).most_common():
            w(f'  {n:>8,}  {why}')
        w('\nKept:')
        for why, n in sorted(plan.tally.items()):
            w(f'  {n:>8,}  {why}')
        if plan.changes:
            w('\nMost by make: ' + ', '.join(f'{m} {n:,}' for m, n in Counter(c['manufacturer'] for c in plan.changes).most_common(12)))
            for c in plan.changes[:6]:
                w(f'  e.g. {c["manufacturer"]} {c["code"]} "{c["name"]}": {c["old_hex"]} -> {c["new_hex"]} ({c["why"]})')
        if opts['apply'] and plan.changes:
            backup = os.path.join(os.path.dirname(os.path.abspath(opts['history'])),
                                  f'swatch_upgrade_backup_{datetime.now():%Y%m%d_%H%M%S}.json')
            ct.apply_upgrades(plan, backup)
            w(f'\nWritten: {len(plan.changes):,} swatches replaced, marked "{src.name}".')
            w(f'Backup of every old value: {backup}')
            w(f'To undo: python manage.py upgrade_swatches --restore "{backup}"')
