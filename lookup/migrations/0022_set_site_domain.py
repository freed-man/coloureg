"""Point django.contrib.sites at the real domain.

The Sites framework seeds a single row with domain 'example.com', and
`django.contrib.sitemaps` builds every <loc> in sitemap.xml from it. Left at the
default, the sitemap advertises https://example.com/... — so every URL Google
fetches from it 404s, and the sitemap does nothing at all despite being declared
in robots.txt and (presumably) submitted to Search Console.

Done as a data migration rather than a manual edit in Django admin so it is
version-controlled, applied automatically on deploy, and can't be silently lost
if the database is ever rebuilt from migrations.

Idempotent, and reversible back to the Django default.
"""
from django.db import migrations

DOMAIN = 'coloureg.com'
NAME = 'coloureg'


def set_site_domain(apps, schema_editor):
    Site = apps.get_model('sites', 'Site')
    Site.objects.update_or_create(
        pk=1,
        defaults={'domain': DOMAIN, 'name': NAME},
    )


def restore_default(apps, schema_editor):
    Site = apps.get_model('sites', 'Site')
    Site.objects.filter(pk=1).update(domain='example.com', name='example.com')


class Migration(migrations.Migration):

    dependencies = [
        ('lookup', '0021_remove_ua_blocklist'),
        ('sites', '0002_alter_domain_unique'),
    ]

    operations = [
        migrations.RunPython(set_site_domain, restore_default),
    ]
