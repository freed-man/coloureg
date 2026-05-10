# Data migration: backfill the new `device` field from each row's user_agent.
# Mirrors the parse_device() logic in views.py — kept here as a snapshot so
# this migration is reproducible regardless of future changes to that function.

from django.db import migrations


def _classify(user_agent):
    """Snapshot of views.parse_device at the time this migration was authored.
    Don't import the live function — migrations should be self-contained so
    a replay against an older app state doesn't pull in newer logic."""
    if not user_agent:
        return 'unknown'
    ua_lower = user_agent.lower()
    if any(kw in ua_lower for kw in ['ipad', 'tablet']):
        return 'tablet'
    if any(kw in ua_lower for kw in ['mobile', 'android', 'iphone', 'ipod']):
        return 'mobile'
    return 'desktop'


def forwards(apps, schema_editor):
    Search = apps.get_model('lookup', 'Search')
    # Iterate in batches via .iterator() so we don't materialise every row in
    # memory at once (Search may have a lot of historical rows by now).
    # Use only() to fetch just the columns we need.
    to_update = []
    BATCH = 500
    for s in Search.objects.only('id', 'user_agent').iterator(chunk_size=BATCH):
        s.device = _classify(s.user_agent)
        to_update.append(s)
        if len(to_update) >= BATCH:
            Search.objects.bulk_update(to_update, ['device'])
            to_update = []
    if to_update:
        Search.objects.bulk_update(to_update, ['device'])


def backwards(apps, schema_editor):
    Search = apps.get_model('lookup', 'Search')
    Search.objects.update(device='')


class Migration(migrations.Migration):

    dependencies = [
        ('lookup', '0008_search_device'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
