"""Make AI pictures of cars in their paint, to judge them before building the feature (paint216).

Nothing on the site uses these pictures yet. For each car this asks OpenAI's
image model for a studio photo in the car's paint, taking the paint's name and
swatch hex from the catalogue when it knows the code, has a vision model check
which side the steering wheel is on, and saves the picture in car_pictures/ at
the top of the repo (in .gitignore, so never committed).

It reads the catalogue and writes nothing to the database. Every picture and
every check costs OpenAI money (about 1.1 and 0.6 cents on 27 Sep); the key is
OPENAI_API_KEY, which env.py sets locally.

    python manage.py car_pictures                     the six sample cars
    python manage.py car_pictures --only golf,jaecoo  some of them
    python manage.py car_pictures --candidates 3      three pictures per car
    python manage.py car_pictures --check-only        check the pictures already made
    python manage.py car_pictures --make Volkswagen --year 2017 --code LR7H \\
        --car "Golf Mk7.5 SE five-door hatchback"     a car of your own

HOW THE RECIPE WAS FOUND (26 and 27 Sep, 36 test pictures):
  * gpt-image-2.5-sunburst: top of the blind-vote leaderboards, the same price
    per token as flare (which was 3 seconds faster), about 1.1 cents a picture
    at medium quality, 11 to 15 seconds each.
  * The angle varied until the prompt fixed it: the camera at the front corner
    on the driver's side, the car pointing to the right of the picture.
  * The steering wheel came out on the left, even when told "right-hand
    drive". The prompt said "right" in two senses a few words apart, the car's
    right-hand side and the picture's right side, which are opposite ends of
    the windscreen from this angle: 0 of 12 right-hand drive. Asking for glass
    reflections worked around it (17 of 18) by hiding the interior. Placing
    the wheel by NEAR and FAR instead fixed it: 6 of 6 with the interior in
    full view.
  * A vision model (gpt-6-sol) checks every picture's wheel: RIGHT, LEFT, BOTH
    or UNSURE. It agreed with a look by eye on 11 of 12; its one miss called a
    right-hand Golf LEFT, the safe way round. When this becomes the feature,
    LEFT and BOTH are redone and UNSURE (no wheel visible) is accepted.
  * Transparent backgrounds (a PNG that sits on any page colour) made no
    difference to the wheel.
  * Tried and dropped (27 Sep): a first picture in DVLA's plain colour,
    REPAINTED in the exact paint through OpenAI's edit endpoint when the code
    arrives. The repaint kept the same car and its wheel side, but took as
    long as a new picture (about 14 seconds), so on the site the exact version
    would land about 27 seconds after the search, later than one picture made
    when the code arrives (about 23), for nearly three times the cost (4.7
    cents against 1.7). flare was no faster (12.6 seconds a picture) and drew
    the first wrong wheel since the near-and-far wording.
  * So the feature will make ONE sunburst picture in the exact paint, started
    the moment the code arrives (a median 10 seconds after the search, in the
    export's last 30 days), and a picture in DVLA's colour only when a lookup
    ends without a code. No redo for a wrong wheel: the check runs after the
    picture is shown and only records the verdict for the admin.
"""
import argparse
import base64
import os
import re
import time
from pathlib import Path

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from lookup.models import PaintLookup
from lookup.services.http import get_session

IMAGES_URL = 'https://api.openai.com/v1/images/generations'
RESPONSES_URL = 'https://api.openai.com/v1/responses'
AI_MODEL = 'gpt-image-2.5-sunburst'
QUALITY = 'medium'
SIZE = '1536x1024'
CHECKER_MODEL = 'gpt-6-sol'

# Per 1M tokens, from OpenAI's pricing page on 26 Sep 2026, standard
# processing: (in, out). For the picture model: text in, picture out.
PRICES = {
    AI_MODEL: (5.00, 30.00),
    CHECKER_MODEL: (2.00, 10.00),
}

# short name, make, the car in words, year, paint code (looked up in the
# catalogue), the paint in words when the catalogue lacks the code
SAMPLES = (
    ('golf', 'Volkswagen', 'Golf Mk7.5 SE five-door hatchback', 2017, 'LR7H', 'Indium Grey Metallic'),
    ('focus', 'Ford', 'Focus ST-Line X five-door hatchback (fourth generation)', 2020, 'PN4FZ',
     'Desert Island Blue metallic'),
    ('jazz', 'Honda', 'Jazz five-door hatchback (third generation, facelift)', 2018, 'B593M',
     'Aegean Blue Metallic'),
    ('sealu', 'BYD', 'Seal U DM-i five-door SUV', 2025, 'WA2', 'Snow White'),
    ('mgzs', 'MG', 'ZS five-door compact SUV (first generation, pre-facelift)', 2019, '',
     "MG's bright orange"),
    ('jaecoo', 'Jaecoo', '5 (also sold as the Jaecoo J5) five-door compact SUV', 2026, '',
     'Graphite Grey Metallic'),
)

