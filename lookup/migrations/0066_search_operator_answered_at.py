from django.db import migrations, models


class Migration(migrations.Migration):
    """paint312: when the operator last gave his own word on a lookup.

    One new column, empty on every existing row. Nothing is rewritten."""

    dependencies = [
        ('lookup', '0065_operatorpaintcode_operator_code_once_per_make'),
    ]

    operations = [
        migrations.AddField(
            model_name='search',
            name='operator_answered_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
