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
        'recovery_attempted',
        'pl24_returned',
        'recovery_name_only',
        'vdg_vehicle_returned',
        'vdg_paint_returned',
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
        'device',
        'registration',
        'make',
        'model',
        'year',
        'colour',
        'category',
        'vehicle_title',
        'vin',
        'paint_code',
        'paint_description',
        'provider',
        'success',
        'error_message',
        'lookup_duration_ms',
        'vdg_vehicle_returned',
        'vdg_paint_returned',
        'vdg_balance_after_call',
        'recovery_attempted',
        'vdg_retry_returned',
        'vdg_retry_code',
        'pl24_code',
        'pl24_attempted',
        'pl24_returned',
        'recovery_name_only',
        'recovery_duration_ms',
        'email',
        'email_sent',
    )
    fieldsets = (
        ('Search Info', {
            'fields': ('timestamp', 'ip_address', 'user_agent', 'device', 'registration')
        }),
        ('Vehicle Data', {
            'fields': ('vehicle_title', 'make', 'model', 'year', 'colour', 'category', 'vin')
        }),
        ('Paint Code', {
            'fields': ('paint_code', 'paint_description')
        }),
        ('Outcome', {
            'fields': ('provider', 'success', 'error_message', 'lookup_duration_ms')
        }),
        ('Cost Tracking', {
            'fields': (
                        'vdg_vehicle_returned',
                'vdg_paint_returned',
                'vdg_balance_after_call',
            )
        }),
        ('Recovery (paint-miss fallback)', {
            'fields': (
                'recovery_attempted',
                'vdg_retry_returned',
                'vdg_retry_code',
                'pl24_code',
                'pl24_attempted',
                'pl24_returned',
                'recovery_name_only',
                'recovery_duration_ms',
            )
        }),
        ('Manual Fallback', {
            'fields': ('email', 'email_sent', 'manual_lookup_completed')
        }),
    )
    date_hierarchy = 'timestamp'
    ordering = ('-timestamp',)