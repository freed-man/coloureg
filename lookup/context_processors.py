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
        # Price, exposed globally so the JSON-LD offer in base.html and any
        # page-level copy read from ONE source. It was previously hardcoded as
        # "1.00" in the schema, which silently disagreed with the £2 setting —
        # Google would have shown £1 in rich results while checkout charged £2.
        # Deriving it here means the two can never drift apart again.
        'lookup_price_display': f'{getattr(settings, "LOOKUP_PRICE_PENCE", 200) / 100:.2f}',
        'lookup_price': getattr(settings, 'LOOKUP_PRICE_PENCE', 200) / 100,
    }