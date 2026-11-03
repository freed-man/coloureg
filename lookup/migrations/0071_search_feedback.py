from django.db import migrations, models


class Migration(migrations.Migration):
    """paint380: the customer's answer to "Was this helpful?" on the results
    page ('yes' or 'no'), and the note a No may carry (300 characters at most).

    Two new columns, empty on every existing row. Nothing is rewritten. The
    database keeps its default ('') so the release before, still serving while
    Railway runs this, can go on saving lookups that do not name the columns."""

    dependencies = [
        ('lookup', '0070_search_mmw_error'),
    ]

    operations = [
        migrations.AddField(
            model_name='search',
            name='feedback',
            field=models.CharField(blank=True, choices=[('yes', 'Helpful'), ('no', 'Not helpful')], db_default='', default='', max_length=3),
        ),
        migrations.AddField(
            model_name='search',
            name='feedback_note',
            field=models.CharField(blank=True, db_default='', default='', max_length=300),
        ),
    ]
