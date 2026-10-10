"""
Bulk load paint_lookup.json into the PaintLookup table.

Reads the canonical paint_lookup.json (committed to the repo at
lookup/data/paint_lookup.json) and populates the PaintLookup table on the
runtime database (Neon Postgres in production). One row per (manufacturer, code).

Modes:
    --replace   Delete all existing PaintLookup rows, then bulk-insert fresh.
                Use for a refresh after re-scraping/re-merging.
    --upsert    Merge the file into the table: add the rows the table lacks,
                and for a row it has, overwrite every field the file has a
                value for. It leaves alone a field named in the row's
                locked_fields, a hidden (suppressed) row, the operator's own
                names, and a value the file has nothing for. A hand edit that
                is NOT locked is overwritten like any other value.
                A PREVIEW unless --apply is added (paint308): it prints what
                it would create and change, and writes nothing.
    (default)   Insert only if the table is empty. Otherwise no-op. Safe for
                automated deploys — won't double-load on every release.

Usage:
    python manage.py load_paint_lookup
    python manage.py load_paint_lookup --replace
    python manage.py load_paint_lookup --upsert              preview, nothing written
    python manage.py load_paint_lookup --upsert --apply      write
    python manage.py load_paint_lookup --file /path/to/paint_lookup.json

paint308: --upsert used to write at once, and to describe itself as preserving
manual corrections. It keeps the LOCKED ones and overwrites the rest. New
sources are added with topup_catalogue (paint236), which never changes what the
table already has.
"""

import json
import os
import time
from collections import Counter

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.conf import settings

from lookup.models import PaintLookup


DEFAULT_PATH = os.path.join(
    settings.BASE_DIR, 'lookup', 'data', 'paint_lookup.json'
)


