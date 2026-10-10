"""Propose catalogue hexes with the Claude API, and write only what verifies.

paint165. Two jobs, one shape:

    manage.py propose_hexes --contradicted      210 rows whose hex contradicts
                                                its own colour name
    manage.py propose_hexes --missing           rows with no hex whose NAME
                                                states a colour (paint205)

THE MODEL PROPOSES, A DETERMINISTIC CHECK DECIDES. Every proposal must land in
the colour family the row's own NAME states, tested with the same
`_colour_families` / `_hex_family` pair the mmw gate uses. A proposal that fails
is discarded, not reviewed: there is no partial credit and no human in the loop
per row, so the check has to be the gate.

WRITES ARE NOT LOCKED (paint167). A generated hex is a guess, and a guess
should yield to a future scrape that has the real value. It used to have to be
locked, because `--upsert` let an empty incoming value erase a populated one;
that is fixed in the loader instead. Locking is for values we have verified.

THE KEY COMES FROM THE ENVIRONMENT, never from an argument, so it cannot end up
in shell history or a process list:

    export ANTHROPIC_API_KEY=sk-...
    manage.py propose_hexes --contradicted --limit 25          # preview
    manage.py propose_hexes --contradicted --limit 25 --apply  # write

Nothing is written without --apply. Start small, look at the output, then widen.

ROWS ALREADY REFUSED ARE NOT ASKED ABOUT AGAIN (paint308, --missing only). A row
the model was asked about and that got no swatch (its proposal failed the colour
check, or it answered null) still has no swatch, so every later run asked about
it first, paid for the answer again and threw it away again. An --apply run now
writes each such row to a list, one line a row, and every later --missing run
leaves those rows out and says how many:

    etc/hex_refused.jsonl     where the list is kept (.gitignore keeps etc/ out
                              of the repo); --skip-file PATH keeps it elsewhere

Nothing to do to use it. To ask about the rows again, delete the file (or, for
one row, its line). A row whose name has been corrected since is asked about
again by itself: the list remembers the name it was refused under. A preview
reads the list and never adds to it.
"""
import json
import os
import re
import time
from datetime import datetime, timezone

from django.core.management.base import BaseCommand, CommandError

from lookup.models import PaintLookup

