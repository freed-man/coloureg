"""List catalogue rows with locked fields, so corrections stay reviewable.

paint160. `locked_fields` stops the scrape overwriting a hand-made correction.
The cost of that is the opposite failure: a locked value is never challenged
again, however wrong it turns out to be.

So this exists to be run occasionally. It shows what is locked, what the row
currently says, and flags a lock that looks questionable — a hex that
contradicts its own name, or a lock naming a field that does not exist.

    manage.py locked_rows
    manage.py locked_rows --make mitsubishi
    manage.py locked_rows --suspect      only rows worth a second look
"""
from django.core.management.base import BaseCommand

from lookup.models import PaintLookup

#: Fields the loader can write, and therefore the only ones worth locking.
_LOADER_FIELDS = {'name', 'all_names', 'normalized_names', 'hex',
                  'color_group', 'models_list', 'sources'}


class Command(BaseCommand):
    help = 'List catalogue rows with locked fields.'

    def add_arguments(self, parser):
        parser.add_argument('--make', default='', help='Limit to one manufacturer.')
        parser.add_argument(
            '--suspect', action='store_true',
            help='Only rows whose lock looks questionable.',
        )

    def handle(self, *args, **options):
        # Imported here, not at module scope: these live in the service layer
        # and pulling them in at import time would tie a management command to
        # the resolver's import graph for no reason.
        from lookup.services.paint_resolver import _colour_families, _hex_family

        qs = PaintLookup.objects.exclude(locked_fields=[])
        if options['make']:
            qs = qs.filter(manufacturer=options['make'].strip().lower())

        rows = list(qs.order_by('manufacturer', 'code'))
        if not rows:
            self.stdout.write('No locked rows.')
            return

        shown = 0
        for r in rows:
            locked = list(r.locked_fields or [])
            notes = []

            # A lock naming a field the loader never writes does nothing. It is
            # not harmful, but it reads as protection that is not there.
            unknown = [f for f in locked if f not in _LOADER_FIELDS]
            if unknown:
                notes.append(f'locks a field the loader never writes: {unknown}')

            # A locked hex that contradicts its own name is the exact defect
            # locking is meant to FIX, so seeing one locked in is worth saying.
            if 'hex' in locked and r.hex and r.name:
                fam = _colour_families(r.name)
                hf = _hex_family(r.hex)
                if fam and hf and hf not in fam:
                    notes.append(f'locked hex {r.hex} reads {hf}, name says {sorted(fam)}')

            if options['suspect'] and not notes:
                continue

            shown += 1
            self.stdout.write(
                f'{r.manufacturer:<16}{r.code:<14}{(r.name or "")[:30]:<32}'
                f'{r.hex or "--":<9}locked={locked}'
            )
            for n in notes:
                self.stdout.write(f'    ! {n}')

        self.stdout.write('')
        self.stdout.write(f'{shown:,} shown of {len(rows):,} locked rows.')