# Answers about the key, the account or the model name: every other car would
# get the same, so the run stops.
STOP_STATUSES = (401, 403, 404)

WHEEL_QUESTION = (
    "This is a studio photo of a car. Where is its steering wheel, from the car's own "
    "point of view (its right-hand side is on your right when you sit in the car facing "
    "forward)? Answer with one word only: RIGHT if it has a single steering wheel on its "
    "right-hand side, LEFT if the single steering wheel is on its left-hand side, BOTH if "
    "you can see more than one steering wheel, UNSURE if you cannot tell.")
VERDICTS = ('RIGHT', 'LEFT', 'BOTH', 'UNSURE')


def paint_words(make, code, fallback):
    """The paint as the prompt names it: the catalogue's name and hex when it
    knows the code (the site's own lookup rules), else the words given."""
    if code:
        hex_value, name, _canonical = PaintLookup.lookup_with_canonical(make, code)
        if name:
            return (f'{name} ({make} paint code {code}'
                    + (f', hex {hex_value}' if hex_value else '') + ')')
        return f'{fallback} ({make} paint code {code})'
    return fallback


def prompt_for(make, car, year, paint):
    """The recipe (see the top of this file). The wheel is placed by near and
    far, never by "right", which the model read as the picture's right."""
    return (f'Photorealistic studio photo of a {year} {make} {car}, painted {paint}. '
            "It is a British right-hand-drive car. The camera stands in front of the car, "
            "off to the driver's side, so the front of the car points towards the right side "
            "of the image. The driver's seat and its steering wheel are on the side of the car "
            "nearest the camera, just behind the door mirror closest to the camera. The front "
            "seat on the far side is the passenger seat and has no steering wheel. "
            'Transparent background with a soft shadow under the car, '
            'no people, no text, no number plate.')


def wheel_verdict(text):
    """RIGHT, LEFT, BOTH or UNSURE from the checker's answer; anything else is UNSURE."""
    m = re.match(r'\W*(RIGHT|LEFT|BOTH|UNSURE)\b', text or '', re.IGNORECASE)
    return m.group(1).upper() if m else 'UNSURE'


def reply_text(data):
    """The text of a Responses API reply."""
    if isinstance(data.get('output_text'), str):
        return data['output_text']
    for item in data.get('output') or []:
        for part in (item.get('content') or []) if isinstance(item, dict) else []:
            if isinstance(part, dict) and part.get('type') == 'output_text':
                return part.get('text') or ''
    return ''


def cost_of(model, usage):
    """What OpenAI charged for one call, from the tokens it reports, or None."""
    rates = PRICES.get(model)
    if not rates or not isinstance(usage, dict):
        return None
    tokens_in = usage.get('input_tokens') or 0
    tokens_out = usage.get('output_tokens') or 0
    if not (tokens_in or tokens_out):
        return None
    return tokens_in * rates[0] / 1e6 + tokens_out * rates[1] / 1e6