MODEL = 'claude-sonnet-4-6'
API_URL = 'https://api.anthropic.com/v1/messages'
#: paint308: the rows --missing has asked about and could write no swatch for.
SKIP_FILE = os.path.join('etc', 'hex_refused.jsonl')

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
                       help='Rows with no hex whose name states a colour, '
                            'the only ones a proposal can pass.')
        parser.add_argument('--limit', type=int, default=25,
                            help='Rows to process (default 25). Start small.')
        parser.add_argument('--batch', type=int, default=25,
                            help='Rows per API call (default 25).')
        parser.add_argument('--make', default='',
                            help='Restrict to one manufacturer.')
        parser.add_argument('--apply', action='store_true',
                            help='Write the verified proposals. Without this, '
                                 'nothing is written.')
        parser.add_argument('--skip-file', default=SKIP_FILE,
                            help='With --missing: the list of rows already asked '
                                 'about and refused, which are left out (default '
                                 'etc/hex_refused.jsonl). Delete it to ask again.')

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
        if getattr(self, '_left_out', 0):
            # paint308: say so, or the rows would seem to have vanished.
            self.stdout.write(
                f'{self._left_out:,} rows left out: already asked about and refused '
                f'(listed in {opt.get("skip_file")}; delete that file to ask again)')
        if not rows:
            self.stdout.write('Nothing to do.')
            return
        self.stdout.write(f'{len(rows):,} rows selected'
                          f'{"" if opt["apply"] else "  (preview — nothing will be written)"}')

        accepted = rejected = null = failed = noted = 0
        for i in range(0, len(rows), opt['batch']):
            chunk = rows[i:i + opt['batch']]
            try:
                proposals = self._ask(key, chunk)
            except Exception as exc:  # noqa: BLE001
                # paint169: name the rows too. A bare parser error says nothing
                # about WHICH 25 rows were lost, and on a 1,700-batch run that
                # is the only way to tell a one-off from a pattern.
                self.stderr.write(
                    f'  batch {i // opt["batch"] + 1} failed ({exc}); '
                    f'{len(chunk)} rows skipped, '
                    f'{chunk[0].manufacturer}/{chunk[0].code} onwards')
                failed += len(chunk)
                continue
            refused = []          # paint308: (row, the proposal, why nothing was written)
            for row in chunk:
                hexv = proposals.get(row.pk)
                if not hexv:
                    null += 1
                    # An answer of null is the model declining, and it would
                    # decline again. A row the reply never mentions (a reply cut
                    # short) was not answered at all, so it is not noted.
                    if hexv == '':
                        refused.append((row, '', 'the model declined to guess'))
                    continue
                ok, why = self._verify(row, hexv)
                if not ok:
                    rejected += 1
                    self.stdout.write(
                        f'  REJECT {row.manufacturer}/{row.code} '
                        f'{(row.name or "")[:26]} -> {hexv}  ({why})')
                    refused.append((row, hexv, why))
                    continue
                accepted += 1
                self.stdout.write(
                    f'  ok     {row.manufacturer}/{row.code} '
                    f'{(row.name or "")[:26]} {row.hex or "--"} -> {hexv}')
                if opt['apply']:
                    self._write(row, hexv)
            if opt['missing'] and opt['apply']:
                noted += self._note_refused(opt.get('skip_file'), refused)
            time.sleep(0.5)     # courtesy, not a rate limit

        self.stdout.write('')
        self.stdout.write(f'  accepted : {accepted:,}'
                          f'{" (written)" if opt["apply"] else " (NOT written)"}')
        self.stdout.write(f'  rejected : {rejected:,}   failed the colour check')
        self.stdout.write(f'  no answer: {null:,}   the model declined to guess')
        if noted:
            self.stdout.write(
                f'  noted    : {noted:,}   refused rows listed in {opt.get("skip_file")}, '
                f'not asked about again')
        if failed:
            self.stdout.write(
                f'  batch fail: {failed:,}   rows skipped; run again to retry them')

    # ------------------------------------------------------------------

    def _select(self, opt):
        qs = PaintLookup.objects.all()
        if opt['make']:
            qs = qs.filter(manufacturer=opt['make'].strip().lower())
        # Never revisit a row whose hex has been LOCKED: that is a verified
        # decision. An unlocked generated value IS revisitable, deliberately.
        #
        # Filtered in Python, not with `locked_fields__contains`: that lookup
        # is unsupported on SQLite, which is what the battery runs on. The
        # query would work in production and fail every check.
        def _hex_locked(row):
            return 'hex' in (row.locked_fields or [])

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
            #
            # paint205: ONLY ROWS WHOSE NAME STATES A COLOUR. _verify refuses
            # every proposal for a name with no colour word (unknown is not
            # approval), so sending those rows paid for answers that could only
            # be thrown away. In production on 24 Sep that was 16,334 of the
            # 20,556 rows with no hex: four calls in five, and --limit filled up
            # with them before reaching the 4,222 that can pass. Those rows need
            # a NAME first, not a hex. Same test as _verify, same function, so
            # the two can never disagree about which rows are checkable.
            #
            # paint308: NOR ROWS ALREADY ASKED ABOUT AND REFUSED. A refused row
            # still has no swatch, so it was selected again on every run, ahead
            # of the rows never asked about: the model was paid for the same
            # answer, the check refused it again, and with a small --limit the
            # run never got past them. Reproduced with a faked model: three
            # runs of --limit 3 --apply sent the same two refused rows and the
            # one declined row each time, and a fifth row that would have
            # passed was never asked about. Production's full run of 25 Sep
            # left 883 refused and 4 declined rows in that position. An --apply
            # run now lists what it could not write (see _note_refused) and the
            # listed rows are passed over here, before --limit is counted.
            from lookup.services.paint_resolver import _colour_families
            listed = self._refused(opt.get('skip_file'))
            self._left_out = 0
            for qs_pass in (qs.filter(hex='').exclude(models_list=[]),
                            qs.filter(hex='', models_list=[])):
                for r in qs_pass.order_by('manufacturer', 'code').iterator():
                    if not (r.name or '').strip() or _hex_locked(r):
                        continue
                    if not _colour_families(r.name):
                        continue
                    if (r.manufacturer, r.code, r.name) in listed:
                        self._left_out += 1
                        continue
                    out.append(r)
                    if len(out) >= opt['limit']:
                        return out
            return out
        for r in qs.iterator():
            if not (r.name or '').strip() or _hex_locked(r):
                continue
            if r.hex and self._contradicts(r):
                out.append(r)
                if len(out) >= opt['limit']:
                    break
        return out

    @staticmethod
    def _refused(path):
        """paint308: the rows the list says not to ask about again, each as
        (make, code, the name it was refused under). A line that cannot be
        read is passed over, as research_codes does with its review file."""
        listed = set()
        if path and os.path.exists(path):
            with open(path, encoding='utf-8') as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(r, dict):
                        listed.add((r.get('manufacturer'), r.get('code'), r.get('name')))
        return listed

    def _note_refused(self, path, refused):
        """paint308: add this batch's refused rows to the list. Returns how
        many were added. Appended batch by batch, so a run that is stopped
        half way keeps what it has paid for. Never raises: the answers are
        already paid for, and a list that cannot be saved (a read-only folder,
        as in the Railway console) must not stop the swatches being written."""
        if not (path and refused):
            return 0
        try:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            with open(path, 'a', encoding='utf-8') as f:
                for row, hexv, why in refused:
                    f.write(json.dumps({
                        'manufacturer': row.manufacturer, 'code': row.code, 'name': row.name,
                        'proposed': hexv, 'why': why, 'model': MODEL,
                        'asked_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                    }, ensure_ascii=False) + '\n')
        except OSError as exc:
            self.stderr.write(f'  could not save the refused rows to {path} ({exc}); '
                              f'they will be asked about again next time')
            return 0
        return len(refused)

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
            json={'model': MODEL, 'max_tokens': 4000,
                  'messages': [{'role': 'user',
                                'content': PROMPT + '\n'.join(lines)}]},
            timeout=90,
        )
        resp.raise_for_status()
        text = ''.join(b.get('text', '') for b in resp.json().get('content', []))
        return self._parse(text)

    @staticmethod
    def _parse(text):
        """Pull id/hex pairs out of a reply, however it arrives.

        paint169. `json.loads` on the whole reply failed on the first real run
        and took the batch with it: a prose preamble, a markdown fence, a
        trailing comma or a truncated array all raise, and one bad character
        discarded 25 rows.

        So the pairs are extracted DIRECTLY rather than by parsing the
        document. Anything shaped like an id next to a hex is read, and
        everything around it is ignored — including a half-written final object
        when the reply ran out of tokens.

        A null hex stays a miss, which is the model declining to guess, and the
        verifier still decides every value that survives.
        """
        out = {}
        for m in re.finditer(
                r'"?id"?\s*:\s*(\d+)\s*,\s*"?hex"?\s*:\s*'
                r'(?:"(#[0-9A-Fa-f]{6})"|null)', text or ''):
            out[int(m.group(1))] = (m.group(2) or '').strip()
        if out:
            return out
        # Nothing matched. Try the whole document, in case a future reply is
        # shaped differently — and if that fails too, say what came back rather
        # than raising a parser error with no context.
        try:
            for item in json.loads(
                    (text or '').strip().removeprefix('```json')
                    .removeprefix('```').removesuffix('```')):
                out[int(item['id'])] = (item.get('hex') or '').strip()
            return out
        except Exception:
            raise ValueError(
                f'no id/hex pairs in reply: {(text or "")[:200]!r}')

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
        # paint167: A GENERATED HEX IS NO LONGER LOCKED.
        #
        # It had to be, because `--upsert` wrote every field it was given and a
        # scrape with no hex blanked the one we had. That is fixed at the
        # source: an empty incoming value never erases a populated one. So a
        # generated value now survives a scrape that knows nothing AND yields
        # to one that knows better — which is right, because this is a guess.
        #
        # Locking stays for values we have actually VERIFIED: the 77 hexes
        # corrected from catalogue evidence, ford/PN4EG's name. Those are
        # decisions, not guesses, and a scrape should not overrule them.
        #
        # CONSEQUENCE, worth knowing: an unlocked row is selectable again, so a
        # later run can revisit and change it. That is the intent — it is not a
        # decision, it is a placeholder good enough to show a customer.
        PaintLookup.all_objects.filter(pk=row.pk).update(hex=hexv)