def _merge_operator_names(row):
    """Union `operator_names` into all_names / normalized_names. True if changed.

    paint161. `all_names` belongs to the SCRAPE — 30,768 rows already carry more
    than one name, and a new source adds more. Locking it to keep one operator
    addition would forfeit every alias that ever arrives afterwards. So the
    addition lives in `operator_names` and is merged back in here, AFTER the
    scrape's own values have been written.

    Order matters: this runs last, so it cannot be overwritten by the same pass.

    The case it exists for is `vauxhall/KKJ`, which holds only its French name
    "Gris Titane". pl24 returned the English "Titanium Grey" with no code, and
    nothing matched, so a resolvable lookup failed.
    """
    # paint202: the body now lives on the model, so the loader and
    # fold_operator_names merge through the same code.
    return row.merge_operator_names()


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
            '--report-orphans', action='store_true',
            help='List rows present in the database but absent from the '
                 'incoming file. They are never deleted.',
        )
        parser.add_argument(
            '--force-replace', action='store_true',
            help='Allow --replace to delete rows that have locked fields. '
                 'Those corrections are lost. See `manage.py locked_rows`.',
        )
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
            help='Merge the file in: add missing rows, and overwrite every '
                 'field the file has a value for, unlocked hand edits '
                 'included. Locked fields and hidden rows are left alone. '
                 'Only a preview unless --apply is given.',
        )
        parser.add_argument(
            '--apply', action='store_true',
            help='With --upsert: write. Without it, --upsert only previews.',
        )
        parser.add_argument(
            '--batch-size',
            type=int,
            default=2000,
            help='Bulk insert batch size (default: 2000).',
        )

    def handle(self, *args, **options):

        self.force_replace = options.get('force_replace', False)
        self.report_orphans = options.get('report_orphans', False)
        path = options['file']
        replace = options['replace']
        upsert = options['upsert']
        apply = options.get('apply', False)
        batch_size = options['batch_size']

        if replace and upsert:
            raise CommandError('Cannot use --replace and --upsert together. Pick one.')

        if not os.path.exists(path):
            raise CommandError(f'File not found: {path}')

        existing_count = PaintLookup.all_objects.count()

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
            self._do_upsert(records, batch_size, apply)
            if not apply:
                self.stdout.write(self.style.WARNING(
                    'PREVIEW ONLY: nothing was written. Add --apply to write.'))
                return
        else:
            self.stdout.write('Mode: initial load (empty table → bulk insert)')
            self._bulk_insert(records, batch_size)

        final_count = PaintLookup.all_objects.count()
        self.stdout.write(self.style.SUCCESS(
            f'Done. PaintLookup table now has {final_count:,} rows.'
        ))

    # ------------------------------------------------------------------

    def _do_replace(self, records, batch_size):
        self.stdout.write('Mode: replace (delete all + bulk insert)')
        # paint160: REPLACE DESTROYS EVERY LOCK. Locked fields are corrections
        # made by hand, and this path deletes the rows holding them, so it
        # refuses rather than silently discarding the work.
        #
        # Not a prompt: this runs in deploys and scripts, so a question would
        # either hang or be answered blind. --force-replace is the deliberate
        # way through, and it says what it costs.
        #
        # paint191: AND EVERY OTHER HAND EDIT. The guard counted only locked
        # fields, while the delete below takes every row — suppressed ones and
        # ones carrying operator names included, both made by hand and neither
        # coming back from a scrape. So a catalogue with suppressions but no
        # locks was wiped without a word; and where locks did make it refuse,
        # --force-replace took the suppressions too under a message that only
        # mentioned locks. Found by an external audit (N7), reproduced: one
        # suppressed row and one operator-named row, --replace ran silently,
        # both gone. In production that is 1,397 suppressions (23 Sep).
        _locked = PaintLookup.all_objects.exclude(locked_fields=[]).count()
        _suppressed = PaintLookup.all_objects.filter(suppressed=True).count()
        _named = PaintLookup.all_objects.exclude(operator_names=[]).count()
        _lost = [f'{n:,} {what}' for n, what in (
            (_locked, 'with locked fields'),
            (_suppressed, 'suppressed'),
            (_named, 'carrying operator names')) if n]
        if _lost and not self.force_replace:
            raise CommandError(
                f'--replace would delete hand edits: {", ".join(_lost)}. Use '
                f'--upsert to keep them, or --force-replace to discard them '
                f'deliberately. `manage.py locked_rows` lists the locked ones.'
            )
        if _lost:
            # Discarding them on purpose still deserves to be said out loud, so
            # the log of the run shows what was thrown away.
            self.stdout.write(self.style.WARNING(
                f'  --force-replace: discarding {", ".join(_lost)}'))
        with transaction.atomic():
            deleted, _ = PaintLookup.all_objects.all().delete()
            self.stdout.write(f'  Deleted {deleted:,} existing rows')
            self._bulk_insert(records, batch_size)

    def _do_upsert(self, records, batch_size, apply=False):
        # paint308: A PREVIEW UNLESS --apply, AND A BANNER THAT IS TRUE.
        #
        # This mode wrote on the one flag, the only catalogue writer left with
        # no preview, and its banner, its help line and the top of this file
        # all said it preserves hand edits. It preserves LOCKED fields
        # (paint160), hidden rows and operator names (paint161) and a value the
        # file has nothing for (paint167). A hand edit that is not locked is
        # overwritten: reproduced on a scratch copy, where a swatch and a name
        # corrected by hand and left unlocked went back to the file's values
        # under the words "preserve admin edits".
        #
        # Without --apply everything below is worked out exactly as before and
        # nothing is saved: the counts, how many rows would lose which field,
        # and a few of the rows themselves are printed instead.
        self.stdout.write('Mode: upsert, WRITING' if apply else
                          'Mode: upsert PREVIEW, nothing written (add --apply to write)')
        self.stdout.write('  It overwrites every field the file has a value for, unless the '
                          'field is locked or the row is hidden. An unlocked hand edit is not kept.')
        existing = {
            (r.manufacturer, r.code): r for r in PaintLookup.all_objects.all()
        }
        self.stdout.write(f'  Loaded {len(existing):,} existing rows')

        to_create = []
        to_update = []
        unchanged = 0
        fields = ['name', 'all_names', 'normalized_names', 'hex',
                  'color_group', 'models_list', 'sources']

        # paint160: LOCKED FIELDS ARE NOT TOUCHED.
        #
        # This mode announces itself as "preserve admin edits" and did the
        # opposite: every field where the scrape differed was overwritten, so no
        # correction to this table has ever survived a load. That is why the
        # operator table exists as a parallel patch, and why rows known to be
        # wrong have stayed wrong.
        #
        # Per FIELD, so a corrected hex is kept while the same row still accepts
        # a better models_list from a later scrape.
        locked_skipped = 0
        suppressed_skipped = 0
        by_field, sample = Counter(), []       # paint308: for the preview
        for r in records:
            key = (r['manufacturer'], r['code'])
            new_inst = build_instance(r)
            if key in existing:
                old = existing[key]
                if old.suppressed:
                    # paint161: deliberately absent. Updating it would quietly
                    # restore a row removed on purpose — the exact failure this
                    # field exists to stop, since the record is still in the
                    # source file and always will be.
                    suppressed_skipped += 1
                    continue
                locked = set(old.locked_fields or [])
                writable = [f for f in fields if f not in locked]
                if locked:
                    locked_skipped += 1
                # paint167: AN EMPTY INCOMING VALUE NEVER ERASES A POPULATED
                # ONE.
                #
                # `--upsert` wrote every field it was given, so a scrape with no
                # hex for a row blanked the hex that was already there. That is
                # not a hex problem: if a source ever drops a column, or one
                # scraper's coverage narrows, the catalogue silently loses data
                # everywhere that source touches.
                #
                # It is also why a GENERATED hex had to be locked. It should not
                # have to be: a guess ought to be overwritable by a future
                # scrape that has the real value, and locking freezes it instead.
                # With this rule, a generated value survives a scrape that knows
                # nothing and yields to one that knows better.
                #
                # Deliberately one-way. A source cannot say "this row genuinely
                # has no hex" and be believed, which is a real cost — but far
                # smaller than mass erasure, and a deliberate blanking is what
                # `locked_fields` and the admin are for.
                writable = [f for f in writable
                            if getattr(new_inst, f) or not getattr(old, f)]
                if writable and any(getattr(old, f) != getattr(new_inst, f)
                                    for f in writable):
                    # paint308: what this row loses, read before it is changed.
                    diffs = [(f, getattr(old, f), getattr(new_inst, f))
                             for f in writable if getattr(old, f) != getattr(new_inst, f)]
                    by_field.update(f for f, _old, _new in diffs)
                    if len(sample) < self.SAMPLE:
                        sample.append((key, diffs))
                    for f in writable:
                        setattr(old, f, getattr(new_inst, f))
                    _merge_operator_names(old)
                    to_update.append(old)
                else:
                    # Unchanged by the scrape, but an operator name may have
                    # been added since the last load and must still be merged.
                    if old.operator_names and _merge_operator_names(old):
                        to_update.append(old)
                    else:
                        unchanged += 1
            else:
                to_create.append(new_inst)

        with transaction.atomic():
            if apply and to_create:
                PaintLookup.all_objects.bulk_create(to_create, batch_size=batch_size)
            if apply and to_update:
                # bulk_update writes every name in `fields`, locked ones
                # included — but a locked field was never reassigned above, so
                # the value written is the one already in the database. Correct,
                # and worth saying: it reads like a leak and is not.
                PaintLookup.all_objects.bulk_update(to_update, fields, batch_size=batch_size)

        self.stdout.write(f'  {"Created" if apply else "Would create"}: {len(to_create):,}')
        self.stdout.write(f'  {"Updated" if apply else "Would update"}: {len(to_update):,}')
        self.stdout.write(f'  Unchanged: {unchanged:,}')
        if locked_skipped:
            self.stdout.write(f'  Rows with locked fields: {locked_skipped:,}')
        if suppressed_skipped:
            self.stdout.write(f'  Suppressed, left absent: {suppressed_skipped:,}')
        if not apply:
            # paint308: the preview's own lines. How many rows would have each
            # field overwritten, then a few rows of each kind.
            if by_field:
                self.stdout.write('  Rows that would have a field overwritten: '
                                  + ', '.join(f'{f} {n:,}' for f, n in by_field.most_common()))
            for new in to_create[:self.SAMPLE]:
                self.stdout.write(f'    e.g. new: {new.manufacturer} {new.code} "{new.name}" '
                                  f'{new.hex or "(no swatch)"}')
            for (mfr, code), diffs in sample:
                self.stdout.write(f'    e.g. change: {mfr} {code}: ' + '; '.join(
                    f'{f} {self._short(was)} -> {self._short(now)}' for f, was, now in diffs))
        # paint161: ORPHANS. --upsert only ever visits rows the incoming file
        # mentions, so a row the new scrape has dropped is never touched and
        # never reported. That is usually right — losing a retired scraper
        # should not cost you its 30,000 rows overnight — but it means a source
        # quietly disappearing is invisible, and a row that was wrong in an old
        # source stays forever.
        #
        # Reported, never deleted: deciding a row is dead is a judgement, and
        # `suppressed` is where that decision belongs.
        seen_keys = {(r['manufacturer'], r['code']) for r in records}
        orphans = [k for k in existing if k not in seen_keys]
        if orphans:
            self.stdout.write(
                f'  In the database but NOT in this file: {len(orphans):,} '
                f'(kept; use --report-orphans to list them)'
            )
            if self.report_orphans:
                for mfr, code in sorted(orphans)[:200]:
                    self.stdout.write(f'      {mfr:<18}{code}')
                if len(orphans) > 200:
                    self.stdout.write(f'      ... and {len(orphans) - 200:,} more')

    #: paint308: how many rows of each kind the --upsert preview lists.
    SAMPLE = 8

    @staticmethod
    def _short(value):
        """A value as the preview prints it: quoted, and cut if it is long."""
        text = repr(value)
        return text if len(text) <= 48 else text[:45] + '...'

    def _bulk_insert(self, records, batch_size):
        start = time.time()
        total = 0
        batch = []
        for r in records:
            batch.append(build_instance(r))
            if len(batch) >= batch_size:
                PaintLookup.all_objects.bulk_create(batch, ignore_conflicts=True)
                total += len(batch)
                batch = []
                if total % 10000 == 0:
                    elapsed = time.time() - start
                    rate = total / elapsed if elapsed > 0 else 0
                    self.stdout.write(f'  Inserted {total:,} rows ({rate:.0f}/s)')
        if batch:
            PaintLookup.all_objects.bulk_create(batch, ignore_conflicts=True)
            total += len(batch)
        elapsed = time.time() - start
        rate = total / elapsed if elapsed > 0 else 0
        self.stdout.write(f'  Bulk insert finished: {total:,} rows in {elapsed:.1f}s ({rate:.0f}/s)')