class Command(BaseCommand):
    help = 'Make AI pictures of cars in their paint with OpenAI (a trial; nothing on the site uses them)'

    def add_arguments(self, parser):
        parser.add_argument('--only', default='', help='sample cars by short name, e.g. golf,jaecoo')
        parser.add_argument('--candidates', type=int, default=1, choices=(1, 2, 3, 4),
                            help='pictures per car, each one paid for')
        parser.add_argument('--check-only', action='store_true',
                            help='check the pictures already made; make none')
        parser.add_argument('--make', default='', help='a car of your own: its make')
        parser.add_argument('--car', default='', help='the car in words, e.g. "Golf Mk7.5 SE five-door hatchback"')
        parser.add_argument('--year', type=int, default=0)
        parser.add_argument('--code', default='', help='its paint code, looked up in the catalogue')
        parser.add_argument('--paint', default='', help='the paint in words, if the catalogue lacks the code')
        parser.add_argument('--out', default='', help=argparse.SUPPRESS)   # the battery's own folder

    def _post(self, url, key, payload):
        """(status, data, seconds, message); never raises."""
        started = time.monotonic()
        try:
            # The shared session, like every outbound call (F12): a
            # connection failure is sent once, never retried into a second
            # paid picture.
            r = get_session().post(
                url, timeout=300, json=payload,
                headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'})
        except requests.RequestException as exc:
            return None, {}, time.monotonic() - started, f'request failed: {type(exc).__name__}'
        seconds = time.monotonic() - started
        try:
            data = r.json()
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        message = ''
        if r.status_code != 200:
            err = data.get('error')
            message = (err.get('message') if isinstance(err, dict) else '') or r.text[:300]
        return r.status_code, data, seconds, message

    def _check(self, key, path):
        """(verdict, cost, note) for one picture file."""
        b64 = base64.b64encode(path.read_bytes()).decode()
        status, data, _seconds, message = self._post(RESPONSES_URL, key, {
            'model': CHECKER_MODEL,
            'input': [{'role': 'user', 'content': [
                {'type': 'input_text', 'text': WHEEL_QUESTION},
                {'type': 'input_image', 'image_url': f'data:image/png;base64,{b64}'},
            ]}]})
        if status != 200:
            return None, None, f'check failed: HTTP {status} {message}'.strip()
        return wheel_verdict(reply_text(data)), cost_of(CHECKER_MODEL, data.get('usage')), ''

    def handle(self, *args, **opts):
        key = os.environ.get('OPENAI_API_KEY', '').strip()
        if not key:
            raise CommandError('No OPENAI_API_KEY: env.py sets it locally.')
        out = Path(opts['out']) if opts['out'] else Path(settings.BASE_DIR) / 'car_pictures'

        if opts['make']:
            if not (opts['car'] and opts['year'] and (opts['code'] or opts['paint'])):
                raise CommandError('A car of your own needs --make, --car, --year and --code or --paint.')
            short = re.sub(r'[^a-z0-9]+', '-', f"{opts['make']} {opts['car']} {opts['year']}".lower()).strip('-')
            cars = [(short, opts['make'], opts['car'], opts['year'], opts['code'],
                     opts['paint'] or 'its factory paint')]
        else:
            only = {s.strip().lower() for s in opts['only'].split(',') if s.strip()}
            cars = [c for c in SAMPLES if not only or c[0] in only]
            if not cars:
                raise CommandError(f"No sample car called {opts['only']!r}: "
                                   + ', '.join(c[0] for c in SAMPLES))

        costs, verdicts, seconds_taken = [], [], []

        def check(path):
            verdict, cost, note = self._check(key, path)
            if cost is not None:
                costs.append((cost, 0))
            if verdict:
                verdicts.append(verdict)
            return (f'  wheel {verdict}' if verdict else f'  {note}') + (
                f' (check about ${cost:.4f})' if cost is not None else '')

        if opts['check_only']:
            self.stdout.write(f'Checking the pictures already in {out}')
            for short, *_rest in cars:
                paths = sorted(out.glob(f'{short}.png')) + sorted(out.glob(f'{short}-*.png'))
                if not paths:
                    self.stdout.write(f'  {short:<8} no picture to check')
                for path in paths:
                    self.stdout.write(f'  {path.name:<12}' + check(path))
        else:
            self.stdout.write(f"{len(cars)} car(s) x {opts['candidates']} picture(s), saving to {out}")
            stop = False
            for short, make, car, year, code, fallback in cars:
                prompt = prompt_for(make, car, year, paint_words(make, code, fallback))
                for number in range(1, opts['candidates'] + 1):
                    name = f'{short}.png' if opts['candidates'] == 1 else f'{short}-{number}.png'
                    status, data, seconds, message = self._post(IMAGES_URL, key, {
                        'model': AI_MODEL, 'prompt': prompt, 'size': SIZE, 'quality': QUALITY,
                        'n': 1, 'background': 'transparent', 'output_format': 'png'})
                    if status != 200:
                        self.stdout.write(f'  {name:<12} HTTP {status} after {seconds:.0f}s: {message}')
                        if status in STOP_STATUSES:
                            self.stdout.write('  (about the key, the account or the model name: stopping)')
                            stop = True
                            break
                        continue
                    items = [i.get('b64_json') for i in (data.get('data') or [])
                             if isinstance(i, dict) and i.get('b64_json')]
                    if not items:
                        self.stdout.write(f'  {name:<12} no picture in the reply after {seconds:.0f}s')
                        continue
                    out.mkdir(parents=True, exist_ok=True)
                    path = out / name
                    path.write_bytes(base64.b64decode(items[0]))
                    seconds_taken.append(seconds)
                    cost = cost_of(AI_MODEL, data.get('usage'))
                    if cost is not None:
                        costs.append((cost, 1))
                    self.stdout.write(f'  {name:<12} {seconds:5.1f}s  saved'
                                      + (f'  about ${cost:.3f}' if cost is not None else '')
                                      + check(path))
                if stop:
                    break

        if seconds_taken:
            self.stdout.write(f'\nSeconds per picture: average {sum(seconds_taken) / len(seconds_taken):.1f}, '
                              f'fastest {min(seconds_taken):.1f}, slowest {max(seconds_taken):.1f}')
        if verdicts:
            self.stdout.write('Steering wheel: ' + ', '.join(
                f'{v} {verdicts.count(v)}' for v in VERDICTS if verdicts.count(v)))
        if costs:
            self.stdout.write(f'Cost as OpenAI counted it: about ${sum(c for c, _ in costs):.2f} '
                              f'for {sum(n for _, n in costs)} picture(s) and {len(verdicts)} check(s)')
        if seconds_taken:
            self.stdout.write(f'Pictures are in {out}')
