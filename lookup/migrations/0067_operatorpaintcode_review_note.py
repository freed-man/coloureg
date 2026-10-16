from django.db import migrations, models


class Migration(migrations.Migration):
    """paint331: why an operator paint code was flagged for review, when the
    reason is a second code recorded under one name (previous_name already
    says when it is a second name on one code).

    One new column, empty on every existing row. Nothing is rewritten."""

    dependencies = [
        ('lookup', '0066_search_operator_answered_at'),
    ]

    operations = [
        migrations.AddField(
            model_name='operatorpaintcode',
            name='review_note',
            field=models.CharField(blank=True, default='', max_length=200),
        ),
    ]
