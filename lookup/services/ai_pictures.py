"""AI pictures of cars in their paint (paint217): the part the car_pictures command and the site share.

Nothing here touches the database except reading the catalogue for a paint's
name and swatch hex. Every outbound call goes through the shared session
(rule F12): a dropped connection is sent once, never retried into a second
paid picture.

HOW THE RECIPE WAS FOUND (26 and 27 Sep; the full story is in the paint217
handoff):
  * The steering wheel came out left-hand drive 0 of 12 times while the
    prompt said "right" in two senses a few words apart (the car's right-hand
    side and the picture's right side, opposite ends of the windscreen from
    this angle). Placing it by NEAR and FAR fixed it: 24 of 24, and every
    picture since.
  * The camera stands at the front corner on the driver's side, so every car
    points the same way.
  * gpt-image-2.5-sunburst at MEDIUM quality: about 15 seconds and 1.1 cents a
    picture (the operator's choice; low was 13.5 seconds and 0.5 cents, high
    26.6 seconds and 4.2 cents).
  * Tried and dropped: gpt-image-2.5-flare (no faster, a wrong wheel); a
    DVLA-colour draft repainted when the code arrives (later and dearer than
    one picture); Google's Nano Banana 2 (10 seconds but about 5p a picture,
    no transparent background, one car turned round); Black Forest Labs'
    FLUX.2 [klein] (4.6 seconds but mostly left-hand drive, some drawing
    errors) and FLUX.2 [pro] (11.5 seconds, 4.5 cents, 3 of 5 wrong).
  * A vision model (gpt-6-sol) checks the wheel: RIGHT, LEFT, BOTH or UNSURE.
    On the site it only records the verdict; a wrong wheel is not redone.
"""
import base64
import concurrent.futures
import io
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

import requests
from django.db import IntegrityError, connection
from django.utils import timezone

from lookup.models import CarPicture, PaintLookup, Search
from lookup.services import picture_store
from lookup.services.http import get_session
from lookup.services.paint_resolver import _colour_families

logger = logging.getLogger(__name__)

OPENAI_IMAGES_URL = 'https://api.openai.com/v1/images/generations'
OPENAI_RESPONSES_URL = 'https://api.openai.com/v1/responses'
AI_MODEL = 'gpt-image-2.5-sunburst'
QUALITY = 'medium'
CHECKER_MODEL = 'gpt-6-sol'

# Per 1M tokens, from OpenAI's pricing page on 26 Sep 2026, standard
# processing: (text in, picture in, out).
PRICES = {
    AI_MODEL: (5.00, 8.00, 30.00),
    CHECKER_MODEL: (2.00, 2.00, 10.00),
}

# Answers about the key, the account or the model name: every other car would
# get the same.
STOP_STATUSES = (401, 403, 404)

WHEEL_QUESTION = (
    "This is a studio photo of a car. Where is its steering wheel, from the car's own "
    "point of view (its right-hand side is on your right when you sit in the car facing "
    "forward)? Answer with one word only: RIGHT if it has a single steering wheel on its "
    "right-hand side, LEFT if the single steering wheel is on its left-hand side, BOTH if "
    "you can see more than one steering wheel, UNSURE if you cannot tell.")
VERDICTS = ('RIGHT', 'LEFT', 'BOTH', 'UNSURE')


@dataclass
class Picture:
    """One drawing: the picture, or why there is none."""
    data: bytes = b''
    mime: str = ''
    seconds: float = 0.0
    cost: float = None            # dollars, when the price is known
    tokens: int = 0
    status: int = None            # HTTP status; None if the request failed
    message: str = ''

    @property
    def ok(self):
        return bool(self.data)

    @property
    def stop(self):
        return self.status in STOP_STATUSES


def openai_key():
    return os.environ.get('OPENAI_API_KEY', '').strip()


def _one_paint(make, part):
    return (f"{part.get('name') or part.get('code')} ({make} paint code {part.get('code')}"
            + (f", hex {part['hex']}" if part.get('hex') else '') + ')')


# paint239: names that say the code is not one colour (special or individual paint).
NOT_ONE_COLOUR = ('special paint', 'special order', 'individual', 'sonderlack', 'bespoke', 'personal line')


def _not_one_colour(name):
    low = (name or '').lower()
    return any(w in low for w in NOT_ONE_COLOUR) and not _colour_families(name)


