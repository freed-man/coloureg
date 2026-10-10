"""paint305 (M11): one entry per make and code in the operator's own table.

OperatorPaintCode.record() has always treated (manufacturer, code) as one
entry, and nothing in the database made it so. A second entry for the same
make and code could be created by hand in the admin, or by two requests
recording the same new code at the same moment, and from then on record()
raised for that code every time.

This adds the rule to the table. Production held 141 entries and no repeats
on 8 Oct, so there it only adds the rule.

ON A DATABASE THAT DOES HOLD A REPEAT IT STOPS, WITH A MESSAGE, AND CHANGES
NOTHING. It does not choose which entry to keep and it deletes nothing: these
are hand researched answers, and which of two is right is the operator's call.
The check runs first, so the message names every repeated make and code (and
the names its entries carry) in place of the database's own one line refusal.
`migrate` then ends with an error, which on Railway stops the deploy before
the new code starts: the site keeps running the release before. Remove the
extra entries in the Django admin (Operator paint codes) and deploy again.
"""
from django.core.management.base import CommandError
from django.db import migrations, models
from django.db.models import Count


def stop_if_repeated(apps, schema_editor):
    Entry = apps.get_model('lookup', 'OperatorPaintCode')
    table = Entry.objects.using(schema_editor.connection.alias)
    repeated = (table.values('manufacturer', 'code').annotate(held=Count('id'))
                .filter(held__gt=1).order_by('manufacturer', 'code'))
    lines = []
    for pair in repeated:
        # The names only. An entry also holds the plate it was researched for,
        # which is a customer's and has no place in a deploy log.
        names = table.filter(manufacturer=pair['manufacturer'], code=pair['code']) \
                     .order_by('id').values_list('colour_name', flat=True)
        lines.append('    %s %s: %d entries, named %s' % (
            pair['manufacturer'], pair['code'], pair['held'],
            ', '.join('"%s"' % (n or '') for n in names)))
    if lines:
        raise CommandError(
            'Migration 0065 has stopped and changed nothing.\n\n'
            'It adds a rule that the operator paint code table holds ONE entry '
            'for each make and code. This database holds more than one for:\n\n'
            + '\n'.join(lines) + '\n\n'
            'Nothing has been deleted. In the Django admin, under Operator '
            'paint codes, keep the entry you want for each pair above and '
            'delete the others. Then deploy again (or run: python manage.py '
            'migrate).')


class Migration(migrations.Migration):

    dependencies = [
        ('lookup', '0064_search_details_alter_search_provider'),
    ]

    operations = [
        migrations.RunPython(stop_if_repeated, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name='operatorpaintcode',
            constraint=models.UniqueConstraint(fields=('manufacturer', 'code'), name='operator_code_once_per_make'),
        ),
    ]
