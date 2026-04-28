from django.db import models


class Search(models.Model):
    """Logs every paint code lookup."""

    PROVIDER_VDG = 'vdg'
    PROVIDER_PARTSLINK24 = 'partslink24'
    PROVIDER_MANUAL = 'manual'
    PROVIDER_NONE = 'none'
    PROVIDER_CHOICES = [
        (PROVIDER_VDG, 'VDG'),
        (PROVIDER_PARTSLINK24, 'Partslink24'),
        (PROVIDER_MANUAL, 'Manual'),
        (PROVIDER_NONE, 'None'),
    ]

    # Identity & timing
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True, default='')

    # Search input
    registration = models.CharField(max_length=10, db_index=True)

    # Vehicle data (from DVLA / VDG)
    make = models.CharField(max_length=50, blank=True, default='')
    model = models.CharField(max_length=100, blank=True, default='')
    year = models.IntegerField(null=True, blank=True)
    colour = models.CharField(max_length=50, blank=True, default='')
    vehicle_title = models.CharField(max_length=200, blank=True, default='')

    # From VDG
    vin = models.CharField(max_length=17, blank=True, default='')
    paint_code = models.CharField(max_length=50, blank=True, default='')
    paint_description = models.CharField(max_length=200, blank=True, default='')

    # Flow/outcome tracking
    provider = models.CharField(
        max_length=20, choices=PROVIDER_CHOICES, default=PROVIDER_NONE
    )
    success = models.BooleanField(default=False)
    error_message = models.TextField(blank=True, default='')
    lookup_duration_ms = models.IntegerField(null=True, blank=True)

    # Cost tracking (VDG charges per call)
    vdg_paint_called = models.BooleanField(default=False)
    vdg_vehicle_called = models.BooleanField(default=False)
    vdg_balance_after_call = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True
    )

    # Email / manual fallback
    email = models.EmailField(blank=True, default='')
    email_sent = models.BooleanField(default=False)
    manual_lookup_completed = models.BooleanField(default=False)

    class Meta:
        ordering = ['-timestamp']
        verbose_name = 'Search'
        verbose_name_plural = 'Searches'
        indexes = [
            models.Index(fields=['-timestamp', 'registration']),
            models.Index(fields=['provider', 'success']),
            models.Index(fields=['success', '-timestamp']),
        ]

    def __str__(self):
        return f"{self.timestamp:%Y-%m-%d %H:%M} {self.registration}"