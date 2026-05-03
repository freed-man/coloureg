"""Custom context processors for the lookup app."""
from django.conf import settings


def analytics(request):
    """Expose GA4 measurement ID and DEBUG flag to all templates.

    The base template uses these to decide whether to inject the gtag snippet.
    GA only fires in production (DEBUG=False) AND when a measurement ID is set,
    AND when the path is not under the admin or admin-stats areas.
    """
    return {
        'GA_MEASUREMENT_ID': getattr(settings, 'GA_MEASUREMENT_ID', ''),
        'GA_DEBUG': settings.DEBUG,
    }