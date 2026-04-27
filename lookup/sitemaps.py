from django.contrib.sitemaps import Sitemap
from django.urls import reverse


class StaticViewSitemap(Sitemap):
    """Sitemap for static (non-database) pages."""
    protocol = 'https'

    def items(self):
        return ['index', 'about']

    def location(self, item):
        return reverse(item)

    def priority(self, item):
        return 1.0 if item == 'index' else 0.6

    def changefreq(self, item):
        return 'weekly' if item == 'index' else 'monthly'