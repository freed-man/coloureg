"""Copy a Renault group base code's hex onto its prefixed twin (paint204).

paint201 taught lookups that a Renault or Dacia code made of a two-letter paint
type and a three-character colour code (TEKNA, OV369) is the colour of its base
(KNA, 369): 127 of 127 bases catalogued, colours agreeing 99 in 99. A lookup of
an UNcatalogued prefixed code now shows the base's swatch. A prefixed code that
IS catalogued resolves as itself, though, and some of those rows have no hex:
production's OV369, TED44 and MV632 came in from the carcolourservices list
(paint183) with a name and no hex, so they show no swatch while their bases
(369 Blanc Glacier, D44 Bleu Odyssee, 632 Gris Boreal) each have one.

This copies the base's hex where both rows say the same colour:

    would copy     the prefixed row's name states a colour, and it agrees with
                   the base's name (or with the base's hex, if that name says
                   no colour)
    disagree       both state colours and share no family: left alone and
                   reported, because one of the two rows is wrong
    cannot check   one side states no colour at all: unknown is not approval
    no base hex    nothing to copy

The makes and prefixes are paint201's measured allow-list, read from the model
(TYPE_PREFIX_MAKES, TYPE_PREFIXES), so the two can never drift apart.

WRITES ARE NOT LOCKED. A copied hex is borrowed, not verified, so a scrape
that one day carries the row's own hex should win (paint167's rule, the one
propose_hexes follows). A row whose hex IS locked is never touched.

PREVIEWS BY DEFAULT: nothing is written without --apply.

    manage.py copy_prefix_hexes             # preview
    manage.py copy_prefix_hexes --apply     # write
"""
from collections import defaultdict

from django.core.management.base import BaseCommand

from lookup.models import PaintLookup

ORDER = ('would copy', 'disagree', 'cannot check', 'no base hex')


class Command(BaseCommand):
    help = ("Copy a Renault/Dacia base code's hex onto its paint-type-prefixed "
            "twin where the colours agree. Previews unless --apply.")

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Write. Without it, nothing is changed.')

    def handle(self, *args, **opt):
        from lookup.services.paint_resolver import _colour_families, _hex_family
        out = defaultdict(list)
        qs = (PaintLookup.objects
              .filter(manufacturer__in=PaintLookup.TYPE_PREFIX_MAKES, hex='')
              .order_by('manufacturer', 'code'))
        for row in qs:
            code = (row.code or '').strip().upper()
            if len(code) != 5 or code[:2] not in PaintLookup.TYPE_PREFIXES:
                continue
            if 'hex' in (row.locked_fields or []):
                continue                      # a decision, never revisited
            base = PaintLookup.objects.filter(manufacturer=row.manufacturer,
                                              code__iexact=code[2:]).first()
            if not base or not base.hex:
                outcome = 'no base hex'
            else:
                want = _colour_families(row.name or '')
                got = (_colour_families(base.name or '')
                       or ({_hex_family(base.hex)} - {None}))
                if not want or not got:
                    outcome = 'cannot check'
                elif want & got:
                    outcome = 'would copy'
                else:
                    outcome = 'disagree'
            if outcome == 'would copy' and opt['apply']:
                # hex='' again at write time: a row that gained a hex since the
                # read is left alone rather than overwritten.
                PaintLookup.all_objects.filter(pk=row.pk, hex='').update(hex=base.hex)
            out[outcome].append((row, base))
        head = 'WRITTEN' if opt['apply'] else 'PREVIEW, nothing written'
        self.stdout.write(f'{head}: {sum(len(v) for v in out.values())} '
                          f'prefixed rows with no hex')
        for k in ORDER:
            if out.get(k):
                label = 'copied' if (k == 'would copy' and opt['apply']) else k
                self.stdout.write(f'  {label:<14}{len(out[k]):>5}')
        for k in ORDER:
            label = 'copied' if (k == 'would copy' and opt['apply']) else k
            for row, base in out.get(k, []):
                src = (f'   <- {base.code} {base.name!r} {base.hex or "no hex"}'
                       if base else '   (base not catalogued)')
                self.stdout.write(f'    {label}: {row.manufacturer}/{row.code} {row.name!r}{src}')
