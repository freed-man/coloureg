"""Draw the car from a saved lookup, to see what the site's picture would be (paint217).

Give it registrations; for each it takes the latest lookup of that plate from
the database (make, model, year, paint code and name, DVLA colour), draws the
picture with the recipe in lookup/services/ai_pictures.py, checks which side
the steering wheel is on, and saves it in car_pictures/ at the top of the repo
(in .gitignore, never committed). It reads the database and writes nothing to
it. Run from the repo folder, env.py makes that PRODUCTION's lookups and
supplies the keys.

    python manage.py car_pictures G66LWP
    python manage.py car_pictures G66LWP VE67NLP          several cars

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
    """The latest lookup of this plate that found a car, or None."""
    reg = normalize_registration(registration)
    return (Search.objects.filter(registration=reg).exclude(make='')
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
        costs, seconds, verdicts = [], [], []
        for raw in opts['registrations']:
            search = latest_lookup(raw)
            reg = normalize_registration(raw)
            if not search:
                self.stdout.write(f'{reg}: no lookup of this registration found a car')
                continue
            prompt, paint = cp.search_prompt(search)
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
            self.stdout.write(
                f'  {pic.seconds:5.1f}s  saved {path.name}'
                + (f'  about ${pic.cost:.3f}' if pic.cost is not None else '')
                + (f'  wheel {verdict}' if verdict else f'  {note}'))
        if seconds:
            self.stdout.write(f'\nSeconds per picture: average {sum(seconds) / len(seconds):.1f}, '
                              f'fastest {min(seconds):.1f}, slowest {max(seconds):.1f}')
        if verdicts:
            self.stdout.write('Steering wheel: ' + ', '.join(
                f'{v} {verdicts.count(v)}' for v in cp.VERDICTS if verdicts.count(v)))
        if costs:
            self.stdout.write(f'Cost as OpenAI counted it: about ${sum(costs):.2f}')
        if seconds:
            self.stdout.write(f'Pictures are in {out}')
