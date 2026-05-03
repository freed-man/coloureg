from django.contrib.sitemaps import Sitemap
from django.urls import reverse


class StaticViewSitemap(Sitemap):
    """Sitemap for static (non-database) pages."""
    protocol = 'https'

    def items(self):
        return ['index', 'about', 'help', 'privacy']

    def location(self, item):
        return reverse(item)

    def priority(self, item):
        priorities = {
            'index': 1.0,
            'help': 0.8,
            'about': 0.6,
            'privacy': 0.3,
        }
        return priorities.get(item, 0.5)

    def changefreq(self, item):
        return 'weekly' if item == 'index' else 'monthly'