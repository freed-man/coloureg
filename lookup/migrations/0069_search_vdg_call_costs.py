from django.db import migrations, models


class Migration(migrations.Migration):
    """paint347: what each VDG call of a lookup charged (the car call, the paint call, the paint call's
    second chance), so the stats page's cost box can show "VDG auto", "VDG paint" and "VDG paint retry"
    with their own prices. vdg_transaction_cost stays the total.

    Three new columns, empty on every existing row. Nothing is rewritten."""

    dependencies = [
        ('lookup', '0068_search_parked_at'),
    ]

    operations = [
        migrations.AddField(
            model_name='search',
            name='vdg_auto_cost',
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=10, null=True),
        ),
        migrations.AddField(
            model_name='search',
            name='vdg_paint_cost',
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=10, null=True),
        ),
        migrations.AddField(
            model_name='search',
            name='vdg_paint_retry_cost',
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=10, null=True),
        ),
    ]