def paint_words(make, code, name, dvla_colour):
    """The paint as the prompt names it: the catalogue's name and hex when it
    knows the code, else the name the lookup found, else DVLA's colour.

    paint229: A TWO-TONE NAMES BOTH PAINTS, EACH WITH ITS OWN HEX, and which
    is the body. The combination row carries one hex at most, and for 2VN it
    was the black roof's, so the prompt gave the model no colour at all for
    the Lunar Rock body, and it drew a warm beige-grey from the name alone."""
    if code:
        try:
            parts = PaintLookup.two_tone_parts(make, code, vdg_colour=dvla_colour)
        except Exception:
            parts = None
        if parts:
            body = next((p for p in parts if p.get('is_body')), None)
            if body is not None:
                rest = ' and '.join(_one_paint(make, p) for p in parts if p is not body)
                return f'two-tone, the body in {_one_paint(make, body)} and the roof in {rest}'
            return 'two-tone, ' + ' and '.join(_one_paint(make, p) for p in parts)
        # paint239: the car's colour goes in, so a code covering two paints is
        # drawn as the one this car has (the same pick the page makes).
        hex_value, cat_name, _canonical = PaintLookup.lookup_with_canonical(make, code, vdg_colour=dvla_colour)
        words = cat_name or name
        if words:
            if not hex_value and dvla_colour and _not_one_colour(words):
                # paint239: a paint that is not one colour (BMW 490, "BMW
                # Individual special paint") would leave the colour to the model's
                # guess; the registered colour is the best we know. Only for such
                # names: a German "Royalblau" states its colour inside the word,
                # and its prompt stays exactly as it was.
                return f'{dvla_colour.lower()} ({words}, {make} paint code {code})'
            return (f'{words} ({make} paint code {code}'
                    + (f', hex {hex_value}' if hex_value else '') + ')')
    return (dvla_colour or 'its factory paint').lower()


# paint234: the operator's idea, the brand on the front number plate, UK style
# with the blue UK band (his choice of style 2). Trialled on six cars: the plate
# spelled right 6 of 6, the wheel still right 6 of 6, 16.4 seconds on average.
# paint235: every picture now carries it, and the checker reads the plate back
# after the wheel (recorded in CarPicture.plate, never redrawn automatically).
PLATE_TEXT = 'COLOUREG'
PLATE_QUESTION = ('Read the front number plate of the car in this picture. Reply with only the '
                  'characters on it, or NONE if there is no readable front plate.')


def prompt_for(year, make, model, paint, plate=None):
    """The recipe. The wheel is placed by near and far, never by "right".
    With `plate`, the front plate carries it (paint234 trial)."""
    ending = ('Transparent background with a soft shadow under the car, '
              'no people, no text, no number plate.')
    if plate:
        ending = ('Transparent background with a soft shadow under the car, no people. '
                  'The front number plate is a UK front plate: white, with a narrow blue band at '
                  'its left end carrying the white letters UK, and the black characters '
                  f'{plate} in the standard UK number plate font. There is no other text anywhere.')
    return (f'Photorealistic studio photo of a {year} {make} {model}, painted {paint}. '
            "It is a British right-hand-drive car. The camera stands in front of the car, "
            "off to the driver's side, so the front of the car points towards the right side "
            "of the image. The driver's seat and its steering wheel are on the side of the car "
            "nearest the camera, just behind the door mirror closest to the camera. The front "
            "seat on the far side is the passenger seat and has no steering wheel. "
            + ending)


def search_prompt(search, plate=None):
    """(prompt, paint words) for a saved lookup."""
    paint = paint_words(search.make, search.paint_code, search.paint_description, search.colour)
    return prompt_for(search.year or '', search.make, search.model, paint, plate=plate), paint


def _post(url, headers, payload):
    """(status, data, seconds, message); never raises."""
    started = time.monotonic()
    try:
        r = get_session().post(url, timeout=300, json=payload,
                               headers=dict(headers, **{'Content-Type': 'application/json'}))
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


def cost_of(model, usage):
    """What OpenAI charged for one call, from the tokens it reports, or None."""
    rates = PRICES.get(model)
    if not rates or not isinstance(usage, dict):
        return None
    tokens_in = usage.get('input_tokens') or 0
    tokens_out = usage.get('output_tokens') or 0
    if not (tokens_in or tokens_out):
        return None
    details = usage.get('input_tokens_details')
    picture_in = (details.get('image_tokens') or 0) if isinstance(details, dict) else 0
    return ((tokens_in - picture_in) * rates[0] + picture_in * rates[1] + tokens_out * rates[2]) / 1e6


