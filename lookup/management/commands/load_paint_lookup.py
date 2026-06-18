"""
Bulk load paint_lookup.json into the PaintLookup table.

Reads the canonical paint_lookup.json (committed to the repo at
lookup/data/paint_lookup.json) and populates the PaintLookup table on the
runtime database (Neon Postgres in production). One row per (manufacturer, code).

Modes:
    --replace   Delete all existing PaintLookup rows, then bulk-insert fresh.
                Use for a refresh after re-scraping/re-merging.
    --upsert    Insert new rows, update changed rows, leave unchanged alone.
                Slower but preserves any manual corrections made via the admin.
    (default)   Insert only if the table is empty. Otherwise no-op. Safe for
                automated deploys — won't double-load on every release.

Usage:
    python manage.py load_paint_lookup
    python manage.py load_paint_lookup --replace
    python manage.py load_paint_lookup --upsert
    python manage.py load_paint_lookup --file /path/to/paint_lookup.json
"""

import json
import os
import time

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.conf import settings

from lookup.models import PaintLookup


DEFAULT_PATH = os.path.join(
    settings.BASE_DIR, 'lookup', 'data', 'paint_lookup.json'
)


def build_instance(record):
    """Construct a PaintLookup instance from a JSON record (without saving).

    Note: the JSON field is `models` but the model attribute is `models_list`
    (renamed to avoid clashing with django.db.models in the model module).
    """
    return PaintLookup(
        manufacturer=record['manufacturer'],
        code=record['code'],
        name=record.get('name', ''),
        all_names=record.get('all_names', []),
        normalized_names=record.get('normalized_names', []),
        hex=record.get('hex', '') or '',
        color_group=record.get('color_group', ''),
        models_list=record.get('models', []),
        sources=record.get('sources', []),
    )


class Command(BaseCommand):
    help = 'Load paint_lookup.json into the PaintLookup table.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--file',
            default=DEFAULT_PATH,
            help='Path to paint_lookup.json (default: lookup/data/paint_lookup.json)',
        )
        parser.add_argument(
            '--replace',
            action='store_true',
            help='Delete existing PaintLookup rows before loading.',
        )
        parser.add_argument(
            '--upsert',
            action='store_true',
            help='Update existing rows in place (preserves admin edits).',
        )
        parser.add_argument(
            '--batch-size',
            type=int,
            default=2000,
            help='Bulk insert batch size (default: 2000).',
        )

    def handle(self, *args, **options):
        path = options['file']
        replace = options['replace']
        upsert = options['upsert']
        batch_size = options['batch_size']

        if replace and upsert:
            raise CommandError('Cannot use --replace and --upsert together. Pick one.')

        if not os.path.exists(path):
            raise CommandError(f'File not found: {path}')

        existing_count = PaintLookup.objects.count()

        # Default mode (no flags) is "load only if empty" — safe for auto-deploys
        if not replace and not upsert and existing_count > 0:
            self.stdout.write(self.style.WARNING(
                f'PaintLookup table already has {existing_count:,} rows. '
                'Use --replace to wipe and reload, or --upsert to merge.'
            ))
            return

        self.stdout.write(f'Loading paint lookup from {path}...')
        start = time.time()

        with open(path, 'r', encoding='utf-8') as f:
            records = json.load(f)

        self.stdout.write(f'  Read {len(records):,} records in {time.time() - start:.1f}s')

        if replace:
            self._do_replace(records, batch_size)
        elif upsert:
            self._do_upsert(records, batch_size)
        else:
            self.stdout.write('Mode: initial load (empty table → bulk insert)')
            self._bulk_insert(records, batch_size)

        final_count = PaintLookup.objects.count()
        self.stdout.write(self.style.SUCCESS(
            f'Done. PaintLookup table now has {final_count:,} rows.'
        ))

    # ------------------------------------------------------------------

    def _do_replace(self, records, batch_size):
        self.stdout.write('Mode: replace (delete all + bulk insert)')
        with transaction.atomic():
            deleted, _ = PaintLookup.objects.all().delete()
            self.stdout.write(f'  Deleted {deleted:,} existing rows')
            self._bulk_insert(records, batch_size)

    def _do_upsert(self, records, batch_size):
        self.stdout.write('Mode: upsert (preserve admin edits)')
        existing = {
            (r.manufacturer, r.code): r for r in PaintLookup.objects.all()
        }
        self.stdout.write(f'  Loaded {len(existing):,} existing rows')

        to_create = []
        to_update = []
        unchanged = 0
        fields = ['name', 'all_names', 'normalized_names', 'hex',
                  'color_group', 'models_list', 'sources']

        for r in records:
            key = (r['manufacturer'], r['code'])
            new_inst = build_instance(r)
            if key in existing:
                old = existing[key]
                if any(getattr(old, f) != getattr(new_inst, f) for f in fields):
                    for f in fields:
                        setattr(old, f, getattr(new_inst, f))
                    to_update.append(old)
                else:
                    unchanged += 1
            else:
                to_create.append(new_inst)

        with transaction.atomic():
            if to_create:
                PaintLookup.objects.bulk_create(to_create, batch_size=batch_size)
            if to_update:
                PaintLookup.objects.bulk_update(to_update, fields, batch_size=batch_size)

        self.stdout.write(f'  Created: {len(to_create):,}')
        self.stdout.write(f'  Updated: {len(to_update):,}')
        self.stdout.write(f'  Unchanged: {unchanged:,}')

    def _bulk_insert(self, records, batch_size):
        start = time.time()
        total = 0
        batch = []
        for r in records:
            batch.append(build_instance(r))
            if len(batch) >= batch_size:
                PaintLookup.objects.bulk_create(batch, ignore_conflicts=True)
                total += len(batch)
                batch = []
                if total % 10000 == 0:
                    elapsed = time.time() - start
                    rate = total / elapsed if elapsed > 0 else 0
                    self.stdout.write(f'  Inserted {total:,} rows ({rate:.0f}/s)')
        if batch:
            PaintLookup.objects.bulk_create(batch, ignore_conflicts=True)
            total += len(batch)
        elapsed = time.time() - start
        rate = total / elapsed if elapsed > 0 else 0
        self.stdout.write(f'  Bulk insert finished: {total:,} rows in {elapsed:.1f}s ({rate:.0f}/s)')
