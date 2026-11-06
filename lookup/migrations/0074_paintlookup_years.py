from django.db import migrations, models


class Migration(migrations.Migration):
    """paint396: the years the paint sites list each paint for (chipex's year
    pages and colorndrive's FOR YEARS), on the paint's row, for the manual
    queue's paint hints only.

    One new column. Nothing is rewritten: every row starts with no years, and
    `paint_years` fills them from his scraped files. Nullable, so the release
    before, still serving while Railway runs this, can go on saving a paint row
    that does not name it, and a roll back is as safe."""

    dependencies = [
        ('lookup', '0073_search_partsouq'),
    ]

    operations = [
        migrations.AddField(
            model_name='paintlookup',
            name='years',
            field=models.JSONField(blank=True, default=list, null=True),
        ),
    ]