def draw(prompt, key=None):
    """One picture, transparent PNG, medium quality."""
    status, data, seconds, message = _post(
        OPENAI_IMAGES_URL, {'Authorization': f'Bearer {key or openai_key()}'},
        {'model': AI_MODEL, 'prompt': prompt, 'size': '1536x1024', 'quality': QUALITY,
         'n': 1, 'background': 'transparent', 'output_format': 'png'})
    pic = Picture(seconds=seconds, status=status, message=message)
    if status == 200:
        items = [i.get('b64_json') for i in (data.get('data') or [])
                 if isinstance(i, dict) and i.get('b64_json')]
        if items:
            pic.data, pic.mime = base64.b64decode(items[0]), 'image/png'
        usage = data.get('usage') or {}
        pic.cost = cost_of(AI_MODEL, usage)
        pic.tokens = (usage.get('total_tokens') or 0) if isinstance(usage, dict) else 0
    return pic


def wheel_verdict(text):
    """RIGHT, LEFT, BOTH or UNSURE from the checker's answer; anything else is UNSURE."""
    m = re.match(r'\W*(RIGHT|LEFT|BOTH|UNSURE)\b', text or '', re.IGNORECASE)
    return m.group(1).upper() if m else 'UNSURE'


def _reply_text(data):
    if isinstance(data.get('output_text'), str):
        return data['output_text']
    for item in data.get('output') or []:
        for part in (item.get('content') or []) if isinstance(item, dict) else []:
            if isinstance(part, dict) and part.get('type') == 'output_text':
                return part.get('text') or ''
    return ''


def plate_reading(text):
    """What the checker read on the plate, as one run of letters and digits;
    '' for NONE or nothing readable. A plate split by a space ("COLOU REG") is
    joined; the UK band's letters are not part of it; if the checker wrapped
    the plate in a sentence, the longest word is the plate."""
    words = [w for w in re.findall(r'[A-Z0-9]+', (text or '').upper()) if w not in ('UK', 'GB')]
    if not words or words == ['NONE']:
        return ''
    return ''.join(words) if len(words) <= 2 else max(words, key=len)


def read_plate(data, mime='image/png', key=None):
    """(what the plate says, cost, note) for one picture (paint234 trial)."""
    key = key or openai_key()
    b64 = base64.b64encode(data).decode()
    status, reply, _seconds, message = _post(OPENAI_RESPONSES_URL, {'Authorization': f'Bearer {key}'}, {
        'model': CHECKER_MODEL,
        'input': [{'role': 'user', 'content': [
            {'type': 'input_text', 'text': PLATE_QUESTION},
            {'type': 'input_image', 'image_url': f'data:{mime};base64,{b64}'},
        ]}]})
    if status != 200:
        return None, None, f'plate check failed: HTTP {status} {message}'.strip()
    return plate_reading(_reply_text(reply)), cost_of(CHECKER_MODEL, reply.get('usage')), ''


def check_wheel(data, mime='image/png', key=None):
    """(verdict, cost, note) for one picture; the verdict is None on failure."""
    key = key or openai_key()
    b64 = base64.b64encode(data).decode()
    status, reply, _seconds, message = _post(OPENAI_RESPONSES_URL, {'Authorization': f'Bearer {key}'}, {
        'model': CHECKER_MODEL,
        'input': [{'role': 'user', 'content': [
            {'type': 'input_text', 'text': WHEEL_QUESTION},
            {'type': 'input_image', 'image_url': f'data:{mime};base64,{b64}'},
        ]}]})
    if status != 200:
        return None, None, f'check failed: HTTP {status} {message}'.strip()
    return wheel_verdict(_reply_text(reply)), cost_of(CHECKER_MODEL, reply.get('usage')), ''


# ---------------------------------------------------------------------------
# paint218: THE FEATURE. Every finished lookup that found a paint code gets
# one picture, drawn in the background and kept in R2, reused for a repeat
# lookup of the same car in the same paint. (paint223: no code, no picture;
# a picture in DVLA's generic colour was more misleading than none.) For now only the operator sees it, via
# the admin panel's "View" link.
#
# paint219: ON wherever the OpenAI and R2 settings are present (Railway, and
# env.py locally), with the limit set here rather than in Railway. The one
# variable left is an emergency brake: CAR_PICTURES=off in Railway stops new
# pictures without a code change. The battery's clone has no R2 settings, so
# nothing there ever draws.
# ---------------------------------------------------------------------------
DAILY_LIMIT = 300            # picture attempts in any 24 hours, about $5 a day at most
STALE_MINUTES = 10           # a picture still "pending" after this was lost with its worker
_executor = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix='car-picture')


