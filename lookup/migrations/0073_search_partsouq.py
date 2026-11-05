from django.db import migrations, models


class Migration(migrations.Migration):
    """paint390: PartSouq (the operator's ps service) as a source of Toyota and
    Lexus paint codes. Its answer on the lookup's row, used or not, as mmw's is:
    ps_attempted, ps_code, ps_name, ps_drive (RHD, LHD or empty), ps_outcome,
    ps_error, ps_ms (the call as coloureg waited for it), ps_refused (why its
    code was not used: lhd or colour) and ps_agreed (whether its code is the
    code given); and 'partsouq' among the providers, for a lookup it answered.

    Nine new columns. Nothing is rewritten. The text and true or false columns
    keep a database default ('' and false) and the other two are nullable, so
    the release before, still serving while Railway runs this, can go on saving
    lookups that do not name them, and a roll back is as safe. The provider's
    new choice changes nothing in the database."""

    dependencies = [
        ('lookup', '0072_search_leg_times'),
    ]

    operations = [
        migrations.AddField(
            model_name='search',
            name='ps_attempted',
            field=models.BooleanField(db_default=False, default=False),
        ),
        migrations.AddField(
            model_name='search',
            name='ps_code',
            field=models.CharField(blank=True, db_default='', default='', max_length=50),
        ),
        migrations.AddField(
            model_name='search',
            name='ps_name',
            field=models.CharField(blank=True, db_default='', default='', max_length=120),
        ),
        migrations.AddField(
            model_name='search',
            name='ps_drive',
            field=models.CharField(blank=True, db_default='', default='', max_length=3),
        ),
        migrations.AddField(
            model_name='search',
            name='ps_outcome',
            field=models.CharField(blank=True, db_default='', default='', max_length=40),
        ),
        migrations.AddField(
            model_name='search',
            name='ps_error',
            field=models.CharField(blank=True, db_default='', default='', max_length=200),
        ),
        migrations.AddField(
            model_name='search',
            name='ps_ms',
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='search',
            name='ps_refused',
            field=models.CharField(blank=True, db_default='', default='', max_length=20),
        ),
        migrations.AddField(
            model_name='search',
            name='ps_agreed',
            field=models.BooleanField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name='search',
            name='provider',
            field=models.CharField(
                choices=[
                    ('vdg', 'VDG (pre-15 Aug)'),
                    ('vdg_retry', 'VDG'),
                    ('partslink24', 'Partslink24'),
                    ('oneauto', 'OneAuto'),
                    ('ezyvin', 'Ezyvin'),
                    ('mmw', 'MMW'),
                    ('partsouq', 'PartSouq'),
                    ('manual', 'Manual'),
                    ('cache', 'Cache'),
                    ('remembered', 'Remembered (3 months)'),
                    ('none', 'None'),
                ],
                default='none',
                max_length=20,
            ),
        ),
    ]
