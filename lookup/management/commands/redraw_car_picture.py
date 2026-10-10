"""Draw a car's stored picture again (paint229).

A picture is kept per registration and paint code, so correcting a paint's
colour in the catalogue does not change a picture already drawn. This redraws
it now, from the latest lookup of that registration that found a code, with
today's catalogue colours, and replaces the stored file.

    python manage.py redraw_car_picture CD34EFG
    python manage.py redraw_car_picture CD34EFG AB12CDE

Costs one picture and one wheel check, about 1.7 cents, per car.
"""
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from lookup.models import CarPicture, Search
from lookup.services import ai_pictures as ap
from lookup.services import picture_store
from lookup.services.protection import normalize_registration


class Command(BaseCommand):
    help = "Redraw a car's stored picture with today's catalogue colours"

    def add_arguments(self, parser):
        parser.add_argument('regs', nargs='+', metavar='REG')

    def handle(self, *args, **opts):
        if not (ap.openai_key() and picture_store.configured()):
            raise CommandError('Pictures need OPENAI_API_KEY and the R2 settings.')
        for raw in opts['regs']:
            reg = normalize_registration(raw)
            search = (Search.objects.filter(registration=reg).exclude(paint_code='')
                      .exclude(make='').order_by('-timestamp').first())
            if search is None:
                self.stdout.write(f'{reg}: no lookup of this registration found a code (no code, no picture)')
                continue
            code = search.paint_code.strip()[:50]
            pic, _created = CarPicture.objects.get_or_create(registration=reg, paint_code=code,
                                                             defaults={'search': search})
            old_key = pic.file_key
            old_verdict = pic.verdict
            CarPicture.objects.filter(id=pic.id).update(status=CarPicture.PENDING, search=search, error='',
                                                        verdict='', started_at=timezone.now())
            ap.make_picture(pic.id)
            pic.refresh_from_db()
            if pic.status != CarPicture.READY:
                self.stdout.write(f'{reg} {code}: failed ({pic.error}); the old picture is kept' if old_key
                                  else f'{reg} {code}: failed ({pic.error})')
                if old_key:
                    # paint308: THE OLD PICTURE'S WHEEL VERDICT COMES BACK WITH IT.
                    # The verdict is blanked above, before the new picture is
                    # drawn, and only the status and the file were put back here.
                    # So after a redraw that failed, the picture still shown was
                    # recorded as never checked: it dropped out of the
                    # dashboard's "wrong wheel" count, even when LEFT was the
                    # reason for redrawing it. Reproduced on a scratch copy with
                    # the picture model faked to fail: RIGHT before, blank after.
                    CarPicture.objects.filter(id=pic.id).update(status=CarPicture.READY, file_key=old_key,
                                                                verdict=old_verdict)
                continue
            if old_key and old_key != pic.file_key:
                try:
                    picture_store.delete(old_key)
                except Exception as exc:          # noqa: BLE001 - an orphaned old file is harmless
                    self.stdout.write(f'  (could not delete the old file {old_key}: {type(exc).__name__})')
            self.stdout.write(f'{reg} {code}: redrawn as {pic.painted}\n'
                              f'  {pic.url}  wheel {pic.verdict or "?"}  about ${pic.cost}')
