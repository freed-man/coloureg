from django.contrib import admin
from django.utils import timezone

from .models import Search, OperatorPaintCode, PaintCodeReport


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
        # paint132: which of the three gates refused this lookup. Written by
        # the pipeline, so readonly like every other diagnostic.
        'gate_reason',
        # paint133: how many catalogue codes the colour NAME matched, and which.
        # Only set when the match was DECLINED for ambiguity.
        'name_match_count',
        'name_match_codes',
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
        'vdg_second_chance',
        'oneauto_second_chance',
        'second_chance_after_race',
        'email',
        'email_sent',
        # Everything below was added after paint65 and was invisible in the row
        # view because fieldsets were never extended to match (F13). Note that
        # listing a field HERE is not enough on its own: with fieldsets defined,
        # anything absent from them does not render regardless of readonly
        # status. Both lists have to know about a column.
        #
        # Diagnostics and money state are readonly — these are written by the
        # pipeline and editing one would corrupt cost data or unlock a paid row.
        # manual_note and no_code_available are deliberately left editable,
        # since those are the staff workflow fields.
        'vdg_paint_name',
        'vdg_transaction_cost',
        'oneauto_cost',
        'oneauto_outcome',
        'oneauto_code',
        'oneauto_name',
        # paint99. Written by the pipeline, so readonly like every other cost
        # and diagnostic field — editing one would corrupt the record of what
        # the reserve actually did.
        'ezyvin_credits',
        'ezyvin_outcome',
        'ezyvin_started_because',
        'pl24_outcome',
        'pl24_name',
        'pl24_started_because',
        'paywalled',
        'paid_unlocked',
        'access_label',
        'customer_message',
        'enriched_from',
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
            'fields': ('provider', 'success', 'error_message', 'gate_reason',
                       'name_match_count', 'name_match_codes',
                       'lookup_duration_ms')
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
                # Which attempt produced the answer, and whether the race was
                # already decided when it fired. Readonly: both are written by
                # worker threads and mean nothing if edited by hand.
                'vdg_second_chance',
                'oneauto_second_chance',
                'second_chance_after_race',
            )
        }),
        ('Manual Fallback', {
            'fields': ('email', 'email_sent', 'manual_lookup_completed')
        }),
        # COLLAPSED BY DEFAULT. These are the columns added across paint66-85,
        # and there are sixteen of them — enough to bury the fields above if
        # they were all expanded. 'collapse' gives a Show/Hide toggle, so the
        # row view stays readable and the newest diagnostics are one click away
        # rather than invisible.
        # paint99: renamed from 'One Auto'. It holds both paid non-VDG legs now —
        # One Auto's historical columns and the reserve's live ones — and a
        # group labelled for a leg that no longer runs would read as though
        # Ezyvin's spend belonged to it.
        ('Paid legs — One Auto (retired) · Ezyvin (reserve)', {
            'classes': ('collapse',),
            'fields': ('oneauto_code', 'oneauto_name', 'oneauto_outcome',
                       'oneauto_cost',
                       # paint99: the reserve, in the same group. Both lists
                       # have to know about a column — readonly_fields alone
                       # does not render it, and fieldsets alone would make it
                       # editable.
                       'ezyvin_credits', 'ezyvin_outcome',
                       'ezyvin_started_because'),
        }),
        ('partslink24 detail', {
            'classes': ('collapse',),
            'fields': ('pl24_name', 'pl24_outcome', 'pl24_started_because'),
        }),
        ('VDG paint detail', {
            'classes': ('collapse',),
            'fields': ('vdg_paint_name', 'vdg_transaction_cost'),
        }),
        ('Payment', {
            'classes': ('collapse',),
            'fields': ('paywalled', 'paid_unlocked', 'access_label'),
        }),
        ('Customer and manual handling', {
            'classes': ('collapse',),
            'fields': ('customer_message', 'manual_note', 'no_code_available',
                       'enriched_from'),
        }),
    )
    date_hierarchy = 'timestamp'
    ordering = ('-timestamp',)

@admin.register(PaintCodeReport)
class PaintCodeReportAdmin(admin.ModelAdmin):
    """Customer reports that a delivered code or colour is wrong.

    paint113. `times_reported` is the column that matters — one report is a
    shrug, three against the same (make, code) is a catalogue error and there is
    no other way to find one.
    """

    list_display = ('created_at', 'registration', 'manufacturer', 'code',
                    'colour_name', 'reason', 'times_reported', 'status')
    list_filter = ('status', 'reason', 'manufacturer')
    search_fields = ('registration', 'code', 'colour_name', 'note')
    # Everything the customer sent is readonly: editing it would rewrite what
    # they said. operator_note and status are the only editable fields, because
    # they are the operator's own.
    readonly_fields = ('search', 'registration', 'manufacturer', 'code',
                       'colour_name', 'reason', 'note', 'ip_address',
                       'session_key', 'created_at', 'times_reported')
    actions = ['mark_actioned', 'mark_ignored']
    ordering = ('-created_at',)

    @admin.display(description='Reports for this code')
    def times_reported(self, obj):
        return PaintCodeReport.count_for_code(obj.manufacturer, obj.code)

    @admin.action(description='Mark as actioned (the code was wrong)')
    def mark_actioned(self, request, queryset):
        n = queryset.update(status=PaintCodeReport.STATUS_ACTIONED,
                            resolved_at=timezone.now())
        self.message_user(request, f'{n} marked actioned.')

    @admin.action(description='Mark as ignored (the code was fine)')
    def mark_ignored(self, request, queryset):
        # IGNORED, never deleted. A dismissed report still records that someone
        # disagreed, and nine dismissals against one code is itself a signal
        # however unconvincing each was on its own.
        n = queryset.update(status=PaintCodeReport.STATUS_IGNORED,
                            resolved_at=timezone.now())
        self.message_user(request, f'{n} marked ignored.')


@admin.register(OperatorPaintCode)
class OperatorPaintCodeAdmin(admin.ModelAdmin):
    """paint92. Hand-researched codes, editable because they are hand-entered:
    a typo here silently answers every future lookup for that colour."""

    # paint100: needs_review FIRST in the filter, because a flagged row is the
    # only thing here that wants acting on — the rest is a reference table.
    list_display = ('manufacturer', 'code', 'colour_name', 'needs_review',
                    'previous_name', 'model_text', 'source_registration',
                    'created_at')
    list_filter = ('needs_review', 'manufacturer')
    # paint101: DERIVED, so not editable. It is recomputed from colour_name on
    # every save; leaving it as a text box invited exactly the drift that
    # override exists to prevent, and a stale value breaks the name->code
    # direction while the row still looks correct.
    readonly_fields = ('normalized_name', 'created_at')
    search_fields = ('manufacturer', 'code', 'colour_name', 'source_registration')
    ordering = ('-created_at',)
