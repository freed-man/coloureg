"""Propose catalogue hexes with the Claude API, and write only what verifies.

paint165. Two jobs, one shape:

    manage.py propose_hexes --contradicted      210 rows whose hex contradicts
                                                its own colour name
    manage.py propose_hexes --missing           42,833 rows with no hex at all

THE MODEL PROPOSES, A DETERMINISTIC CHECK DECIDES. Every proposal must land in
the colour family the row's own NAME states, tested with the same
`_colour_families` / `_hex_family` pair the mmw gate uses. A proposal that fails
is discarded, not reviewed: there is no partial credit and no human in the loop
per row, so the check has to be the gate.

WRITES ARE LOCKED. A row with no hex is empty because no scraper has one for
it, so an unlocked value is blanked by the next load writing the source's empty
string back over it. `hex` goes into `locked_fields` on every write.

THE KEY COMES FROM THE ENVIRONMENT, never from an argument, so it cannot end up
in shell history or a process list:

    export ANTHROPIC_API_KEY=sk-...
    manage.py propose_hexes --contradicted --limit 25          # preview
    manage.py propose_hexes --contradicted --limit 25 --apply  # write

Nothing is written without --apply. Start small, look at the output, then widen.
"""
import json
import os
import time

from django.core.management.base import BaseCommand, CommandError

from lookup.models import PaintLookup

MODEL = 'claude-sonnet-4-6'
API_URL = 'https://api.anthropic.com/v1/messages'

#: Hues that are genuinely different, as opposed to a classifier boundary. A
#: dark teal named green reading blue is imprecision, not a contradiction —
#: the same adjacency table paint143 settled on.
_ADJACENT = {
    'red': {'orange', 'pink', 'purple', 'brown'},
    'orange': {'red', 'yellow', 'brown', 'gold'},
    'yellow': {'orange', 'green', 'gold', 'brown'},
    'gold': {'yellow', 'orange', 'brown'},
    'green': {'yellow', 'blue'},
    'blue': {'green', 'purple'},
    'purple': {'blue', 'pink', 'red'},
    'pink': {'purple', 'red'},
    'brown': {'red', 'orange', 'yellow', 'gold'},
}

PROMPT = """You are given car paint colours. For each, return the hex that best
represents the finished colour of the paint on a car.

Rules:
- Return ONLY a JSON array, no prose, no markdown fences.
- One object per input, same order, each {"id": <id>, "hex": "#RRGGBB"}.
- If you are not confident what colour the name describes, use null for the hex
  rather than guessing. A null is discarded; a wrong answer is not.
- Names may be in German, French, Italian, Spanish or Portuguese.
- For a PEARL or METALLIC finish give the base coat as a paint supplier would
  mix it, not the flake appearance in sunlight.

Colours:
"""


