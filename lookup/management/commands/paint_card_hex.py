"""paint286: a swatch from the picture model's own idea of a paint.

Asks the same OpenAI image model that draws the cars for a FLAT paint sample
card (no car, no shading), measures the middle of the card, and prints that
colour beside the catalogue swatch. Measuring a drawn car failed (7 Oct): its
studio shading made every reading darker than the eye sees. A flat card has no
shading, and it comes from the model's own idea of the paint, the one it paints
cars with, so a swatch taken from it should agree with the pictures.

READ-ONLY: prints, writes nothing. About 1.1 to 1.7p per card.

    python manage.py paint_card_hex bmw:300 volkswagen:M7P suzuki:Y7M
"""
import io

from django.core.management.base import BaseCommand, CommandError

from lookup.models import PaintLookup
from lookup.services import ai_pictures

PROMPT = ('A flat automotive paint sample card filling the whole frame, sprayed in {name} '
          '({make} paint code {code}), seen straight on under even, neutral daylight. '
          'One uniform colour edge to edge: no text, no shadow, no gradient, no reflection, no background.')


def card_hex(png_bytes):
    """The median colour of the middle of the card, as #RRGGBB, or '' if unreadable."""
    from PIL import Image
    im = Image.open(io.BytesIO(png_bytes)).convert('RGBA')
    w, h = im.size
    px = [p for p in im.crop((int(w * .3), int(h * .3), int(w * .7), int(h * .7))).resize((64, 64)).getdata() if p[3] > 200]
    if not px:
        return ''
    mid = [sorted(c[i] for c in px)[len(px) // 2] for i in range(3)]
    return '#%02X%02X%02X' % tuple(mid)


class Command(BaseCommand):
    help = 'Print a swatch measured from a flat paint card drawn by the picture model (changes nothing).'

    def add_arguments(self, parser):
        parser.add_argument('codes', nargs='+', help='make:code pairs, for example bmw:300')

    def handle(self, *args, **opts):
        if not ai_pictures.openai_key():
            raise CommandError('OPENAI_API_KEY is not set here; run this in the Railway console.')
        total = 0.0
        for item in opts['codes']:
            make, _, code = item.partition(':')
            row = PaintLookup.all_objects.filter(manufacturer=PaintLookup.normalize_manufacturer(make), code=code.strip()).first()
            if row is None:
                self.stdout.write(f'{item}: not in the catalogue, skipped')
                continue
            pic = ai_pictures.draw(PROMPT.format(name=row.name, make=make.strip().title(), code=row.code))
            total += pic.cost or 0
            got = card_hex(pic.data) if getattr(pic, 'data', None) else ''
            self.stdout.write(f'{row.manufacturer} {row.code} {row.name!r}: catalogue {row.hex or "-"}, card {got or "unreadable (" + str(pic.status) + ")"}')
        self.stdout.write(f'cost about ${total:.3f}; nothing was changed')
