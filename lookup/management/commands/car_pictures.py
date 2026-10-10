"""Draw the car from a saved lookup, to see what the site's picture would be (paint217).

Give it registrations; for each it takes the latest lookup of that plate from
the database (make, model, year, paint code and name, DVLA colour), draws the
picture with the recipe in lookup/services/ai_pictures.py, checks which side
the steering wheel is on, and saves it in car_pictures/ at the top of the repo
(in .gitignore, never committed). It reads the database and writes nothing to
it. Run from the repo folder, env.py makes that PRODUCTION's lookups and
supplies the keys.

paint308: only a lookup that found a paint code is drawn (no code, no picture,
as on the site); a plate with none is reported and nothing is paid for.

    python manage.py car_pictures AB12CDE
    python manage.py car_pictures AB12CDE CD34EFG          several cars

Each picture is saved as car_pictures/REG.png.
"""
import argparse
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from lookup.models import Search
from lookup.services import ai_pictures as cp
from lookup.services.protection import normalize_registration


def latest_lookup(registration):
    """The latest lookup of this plate that found a car and a paint code, or None.

    paint308: NO CODE, NO PICTURE, HERE TOO. The site stopped drawing a lookup
    with no code in paint223 (a picture in DVLA's bare colour misled more than
    none), and redraw_car_picture never drew one, but this command took the
    plate's latest lookup whatever it held: it paid for a picture, a wheel
    check and a plate check to draw the car "painted blue". Reproduced on a
    scratch copy with the paid calls faked. And where an older lookup of the
    plate had the code and a newer one had none, it drew the newer one. A
    lookup with no code is now left out, as redraw_car_picture leaves it out."""
    reg = normalize_registration(registration)
    return (Search.objects.filter(registration=reg).exclude(make='').exclude(paint_code='')
            .order_by('-timestamp').first())


class Command(BaseCommand):
    help = "Draw a saved lookup's car in its paint with AI, to see the picture the site would show"

    def add_arguments(self, parser):
        parser.add_argument('registrations', nargs='+', help='one or more registrations already looked up')
        parser.add_argument('--out', default='', help=argparse.SUPPRESS)   # the battery's own folder

    def handle(self, *args, **opts):
        if not cp.openai_key():
            raise CommandError('No OPENAI_API_KEY: env.py sets it locally.')
        out = Path(opts['out']) if opts['out'] else Path(settings.BASE_DIR) / 'car_pictures'
        costs, seconds, verdicts, plates = [], [], [], []
        plate = cp.PLATE_TEXT          # paint235: the same recipe as the site, plate and all
        for raw in opts['registrations']:
            search = latest_lookup(raw)
            reg = normalize_registration(raw)
            if not search:
                self.stdout.write(f'{reg}: no lookup of this registration found a code (no code, no picture)')
                continue
            prompt, paint = cp.search_prompt(search, plate=plate)
            self.stdout.write(f'{reg}: {search.year or ""} {search.make} {search.model}, painted {paint}')
            pic = cp.draw(prompt)
            if not pic.ok:
                self.stdout.write(f'  HTTP {pic.status} after {pic.seconds:.0f}s: '
                                  f'{pic.message or "no picture in the reply"}')
                if pic.stop:
                    self.stdout.write('  (about the key, the account or the model name: stopping)')
                    break
                continue
            out.mkdir(parents=True, exist_ok=True)
            path = out / f'{reg}.png'
            path.write_bytes(pic.data)
            seconds.append(pic.seconds)
            if pic.cost is not None:
                costs.append(pic.cost)
            verdict, check_cost, note = cp.check_wheel(pic.data, pic.mime)
            if check_cost is not None:
                costs.append(check_cost)
            if verdict:
                verdicts.append(verdict)
            read = ''
            if plate:
                read, plate_cost, plate_note = cp.read_plate(pic.data, pic.mime)
                if plate_cost is not None:
                    costs.append(plate_cost)
                if read is not None:
                    plates.append(read == plate)
                read = (f'  plate {read or "unreadable"} ' + ('(right)' if read == plate else '(WRONG)')
                        if read is not None else f'  {plate_note}')
            self.stdout.write(
                f'  {pic.seconds:5.1f}s  saved {path.name}'
                + (f'  about ${pic.cost:.3f}' if pic.cost is not None else '')
                + (f'  wheel {verdict}' if verdict else f'  {note}') + read)
        if seconds:
            self.stdout.write(f'\nSeconds per picture: average {sum(seconds) / len(seconds):.1f}, '
                              f'fastest {min(seconds):.1f}, slowest {max(seconds):.1f}')
        if verdicts:
            self.stdout.write('Steering wheel: ' + ', '.join(
                f'{v} {verdicts.count(v)}' for v in cp.VERDICTS if verdicts.count(v)))
        if plates:
            self.stdout.write(f'Plate spelled right: {sum(plates)} of {len(plates)}')
        if costs:
            self.stdout.write(f'Cost as OpenAI counted it: about ${sum(costs):.2f}')
        if seconds:
            self.stdout.write(f'Pictures are in {out}')
