"""Park the cars the operator has given up on for now, and unpark them.

paint335. A parked car is one he has tried every route for (his dealer software, the parts
desks, the catalogues) and found no code, so he stops trying. Every lookup of the plate that
holds no code is marked with the time, and etc/open_cars.py lists the car under PARKED rather
than its make. paint350: that is all; the dashboard marks a parked lookup like any other (the
red cross when no code was found), where paint335 showed the grey circle. A code found later wins.

PREVIEW BY DEFAULT: nothing is written without --apply, and the preview says exactly what would be.

    manage.py park --list                       every parked car: plate, date, lookup ids (read only)
    manage.py park 1234 5678                    park the cars these lookup ids belong to
    manage.py park 1234 5678 --apply
    manage.py park --unpark 1234 --apply        take a car out again
    manage.py park --from-file etc/open_cars.txt [--apply]
        read the file's PARKED section: park every car listed there, unpark every parked car
        that is no longer listed (the file is the operator's word; the MOTORBIKES section and
        the makes above are not read). Lookup ids are the first field of each car line.

Ids that are not lookups are reported and ignored. A car whose every lookup holds a code has
nothing to park and is reported as such.
"""
import re

from django.core.management.base import BaseCommand, CommandError

from lookup.models import Search

#: The PARKED section of etc/open_cars.txt starts at a line beginning with this word (any case) and
#: ends at the MOTORBIKES section or the end of the file. The same words are in etc/open_cars.py.
PARKED_HEADING = 'PARKED'
BIKES_HEADING = 'MOTORBIKES'
_CAR_LINE = re.compile(r'^[ *](\d[\d, ]*?)\s+\S+,\s')      # ids, then the registration with its comma


def parked_ids_in_file(text):
    """paint335: the lookup ids of every car line in the file's PARKED section, in file order."""
    ids, inside = [], False
    for line in text.splitlines():
        head = line.strip().upper()
        if head.startswith(PARKED_HEADING):
            inside = True
            continue
        if head.startswith(BIKES_HEADING):
            inside = False
            continue
        if inside:
            m = _CAR_LINE.match(line)
            if m:
                ids.extend(int(x) for x in m.group(1).replace(' ', '').split(',') if x)
    return ids


class Command(BaseCommand):
    help = 'Park a car (every route tried, no code yet) or unpark it; preview unless --apply.'

    def add_arguments(self, parser):
        parser.add_argument('ids', nargs='*', help='Lookup ids of the cars to park.')
        parser.add_argument('--unpark', nargs='*', default=None, metavar='ID', help='Lookup ids of cars to unpark.')
        parser.add_argument('--from-file', default='', metavar='PATH', help='Sync with the PARKED section of this open cars file.')
        parser.add_argument('--list', action='store_true', help='List every parked car. Read only.')
        parser.add_argument('--apply', action='store_true', help='Write. Without it, a preview.')

    def handle(self, *args, **options):
        apply = options['apply']
        if options['list']:
            parked = Search.parked_plates()
            if not parked:
                self.stdout.write('No car is parked.')
                return
            for plate, (when, ids) in sorted(parked.items(), key=lambda kv: kv[1][0]):
                self.stdout.write(f'  {plate:<10} parked {when:%Y-%m-%d}  lookups {", ".join(str(i) for i in ids)}')
            self.stdout.write(f'{len(parked)} parked car(s). Nothing was written.')
            return

        to_park = [int(i) for i in options['ids'] if str(i).isdigit()]
        to_unpark = [int(i) for i in (options['unpark'] or []) if str(i).isdigit()]
        bad = [i for i in list(options['ids']) + list(options['unpark'] or []) if not str(i).isdigit()]
        if bad:
            raise CommandError(f'not lookup ids: {bad}')
        if options['from_file']:
            try:
                with open(options['from_file'], encoding='utf-8') as f:
                    listed = parked_ids_in_file(f.read())
            except OSError as exc:
                raise CommandError(f'cannot read {options["from_file"]}: {exc}')
            listed_plates = set(Search._plates_of(listed))
            unknown = sorted(set(listed) - set(Search.objects.filter(id__in=listed).values_list('id', flat=True)))
            if unknown:
                self.stdout.write(f'  not lookups (ignored): {unknown}')
            to_park.extend(listed)
            # every parked plate the file no longer lists comes out again
            for plate, (_when, ids) in Search.parked_plates().items():
                if plate not in listed_plates:
                    to_unpark.extend(ids)
            self.stdout.write(f'PARKED section of {options["from_file"]}: {len(listed)} lookup id(s), {len(listed_plates)} car(s).')
        if not to_park and not to_unpark:
            raise CommandError('nothing to do: give lookup ids, --unpark ids, --from-file PATH or --list')

        mode = 'WRITE' if apply else 'would'
        parked = Search.park(to_park, apply=apply) if to_park else {}
        unparked = Search.unpark(to_unpark, apply=apply) if to_unpark else {}
        for plate, ids in sorted(parked.items()):
            self.stdout.write(f'  {mode} park   {plate:<10} lookups {", ".join(str(i) for i in ids)}')
        for plate, ids in sorted(unparked.items()):
            self.stdout.write(f'  {mode} unpark {plate:<10} lookups {", ".join(str(i) for i in ids)}')
        known = set(Search._plates_of(to_park))
        nothing = sorted(known - set(parked))
        if nothing:
            self.stdout.write(f'  nothing to park (a code by now, "no code exists", or parked already): {", ".join(nothing)}')
        self.stdout.write(f"{'parked' if apply else 'would park'}: {len(parked)} | {'unparked' if apply else 'would unpark'}: {len(unparked)}"
                          + ('' if apply else ' | nothing was written (add --apply)'))