class Command(BaseCommand):
    help = 'Propose catalogue hexes with the Claude API; write only what verifies.'

    def add_arguments(self, parser):
        g = parser.add_mutually_exclusive_group(required=True)
        g.add_argument('--contradicted', action='store_true',
                       help='Rows whose current hex contradicts their own name.')
        g.add_argument('--missing', action='store_true',
                       help='Rows with no hex at all.')
        parser.add_argument('--limit', type=int, default=25,
                            help='Rows to process (default 25). Start small.')
        parser.add_argument('--batch', type=int, default=25,
                            help='Rows per API call (default 25).')
        parser.add_argument('--make', default='',
                            help='Restrict to one manufacturer.')
        parser.add_argument('--apply', action='store_true',
                            help='Write the verified proposals. Without this, '
                                 'nothing is written.')

    # ------------------------------------------------------------------

    def handle(self, *args, **opt):
        key = os.environ.get('ANTHROPIC_API_KEY', '').strip()
        if not key:
            raise CommandError(
                'ANTHROPIC_API_KEY is not set. Export it rather than passing '
                'it as an argument, so it stays out of shell history.'
            )
        # The SHARED session, not requests directly (F12). It carries the
        # connection pooling and retry policy every other outbound call uses,
        # and a batch run makes a lot of calls.
        from lookup.services.http import get_session
        self._session = get_session()

        from lookup.services.paint_resolver import _colour_families, _hex_family
        self._families = _colour_families
        self._hex_family = _hex_family

        rows = self._select(opt)
        if not rows:
            self.stdout.write('Nothing to do.')
            return
        self.stdout.write(f'{len(rows):,} rows selected'
                          f'{"" if opt["apply"] else "  (preview — nothing will be written)"}')

        accepted = rejected = null = 0
        for i in range(0, len(rows), opt['batch']):
            chunk = rows[i:i + opt['batch']]
            try:
                proposals = self._ask(key, chunk)
            except Exception as exc:  # noqa: BLE001
                self.stderr.write(f'  batch {i // opt["batch"] + 1} failed: {exc}')
                continue
            for row in chunk:
                hexv = proposals.get(row.pk)
                if not hexv:
                    null += 1
                    continue
                ok, why = self._verify(row, hexv)
                if not ok:
                    rejected += 1
                    self.stdout.write(
                        f'  REJECT {row.manufacturer}/{row.code} '
                        f'{(row.name or "")[:26]} -> {hexv}  ({why})')
                    continue
                accepted += 1
                self.stdout.write(
                    f'  ok     {row.manufacturer}/{row.code} '
                    f'{(row.name or "")[:26]} {row.hex or "--"} -> {hexv}')
                if opt['apply']:
                    self._write(row, hexv)
            time.sleep(0.5)     # courtesy, not a rate limit

        self.stdout.write('')
        self.stdout.write(f'  accepted : {accepted:,}'
                          f'{" (written)" if opt["apply"] else " (NOT written)"}')
        self.stdout.write(f'  rejected : {rejected:,}   failed the colour check')
        self.stdout.write(f'  no answer: {null:,}   the model declined to guess')

    # ------------------------------------------------------------------

    def _select(self, opt):
        qs = PaintLookup.objects.all()
        if opt['make']:
            qs = qs.filter(manufacturer=opt['make'].strip().lower())
        # Never revisit a locked row: it has already been decided.
        qs = qs.filter(locked_fields=[])

        # paint166: WORK IN ORDER OF WHETHER A CUSTOMER CAN EVER SEE IT.
        #
        # 42,832 rows have no hex, but only 1,141 have any model coverage. A row
        # with no models is far less likely to be the answer for a real car, so
        # doing them alphabetically spends the first 40,000 calls on rows nobody
        # will look at. Model coverage first, then the rest.
        #
        # Sharper still: of the 984 (make, code) pairs ever DELIVERED to a
        # customer, 36 have no hex. Those are the ones worth doing by hand.
        out = []
        if opt['missing']:
            # TWO PASSES, not an order_by: `models_list` is a JSONField, and
            # ordering on it sorts by the JSON value rather than by length, so
            # `-models_list` put the empty rows first — the opposite of what is
            # wanted. Ask for the populated ones explicitly instead.
            for qs_pass in (qs.filter(hex='').exclude(models_list=[]),
                            qs.filter(hex='', models_list=[])):
                for r in qs_pass.order_by('manufacturer', 'code').iterator():
                    if not (r.name or '').strip():
                        continue
                    out.append(r)
                    if len(out) >= opt['limit']:
                        return out
            return out
        for r in qs.iterator():
            if not (r.name or '').strip():
                continue
            if r.hex and self._contradicts(r):
                out.append(r)
                if len(out) >= opt['limit']:
                    break
        return out

    def _contradicts(self, row):
        fam = self._families(row.name)
        if not fam:
            return False
        hf = self._hex_family(row.hex)
        if hf not in _ADJACENT:
            return False
        named = fam & set(_ADJACENT)
        if not named or hf in named:
            return False
        return not any(n in _ADJACENT[hf] for n in named)

    # ------------------------------------------------------------------

    def _ask(self, key, chunk):
        lines = [f'{r.pk}: {r.manufacturer} {r.code} "{r.name}"' for r in chunk]
        resp = self._session.post(
            API_URL,
            headers={'x-api-key': key,
                     'anthropic-version': '2023-06-01',
                     'content-type': 'application/json'},
            json={'model': MODEL, 'max_tokens': 2000,
                  'messages': [{'role': 'user',
                                'content': PROMPT + '\n'.join(lines)}]},
            timeout=90,
        )
        resp.raise_for_status()
        text = ''.join(b.get('text', '') for b in resp.json().get('content', []))
        text = text.strip().removeprefix('```json').removeprefix('```').removesuffix('```')
        out = {}
        for item in json.loads(text):
            try:
                out[int(item['id'])] = (item.get('hex') or '').strip()
            except (TypeError, ValueError, KeyError):
                continue
        return out

    def _verify(self, row, hexv):
        """The gate. A proposal must be well formed AND agree with the name."""
        h = (hexv or '').strip()
        if len(h) != 7 or not h.startswith('#'):
            return False, 'malformed'
        try:
            int(h[1:], 16)
        except ValueError:
            return False, 'malformed'
        want = self._families(row.name)
        if not want:
            # No colour word in the name means nothing to check against, and
            # unknown is not approval — the same rule the mmw gate follows.
            return False, 'the name states no colour to verify against'
        got = self._hex_family(h)
        if got is None:
            return False, 'unreadable'
        if got in want:
            return True, ''
        if any(g in _ADJACENT.get(got, set()) for g in want):
            return True, ''
        return False, f'proposed {got}, name says {sorted(want)}'

    def _write(self, row, hexv):
        locked = list(row.locked_fields or [])
        if 'hex' not in locked:
            locked.append('hex')
        PaintLookup.all_objects.filter(pk=row.pk).update(hex=hexv,
                                                         locked_fields=locked)
