from django.core.management import call_command
from django.db import migrations


def create_cache_table(apps, schema_editor):
    """Provision the DatabaseCache table as part of `migrate` (F6).

    `migrate` does not create cache tables, so on any fresh database — a DR
    restore, a staging clone, a Neon branch — the table is simply absent, and
    the failure modes are asymmetric: every rate limiter wraps its cache calls
    in a fail-open except, so the site looks healthy while running unlimited,
    while the payment path's fulfil-lock `add()` is unwrapped and raises.

    Doing it here rather than in the deploy command means it cannot be
    forgotten and travels with the repo. `createcachetable` is idempotent.
    """
    call_command('createcachetable', database=schema_editor.connection.alias,
                 verbosity=0)


class Migration(migrations.Migration):
    dependencies = [('lookup', '0034_alter_search_provider')]
    operations = [
        migrations.RunPython(create_cache_table,
                             migrations.RunPython.noop),
    ]
