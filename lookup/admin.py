from django.contrib import admin
from .models import Search


@admin.register(Search)
class SearchAdmin(admin.ModelAdmin):
    list_display = (
        'timestamp',
        'registration',
        'make',
        'model',
        'year',
        'colour',
        'paint_code',
        'provider',
        'success',
        'lookup_duration_ms',
    )
    list_filter = ('success', 'provider', 'vdg_paint_called', 'vdg_vehicle_called', 'email_sent', 'manual_lookup_completed')
    search_fields = ('registration', 'make', 'model', 'vin', 'paint_code', 'email', 'ip_address')
    readonly_fields = (
        'timestamp',
        'ip_address',
        'user_agent',
        'registration',
        'make',
        'model',
        'year',
        'colour',
        'vin',
        'paint_code',
        'paint_description',
        'provider',
        'success',
        'error_message',
        'lookup_duration_ms',
        'vdg_paint_called',
        'vdg_vehicle_called',
        'email',
        'email_sent',
    )
    fieldsets = (
        ('Search Info', {
            'fields': ('timestamp', 'ip_address', 'user_agent', 'registration')
        }),
        ('Vehicle Data (DVLA)', {
            'fields': ('make', 'model', 'year', 'colour')
        }),
        ('VDG Data', {
            'fields': ('vin', 'paint_code', 'paint_description')
        }),
        ('Outcome', {
            'fields': ('provider', 'success', 'error_message', 'lookup_duration_ms')
        }),
        ('Cost Tracking', {
            'fields': ('vdg_paint_called', 'vdg_vehicle_called')
        }),
        ('Manual Fallback', {
            'fields': ('email', 'email_sent', 'manual_lookup_completed')
        }),
    )
    date_hierarchy = 'timestamp'
    ordering = ('-timestamp',)