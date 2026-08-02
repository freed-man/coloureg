"""Scrub personal data from Search records older than 12 months.

This implements the retention policy declared in the privacy notice. Run manually:

    python manage.py prune_old_data           # actually perform the scrub
    python manage.py prune_old_data --dry-run # just show what would be changed

Personal fields scrubbed: ip_address, user_agent, vin, email, customer_message,
manual_note. Also deletes stale VrmCache entries (which cache a VIN in their
payload and would otherwise retain it indefinitely, contradicting the notice).
Aggregate fields preserved: registration, make, model, year, colour, vehicle_title,
paint_code, paint_description, timestamp, success, lookup_duration_ms, etc.

After scrubbing, the row is no longer personally identifying but remains useful
for trend reporting and product analytics.
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from lookup.models import Search, VrmCache
from lookup.services.protection import VRM_CACHE_TTL_DAYS


class Command(BaseCommand):
    help = "Scrub personal fields from Search records older than 12 months."

    # The cutoff window. Update this if the retention policy changes — but
    # remember to update the privacy notice's "How long we keep it" section
    # at the same time so they stay in sync.
    RETENTION_DAYS = 365

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show how many records would be scrubbed without making changes.',
        )

    def handle(self, *args, **options):
        cutoff = timezone.now() - timedelta(days=self.RETENTION_DAYS)
        dry_run = options['dry_run']

        # Find rows older than cutoff that still have at least one personal
        # field populated. Using positive Q-disjunction makes intent clear:
        # "row has at least one of these fields set." Once a row's personal
        # fields are already empty, there's nothing left to scrub.
        has_personal = (
            Q(ip_address__isnull=False)
            | ~Q(user_agent='')
            | ~Q(vin='')
            | ~Q(email='')
            # paint16: free text the customer wrote when requesting a manual
            # lookup. It is arbitrary user input and therefore the field MOST
            # likely to contain personal detail ("it's my wife's car", an
            # address, a phone number), so it must be scrubbed like any other.
            | ~Q(customer_message='')
            # ...and the note we wrote back, which frequently references them.
            | ~Q(manual_note='')
        )
        candidates = Search.objects.filter(timestamp__lt=cutoff).filter(has_personal)

        count = candidates.count()
        oldest = candidates.order_by('timestamp').values_list('timestamp', flat=True).first()
        newest = candidates.order_by('-timestamp').values_list('timestamp', flat=True).first()

        self.stdout.write('')
        self.stdout.write(self.style.HTTP_INFO('Privacy retention scrub'))
        self.stdout.write(f'  Cutoff:                 {cutoff.isoformat()}')
        self.stdout.write(f'  Records to scrub:       {count}')
        if oldest and newest:
            self.stdout.write(f'  Oldest record:          {oldest.isoformat()}')
            self.stdout.write(f'  Newest record:          {newest.isoformat()}')
        self.stdout.write(f'  Mode:                   {"DRY RUN (no changes)" if dry_run else "LIVE (will modify)"}')
        self.stdout.write('')

        if count == 0:
            self.stdout.write(self.style.SUCCESS('Nothing to scrub. Database is clean.'))
            return

        if dry_run:
            self.stdout.write(self.style.WARNING(
                f'Dry run only. {count} records WOULD be scrubbed. Run without --dry-run to apply.'
            ))
            return

        # Perform the scrub. Set personal fields to their empty defaults.
        # IP gets None (it's nullable), the rest get empty strings.
        updated = candidates.update(
            ip_address=None,
            user_agent='',
            vin='',
            email='',
            customer_message='',
            manual_note='',
        )

        # --- VrmCache (paint16) --------------------------------------------
        # Cache payloads embed the VIN, so leaving them in place would retain a
        # VIN indefinitely — directly contradicting the privacy notice's promise
        # to scrub VINs after 12 months, and outliving the Search row it came
        # from. Entries older than the cache TTL are already ignored at read
        # time, so deleting them costs nothing and also stops the table growing
        # without bound (nothing else ever removes a row).
        stale_cutoff = timezone.now() - timedelta(days=VRM_CACHE_TTL_DAYS)
        stale = VrmCache.objects.filter(updated_at__lt=stale_cutoff)
        stale_count = stale.count()
        stale.delete()

        self.stdout.write(self.style.SUCCESS(
            f'Scrubbed personal fields from {updated} records.'
        ))
        self.stdout.write(self.style.SUCCESS(
            f'Deleted {stale_count} stale VrmCache entries '
            f'(older than {VRM_CACHE_TTL_DAYS} days, already past their TTL).'
        ))