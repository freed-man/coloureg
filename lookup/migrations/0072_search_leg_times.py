from django.db import migrations, models


class Migration(migrations.Migration):
    """paint387: partslink24's and VDG's paint calls' own times on the lookup's
    row, in milliseconds, as mmw_ms has been since paint140: pl24_ms (the
    partslink24 call, its one quick retry included), vdg_paint_ms (VDG's first
    paint call in the search) and vdg_paint_retry_ms (VDG's second chance, when
    it was made). Telemetry only, for the measure of whether holding VDG's paint
    call a few seconds for partslink24's answer would save its charge without a
    long wait on the searches partslink24 cannot answer.

    Three nullable columns, NULL on every existing row. Nothing is rewritten,
    and no default is needed: the release before, still serving while Railway
    runs this, saves lookups without naming the columns, and an added nullable
    column is NULL for such a row. A roll back is as safe, for the same reason."""

    dependencies = [
        ('lookup', '0071_search_feedback'),
    ]

    operations = [
        migrations.AddField(
            model_name='search',
            name='pl24_ms',
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='search',
            name='vdg_paint_ms',
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='search',
            name='vdg_paint_retry_ms',
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
    ]
