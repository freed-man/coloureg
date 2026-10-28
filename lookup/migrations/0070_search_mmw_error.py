from django.db import migrations, models


class Migration(migrations.Migration):
    """paint368: why an mmw call ended as it did, in mmw's own words (its reply's
    `error` text, the plate masked) or, when the call failed on coloureg's side,
    the status or the exception.

    One new column, empty on every existing row. Nothing is rewritten. The
    database keeps its default ('') so the release before, still serving while
    Railway runs this, can go on saving lookups that do not name the column."""

    dependencies = [
        ('lookup', '0069_search_vdg_call_costs'),
    ]

    operations = [
        migrations.AddField(
            model_name='search',
            name='mmw_error',
            field=models.CharField(blank=True, db_default='', default='', max_length=200),
        ),
    ]
