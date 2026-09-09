"""Backfill OperatorPaintCode from manual lookups already fulfilled.

paint100. OperatorPaintCode (paint92) only ever wrote FORWARD, from
submit_manual_lookup. Every manual code fulfilled before it shipped stayed on
its Search row and nowhere else — 134 of them across four months, 124 distinct
(make, code) pairs, all of them hand-researched and none reusable.

That is the most valuable data in the system sitting idle: these are precisely
the codes paint_lookup.json does NOT hold, because a catalogue miss is what
sent them to a manual lookup in the first place.

Run it once. It is idempotent — record() uses update_or_create — so a second
run changes nothing, and it will not overwrite anything entered since.

    python manage.py backfill_operator_codes --dry-run
    python manage.py backfill_operator_codes
"""

import re

from django.core.management.base import BaseCommand
from django.db.models import Q

from lookup.models import OperatorPaintCode, PaintLookup, Search

#: A "code" that is the operator saying there ISN'T one. Storing these would
#: answer future lookups for that colour with a non-answer — worse than the
#: miss it replaces, because a miss at least offers the free manual lookup.
_NON_ANSWER = re.compile(r'^\s*(n/?a|none|unknown|xxx|-+|\?+)\s*$', re.I)


class Command(BaseCommand):
    help = 'Import already-fulfilled manual paint codes into OperatorPaintCode.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Report what would be imported without writing anything.')

    def handle(self, *args, **options):
        dry = options['dry_run']

        rows = (Search.objects
                .filter(Q(provider=Search.PROVIDER_MANUAL)
                        | Q(manual_lookup_completed=True))
                .exclude(paint_code='')
                .exclude(make='')
                # OLDEST FIRST, so that when the same code was fulfilled twice
                # the NEWER name wins — matching what record() does live, and
                # what the operator most recently believed.
                .order_by('timestamp')
                .values('id', 'registration', 'make', 'model',
                        'paint_code', 'paint_description'))

        seen, skipped, conflicts, imported = {}, [], [], 0
        for r in rows:
            code = (r['paint_code'] or '').strip()
            if _NON_ANSWER.match(code):
                skipped.append((r['registration'], code))
                continue
            key = (PaintLookup.normalize_manufacturer(r['make']), code.upper())
            name = (r['paint_description'] or '').strip()
            prev = seen.get(key)
            if prev is not None and name:
                if PaintLookup.normalize_name(prev) != PaintLookup.normalize_name(name):
                    conflicts.append((key, prev, name))
            seen[key] = name or prev or ''
            if not dry:
                OperatorPaintCode.record(
                    make=r['make'], code=code, colour_name=name,
                    model=r['model'], registration=r['registration'],
                    search_id=r['id'])
                imported += 1

        w = self.stdout.write
        w('')
        w(self.style.HTTP_INFO('Operator paint code backfill'))
        w(f'  manual rows with a code : {len(rows)}')
        w(f'  distinct (make, code)   : {len(seen)}')
        w(f'  skipped as non-answers  : {len(skipped)}  {skipped or ""}')
        w(f'  name disagreements      : {len(conflicts)}')
        for key, old, new in conflicts:
            w(f'      {key[0]}/{key[1]:<12}{old!r} -> {new!r}')
        w('')
        if dry:
            w(self.style.WARNING(
                f'Dry run. {len(seen)} pairs WOULD be imported. '
                'Re-run without --dry-run to write them.'))
            return
        w(self.style.SUCCESS(f'Imported. Table now holds '
                             f'{OperatorPaintCode.objects.count()} codes, '
                             f'{OperatorPaintCode.objects.filter(needs_review=True).count()} '
                             f'flagged for review.'))
