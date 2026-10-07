# Name against swatch: catalogue rows whose NAME says one colour and whose SWATCH
# shows another. READ-ONLY; writes swatch_report.csv in the current folder.
# Run from the repo folder:  python manage.py shell -c "exec(open('swatch_report.py').read())"
import csv, math, re
from lookup.models import PaintLookup

WORDS = {   # one colour family per word, in the languages the catalogue uses
    'white': r'white|blanc|blanco|bianco|wei(?:ss|\u00df)|alpin', 'black': r'black|noir|negro|nero|schwarz',
    'silver': r'silver|argent|plata|argento|silber', 'grey': r'gr[ae]y|gris|grigio|grau',
    'red': r'red|rouge|rojo|rosso|rot', 'blue': r'blue|bleu|azul|blu|blau|azzurro', 'green': r'green|vert|verde|gr(?:ue|\u00fc)n',
    'yellow': r'yellow|jaune|amarillo|giallo|gelb', 'orange': r'orange|naranja|arancio', 'brown': r'brown|marron|marr\u00f3n|braun'}
FAMILY = {k: re.compile(r'(?:^|[^a-z])(?:' + v + r')(?:$|[^a-z])', re.I) for k, v in WORDS.items()}

def lab(h):
    c = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    c = [((x + .055) / 1.055) ** 2.4 if x > .04045 else x / 12.92 for x in c]
    X = (.4124 * c[0] + .3576 * c[1] + .1805 * c[2]) / .95047
    Y = .2126 * c[0] + .7152 * c[1] + .0722 * c[2]
    Z = (.0193 * c[0] + .1192 * c[1] + .9505 * c[2]) / 1.08883
    f = lambda t: t ** (1 / 3) if t > .008856 else 7.787 * t + 16 / 116
    L, a, b = 116 * f(Y) - 16, 500 * (f(X) - f(Y)), 200 * (f(Y) - f(Z))
    return L, math.hypot(a, b), math.degrees(math.atan2(b, a)) % 360

def wrong(fam, L, C, H):
    """A reason the swatch is NOT that family, or '' if it plausibly is. Generous on purpose: only clear contradictions."""
    if fam == 'white':  return '' if L >= 85 and C <= 15 else f'a white that reads grey or tinted (lightness {L:.0f}, colour {C:.0f})'
    if fam == 'black':  return '' if L <= 30 else f'a black with lightness {L:.0f} (over 30)'
    if fam == 'silver': return '' if 50 <= L <= 90 and C <= 15 else f'a silver with lightness {L:.0f}, colour {C:.0f}'
    if fam == 'grey':   return '' if C <= 20 else f'a grey with colour {C:.0f} (over 20)'
    hues = {'red': (320, 50), 'orange': (20, 80), 'yellow': (45, 115), 'green': (90, 215), 'blue': (180, 310), 'brown': (10, 100)}
    lo, hi = hues[fam]; inside = (lo <= H <= hi) if lo < hi else (H >= lo or H <= hi)
    if fam == 'brown':  return '' if inside and L <= 65 else f'a brown at hue {H:.0f}, lightness {L:.0f}'
    if C < 15:          return ''      # dark or muted shades of a colour carry little colour; not a contradiction
    return '' if inside else f'a {fam} at hue {H:.0f} (outside {lo} to {hi})'

from django.db.models import Count
from lookup.models import Search
used = {}
for s_ in Search.objects.exclude(paint_code='').values('make', 'paint_code').annotate(n=Count('id')):
    k = (PaintLookup.normalize_manufacturer(s_['make'] or ''), (s_['paint_code'] or '').strip().upper())
    used[k] = used.get(k, 0) + s_['n']
rows = []
for r in PaintLookup.all_objects.exclude(hex='').only('manufacturer', 'code', 'name', 'hex', 'locked_fields'):
    if not re.fullmatch(r'#[0-9A-Fa-f]{6}', r.hex or '') or '+' in (r.name or ''):
        continue
    fams = [f for f, rx in FAMILY.items() if rx.search(r.name or '')]
    if len(fams) != 1:                    # no colour word, or two ("Blue Grey"): skip, too ambiguous
        continue
    why = wrong(fams[0], *lab(r.hex))
    if why:
        rows.append([used.get((r.manufacturer, (r.code or '').upper()), 0), r.manufacturer, r.code, r.name, r.hex.upper(), fams[0], why, 'hex' in (r.locked_fields or [])])
with open('swatch_report.csv', 'w', newline='', encoding='utf-8') as fh:
    rows.sort(key=lambda x: -x[0])
    w = csv.writer(fh); w.writerow(['times_looked_up', 'make', 'code', 'name', 'swatch', 'name_says', 'why_flagged', 'swatch_locked']); w.writerows(rows)
print(f'{len(rows)} rows flagged ({sum(1 for x in rows if x[0])} of them on cars your customers looked up); written to swatch_report.csv')
