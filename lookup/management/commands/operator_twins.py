"""List the names in the operator's paint code table that two or more codes of one make answer.

paint331. OperatorPaintCode.code_for_name declines such a name, on purpose: two rows typed
by hand sharing a name is the case where guessing is worst. The cost is that the name then
gives no code to any car, and until paint331 nothing said so. Ford "Smoke" was answered by
BMU from 13 September until a batch recorded PN3LS "Smoke" on 12 October; two EcoSports
then came through with no code before it was found, on 16 October.

Read only. Nothing is written, whatever the options. Run it before and after a batch, and
whenever the dashboard says a Save made a twin. The operator decides which code stands and
removes the other (Django admin, or a shell command with a preview first).

    manage.py operator_twins
    manage.py operator_twins --make ford

A name that is only a colour word ("Black Pearl") is left out: code_for_name never answers
such a name (paint310), so two codes under it take nothing away.
"""
from collections import defaultdict

from django.core.management.base import BaseCommand

from lookup.models import OperatorPaintCode, PaintLookup


def twin_groups(make=''):
    """paint331: {(manufacturer, normalized_name): [rows oldest first]} for every name two or
    more codes of one make answer; bare colour words left out. Read only."""
    from lookup.services.paint_resolver import is_bare_colour_name
    rows = OperatorPaintCode.objects.exclude(normalized_name='').order_by('id')
    if make:
        rows = rows.filter(manufacturer=PaintLookup.normalize_manufacturer(make))
    groups = defaultdict(list)
    for row in rows:
        groups[(row.manufacturer, row.normalized_name)].append(row)
    return {
        key: members for key, members in sorted(groups.items())
        if len(members) > 1 and not is_bare_colour_name(members[0].normalized_name)
    }


class Command(BaseCommand):
    help = "List names in the operator's table that two or more codes of one make answer. Read only."

    def add_arguments(self, parser):
        parser.add_argument('--make', default='', help='Limit to one manufacturer.')

    def handle(self, *args, **options):
        groups = twin_groups(options['make'])
        if not groups:
            self.stdout.write('No name is answered by two codes of one make.')
            return
        for (mfr, norm), members in groups.items():
            self.stdout.write(f"{mfr} '{norm}': {len(members)} codes (the name gives no code)")
            for row in members:
                flag = ' [review]' if row.needs_review else ''
                self.stdout.write(
                    f"    {row.id:>5}  {row.code:<10} {row.colour_name!r:<36} lookup {row.source_search_id or '-'}"
                    f"  {row.created_at:%Y-%m-%d}{flag}")
        self.stdout.write(f'{len(groups)} name(s). Nothing was written.')
