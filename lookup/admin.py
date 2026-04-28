from django.contrib import admin
from .models import Search


@admin.register(Search)
class SearchAdmin(admin.ModelAdmin):
    list_display = (
        'timestamp',
        'registration',
        'vehicle_title',
        'paint_code',
        'paint_description',
        'success',
        'manual_lookup_completed',
    )
    list_filter = (
        'success',
        'provider',
        'vdg_paint_called',
        'vdg_vehicle_called',
        'email_sent',
        'manual_lookup_completed',
    )
    list_editable = ('manual_lookup_completed',)
    search_fields = (
        'registration',
        'make',
        'model',
        'vehicle_title',
        'vin',
        'paint_code',
        'paint_description',
        'email',
        'ip_address',
    )
    readonly_fields = (
        'timestamp',
        'ip_address',
        'user_agent',
        'registration',
        'make',
        'model',
        'year',
        'colour',
        'vehicle_title',
        'vin',
        'paint_code',
        'paint_description',
        'provider',
        'success',
        'error_message',
        'lookup_duration_ms',
        'vdg_paint_called',
        'vdg_vehicle_called',
        'vdg_balance_after_call',
        'email',
        'email_sent',
    )
    fieldsets = (
        ('Search Info', {
            'fields': ('timestamp', 'ip_address', 'user_agent', 'registration')
        }),
        ('Vehicle Data', {
            'fields': ('vehicle_title', 'make', 'model', 'year', 'colour', 'vin')
        }),
        ('Paint Code', {
            'fields': ('paint_code', 'paint_description')
        }),
        ('Outcome', {
            'fields': ('provider', 'success', 'error_message', 'lookup_duration_ms')
        }),
        ('Cost Tracking', {
            'fields': (
                'vdg_paint_called',
                'vdg_vehicle_called',
                'vdg_balance_after_call',
            )
        }),
        ('Manual Fallback', {
            'fields': ('email', 'email_sent', 'manual_lookup_completed')
        }),
    )
    date_hierarchy = 'timestamp'
    ordering = ('-timestamp',)