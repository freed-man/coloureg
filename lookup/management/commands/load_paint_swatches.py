"""
Bulk load paint_swatches.json into the PaintSwatch table.

This command reads the canonical paint_swatches.json file (committed to the
repo at lookup/data/paint_swatches.json) and populates the PaintSwatch table
on the runtime database (Neon Postgres in production).

Modes:
    --replace   Delete all existing PaintSwatch rows, then bulk-insert the file
                contents fresh. Use this for a quarterly refresh after re-scraping.
    --upsert    For each row in the file, insert if new, update if changed.
                Slower but preserves any manual corrections made via the admin.
    (default)   Insert only if the table is empty. Otherwise no-op. Safe for
                automated deploys — won't double-load on every release.

Usage:
    # First-time load
    python manage.py load_paint_swatches

    # Force a fresh reload (after data refresh)
    python manage.py load_paint_swatches --replace

    # Upsert mode (preserves admin edits)
    python manage.py load_paint_swatches --upsert

    # Use a different file path
    python manage.py load_paint_swatches --file /path/to/swatches.json
"""

import json
import os
import re
import time

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.conf import settings

from lookup.models import PaintSwatch


DEFAULT_PATH = os.path.join(
    settings.BASE_DIR, 'lookup', 'data', 'paint_swatches.json'
)

# Match the pattern used in the prepare script to derive model families
FIRST_WORD_RE = re.compile(r'^[a-z]+')


def first_word(model):
    """Extract first alphabetic chunk from a normalised model string."""
    if not model:
        return ''
    m = FIRST_WORD_RE.match(model)
    if m:
        return m.group(0)
    return model[:6]


def derive_model_families(applicable_models):
    """Compute model_families from applicable_models for runtime queries."""
    fams = set()
    for model in applicable_models or []:
        fw = first_word(model)
        if fw:
            fams.add(fw)
    return sorted(fams)


def build_swatch_instance(record):
    """Construct a PaintSwatch instance from a JSON record (without saving)."""
    applicable_models = record.get('applicable_models', [])
    return PaintSwatch(
        manufacturer=record['manufacturer'],
        code=record['code'],
        hex=record['hex'],
        name=record.get('name', ''),
        applicable_models=applicable_models,
        model_families=derive_model_families(applicable_models),
        color_group=record.get('color_group', ''),
        year_min=record.get('year_min'),
        year_max=record.get('year_max'),
        sources_count=record.get('sources_count', 1),
        sources=record.get('sources', []),
    )


