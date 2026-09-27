"""Prove the picture store works before anything relies on it (paint218).

Uploads a small text file to the R2 bucket, reads it back through the public
address (R2_PUBLIC_URL), and deletes it. Writes nothing to the database.

    python manage.py check_picture_store
"""
import uuid

from django.core.management.base import BaseCommand, CommandError

from lookup.services import picture_store as store
from lookup.services.http import get_session


class Command(BaseCommand):
    help = 'Upload, read back and delete a test file in the R2 picture store'

    def handle(self, *args, **opts):
        missing = [n for n in store.SETTINGS if not store.setting(n)]
        if missing:
            raise CommandError('Missing: ' + ', '.join(missing) + ' (Railway variables, and env.py locally)')
        key = f'checks/coloureg-check-{uuid.uuid4().hex}.txt'
        body = f'coloureg picture store check {key}'.encode()
        try:
            store.upload(key, body, 'text/plain')
        except Exception as exc:          # noqa: BLE001 - report whatever R2 said
            raise CommandError(f'Upload failed: {type(exc).__name__}: {exc}') from exc
        self.stdout.write(f'uploaded   {key}')
        url = store.public_url(key)
        try:
            r = get_session().get(url, timeout=30)
            read_ok = r.status_code == 200 and r.content == body
            detail = f'HTTP {r.status_code}'
        except Exception as exc:          # noqa: BLE001
            read_ok, detail = False, type(exc).__name__
        self.stdout.write(f"read back  {url}  {'OK' if read_ok else 'FAILED: ' + detail}")
        try:
            store.delete(key)
            self.stdout.write('deleted    the test file')
        except Exception as exc:          # noqa: BLE001
            self.stdout.write(f'delete failed: {type(exc).__name__} (remove {key} by hand)')
        if not read_ok:
            raise CommandError('Uploading works but the public address does not serve the file: '
                               'check the bucket\'s custom domain is connected and Active.')
        self.stdout.write('The picture store works.')
