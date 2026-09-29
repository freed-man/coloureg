"""Research paint codes whose names disagree, with Claude and web search (paint238).

Writes NOTHING to the catalogue: every finding goes to a review file (one JSON
line per code), and a rerun skips codes already in it. See
lookup/services/code_research.py.

    python manage.py research_codes --dry-run
    python manage.py research_codes --pilot 50
    python manage.py research_codes --summary
    python manage.py research_codes --customers              (paint240)

The review file is etc/code_research.jsonl unless --out says otherwise (paint241):
.gitignore keeps etc/ out of the repo, which is public, so it is never committed
or deployed, and a rerun always finds the codes already researched.

--customers researches only codes customers have landed on and that are not in
the review file yet, the most recent first: research on demand. Run it now and
then; the summary lists any code whose shown name the agent says is wrong.

Needs ANTHROPIC_API_KEY in the environment (as propose_hexes does), and web search
enabled for the organisation in the Claude Console.
"""
import json
import os
from collections import Counter
from datetime import datetime, timezone

from django.core.management.base import BaseCommand, CommandError

from lookup.services import code_research as cr


class Command(BaseCommand):
    help = 'Research paint codes whose names disagree (Claude with web search); writes only a review file'

    def add_arguments(self, parser):
        parser.add_argument('--out', default=os.path.join('etc', 'code_research.jsonl'),
                            help='the review file (JSON lines); default etc/code_research.jsonl, which .gitignore keeps out of the repo')
        parser.add_argument('--pilot', type=int, default=50, help='how many codes this run (default 50)')
        parser.add_argument('--model', default=cr.DEFAULT_MODEL, choices=sorted(cr.PRICES))
        parser.add_argument('--max-searches', type=int, default=3, help='web searches allowed per code')
        parser.add_argument('--max-cost', type=float, default=5.0, help='stop once this many dollars are spent')
        parser.add_argument('--dry-run', action='store_true', help='list the codes it would research; no API calls')
        parser.add_argument('--summary', action='store_true', help='summarise the review file; no API calls')
        parser.add_argument('--customers', action='store_true',
                            help='only codes customers have landed on, not yet researched, newest first (paint240)')

    def handle(self, *args, **o):
        w = self.stdout.write
        if o['summary']:
            return self._summary(o['out'])
        codes = cr.conflicting_codes()
        known = cr.known_answers(codes)
        done = self._done(o['out'])
        if o['customers']:
            seen = cr.customer_codes(codes)
            pool = sorted((c for c in codes if (c['make_key'], c['code']) in seen),
                          key=lambda c: seen[(c['make_key'], c['code'])], reverse=True)
            w(f'Codes customers have landed on: {len(pool):,}')
        else:
            pool = cr.choose(codes, known, len(codes))
        todo = [c for c in pool if f"{c['make_key']}|{c['code']}" not in done][:o['pilot']]
        tested = sum(1 for c in todo if (c['make_key'], c['code']) in known)
        w(f'Codes whose names state different colours: {len(codes):,}; already in the review file: {len(done):,}')
        w(f'This run: {len(todo):,} codes, {tested:,} of them with a name a provider gave for a real car')
        if o['dry_run']:
            for c in todo[:20]:
                k = known.get((c['make_key'], c['code']))
                w(f"  {c['make_key']} {c['code']}: {', '.join(c['all_names'][:4])}"
                  + (f"  | providers said: {', '.join(k)}" if k else ''))
            w('DRY RUN: no API calls, nothing written.')
            return
        key = os.environ.get('ANTHROPIC_API_KEY', '').strip()
        if not key:
            raise CommandError('ANTHROPIC_API_KEY is not set in this terminal.')
        os.makedirs(os.path.dirname(os.path.abspath(o['out'])), exist_ok=True)
        spent, fails, scores = 0.0, 0, Counter()
        for i, c in enumerate(todo, 1):
            if spent >= o['max_cost']:
                w(f'Stopped: ${spent:.2f} spent, the cap is ${o["max_cost"]:.2f} (it stops once the cap is reached).')
                break
            res = cr.research(c, key, model=o['model'], max_searches=o['max_searches'])
            kn = list(known.get((c['make_key'], c['code']), {}))
            res['score'] = cr.score(res, kn)
            spent += res['cost']
            answer = res['correct_name'] or ', '.join(p.get('name', '') for p in res['paints'][:2]) or res['error']
            w(f"[{i}/{len(todo)}] {c['make_key']} {c['code']}: {res['verdict']} {answer[:60]!r} "
              f"· {res['searches']} searches · ${res['cost']:.3f}" + (f" · {res['score']}" if kn else ''))
            if res.pop('api_error'):
                fails += 1                    # not written: a rerun tries this code again
            else:
                fails = 0
                scores[res['score']] += 1
                line = dict(c, known_names=kn, researched_at=datetime.now(timezone.utc).isoformat(timespec='seconds'), **res)
                line.pop('id', None)
                with open(o['out'], 'a', encoding='utf-8') as f:
                    f.write(json.dumps(line, ensure_ascii=False) + '\n')
            if fails >= 3:
                w('Stopped: three API errors in a row. Check the key, and that web search is enabled for '
                  'your organisation in the Claude Console.')
                break
        w(f'\nSpent about ${spent:.2f}. Scores this run: ' + ', '.join(f'{k} {n}' for k, n in scores.most_common()))
        w(f'Review file: {o["out"]} (python manage.py research_codes --summary'
          + ('' if o['out'] == os.path.join('etc', 'code_research.jsonl') else f' --out "{o["out"]}"') + ')')

    @staticmethod
    def _done(path):
        done = set()
        if os.path.exists(path):
            with open(path, encoding='utf-8') as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    done.add(f"{r.get('make_key')}|{r.get('code')}")
        return done

    def _summary(self, path):
        if not os.path.exists(path):
            raise CommandError(f'No such file: {path}')
        rows = []
        with open(path, encoding='utf-8') as f:
            for line in f:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
        w = self.stdout.write
        cost = sum(r.get('cost') or 0 for r in rows)
        w(f'Codes researched: {len(rows):,}; spent about ${cost:.2f} (${cost / max(len(rows), 1):.3f} a code)')
        w('Verdicts: ' + ', '.join(f'{k} {n}' for k, n in Counter(r.get('verdict') for r in rows).most_common()))
        tested = [r for r in rows if r.get('score') not in ('untested', None)]
        w(f'Against names providers gave ({len(tested)} codes): '
          + ', '.join(f'{k} {n}' for k, n in Counter(r['score'] for r in tested).most_common()))
        for r in [r for r in tested if r['score'] == 'wrong'][:10]:
            w(f"  wrong? {r['make_key']} {r['code']}: agent {r.get('correct_name') or [p.get('name') for p in r.get('paints', [])]}"
              f" | providers {r.get('known_names')}")
        # paint240: THE ACTION LIST. One paint, and the name the site shows is
        # not it (Peugeot EEQ shown "Brun Epicee", every source says Jaune
        # Agueda). A code shared by two paints is left to pick_by_colour.
        fix = [r for r in rows if r.get('verdict') == 'one' and r.get('correct_name')
               and not cr._same_name(r.get('shown_name'), r['correct_name'].split(' (')[0])]
        if fix:
            w(f'Shown name may be wrong ({len(fix)}): check the source, then rename and lock by hand')
            for r in fix[:25]:
                w(f"  {r['make_key']} {r['code']}: shows {r.get('shown_name')!r}, the agent says {r['correct_name']!r}"
                  + (f"  [{(r.get('sources') or [''])[0]}]" if r.get('sources') else ''))
        total = len(cr.conflicting_codes())
        if rows:
            w(f'At this rate, all {total:,} such codes would cost about ${cost / len(rows) * total:.0f}.')
