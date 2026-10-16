from django.db import migrations, models


class Migration(migrations.Migration):
    """paint335: when the operator parked a lookup's car (every route tried, no
    code yet), so the dashboard shows the grey circle rather than the red cross
    and the open cars file lists the car under PARKED.

    One new column, empty on every existing row. Nothing is rewritten."""

    dependencies = [
        ('lookup', '0067_operatorpaintcode_review_note'),
    ]

    operations = [
        migrations.AddField(
            model_name='search',
            name='parked_at',
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
    ]