class Command(BaseCommand):
    help = 'Load paint_swatches.json into the PaintSwatch table.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--file',
            default=DEFAULT_PATH,
            help='Path to paint_swatches.json (default: lookup/data/paint_swatches.json)',
        )
        parser.add_argument(
            '--replace',
            action='store_true',
            help='Delete existing PaintSwatch rows before loading.',
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
            raise CommandError(
                'Cannot use --replace and --upsert together. Pick one.'
            )

        if not os.path.exists(path):
            raise CommandError(f'File not found: {path}')

        existing_count = PaintSwatch.objects.count()

        # Default mode (no flags) is "load only if empty" — safe for auto-deploys
        if not replace and not upsert and existing_count > 0:
            self.stdout.write(self.style.WARNING(
                f'PaintSwatch table already has {existing_count:,} rows. '
                'Use --replace to wipe and reload, or --upsert to merge.'
            ))
            return

        self.stdout.write(f'Loading swatches from {path}...')
        start = time.time()

        with open(path, 'r', encoding='utf-8') as f:
            records = json.load(f)

        elapsed = time.time() - start
        self.stdout.write(f'  Read {len(records):,} records in {elapsed:.1f}s')

        if replace:
            self._do_replace(records, batch_size)
        elif upsert:
            self._do_upsert(records)
        else:
            self._do_initial_load(records, batch_size)

        final_count = PaintSwatch.objects.count()
        self.stdout.write(self.style.SUCCESS(
            f'Done. PaintSwatch table now has {final_count:,} rows.'
        ))

    # ------------------------------------------------------------------
    # Mode handlers
    # ------------------------------------------------------------------

    def _do_initial_load(self, records, batch_size):
        """First-time load into an empty table — fastest mode."""
        self.stdout.write('Mode: initial load (empty table → bulk insert)')
        self._bulk_insert(records, batch_size)

    def _do_replace(self, records, batch_size):
        """Wipe table, then bulk insert. Used for refresh cycles."""
        self.stdout.write('Mode: replace (delete all + bulk insert)')

        with transaction.atomic():
            deleted, _ = PaintSwatch.objects.all().delete()
            self.stdout.write(f'  Deleted {deleted:,} existing rows')
            self._bulk_insert(records, batch_size)

    def _do_upsert(self, records):
        """Insert new rows, update changed rows, leave unchanged rows alone."""
        self.stdout.write('Mode: upsert (preserve admin edits)')
        
        # Build lookup of existing keys to instances
        existing = {
            (s.manufacturer, s.code, s.hex): s
            for s in PaintSwatch.objects.all()
        }
        self.stdout.write(f'  Loaded {len(existing):,} existing rows')
        
        to_create = []
        to_update = []
        unchanged = 0
        
        for r in records:
            key = (r['manufacturer'], r['code'], r['hex'])
            new_inst = build_swatch_instance(r)
            
            if key in existing:
                old = existing[key]
                # Compare relevant fields
                if (old.name != new_inst.name
                    or old.applicable_models != new_inst.applicable_models
                    or old.model_families != new_inst.model_families
                    or old.color_group != new_inst.color_group
                    or old.year_min != new_inst.year_min
                    or old.year_max != new_inst.year_max
                    or old.sources_count != new_inst.sources_count
                    or old.sources != new_inst.sources):
                    # Apply updates to the existing instance
                    old.name = new_inst.name
                    old.applicable_models = new_inst.applicable_models
                    old.model_families = new_inst.model_families
                    old.color_group = new_inst.color_group
                    old.year_min = new_inst.year_min
                    old.year_max = new_inst.year_max
                    old.sources_count = new_inst.sources_count
                    old.sources = new_inst.sources
                    to_update.append(old)
                else:
                    unchanged += 1
            else:
                to_create.append(new_inst)
        
        with transaction.atomic():
            if to_create:
                PaintSwatch.objects.bulk_create(to_create, batch_size=2000)
            if to_update:
                PaintSwatch.objects.bulk_update(
                    to_update,
                    ['name', 'applicable_models', 'model_families',
                     'color_group', 'year_min', 'year_max',
                     'sources_count', 'sources'],
                    batch_size=2000,
                )
        
        self.stdout.write(f'  Created: {len(to_create):,}')
        self.stdout.write(f'  Updated: {len(to_update):,}')
        self.stdout.write(f'  Unchanged: {unchanged:,}')

    # ------------------------------------------------------------------
    # Bulk insert helper
    # ------------------------------------------------------------------

    def _bulk_insert(self, records, batch_size):
        """Insert all records in batches. Assumes table is empty or duplicates OK."""
        start = time.time()
        instances = (build_swatch_instance(r) for r in records)
        
        total = 0
        batch = []
        for inst in instances:
            batch.append(inst)
            if len(batch) >= batch_size:
                PaintSwatch.objects.bulk_create(batch, ignore_conflicts=True)
                total += len(batch)
                batch = []
                if total % 10000 == 0:
                    elapsed = time.time() - start
                    rate = total / elapsed if elapsed > 0 else 0
                    self.stdout.write(f'  Inserted {total:,} rows ({rate:.0f}/s)')
        
        if batch:
            PaintSwatch.objects.bulk_create(batch, ignore_conflicts=True)
            total += len(batch)
        
        elapsed = time.time() - start
        rate = total / elapsed if elapsed > 0 else 0
        self.stdout.write(f'  Bulk insert finished: {total:,} rows in {elapsed:.1f}s ({rate:.0f}/s)')
