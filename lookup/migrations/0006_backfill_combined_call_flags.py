# Data migration: backfill the new per-document tracking flags from the
# old call flags. Runs once after 0005 adds the new columns.
#
# Mapping:
#   vdg_paint_called    -> vdg_paint_returned
#   vdg_vehicle_called  -> vdg_vehicle_returned
#   vdg_combined_called  = (vdg_paint_called OR vdg_vehicle_called)
#
# Historically these were two separate calls, not one combined call, so
# vdg_combined_called isn't strictly accurate for old rows — but the
# *outcome* (which documents returned data) is preserved, which is what
# the dashboard's cost calculation actually cares about.

from django.db import migrations


def forwards(apps, schema_editor):
    Search = apps.get_model('lookup', 'Search')
    # Bulk update — much faster than iterating, and we don't need the
    # row-by-row logic since the mapping is a simple field copy.
    Search.objects.update(
        vdg_paint_returned=models_F('vdg_paint_called'),
        vdg_vehicle_returned=models_F('vdg_vehicle_called'),
    )
    # Combined-called is True if either old flag was True. Two updates
    # because Django's update() can't OR two F() expressions cleanly.
    Search.objects.filter(vdg_paint_called=True).update(vdg_combined_called=True)
    Search.objects.filter(vdg_vehicle_called=True).update(vdg_combined_called=True)


def backwards(apps, schema_editor):
    Search = apps.get_model('lookup', 'Search')
    # Reverse op: clear the new flags. Old flags untouched.
    Search.objects.update(
        vdg_combined_called=False,
        vdg_vehicle_returned=False,
        vdg_paint_returned=False,
    )


# Late-bound import so we can call F() inside forwards() without importing
# at module level (cleaner for migrations that may be replayed in funny envs).
def models_F(field_name):
    from django.db.models import F
    return F(field_name)


class Migration(migrations.Migration):

    dependencies = [
        ('lookup', '0005_search_combined_call_flags'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
