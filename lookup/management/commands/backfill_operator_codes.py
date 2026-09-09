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
        parser.add_argument(
            '--update', action='store_true',
            help='Also overwrite rows that already exist. Off by default so a '
                 're-run cannot undo a hand correction.')

    def handle(self, *args, **options):
        dry = options['dry_run']
        update = options['update']
        existing = set(
            OperatorPaintCode.objects.values_list('manufacturer', 'code'))

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

        seen, skipped, names, kept, imported = {}, [], {}, [], 0
        for r in rows:
            code = (r['paint_code'] or '').strip()
            if _NON_ANSWER.match(code):
                skipped.append((r['registration'], code))
                continue
            key = (PaintLookup.normalize_manufacturer(r['make']), code.upper())
            name = (r['paint_description'] or '').strip()
            # ONE ENTRY PER CODE, holding every distinct name the history used.
            # Reporting each TRANSITION instead printed bmw/B39 twice — once for
            # Mineralgrau->Mineral Grey and again for the flip back — which reads
            # as two problems when it is one code recorded three times.
            if name:
                names.setdefault(key, [])
                if not any(PaintLookup.normalize_name(n) == PaintLookup.normalize_name(name)
                           for n in names[key]):
                    names[key].append(name)
            seen[key] = name or seen.get(key) or ''
            if key in existing and not update:
                kept.append(key)
                continue
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
        # DISTINCT pairs, not rows. 134 rows over 123 codes read as though more
        # was skipped than exists.
        w(f'  already present, skipped: {len(set(kept))}'
          + ('' if update else '   (--update to overwrite)'))

        # SAY WHAT THE TABLE HOLDS NOW. The history is immutable, so a code
        # recorded under two names reports forever — and after the operator has
        # corrected it, an unchanging list of "disagreements" is a report you
        # stop reading. Showing the current value turns each line into a
        # question that can be answered rather than a permanent complaint.
        multi = {k: v for k, v in names.items() if len(v) > 1}
        w(f'  codes named >1 way      : {len(multi)}   (in SEARCH history — '
          f'immutable, so these always list)')
        for key in sorted(multi):
            row = OperatorPaintCode.objects.filter(
                manufacturer=key[0], code=key[1]).first()
            w(f'      {key[0]}/{key[1]}')
            w(f'          history : {" | ".join(multi[key])}')
            if row is None:
                w('          table   : not imported')
            else:
                mark = 'flagged' if row.needs_review else 'not flagged'
                w(f'          table   : {row.colour_name!r}  ({mark})')
        w('')
        if dry:
            _new = len(seen) - len(set(kept))
            w(self.style.WARNING(
                f'Dry run. {_new} pairs WOULD be written '
                f'({len(set(kept))} already present and untouched). '
                'Re-run without --dry-run to write them.'))
            return
        w(self.style.SUCCESS(f'Imported. Table now holds '
                             f'{OperatorPaintCode.objects.count()} codes, '
                             f'{OperatorPaintCode.objects.filter(needs_review=True).count()} '
                             f'flagged for review.'))
