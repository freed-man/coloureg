"""Re-run the recovery legs on gated makes, once a month (paint210).

A make on the unsupported list skips the recovery legs (the second VDG try,
partslink24, Ezyvin, mmw) for customers: they get their vehicle and the free
manual lookup offer at once. That leaves one question the live site can no
longer answer: has any leg started cracking those makes? This answers it, on
real cars from the search history, and for YOU only.

WRITES NOTHING. No Search row, no cache, no catalogue: resolve_paint is called
with search_id=None, the switch that stops every leg recording onto a row.

IT COSTS MONEY. Each car can spend a VDG paint call and, when Ezyvin finds the
car, its credits. None of that spend appears on the dashboard, because no
Search row carries it.

RUN IT WHERE THE PROVIDER KEYS LIVE: the Railway service (its console, or
`railway ssh`). On a machine without those settings every leg just fails.

    manage.py probe_gated --dry-run            # list the cars, call nothing
    manage.py probe_gated                      # 3 most recent cars per gated make
    manage.py probe_gated --make MG --limit 5
"""
from django.core.management.base import BaseCommand

from lookup.models import Search


def pick_gated_cars(make=None, limit=3):
    """{make: [Search, ...]}: the most recent distinct gated cars per make."""
    qs = (Search.objects.filter(error_message__contains='make_not_automated')
          .exclude(make='').exclude(registration='').order_by('-timestamp'))
    if make:
        qs = qs.filter(make__iexact=make.strip())
    picked, seen = {}, set()
    for s in qs.only('id', 'registration', 'make', 'model', 'year', 'colour',
                     'vin', 'category', 'timestamp').iterator():
        if s.registration in seen:
            continue
        cars = picked.setdefault(s.make, [])
        if len(cars) >= limit:
            continue
        seen.add(s.registration)
        cars.append(s)
    return picked


class Command(BaseCommand):
    help = ('Re-run the recovery legs on recent cars of each gated make and '
            'print what each leg said. Writes nothing. Costs provider money.')

    def add_arguments(self, parser):
        parser.add_argument('--make', default='', help='Only this make.')
        parser.add_argument('--limit', type=int, default=3,
                            help='Cars per make (default 3).')
        parser.add_argument('--dry-run', action='store_true',
                            help='List the cars; call nothing.')

    def handle(self, *args, **opt):
        from lookup.services import paint_resolver
        picked = pick_gated_cars(opt['make'] or None, max(1, opt['limit']))
        total = sum(len(v) for v in picked.values())
        head = 'DRY RUN, nothing called' if opt['dry_run'] else 'PROBE, calling the recovery legs'
        self.stdout.write(f'{head}: {total} cars across {len(picked)} gated makes')
        cracked = {}
        for make, cars in picked.items():
            self.stdout.write(f'\n{make}')
            for s in cars:
                line = f'  {s.registration}  {s.year or "?"} {s.model or ""}'.rstrip()
                if not s.vin:
                    line += '  (no VIN: partslink24 and Ezyvin cannot run)'
                if opt['dry_run']:
                    self.stdout.write(line)
                    continue
                tel = {}
                try:
                    res = paint_resolver.resolve_paint(
                        s.registration, s.vin or '', s.make, s.category or None,
                        telemetry=tel, model=s.model or '', search_id=None,
                        vdg_colour=s.colour or '', year=s.year)
                except Exception as exc:  # noqa: BLE001 - one car must not stop the rest
                    res, tel['error'] = None, type(exc).__name__
                code = (res or {}).get('paint_code') or ''
                if code:
                    cracked[make] = cracked.get(make, 0) + 1
                legs = ', '.join(f'{k[:-8]} {v}' for k, v in sorted(tel.items())
                                 if k.endswith('_outcome') and v)
                if tel.get('error'):
                    legs = (legs + ', ' if legs else '') + f'error {tel["error"]}'
                said = (f'CODE {code} {(res or {}).get("paint_description", "")!r} '
                        f'via {(res or {}).get("source", "?")}') if code else 'no code'
                self.stdout.write(f'{line}\n      -> {said}' + (f'  | {legs}' if legs else ''))
        if not opt['dry_run']:
            self.stdout.write('\nSummary: ' + (', '.join(
                f'{m} {cracked.get(m, 0)}/{len(c)}' for m, c in picked.items()) or 'nothing to probe'))
            if cracked:
                self.stdout.write('A leg cracked a gated make: check those answers before '
                                  'taking the make off the list.')