def enabled():
    return (os.environ.get('CAR_PICTURES', '').strip().lower() != 'off'
            and bool(openai_key()) and picture_store.configured())


def start_for(search_id):
    """Queue the picture for a finished lookup. Returns its CarPicture, or
    None. Never raises: a picture must never break a lookup."""
    try:
        return _start_for(search_id)
    except Exception:
        logger.exception('car picture: could not start for search %s', search_id)
        return None


def _start_for(search_id):
    if not enabled():
        return None
    search = Search.objects.filter(id=search_id).first()
    if not search or not (search.make or '').strip() or not (search.registration or '').strip():
        return None
    reg, code = search.registration, (search.paint_code or '').strip()[:50]
    if not code:
        return None                           # paint223: no code, no picture
    now = timezone.now()
    existing = CarPicture.objects.filter(registration=reg, paint_code=code).first()
    if existing and (existing.status == CarPicture.READY or (
            existing.status == CarPicture.PENDING
            and existing.started_at > now - timedelta(minutes=STALE_MINUTES))):
        return existing                       # already drawn, or being drawn
    if CarPicture.objects.filter(started_at__gte=now - timedelta(hours=24)).count() >= DAILY_LIMIT:
        logger.warning('car picture: daily limit of %s reached, none for search %s',
                       DAILY_LIMIT, search_id)
        return existing
    if existing:                              # it failed, or its worker died: try again
        CarPicture.objects.filter(id=existing.id).update(
            status=CarPicture.PENDING, search=search, error='', started_at=now)
        pic = existing
    else:
        try:
            pic = CarPicture.objects.create(registration=reg, paint_code=code, search=search)
        except IntegrityError:                # another request started it a moment ago
            return CarPicture.objects.filter(registration=reg, paint_code=code).first()
    _executor.submit(_make_in_thread, pic.id)
    return pic


def _make_in_thread(picture_id):
    try:
        make_picture(picture_id)
    finally:
        connection.close()                    # a worker thread's own connection, not the request's


def to_webp(png):
    """(data, extension, content type): WebP keeps the transparency at a
    fraction of the PNG's size; the PNG itself if the conversion fails."""
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(png))
        img.load()
        out = io.BytesIO()
        img.save(out, format='WEBP', quality=85, method=4)
        return out.getvalue(), 'webp', 'image/webp'
    except Exception:
        logger.exception('car picture: WebP conversion failed, keeping the PNG')
        return png, 'png', 'image/png'


def _fail(picture_id, why):
    CarPicture.objects.filter(id=picture_id).update(status=CarPicture.FAILED, error=why[:200])


def make_picture(picture_id):
    """Draw, store and check one picture. The picture is marked ready before
    the wheel check, which only records its verdict."""
    try:
        pic = CarPicture.objects.select_related('search').get(id=picture_id)
        if pic.search is None:
            _fail(picture_id, 'the lookup is gone')
            return
        prompt, painted = search_prompt(pic.search, plate=PLATE_TEXT)     # paint235
        # paint231: a redraw or retry ADDS to what the picture has cost; it
        # used to replace it, so redraws vanished from the dashboard's cost.
        prior = float(pic.cost or 0)
        drawn = draw(prompt)
        if not drawn.ok:
            _fail(picture_id, f'HTTP {drawn.status}: {drawn.message}')
            return
        data, ext, content_type = to_webp(drawn.data)
        key = f'cars/{uuid.uuid4().hex}.{ext}'
        picture_store.upload(key, data, content_type)
        cost = prior + (drawn.cost or 0.0)
        CarPicture.objects.filter(id=picture_id).update(
            status=CarPicture.READY, file_key=key, painted=painted[:200],
            seconds=round(drawn.seconds, 1), cost=Decimal(str(round(cost, 4))))
        verdict, check_cost, note = check_wheel(drawn.data, drawn.mime)
        read, plate_cost, plate_note = read_plate(drawn.data, drawn.mime)      # paint235
        CarPicture.objects.filter(id=picture_id).update(
            verdict=verdict or '', plate=(read or '')[:20],
            error='; '.join(n for n in (note, plate_note) if n)[:200],
            cost=Decimal(str(round(cost + (check_cost or 0.0) + (plate_cost or 0.0), 4))))
    except Exception as exc:
        logger.exception('car picture %s failed', picture_id)
        _fail(picture_id, f'{type(exc).__name__}: {exc}')
