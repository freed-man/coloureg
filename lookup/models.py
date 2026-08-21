import logging
import re
import unicodedata
from collections import namedtuple
from decimal import Decimal

from django.core.cache import caches
from django.db import models

logger = logging.getLogger(__name__)


class Search(models.Model):
    """Logs every paint code lookup."""

    PROVIDER_VDG = 'vdg'
    PROVIDER_VDG_RETRY = 'vdg_retry'
    PROVIDER_PARTSLINK24 = 'partslink24'
    PROVIDER_ONEAUTO = 'oneauto'
    PROVIDER_MANUAL = 'manual'
    PROVIDER_CACHE = 'cache'
    PROVIDER_NONE = 'none'
    PROVIDER_CHOICES = [
        # paint78: the labels, corrected for the vehicle/paint split.
        #
        # 'vdg' meant a paint code arriving in the FIRST call, back when that
        # call was a bundle carrying vehicle and paint together. It cannot
        # happen any more — vehicle_lookup never returns paint — so the value
        # only appears on historical rows and is labelled as what it was.
        #
        # 'vdg_retry' is no longer a retry. Post-split it is the ONLY VDG paint
        # call there is, so "VDG (retry)" was telling the reader a second
        # attempt had been needed when in fact nothing had been attempted before
        # it. The stored value stays put — renaming it would rewrite the meaning
        # of every historical row — but the label now says what it does.
        #
        # THE CUTOVER IS 15 AUGUST 2026, and this is the part that will mislead
        # anyone analysing across it. 'vdg_retry' means two different things
        # depending on which side of that date a row was written:
        #
        #   before  — the bundle came back without paint and a SECOND bundle
        #             call was made. A genuine retry. 261 rows.
        #   after   — the single PaintCodeDetails call. Not a retry at all,
        #             because nothing precedes it. 58 rows as of 19 Aug.
        #
        # That is exactly why the values are not being renamed. 'vdg_paint'
        # would be right for the 58 and wrong for the 261, collapsing a real
        # distinction and making the history less honest rather than more.
        # Anyone aggregating VDG performance across 15 August is comparing two
        # architectures and should say so.
        (PROVIDER_VDG, 'VDG (pre-15 Aug)'),
        (PROVIDER_VDG_RETRY, 'VDG'),
        (PROVIDER_PARTSLINK24, 'Partslink24'),
        # paint76. Without this a One Auto win left `provider` unset, so the one
        # question the second paid leg exists to answer — is it earning its 30p,
        # or duplicating what VDG already had — could not be asked of the data.
        # One word (paint82): this label renders in the Source badge, which is a
        # narrow uppercase pill in a table column. 'ONE AUTO' wraps or crowds
        # its neighbours where 'ONEAUTO' sits on one line like PARTSLINK24 and
        # VDG. The supplier's own name has the space; the badge does not have
        # the room, and consistency across that column matters more here.
        (PROVIDER_ONEAUTO, 'OneAuto'),
        (PROVIDER_MANUAL, 'Manual'),
        (PROVIDER_CACHE, 'Cache'),
        (PROVIDER_NONE, 'None'),
    ]

    # Identity & timing
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True, default='')
    # Computed at write time from user_agent, then queried as a plain CharField
    # for fast aggregation in /admin-stats/. Saves iterating every Search row
    # in Python on every dashboard render. Possible values match parse_device():
    # 'mobile', 'tablet', 'desktop', 'unknown'.
    device = models.CharField(max_length=8, blank=True, default='', db_index=True)

    # Search input
    registration = models.CharField(max_length=10, db_index=True)

    # Vehicle data (from DVLA / VDG)
    make = models.CharField(max_length=50, blank=True, default='')
    model = models.CharField(max_length=100, blank=True, default='')
    year = models.IntegerField(null=True, blank=True)
    colour = models.CharField(max_length=50, blank=True, default='')
    vehicle_title = models.CharField(max_length=200, blank=True, default='')
    # EU type-approval category from VDG (M1 passenger car, N1/N2/N3 commercial
    # van/truck). Used to route the pl24 fallback (commercial vehicles live in a
    # different part of the catalogue); stored so the dashboard can correlate
    # vehicle class with recovery time and pl24's hit rate. Empty when VDG didn't
    # provide it.
    category = models.CharField(max_length=8, blank=True, default='', db_index=True)

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
    # Per-document flags for the combined PaintCodeDetails call. Every search
    # makes exactly one combined call, so "was a call made" is implicit in the
    # row existing — what matters for cost is which DOCUMENTS came back:
    # vdg_vehicle_returned  — did Results.VehicleDetails return successfully?
    # vdg_paint_returned    — did Results.PaintCodeDetails return ≥1 paint code?
    #                          (if False, the paint portion is refunded by VDG)
    vdg_vehicle_returned = models.BooleanField(default=False)
    vdg_paint_returned = models.BooleanField(default=False)
    vdg_balance_after_call = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True
    )
    # The REAL amount VDG billed for this lookup (BillingInformation.
    # TransactionCost). Authoritative and tier-correct — summing this gives exact
    # spend with no assumed per-document price. Null on rows from before this
    # field existed, or where VDG returned no billing block (e.g. DVLA-fallback
    # lookups that never called VDG).
    vdg_transaction_cost = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True
    )

    # Stage 2 — automatic paint recovery (the /lookup-status fallback).
    # When the first VDG call returns a vehicle but no paint, the results page
    # polls /lookup-status, which races a 2nd VDG bundle call against the pl24
    # scraper and takes whichever returns paint first. These fields log what that
    # recovery did, so the dashboard can measure its hit rate and attribution:
    #   recovery_attempted    — did the /lookup-status recovery run at all?
    #   vdg_retry_returned    — did the 2nd (retry) VDG call return paint?
    #   pl24_attempted        — was the pl24 scraper queried?
    #   pl24_returned         — did pl24 return a usable CODE?
    #   recovery_name_only    — did pl24 return a colour NAME but no code? (a
    #                           partial result: we show + can email the name, but
    #                           it is NOT a code recovery, so `success` stays
    #                           False and `provider` is not set to partslink24.
    #                           Kept distinct so admin stats don't count it as a
    #                           full hit.)
    #   recovery_duration_ms  — wall-clock time the recovery took (ms); lets us
    #                           spot slow commercial-vehicle lookups.
    recovery_attempted = models.BooleanField(default=False)
    vdg_retry_returned = models.BooleanField(default=False)
    # What the SECOND VDG call actually returned (paint21).
    #
    # The recovery races the VDG retry against pl24 and serves whichever lands
    # first, so when pl24 wins the retry's answer is thrown away — including on
    # calls that DID find a code and that we had already paid for. Nothing has
    # ever recorded what was discarded, so there is no way to know whether the
    # two sources agree.
    #
    # Written by the retry worker itself, whenever it finishes, so it lands even
    # when the response has already gone out. Empty means the retry found no
    # code. Compare against paint_code to see whether the discarded answer
    # matched the one served.
    vdg_retry_code = models.CharField(max_length=100, blank=True, default='')

    # What pl24 returned, even when its answer was NOT the one served (paint26).
    #
    # Recovery races the VDG retry against a pl24 scrape and serves whichever
    # lands first. When the retry wins we return immediately, but the pl24
    # thread is not cancelled — cancel_futures only stops work that has not
    # begun — so the scrape runs to completion anyway and its answer is thrown
    # away. That happened on 215 of 750 recoveries.
    #
    # Since the work happens regardless, recording it is close to free and it
    # doubles the rate at which we learn whether the two sources actually agree.
    # vdg_retry_code captures the same thing from the other direction; between
    # them every contested recovery yields a comparison.
    #
    # NOTE the standing trade-off: an abandoned scrape holds the partslink24
    # session (pool_size=1) for up to 65s. At current volume only 8.4% of pl24
    # lookups begin within that window of the previous one, so it rarely blocks
    # anyone — but if traffic grows, the better answer is to CANCEL the loser
    # rather than record it, and that needs disconnect handling in the pl24
    # service, not here.
    pl24_code = models.CharField(max_length=100, blank=True, default='')
    # WHY pl24 gave what it gave (paint65). The service returns an outcome
    # string — success, name_only, paint_data_missing, not_found_as_routed,
    # catalog_ui_error, page_load_timeout — and coloureg used to read only
    # paint_code/paint_description and drop it.
    #
    # That cost a real diagnosis: 42 Renault lookups have pl24_attempted=True
    # and pl24_returned=False, and NOTHING on the row distinguishes "the page
    # had no code" from "the extractor could not read it" from "partslink24
    # does not carry the car". Those need different fixes, and the stats could
    # not tell them apart.
    #
    # Not retroactive — the existing rows stay blank. This only makes the NEXT
    # occurrence answerable without a manual debug dump.
    pl24_outcome = models.CharField(max_length=40, blank=True, default='')
    # What One Auto billed for this lookup (paint67). SEPARATE from
    # vdg_transaction_cost rather than folded into one total, because the two
    # answer different questions: the budget breaker wants the sum, but "is One
    # Auto earning its 30p or duplicating what VDG already had" needs them
    # apart. A 206 is free and leaves this null; a 200 with a NULL COLOUR still
    # bills, which is the case worth being able to count.
    oneauto_cost = models.DecimalField(
        max_digits=6, decimal_places=2, null=True, blank=True,
    )
    oneauto_outcome = models.CharField(max_length=40, blank=True, default='')
    # WHY pl24 was brought into the race, or blank if it never was (paint68).
    # pl24 is now held back as reinforcement rather than started on every
    # lookup, so "did it run" is no longer implied by "a lookup happened" — and
    # when it does run, the reason (a paid leg empty, a name with no code, or
    # the backstop firing on two slow legs) is the thing that tells you whether
    # the holding-back is costing answers.
    pl24_started_because = models.CharField(max_length=24, blank=True, default='')

    # WHAT EACH PROVIDER ACTUALLY SAID (paint69). Until now only pl24 recorded
    # its own answer, so a row could not tell you whether two sources agreed,
    # disagreed, or one merely had less of the same answer. That distinction is
    # the whole question: of 27 observed disagreements between pl24 and VDG, 18
    # were COMPLETENESS rather than conflict — pl24 returning 'C9X' where VDG
    # returned '2T2T/C9X', or '755' against '755U'. Only nine were genuinely
    # different codes, and five of those were Jaguar using a different code
    # system entirely.
    #
    # Each provider's answer is stored whether or not it won the race, because
    # the loser's answer is what makes the comparison possible — and because a
    # name we could not resolve today may be resolvable once the table grows.
    pl24_name = models.CharField(max_length=120, blank=True, default='')
    vdg_paint_name = models.CharField(max_length=120, blank=True, default='')
    oneauto_code = models.CharField(max_length=100, blank=True, default='')
    oneauto_name = models.CharField(max_length=120, blank=True, default='')

    @property
    def total_cost(self):
        """What this lookup actually cost across every paid provider (paint76).

        A property rather than a column: it is derived, and a stored copy would
        be one more thing to keep in step with two workers that both write late.
        Returns None only when NOTHING recorded a cost — which is different from
        zero, and the dashboard shows the two differently.
        """
        parts = [c for c in (self.vdg_transaction_cost, self.oneauto_cost)
                 if c is not None]
        return sum(parts) if parts else None

    # Which access key (if any) exempted this lookup from the hourly limit
    # (paint41). Stores the LABEL, not the key: the label is what you read on
    # the dashboard, and it keeps the secret out of the row.
    access_label = models.CharField(max_length=60, blank=True, default='')

    # --- Pay-to-reveal (paint22) ---------------------------------------------
    # The paid flow used to charge FIRST and look up second, so roughly a
    # quarter of paying customers were told afterwards that nothing was found
    # and their authorisation had been reversed. A reversal is not a refund and
    # costs them nothing, but it still shows as a pending charge on their
    # statement for days — so the experience was "you took my money and found
    # nothing", on one lookup in four.
    #
    # Now the lookup runs first and payment gates only the REVEAL. Charging
    # happens solely when a result is worth paying for, which by deliberate
    # policy means BOTH a code and a colour name: a code with no name (2 of 867
    # lookups in a month) is given away rather than sold as a partial answer.
    #
    # paywalled  = this result was complete enough to charge for and is being
    #              withheld pending payment.
    # paid_unlocked = payment completed and the result has been released.
    #
    # Together they are the conversion funnel: paywalled rows are the offers
    # made, paid_unlocked the ones taken.
    paywalled = models.BooleanField(default=False)
    paid_unlocked = models.BooleanField(default=False)

    def is_locked(self):
        """True while a chargeable result is being withheld pending payment."""
        return bool(self.paywalled) and not self.paid_unlocked
    pl24_attempted = models.BooleanField(default=False)
    pl24_returned = models.BooleanField(default=False)
    recovery_name_only = models.BooleanField(default=False)
    recovery_duration_ms = models.IntegerField(null=True, blank=True)

    @property
    def source_label(self):
        """What the Source column shows for THIS row.

        get_provider_display() can only see `provider`, so every VDG win reads
        the same whether the first paint call produced it or the second. Now
        that vdg_second_chance records which attempt won, the row itself can
        say so — which is the question "was that one VDG call or two?" answered
        where it is actually asked, instead of in a separate column nobody
        opens.

        'VDG (pre-15 Aug)' rather than 'VDG (bundle)': the reader does not need
        to know what a bundle was, only that the value is historical and
        nothing new will land there.
        """
        if self.provider == self.PROVIDER_VDG_RETRY:
            if self.vdg_second_chance == self.SECOND_CHANCE_WON:
                return 'VDG (2nd)'
            return 'VDG'
        return self.get_provider_display()

    #: Which attempt produced the answer, per provider. Both VDG and One Auto
    #: make a SECOND call when the first comes back without paint, and until now
    #: both recovered silently: `data = second` simply overwrote, leaving no
    #: trace. So a row that won on the second attempt was indistinguishable from
    #: one that won on the first.
    #:
    #: That mattered for two open questions neither of which could be answered:
    #:
    #:   1. Does VDG's second chance earn its £0.27? It is justified by
    #:      pre-split evidence — the 214 answers the old bundle-retry supplied —
    #:      measured under an architecture that no longer exists.
    #:   2. How often does it fire AFTER another provider has already won? That
    #:      is the entire value of a race-over flag, and without this it can
    #:      only be guessed at.
    #:
    #: Nullable on purpose: null means "no second call was made", which is the
    #: common case and must not be confused with "made and failed".
    SECOND_CHANCE_NOT_RUN = ''
    SECOND_CHANCE_EMPTY = 'empty'
    SECOND_CHANCE_WON = 'won'
    SECOND_CHANCE_CHOICES = [
        (SECOND_CHANCE_EMPTY, 'Fired, returned nothing'),
        (SECOND_CHANCE_WON, 'Fired, produced the code'),
    ]
    vdg_second_chance = models.CharField(
        max_length=8, blank=True, default='', choices=SECOND_CHANCE_CHOICES,
        help_text='Blank = no second VDG paint call was made.')
    oneauto_second_chance = models.CharField(
        max_length=8, blank=True, default='', choices=SECOND_CHANCE_CHOICES,
        help_text='Blank = no second One Auto call was made.')
    #: True when a provider had ALREADY won by the time this row's second chance
    #: fired. Every one of these is spend a race-over flag would have prevented,
    #: which turns that feature from an intuition into a number.
    second_chance_after_race = models.BooleanField(default=False)

    # Which part (if any) was filled from the PaintLookup database rather than
    # returned by the provider: 'code' (name→code), 'name' (code→name), or ''
    # (provider supplied everything / nothing filled).
    ENRICHED_NONE = ''
    ENRICHED_CODE = 'code'
    ENRICHED_NAME = 'name'
    ENRICHED_CHOICES = [
        (ENRICHED_NONE, 'Not enriched'),
        (ENRICHED_CODE, 'Code from database'),
        (ENRICHED_NAME, 'Name from database'),
    ]
    enriched_from = models.CharField(
        max_length=8, choices=ENRICHED_CHOICES, blank=True, default='')

    # Email / manual fallback
    email = models.EmailField(blank=True, default='')
    email_sent = models.BooleanField(default=False)
    manual_lookup_completed = models.BooleanField(default=False)

    # --- Manual lookup: context in both directions (paint16) ---------------
    # What the CUSTOMER told us when requesting a manual lookup. Optional free
    # text from the request form ("it's a Japanese import", "previous owner
    # resprayed it", "estate not saloon"). This is often the difference between
    # a dead end and a found code, and it used to exist only in the notification
    # email — invisible when you came back to the row later.
    customer_message = models.TextField(blank=True, default='')

    # What WE wrote back when fulfilling the manual lookup. Previously this went
    # straight into the outgoing email and was discarded, so there was no record
    # of what the customer had actually been told — including any caveat
    # ("closest match for your year", "verify against the label"). Stored so the
    # row is a complete account of the exchange.
    manual_note = models.TextField(blank=True, default='')

    # THIRD OUTCOME STATE (paint16). Set when a manual lookup concludes that no
    # paint code exists for this vehicle in ANY source — VDG, partslink24, and
    # the manufacturer/dealer route all checked, and the code genuinely isn't
    # published. This is neither a success (no code delivered) nor a failure
    # (the pipeline worked correctly; the data does not exist). Folding it into
    # either one corrupts the success rate: counting it as failure punishes us
    # for the manufacturer's gap, counting it as success claims a code we never
    # gave. It is therefore excluded from the success-rate denominator and
    # reported separately. In paid mode it is unambiguously a no-charge.
    no_code_available = models.BooleanField(default=False)

    def save(self, *args, **kwargs):
        """Truncate over-length text before it reaches the database.

        Every string on this row originates from an external system (VDG, DVLA,
        MOT, partslink24) or from a user, and Postgres enforces varchar limits
        strictly — an over-length value raises DataError and 500s the request,
        after the paid API call has already been made. SQLite does not enforce
        them, so this class of failure is invisible in local development and
        only appears in production.

        Truncating is the right trade: a clipped model name is a cosmetic loss,
        a failed save loses the lookup entirely AND the money spent on it. The
        clip is logged so a systematically over-length field is visible rather
        than silently shortened.
        """
        for field in self._meta.fields:
            max_length = getattr(field, 'max_length', None)
            if not max_length:
                continue
            value = getattr(self, field.name, None)
            if isinstance(value, str) and len(value) > max_length:
                logger.warning(
                    'Search.%s exceeded max_length (%d > %d) and was truncated',
                    field.name, len(value), max_length,
                )
                setattr(self, field.name, value[:max_length])
        super().save(*args, **kwargs)

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

# =============================================================================
# PaintLookup — runtime paint-colour table (replaces the old PaintSwatch).
#
# Populated by `python manage.py load_paint_lookup` from paint_lookup.json
# (committed to the repo at lookup/data/paint_lookup.json). One row per unique
# (manufacturer, code) — codes are 1:1 with a colour within a make, so this key
# is unique. Produced by the paintscraper merge of chipex + colorndrive.
#
# Multipurpose, sitting BEHIND the live VDG/pl24 race to fill the gaps providers
# leave:
#   - code  -> name + hex   (reliable: one code = one colour within a make)
#   - name  -> code         (conservative: filled ONLY when a (make, name) maps
#                            to exactly one code; names are 1:many so ambiguous
#                            names return nothing — a wrong code is worse than none)
#   - either -> swatch (hex)
# =============================================================================


class PaintLookup(models.Model):
    """A paint colour row keyed by manufacturer + code (1:1 with a colour)."""

    # Lightweight row used by code_from_name (paint17). The matcher has to pull
    # an ENTIRE make into Python to test names — 4,081 rows for Ford — and
    # building full model instances for all of them, each decoding four
    # JSONFields, dominated the cost: ~338ms and 6.2MB peak to find nine
    # matches. Fetching plain tuples and wrapping them in a namedtuple is ~2.8x
    # faster for ~40% of the memory, and because a namedtuple keeps ATTRIBUTE
    # access (r.code, r.hex, r.name, r.models_list) the downstream helpers —
    # _collapse_to_single_code and _model_matches — are untouched.
    #
    # Verified data-equivalent, not assumed: all 120,465 rows across all 281
    # makes come back with identical values AND identical types (the JSONFields
    # decode to real lists either way).
    #
    # It also sidesteps the trap in the .only() approach this replaces, where
    # reading any field outside the deferred set silently costs a query PER ROW.
    # These tuples simply have no other fields to read.
    # 'all_names' added (paint80) so a name-resolved lookup can DISPLAY the name
    # it actually matched. Costs one more column on a values_list that paint17
    # measured; the row count is unchanged and the name test still runs on the
    # raw tuple before any namedtuple is built.
    _MATCH_FIELDS = ('code', 'hex', 'name', 'normalized_names', 'models_list',
                     'all_names')
    _MatchRow = namedtuple('_MatchRow', _MATCH_FIELDS)
    # Positional index used to test the name before paying for the namedtuple.
    # Derived from the field list so the two cannot drift apart.
    _MATCH_NAMES_IDX = _MATCH_FIELDS.index('normalized_names')

    # Lookup keys (normalised: lowercase mfr / uppercase code)
    manufacturer = models.CharField(max_length=64, db_index=True)
    code = models.CharField(max_length=64, db_index=True)

    # Colour data
    name = models.CharField(max_length=200, blank=True, default='')   # best/most-common name
    all_names = models.JSONField(default=list)                        # every name variant seen
    normalized_names = models.JSONField(default=list)                 # normalised name variants
    hex = models.CharField(max_length=7, blank=True, default='')      # canonical hex; '' if none

    # Disambiguation / provenance
    color_group = models.CharField(max_length=20, blank=True, default='', db_index=True)
    models_list = models.JSONField(default=list)   # models the colour appears on (colorndrive); may be []
    sources = models.JSONField(default=list)       # ["chipex"], ["colorndrive"], or both — trust signal

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['manufacturer', 'code'],
                name='lookup_paintlookup_uniq',
            ),
        ]
        indexes = [
            models.Index(fields=['manufacturer', 'code']),
            models.Index(fields=['manufacturer', 'color_group']),
        ]
        verbose_name = 'Paint lookup'
        verbose_name_plural = 'Paint lookups'

    def __str__(self):
        return f"{self.manufacturer} {self.code} {self.hex} ({self.name})"

    # ------------------------------------------------------------------
    # Normalisation
    # ------------------------------------------------------------------

    # Manufacturer aliases — VDG/DVLA brand name → how the scraped data stores it.
    MANUFACTURER_ALIASES = {
        'mercedesbenz': 'mercedes',  # VDG 'Mercedes-Benz' → DB 'mercedes'
        # Range Rover is a Land Rover model line, not a separate marque. The paint
        # data folds all 'rangerover' rows into 'landrover' (same codes, same hex),
        # so a provider returning make 'Range Rover' must route to 'landrover' where
        # the rows now live — otherwise these lookups hit an empty make key and miss.
        # (Note: 'blmcrover', the British-Leyland-era bucket, is deliberately kept
        # separate — it is NOT folded here.)
        'rangerover': 'landrover',
        # AMG is a Mercedes sub-brand, not a separate marque: the paint data
        # files every AMG colour under 'mercedes'. Without this an AMG lookup
        # normalises to 'mercedesamg', which has ZERO rows, so a correct code
        # resolves to no name, no swatch and no catalogue cross-check — the
        # same failure 'mercedesbenz' above was added to prevent, for the same
        # marque, just never extended to the performance badge.
        #
        # Verified 19 Aug 2026: the codes AMG lookups delivered (799, 144) are
        # present under 'mercedes' and absent under 'mercedesamg'.
        'mercedesamg': 'mercedes',
    }

    @staticmethod
    def normalize_manufacturer(text):
        """Match the normalisation used at merge time, with aliasing."""
        if not text:
            return ''
        norm = text.strip().lower().replace('-', '').replace(' ', '').replace('.', '')
        return PaintLookup.MANUFACTURER_ALIASES.get(norm, norm)

    # Lazily-built tuple of compiled finish-word patterns; see normalize_name.
    _FINISH_RE = None

    @staticmethod
    def normalize_name(text):
        """Normalise a colour NAME for the name->code direction.

        MUST match the rules paintscraper used to build `normalized_names`
        (accent-fold to ASCII, lowercase, punctuation stripped, finish words
        removed) — otherwise name->code lookups will silently miss. The merge
        pipeline is the single source of truth; this mirrors it (accent fold +
        the merge's exact finish-word list). If that pipeline's rules change,
        update this to match.
        """
        if not text:
            return ''
        t = text.strip().lower()
        # Fold accents to base ASCII (ü->u, ê->e, ã->a, ...) to match how the merge
        # built normalized_names. NFKD splits an accented char into base+combining
        # mark; dropping the combining marks leaves the base letter. Chars NFKD
        # doesn't decompose (e.g. ß) fall through to the punctuation strip below,
        # which the merge also does (ß -> space), so they stay in sync.
        t = unicodedata.normalize('NFKD', t)
        t = ''.join(c for c in t if not unicodedata.combining(c))
        # Strip finish/qualifier words — this list MUST match the merge's exact
        # set (the merge stripped all of these when building normalized_names, so
        # the query must strip them too or accented/finish-suffixed names silently
        # miss). Ordered longest-first so multi-token words ('clearcoat' before
        # 'coat', 'metallise' before 'met') are removed before their substrings.
        # Expanding this list cannot produce a WRONG code: the data already made
        # the name-conflation when it stripped these, and code_from_name declines
        # on any residual ambiguity — so the worst case is a decline, never a
        # wrong answer (same risk class as the long-standing 'metallic'/'pearl'
        # stripping).
        # 'exterior paint' and 'paintwork' are partslink24 WRAPPER words, not
        # finishes: it returns Jaguar/Land Rover names as "Exterior Paint - X"
        # and Vauxhall/Opel names as "Metallic X Paintwork". Left in place they
        # defeat the name lookup entirely — 'Metallic Voltaic Blue Paintwork'
        # missed while a bare 'Voltaic Blue' resolved to 23D. Verified safe:
        # NEITHER phrase appears in any of the 120,465 stored colour names, so
        # stripping them can only remove noise, never part of a real name.
        # Listed before the shorter finish words so the phrase is consumed whole.
        # COMPILED ONCE (F14). This rebuilt ~30 pattern strings on every call —
        # re caches the compiled objects, but the concatenation and re.escape
        # ran each time regardless. Measured at 51us/call before, 15us after.
        # Built lazily and cached on the class because PROVIDER_WRAPPER_WORDS is
        # defined further down the class body and is not available yet at this
        # point; the ORDER is preserved exactly, which matters — the list is
        # longest-first so 'clearcoat' is consumed before 'coat' and
        # 'metallise' before 'met'.
        rxs = PaintLookup._FINISH_RE
        if rxs is None:
            rxs = PaintLookup._FINISH_RE = tuple(
                re.compile(r'\b' + re.escape(w) + r'\b')
                for w in PaintLookup.PROVIDER_WRAPPER_WORDS + (
                    'clearcoat', 'pearlescent', 'metalizado', 'metallise',
                    'metalise', 'tricoat', 'metallic', 'metalic', 'perlato',
                    'nacre', 'pearl', 'perl', 'satin', 'solid', 'gloss', 'matte',
                    'matt', 'mica', 'effect', 'tri', 'coat', 'uni', 'met')
            )
        for rx in rxs:
            t = rx.sub(' ', t)
        # strip punctuation
        t = re.sub(r'[^a-z0-9 ]', ' ', t)
        # collapse whitespace
        t = re.sub(r'\s+', ' ', t).strip()
        return t

    @staticmethod
    def normalize_code_variants(paint_code):
        """Generate reasonable variants of an incoming code to try, EXACT-match
        first.

        Per the merge pipeline's decision: codes are matched exactly (uppercased
        only); punctuation is NOT stripped (a Renault dot can mean matt vs gloss,
        dashes mark shade variants). So we try the full uppercased string first,
        then split on / and , (VDG joins two codes like '8E8E/A7W'), then a
        last-resort numeric-suffix strip ('197U' -> '197') tried LAST.
        """
        if not paint_code:
            return []

        variants = []
        seen = set()

        full = paint_code.strip().upper()
        if full and full not in seen:
            variants.append(full)
            seen.add(full)

        for part in re.split(r'[/,]', full):
            part = part.strip()
            if part and part not in seen:
                variants.append(part)
                seen.add(part)

        # Last-resort numeric-suffix strip (appended last → only used if exact misses)
        suffix_candidates = []
        for v in list(variants):
            if len(v) >= 4 and v[0].isdigit():
                if v[-1].isalpha():
                    base = v[:-1]
                    if base not in seen:
                        suffix_candidates.append(base)
                        seen.add(base)
                if len(v) >= 5 and v[-1].isalpha() and v[-2].isalpha():
                    base = v[:-2]
                    if base not in seen:
                        suffix_candidates.append(base)
                        seen.add(base)
        variants.extend(suffix_candidates)

        return variants

    # ------------------------------------------------------------------
    # code -> swatch (hex + name)  [the reliable direction]
    # ------------------------------------------------------------------

    # Makes that prefix paint codes with a leading 'L' at the factory (e.g. the
    # body code 'X7W' is stored as 'LX7W'). VDG/partslink24 report the code
    # WITHOUT that leading L, so a VW/Audi lookup can miss on the exact code.
    # Scoped to VW + Audi ONLY — there ~86-89% of codes carry the L (so "they
    # dropped the L" is a sound inference). NOT applied to Seat/Porsche/Skoda,
    # where the L-prefix is rare (~8-22%) and the inference would be a guess.
    LEADING_L_MAKES = {'volkswagen', 'audi'}

    @classmethod
    def lookup(cls, manufacturer, paint_code, model=None, year=None, vdg_colour=None):
        """Find the row for (manufacturer, code). Returns a PaintLookup or None.

        `(manufacturer, code)` is unique in this table (one code = one colour
        within a make), so the only real work is trying code variants in order.
        The `model`, `year`, `vdg_colour` params are accepted for call-site
        compatibility with the old PaintSwatch.lookup signature but are not
        needed to disambiguate (there is at most one row per code).

        VW/Audi leading-L fallback: if the plain code(s) don't match, retry each
        variant with a leading 'L' (see LEADING_L_MAKES). This is tried ONLY
        after the exact/split variants miss, and a given variant's L-form is only
        used when the plain form is ABSENT — so it can never override a correct
        exact match, and never collides with the solid-vs-metallic finish
        variants (those have BOTH forms present, so exact wins). This also
        recovers compound two-tone codes like 'B4B4/B9A' → split → 'B9A' →
        'LB9A', since the split parts are among the variants.
        """
        if not manufacturer or not paint_code:
            return None

        mfr_norm = cls.normalize_manufacturer(manufacturer)
        variants = cls.normalize_code_variants(paint_code)

        # 1) Exact / split variants (the normal, reliable path).
        # ONE query for all variants instead of one per variant (paint17). A
        # compound code like 'B4B4/B9A' produced three round-trips here and six
        # with the L-fallback below, each a full hop to Neon from its 0.25 CU
        # floor — and results() repeats the whole thing per entry in
        # all_paint_codes, so a three-code vehicle cost ten queries just for
        # swatches. Precedence is unchanged: the loop below still walks
        # `variants` in order and returns the first hit, exactly as the
        # query-per-variant version did.
        rows = {
            r.code: r
            for r in cls.objects.filter(manufacturer=mfr_norm, code__in=variants)
        }
        for code in variants:
            match = rows.get(code)
            if match:
                return match

        # 2) VW/Audi leading-L fallback (only when the plain form is absent).
        if mfr_norm in cls.LEADING_L_MAKES:
            l_variants = ['L' + code for code in variants]
            l_rows = {
                r.code: r
                for r in cls.objects.filter(manufacturer=mfr_norm, code__in=l_variants)
            }
            for code in l_variants:
                # One 'L' has been prepended above. That is safe even when the
                # variant already starts with 'L' (the body code itself can be
                # e.g. 'L5M', stored at the factory as 'LL5M'): step 1 already
                # confirmed the plain form has no row, and there are no cases
                # where both 'LX' and 'LLX' exist as different colours — so the
                # plain-absent check is the real guard, not a no-double-L rule.
                match = l_rows.get(code)
                if match:
                    return match

        return None

    @classmethod
    def find_canonical_code(cls, manufacturer, paint_code, swatch=None):
        """If paint_code is a short/abbreviated form, find the canonical longer
        code that points to the same hex (e.g. 'L8' -> 'LZ9Y').

        Conservative: input ≤3 chars; candidate strictly longer; not a 1-2 char
        suffix variant (those are process variants like L8PA); not the input
        doubled. Returns the longer code string or None.
        """
        if not manufacturer or not paint_code:
            return None

        original = paint_code.strip().upper()
        if len(original) > 3:
            return None

        mfr_norm = cls.normalize_manufacturer(manufacturer)

        if swatch is None:
            swatch = cls.lookup(manufacturer, paint_code)
        if swatch is None or not swatch.hex:
            return None

        target_hex = swatch.hex
        siblings = cls.objects.filter(
            manufacturer=mfr_norm, hex=target_hex
        ).exclude(code=original)

        candidates = []
        for s in siblings:
            c = s.code
            if len(c) <= len(original):
                continue
            if c == original + original:
                continue
            if c.startswith(original) and len(c) - len(original) <= 2:
                continue
            candidates.append(s)

        if not candidates:
            return None

        # Prefer the most cross-validated (most sources), then shortest, then alpha
        best = max(candidates, key=lambda s: (len(s.sources or []), -len(s.code), s.code))
        return best.code

    @classmethod
    def lookup_with_canonical(cls, manufacturer, paint_code, model=None, year=None, vdg_colour=None):
        """Convenience: swatch lookup + canonical expansion.

        Returns (paint_hex, paint_name, canonical_code) — all None on miss or
        exception. Never raises; a swatch failure must never break the caller.
        Same signature/return as the old PaintSwatch method so the existing
        call sites in views.py work unchanged.
        """
        if not paint_code:
            return None, None, None
        try:
            swatch = cls.lookup(
                manufacturer=manufacturer,
                paint_code=paint_code,
                model=model,
                year=year,
                vdg_colour=vdg_colour,
            )
            if not swatch:
                # L-PREFIX FALLBACK (paint72). BMW paint codes exist in two
                # forms — bare ('475') and the catalogue form with an L for
                # Lack ('L475'). Our table holds the bare form (2 of 1825 BMW
                # rows start with L) and so do pl24 and VDG, but One Auto
                # returns the full form: it gave 'L475' for the same 530e that
                # pl24 answered as '475'. Unresolved, that reaches the customer
                # as a bare number with no colour name and no swatch.
                #
                # NOT a blanket strip. Audi codes genuinely start with L —
                # LY2Z, LI3K, LSP3 are real paints — so this fires ONLY when
                # the given code missed AND the stripped form hits. An Audi
                # code resolves on the first attempt and never reaches here.
                stripped = (paint_code or '').strip()
                if len(stripped) > 1 and stripped[0] in ('L', 'l'):
                    alt = cls.lookup(
                        manufacturer=manufacturer,
                        paint_code=stripped[1:],
                        model=model,
                        year=year,
                        vdg_colour=vdg_colour,
                    )
                    if alt:
                        logger.info(
                            'paint code %s resolved as %s after dropping the '
                            'L prefix (%s)', paint_code, stripped[1:], manufacturer,
                        )
                        swatch = alt
                        paint_code = stripped[1:]
            if not swatch:
                return None, None, None
            canonical = cls.find_canonical_code(
                manufacturer=manufacturer,
                paint_code=paint_code,
                swatch=swatch,
            )
            # hex may be '' (name-only rows) — normalise to None for the caller
            _name = swatch.name or None
            if cls.is_combination_name(_name):
                # paint47: try to EXPAND it first. A combination row is a real
                # two-tone car, and both halves usually exist as proper rows —
                # "QAB Pearl White with Z11 Black Metallic" is useful where
                # "Z11 + Qab" is not. Only if expansion fails do we fall back to
                # paint46's suppression.
                _parts = cls.expand_combination(manufacturer, _name, vdg_colour=vdg_colour)
                if _parts:
                    _body = _parts[0]
                    _roof = _parts[1]
                    # paint49: name the halves as body/roof ONLY when the DVLA
                    # colour actually identified one. Otherwise say "and", which
                    # claims nothing about which is which.
                    if _body.get('is_body'):
                        _label = 'Two-tone: %s %s (body) with %s %s (roof)' % (
                            _body['code'], _body['name'], _roof['code'], _roof['name'])
                    else:
                        _label = 'Two-tone: %s %s and %s %s' % (
                            _body['code'], _body['name'], _roof['code'], _roof['name'])
                    logger.info(
                        'Combination row expanded: %s %s -> %s',
                        manufacturer, paint_code, _label,
                    )
                    # Body hex drives the swatch; the caption names both halves.
                    return (_body['hex'] or swatch.hex or None), _label, canonical
                # A two-tone combination row, not a colour. Suppress the NAME but
                # keep the hex: 71 of the 6,703 carry a blended hex the merge
                # resolved, and a swatch with no caption still helps a customer.
                #
                # LOGGED, not silent. A combination code arriving from a provider
                # means an extractor is reading a trim field — that is the signal,
                # and hiding it quietly would remove the only evidence. This is
                # exactly how partslink24's "Exterior color" (dealer: TRIM COLOR)
                # went unnoticed on two Suzukis.
                logger.warning(
                    'Combination row suppressed: %s %s -> %r (extractor may be '
                    'reading a trim field)', manufacturer, paint_code, _name,
                )
                _name = None
            return (swatch.hex or None), _name, canonical
        except Exception:
            return None, None, None

    # ------------------------------------------------------------------
    # name -> code  [the conservative direction]
    # ------------------------------------------------------------------

    # Hand-verified name->code overrides, checked BEFORE the general matcher in
    # code_from_name(). Scope is deliberately tiny: the only (make, colour) pairs
    # that actually appear as live name-only (the matcher's declined / partial
    # case) lookups in the admin log and are genuinely ambiguous to the matcher
    # below, but which we have verified by hand to resolve to a single code. This
    # is the small, zero-leak-risk alternative to a full cross-ref canonical merge.
    #   - Keys are LIGHT-normalized only (lowercase + single-spaced); they are NOT
    #     run through normalize_name(), because that strips finish words and would
    #     merge a solid into its pearl. Ford 'panther black solid' -> PNJAB vs
    #     'panther black pearl' -> 17V is the case that needs this, and the two
    #     entries below are what keep them apart.
    #
    #   - CORRECTED 8 Aug: 'santorini black pearl' -> PAB (was PBF, then briefly
    #     removed entirely). Two separate errors, worth keeping both on record:
    #
    #     ERROR 1, the original entry: -> PBF. Justified on the basis that PBF is
    #     that pearl's bare code and 'Sumatra Black Pearl' is merely its older
    #     name. That does not hold. PBF / 797 / LRC797 are Sumatra Black Pearl
    #     (#1B2125) in THREE sources (chipex, colorndrive, peinturevoiture);
    #     Santorini is 1AG / 820 / LRC820 / PAB / 2103 (#111112) in the same
    #     three. The families are disjoint. The ONLY row naming anything
    #     'Santorini Black Pearl' is LRC797-PBF, from ONE source, whose all_names
    #     carries BOTH names and whose hex (#13171A) matches neither family. That
    #     is a conflated listing, not a rename.
    #
    #     ERROR 2, the first fix: removing the entry so the name DECLINED. That
    #     assumed no Santorini Black Pearl existed. It does. Retailers list it
    #     against 1AG / PAB / 820 (PaintScratch: "1AG/PAB/820 Santorini Black
    #     Pearl", model years 2017-2022), the same code set they return for
    #     Santorini Black. Metallic and Pearl are retailer descriptors for ONE
    #     Land Rover black, not two paints, which is why every row in that family
    #     shares #111112.
    #
    #     So the finish word does not separate anything here and the answer is
    #     PAB either way. Note this restores the invariant that every override
    #     resolves to a code whose own canonical name matches the key once
    #     normalized: PBF was the single violation across all 21 entries.
    #     Do NOT map it back to PBF without dealer confirmation.
    # -----------------------------------------------------------------------
    # MODEL-AWARE overrides (paint16b).
    #
    # Distinct from CURATED_NAME_OVERRIDES below, which is blanket-per-make and
    # is consulted BEFORE model matching. A blanket entry is right when a name
    # maps to one code across the whole range (e.g. Kinetic Blue -> BDU), but
    # WRONG when a manufacturer genuinely uses two different paints of the same
    # name on different models. Ford 'Race Red' is the case that forced this:
    #
    #   BRQAWHA (#A70000)  focus, focusst, fiesta, mustang, ranger, transit*, ...
    #   BRQAWWA (#A40000)  focus, focusst, fiesta, kuga, edge, cmax, bmax, ...
    #
    # Model matching already resolves the models unique to one row (Kuga and
    # Edge -> BRQAWWA, Mustang and F-Max -> BRQAWHA). It correctly DECLINES for
    # the 13 models listed on both, because guessing a paint code is worse than
    # returning nothing. A blanket override would have fixed Focus while
    # silently breaking Kuga and Edge, which currently resolve correctly.
    #
    # So: this table resolves a name ONLY for a specific model, leaving every
    # other model to the existing logic. Entries must be evidenced (a
    # manufacturer catalogue or a confirmed paint label), never inferred from a
    # sibling model.
    #
    # Structure: {make: {normalised name: {model tag: code}}}
    # Model tags use the same prefix matching as _model_matches.
    CURATED_MODEL_OVERRIDES = {
        'ford': {
            'race red': {
                # Confirmed against Roland's Ford catalogue for a 2018 Focus
                # ST-3 (VIN WF05XXGCC5JT23802). partslink24 returns the NAME
                # for Ford passenger cars but no code, so this is the path that
                # turns a name-only result into a usable answer.
                'focus': 'BRQAWHA',
                'focusst': 'BRQAWHA',

                # --- paint20 ------------------------------------------------
                # Everything below was previously declined. Ford lists Race Red
                # under both BRQAWHA and BRQAWWA, and every model here appears
                # in BOTH codes' model lists, so the matcher could not choose
                # and returned nothing — a name with no code, on a colour we
                # actually know.
                #
                # Evidence that the choice does not matter: Ford issues several
                # codes for one Race Red formulation depending on plant, region
                # and model year, and paint suppliers (Chipex, Touch Up Paint
                # Factory) sell BRQAWHA and BRQAWWA under a single mix formula
                # alongside BRQAXWA / M7236A / PN4A7 / PQ / 7236. Either code
                # buys the same paint. BRQAWHA is used for consistency with the
                # Focus entries above.
                #
                # Scope is deliberately narrow: ONLY models whose candidate set
                # is exactly {BRQAWHA, BRQAWWA} — verified, no third code is
                # ever involved. Models that already resolve on their own
                # (kuga, edge, transit -> BRQAWWA; etransit, fmax, mustang* ->
                # BRQAWHA) are untouched.
                #
                # This is NOT a general rule. Two codes sharing a name usually
                # means two different colours, and the `hex` column cannot tell
                # them apart — one hex value in this dataset spans 1,516
                # distinct colour names, and #851718 covers 'bursting green'
                # and 'electric yellow' as well as 'race red'. So it stays what
                # the surrounding table says it must be: evidenced entries only.
                'fiesta': 'BRQAWHA',
                'fiestast': 'BRQAWHA',
                'fiestavan': 'BRQAWHA',
                'ecosport': 'BRQAWHA',
                'puma': 'BRQAWHA',
                'ranger': 'BRQAWHA',
                'tourneoconnect': 'BRQAWHA',
                'tourneocourier': 'BRQAWHA',
                'tourneocustom': 'BRQAWHA',
                'transitconnect': 'BRQAWHA',
                'transitcourier': 'BRQAWHA',
                'transitcustom': 'BRQAWHA',
            },
        },
    }

    # CODE -> CODE, where a provider reports a real code that nobody sells.
    #
    # Distinct from CURATED_NAME_OVERRIDES below (name -> code) and
    # CURATED_MODEL_OVERRIDES above (model-scoped disambiguation). This one axis
    # over: same paint, two identifiers, one of which is the one a customer can
    # actually buy.
    #
    # mitsubishi P26B -> P26. Both pl24 AND the dealer return P26B, so it is
    # genuinely what Mitsubishi's system holds and NOT an extractor artefact.
    # But no retailer sells it — Central Paints, PaintScratch, TouchUpDirect and
    # Color N Drive all list P26, and TouchUpDirect states Mitsubishi codes are
    # three characters (our own table agrees: 1,619 three-character Mitsubishi
    # codes against 125 four-character). Delivering P26B would hand someone a
    # code they cannot search for, with a colour name attached to make it look
    # authoritative.
    #
    # WHY A MAPPING AND NOT A ROW. paintscraper has P26B in none of its 251,709
    # raw records, so a row would be hand-written with no provenance and would
    # need defending against every rebuild — and it would deliver the
    # unpurchasable code, which is the problem it was meant to solve.
    #
    # WHY NOT A GENERAL SUFFIX RULE. Measured across the table: of 2,948
    # four-character codes whose three-character base also exists, 94% are a
    # DIFFERENT colour by name and 91% by hex. acura/R513 is Rallye Red while
    # R51 is Phoenix Red; alfaromeo/109C is Rosso Granturismo while 109 is Ochre
    # Yellow. Stripping blindly would be wrong five times in six, so each entry
    # here is one verified pair and nothing is inferred from it.
    #
    # The provider's original string is NOT lost: pl24_code and oneauto_code
    # keep what was actually returned, so this stays auditable.
    CURATED_CODE_OVERRIDES = {
        'mitsubishi': {'P26B': 'P26'},
    }

    @classmethod
    def map_code(cls, manufacturer, code):
        """Rewrite a provider code to the one a customer can buy, if we know of
        one. Returns the code unchanged otherwise — this must never invent."""
        if not code:
            return code
        by_make = cls.CURATED_CODE_OVERRIDES.get((manufacturer or '').strip().lower())
        if by_make:
            mapped = by_make.get(code.strip().upper())
            if mapped:
                return mapped
        return cls._strip_unsellable_form(manufacturer, code)

    #: Compound forms whose sellable half is the SECOND one. Measured across
    #: 1,229 delivered codes on 19 Aug 2026: 276 carried a slash and 39 were a
    #: Mercedes number with a trailing letter — 25.6% of everything delivered,
    #: in a form the 120,594-row catalogue does not stock.
    #:
    #: Both halves are genuine manufacturer data. VW's own build sheet calls the
    #: Golf colour "Night blue metallic (Z2Z2)", and One Auto returns
    #: "Z2Z2/H5X" — Z2Z2 is the marketing code, H5X the base paint code. Same
    #: relationship as SEAT's M6M6 and S7F, which are the same Oniric colour
    #: under two naming systems. Only the base code is sold by anyone.
    #:
    #: Mercedes is the same idea with a suffix rather than a separator: 799U and
    #: 799 are one colour, and the catalogue stocks 799.
    _MB_MAKES = ('mercedes', 'mercedesamg', 'mercedesbenz')

    @classmethod
    def _strip_unsellable_form(cls, manufacturer, code):
        """Reduce a compound or suffixed code to the half a retailer sells.

        ONLY REWRITES WHEN IT RESCUES THE CODE. The candidate must resolve in
        the catalogue AND the original must not. That single condition is what
        makes this safe: it cannot degrade a code that already works, whatever
        odd shape some future provider invents. Verified against every affected
        row in production — 310 rescued, 0 regressions.

        The guards below exist because the naive rule is actively harmful:

          * 'N/A' splits to 'A'. Two live rows carried it — a Ford and an AJS —
            and 'A' is a plausible-looking code that is entirely fictional.
            Anything under three characters is refused for that reason.
          * '379 / FQ 95-3853' (BMW) has spaces round the separator and inside
            the tail, so the split has to be trimmed rather than taken raw.
          * 'PDM/QDMS' (Jeep) is a real compound whose base is simply not in the
            catalogue. It is left alone: no rescue is available, and returning
            half a code we cannot verify would be a guess.
        """
        raw = (code or '').strip()
        if not raw:
            return code
        mfr = cls.normalize_manufacturer(manufacturer or '')

        candidates = []
        if '/' in raw:
            tail = raw.rsplit('/', 1)[-1].strip()
            if len(tail) >= 3:
                candidates.append(tail)
        elif mfr in cls._MB_MAKES and len(raw) >= 4 \
                and raw[-1].isalpha() and raw[:-1].isdigit():
            candidates.append(raw[:-1])

        if not candidates:
            return code

        # AMG needs no special case here any more: MANUFACTURER_ALIASES routes
        # 'mercedesamg' to 'mercedes' for every caller, so normalize_manufacturer
        # has already done it. This used to carry a private _PARENT table, which
        # was the same knowledge in two places and would have drifted.
        mfr_cat = mfr

        def sellable(value):
            """Does the catalogue stock this, under this make?

            The leading-L variant is tried because VAG codes are catalogued as
            LH5X while the providers return H5X — the same fallback the swatch
            lookup already makes.
            """
            if not value:
                return False
            forms = ([value, 'L' + value] if mfr in cls.LEADING_L_MAKES
                     else [value])
            return cls.objects.filter(manufacturer=mfr_cat,
                                      code__in=forms).exists()

        if sellable(raw):
            return code                      # already fine — never touch it
        for cand in candidates:
            if sellable(cand):
                logger.info('code %s reduced to %s for %s', raw, cand, manufacturer)
                return cand
        return code

    CURATED_NAME_OVERRIDES = {
        'landrover': {
            'santorini black': 'PAB',
            'santorini black metallic': 'PAB',
            'santorini black pearl': 'PAB',
            'varesine blue': 'JJA',
            'varesine blue metallic': 'JJA',
        },
        'ford': {
            # 'Frozen White' (= German 'Frostweiss') comes back name-only from
            # partslink24 and the matcher can't choose among ~10 chipex catalog
            # SKUs for what is a single white (#E0EEEF). 7VTAWWA is the code the
            # providers and manual resolutions actually return for it. This is the
            # only Ford name-only colour with both recurring live volume and a
            # confident canonical; the rest (Moondust, Panther Black, etc.) have
            # uncertain or genuinely ambiguous codes and are left to decline.
            'frozen white': '7VTAWWA',
            'frozen white solid': '7VTAWWA',
            # Further name-only Ford colours, each cross-verified by both repo chats
            # against paint_lookup.json and the provider returns. Most are a SINGLE
            # paint the matcher can't collapse (a same-named chipex 'PN' code or a
            # short code sits in the candidate set); the override just returns one
            # valid code for the paint. Keys are light-normalized (parens dropped),
            # so a provider '(Metallic)' finish lands on the '... metallic' key.
            'scuba': '8CLC',
            'scuba metallic': '8CLC',
            'midnight sky': 'BMZE',
            'morello': '8RTE',
            # 'Magnetic' (= 'Cinza Moscou' / 'Magnetic Grau') is one dark-grey paint
            # (#383838) wearing 7 codes the matcher can't collapse. PN4DQ is the
            # strongest row: exact name 'Magnetic', the dominant #383838 (5 of 7
            # codes agree on it), and a PN-code — the format providers return for
            # Ford. A live recurring name-only lookup. (Not provider-confirmed for
            # this colour, but the cleanest canonical; FM6E is the pv base but
            # carries no hex, so it would return a code with no swatch.)
            'magnetic': 'PN4DQ',
            'magnetic metallic': 'PN4DQ',
            # 'Moondust Silver' (= 'Gris Lunaire') is one silver (#C0C1C3) under 7
            # codes the matcher can't collapse. PNZJB is PROVIDER-CONFIRMED — VDG-retry
            # returned it for a Moondust Silver Ford (reg Y25SBS, code straight from the
            # provider, enriched_from='' so not DB-filled) — so it's anchored like
            # PNJAB, not a guess. (Parked earlier on a hex-spread doubt; the exact-name
            # codes all agree on #C0C1C3, the spread was just colorndrive's 'Gris
            # Lunaire Metallic' variants.)
            'moondust silver': 'PNZJB',
            'moondust silver metallic': 'PNZJB',
            # Panther Black is genuinely TWO paints (51 RGB apart), so it's a split,
            # not one canonical: solid #222327 -> PNJAB (provider-confirmed), pearl
            # #090C11 -> 17V. Plain name defaults to the confirmed solid; the
            # 'metallic'/'pearl' finishes route to the pearl. (The pearl code is the
            # clearest pearl-cluster code but is not itself provider-confirmed.)
            'panther black': 'PNJAB',
            'panther black solid': 'PNJAB',
            'panther black metallic': '17V',
            'panther black pearl': '17V',
            # 'Kinetic Blue' comes back name-only from partslink24 and maps to 5
            # codes the matcher can't collapse (9DSE has no hex; BDU #004A81 and
            # CDUCWWA #00487B are both EcoSport-tagged dark blues; BDUWWA/VBM are a
            # brighter #0080C0). The model tiebreaker narrows to BDU + CDUCWWA but
            # can't choose between them. BDU is Ford-retail-CONFIRMED: Ford's own
            # shop sells this paint as "Kinetic Blue, Colour Code BDU" (part
            # 2573827) -> anchored like DDSEWTA/9SSEWTA, not a guess. BDU (not the
            # research's other 'master' 9DSE) because 9DSE carries no hex, so it
            # would return a code with no swatch; BDU has #004A81 and the EcoSport
            # tag. Override returns BDU regardless of the other four candidates.
            'kinetic blue': 'BDU',
            'kinetic blue metallic': 'BDU',
        },
    }

    # partslink24 wraps colour names in provider boilerplate: Jaguar/Land Rover
    # come back as "Exterior Paint - X", Vauxhall/Opel as "Metallic X Paintwork".
    # These are NOT finishes — they carry no colour meaning — so both normalisers
    # strip them. Kept as one constant because the two normalisers must agree:
    # normalize_name feeds the name->code index, _light_normalize_name feeds the
    # curated-override lookup, and a name stripped by one but not the other
    # silently misses in whichever path it wasn't stripped in.
    # Verified safe: neither phrase appears in any of the 120,465 stored names.
    # 'paint' added (paint79) for One Auto, which renders Stellantis names with
    # the word attached at either end and a trailing dash: 'PAINT AGUEDA
    # YELLOW-', 'OKENITE WHITE PAINT-', 'BANQUISE WHITE PAINT'. Left in, those
    # normalise to 'paint agueda yellow' and 'okenite white paint', which match
    # nothing — while the same colours from partslink24 arrive bare and resolve.
    #
    # It belongs HERE rather than in the One Auto adapter because it is the same
    # class of thing as the two beside it: a provider's wrapper vocabulary, not
    # a finish. Fixing it here covers every Stellantis colour at once instead of
    # one curated alias per incident — the ESU alias shipped this morning has
    # the identical gap and this closes it too.
    #
    # Verified safe by the same test as the original two: the standalone word
    # 'paint' appears in NONE of the 120,594 stored colour names, so stripping
    # it can only remove noise. It is LAST in the tuple so 'exterior paint' is
    # consumed whole before the shorter word can break it.
    PROVIDER_WRAPPER_WORDS = ('exterior paint', 'paintwork', 'paint')

    @staticmethod
    def _light_normalize_name(colour_name):
        """Lowercase + trim + collapse internal whitespace, and drop parentheses so a
        provider finish in parens ('Scuba (Metallic)') lands on the same key as the
        bare form ('scuba metallic'). Deliberately does NOT strip finish words (cf.
        normalize_name) so 'pearl'/'metallic' survive to distinguish paint variants
        (e.g. Panther Black solid vs pearl)."""
        s = (colour_name or '').strip().lower().replace('(', ' ').replace(')', ' ')
        # Strip provider wrapper boilerplate only — finish words must survive here.
        for w in PaintLookup.PROVIDER_WRAPPER_WORDS:
            s = re.sub(r'\b' + re.escape(w) + r'\b', ' ', s)
        # A stripped prefix can leave a dangling separator ("- santorini black").
        s = re.sub(r'^[\s\-–—:,]+|[\s\-–—:,]+$', ' ', s)
        return re.sub(r'\s+', ' ', s).strip()

    # Trailing parenthetical suffixes that providers append to a colour name.
    # Matches the LAST bracketed group only, e.g. 'Squeeze (G)' -> 'Squeeze'.
    _TRAILING_PAREN_RE = re.compile(r'\s*\([^)]*\)\s*$')

    @classmethod
    def code_from_name(cls, manufacturer, colour_name, model=None):
        """Resolve a colour name, retrying without a trailing parenthetical.

        partslink24 decorates names in several ways: 'Squeeze (G)',
        'Barolo Black (861)', 'Bianco Perlato (Pearl White)'. About 6% of the
        names we receive carry one, and for a chunk of them the decorated form
        matches nothing while the bare name matches a paint we hold — the
        customer got a colour name and no code for a colour that was sitting in
        the table.

        The suffix is NOT simply noise, which is why this is a FALLBACK and not
        a normalisation. Stripping it unconditionally was measured against real
        traffic and changed five working answers: 'Panther Black (Metallic)'
        resolves to 17V today but to PNJAB once stripped, and 'Blue Candy (Foe)'
        goes from DDSEWTA to DDSE. The decorated name is matching a more
        specific row in those cases, and that row is the right one.

        So: try the name EXACTLY as given first, and only if that yields nothing
        try again without the suffix. Measured on real data that is 11 newly
        resolved and 0 changed — additive by construction, since the fallback
        can only run where the answer was already None.
        """
        result = cls._code_from_name_exact(manufacturer, colour_name, model=model)
        if result[0] is not None:
            return result
        if not colour_name:
            return result
        stripped = cls._TRAILING_PAREN_RE.sub('', colour_name.strip()).strip()
        if not stripped or stripped == colour_name.strip():
            return result
        return cls._code_from_name_exact(manufacturer, stripped, model=model)

    @classmethod
    def _code_from_name_exact(cls, manufacturer, colour_name, model=None):
        """Given a colour NAME (and make), return a single paint code — but ONLY
        when it is unambiguous.

        Colour names are 1:many with codes (one make can have many 'grey's), so
        this returns a code ONLY when the candidates collapse to a single paint.
        Three collapse rules are tried, strongest-evidence first; if none yields
        a single code the name is genuinely ambiguous and we return None — a
        wrong code is worse than none.

        Resolution order:
          1. Exactly one matching code -> return it.
          2. Dash-suffix variants of one base ('B554P-L'/'B554P-S' -> 'B554P').
          3. Prefix variants: one code is a strict prefix of all the others
             (Ford 'FLVA' / 'FLVAWWA' — the same paint in a bare vs suffixed code
             convention) -> return the shortest (the canonical base). Safe because
             genuinely different codes ('001'/'826') have no prefix relationship.
          4. If the full candidate set is still ambiguous, retry rules 1-3 on just
             the rows whose PRIMARY name equals the query — i.e. paints actually
             *named* this, excluding ones that merely list it as a secondary alias
             (e.g. a 'Stealth' search ignores 'Slate Grau' which only aliases
             Stealth). If that narrower set resolves, use it.
          5. If STILL ambiguous and the vehicle's `model` is known, narrow to the
             rows whose models_list includes that model, then retry rules 1-4 on
             that subset. This resolves names that map to different codes on
             different models (Ford 'Ruby Red' -> DSTEWTA on a Mondeo but 5R on a
             Figo; 'Midnight' -> 9AZCWWA on a Ka).

        Each step only ever NARROWS toward a single code; it never invents a match
        the base logic wouldn't have found, so this can only turn previously
        ambiguous (declined) names into resolved ones, never the reverse.

        Returns (code, hex, canonical_name) or (None, None, None).
        """
        if not manufacturer or not colour_name:
            return None, None, None
        try:
            mfr_norm = cls.normalize_manufacturer(manufacturer)

            # Curated overrides first (see CURATED_NAME_OVERRIDES). Matched on the
            # LIGHT-normalized name so finish words survive; on a hit we resolve the
            # code through the normal code->name lookup so the returned hex/name
            # come straight from the data. If the override code somehow doesn't
            # resolve, fall through to the general matcher rather than guess.
            # Model-aware overrides first: they are strictly more specific than
            # the blanket table, and only fire when we actually know the model.
            if model:
                by_name = cls.CURATED_MODEL_OVERRIDES.get(mfr_norm, {})
                model_map = by_name.get(cls._light_normalize_name(colour_name))
                if model_map:
                    for tag, code in model_map.items():
                        if cls._model_matches(model, [tag]):
                            row = cls.lookup(manufacturer, code)
                            if row is not None:
                                return row.code, row.hex, row.name

            override = cls.CURATED_NAME_OVERRIDES.get(mfr_norm)
            if override:
                hit = override.get(cls._light_normalize_name(colour_name))
                if hit:
                    row = cls.lookup(manufacturer, hit)
                    if row is not None:
                        return row.code, row.hex, row.name

            name_norm = cls.normalize_name(colour_name)
            if not name_norm:
                return None, None, None

            # Match the name in Python rather than via a JSONField `contains`
            # lookup: `contains` isn't supported on SQLite at all and its
            # semantics vary by backend, which would make this silently return
            # None. Filtering by make is indexed and cheap (a make has at most a
            # couple thousand rows), so scanning those in Python is fast and
            # behaves identically on every database.
            #
            # Fetched as plain tuples and wrapped in _MatchRow (paint17) rather
            # than as model instances — see _MATCH_FIELDS above for the
            # measurements and the equivalence check. The name test runs on the
            # raw tuple first, so a namedtuple is only built for rows that
            # actually match (nine out of 4,081 for a Ford query).
            #
            # Deliberately NOT .iterator(): on PostgreSQL that uses a
            # server-side cursor, which breaks behind a transaction-pooling
            # connection pooler — exactly what Neon's -pooler endpoint is. It
            # would buy ~13% and 1.5MB in exchange for a failure mode that
            # depends on which Neon endpoint DATABASE_URL happens to point at.
            rows = [
                cls._MatchRow(*t)
                for t in cls.objects.filter(manufacturer=mfr_norm).values_list(
                    *cls._MATCH_FIELDS
                )
                if name_norm in (t[cls._MATCH_NAMES_IDX] or [])
            ]
            if not rows:
                return None, None, None

            # Try to collapse the full candidate set to a single code.
            resolved = cls._collapse_to_single_code(rows, name_norm)
            if resolved is not None:
                return resolved

            # Still ambiguous across the full set. Retry on just the rows whose
            # PRIMARY name matches the query — paints actually named this, not
            # ones that only alias it. (normalize_name on the stored primary name
            # so the comparison matches the same way the query was normalised.)
            primary_rows = [
                r for r in rows
                if cls.normalize_name(r.name or '') == name_norm
            ]
            if primary_rows and len(primary_rows) < len(rows):
                resolved = cls._collapse_to_single_code(primary_rows, name_norm)
                if resolved is not None:
                    return resolved

            # Still ambiguous. If we know the vehicle's MODEL, prefer the code(s)
            # whose models_list actually includes it. This resolves names that map
            # to different codes on different models (Ford 'Ruby Red' is DSTEWTA on
            # a Mondeo but 5R on a Figo; 'Midnight' is 9AZCWWA on a Ka). It runs
            # only when the name is otherwise ambiguous and only ever NARROWS, so
            # it can turn a declined name into a resolved one, never the reverse.
            if model:
                # TWO PASSES (paint45). Strict first: a tag must PREFIX the trim
                # string. Only if that narrows nothing do we allow a tag to
                # appear anywhere in it.
                #
                # The order is what makes it safe. 'transit' is a substring of
                # 'etransit', so a single lenient pass made Ford E-Transit
                # ambiguous between BRQAWWA (tag 'transit') and BRQAWHA (tag
                # 'etransit') — a previously correct answer turned into a
                # decline. Strict-first keeps E-Transit exact while still
                # rescuing 'Range Rover Evoque', 'Grand C-Max' and 'A6 E-Tron',
                # where NO tag prefixes the trim string at all.
                model_rows = [
                    r for r in rows
                    if cls._model_matches(model, r.models_list, anywhere=False)
                ]
                if not model_rows:
                    model_rows = [
                        r for r in rows
                        if cls._model_matches(model, r.models_list, anywhere=True)
                    ]
                if model_rows and len(model_rows) < len(rows):
                    resolved = cls._collapse_to_single_code(model_rows, name_norm)
                    if resolved is not None:
                        return resolved
                    # Model-matched set still ambiguous — narrow again to the rows
                    # PRIMARY-named this colour and retry.
                    primary_model_rows = [
                        r for r in model_rows
                        if cls.normalize_name(r.name or '') == name_norm
                    ]
                    if primary_model_rows:
                        resolved = cls._collapse_to_single_code(primary_model_rows, name_norm)
                        if resolved is not None:
                            return resolved

            # Genuinely ambiguous — decline
            return None, None, None
        except Exception:
            return None, None, None

    @staticmethod
    def _display_name(row, name_norm):
        """The name to SHOW for a row matched by name (paint80).

        A row's primary `name` is whichever spelling won a frequency vote when
        the table was built, and on a code-reuse row that can be a completely
        different colour. Peugeot EEQ is named 'Brun Epicee' — spiced brown —
        while also carrying 'Jaune Agueda' and 'Agueda Yellow' for the yellow
        that a 2024 208 actually is. Showing the primary there gives the
        customer a brown name against a #A69A37 yellow swatch: right code,
        wrong label, and a label wrong enough to undermine the code beside it.

        So when the match was made BY NAME, prefer the stored spelling that
        matched. Falls back to the primary whenever nothing matches, which keeps
        every code-resolved lookup exactly as it was — this can only affect the
        name->code direction.
        """
        if not name_norm:
            return row.name
        for candidate in (getattr(row, 'all_names', None) or []):
            if PaintLookup.normalize_name(candidate) == name_norm:
                return candidate
        return row.name

    @staticmethod
    def _collapse_to_single_code(rows, name_norm=None):
        """Try to reduce a set of matching rows to one code via (in order):
        exact-single, dash-suffix collapse, then prefix collapse. Returns
        (code, hex, name) if the rows resolve to a single paint, else None.

        `name_norm` is the normalised query, used only to choose WHICH stored
        spelling to display — see _display_name. Omitted, behaviour is unchanged.

        Pure/stateless helper for code_from_name; never raises on normal input.
        """
        codes = {r.code for r in rows}

        # 1. Exactly one code.
        if len(codes) == 1:
            r = rows[0]
            return r.code, (r.hex or None), PaintLookup._display_name(r, name_norm)

        # 2. Dash-suffix variants of a single base ('B554P-L'/'B554P-S').
        bases = {c.split('-')[0] for c in codes}
        if len(bases) == 1:
            base = next(iter(bases))
            exact = next((r for r in rows if r.code == base), rows[0])
            return base, (exact.hex or None), PaintLookup._display_name(exact, name_norm)

        # 3. Prefix variants: the shortest code is a strict prefix of all others
        #    (Ford 'FLVA' prefixes 'FLVAWWA'). Same paint, two code conventions —
        #    return the shortest (canonical bare code). Genuinely different codes
        #    have no prefix relationship, so this can't merge unrelated paints.
        ordered = sorted(codes, key=len)
        shortest = ordered[0]
        if all(c.startswith(shortest) for c in ordered):
            # Prefer a row that has the bare code (it may carry hex/canonical
            # name); fall back to the row with the most complete data.
            exact = next((r for r in rows if r.code == shortest), None)
            if exact is None:
                # No row for the bare code itself — use a suffixed row's hex/name
                # but still return the canonical short code.
                exact = next((r for r in rows if r.hex), rows[0])
            return shortest, (exact.hex or None), PaintLookup._display_name(exact, name_norm)

        return None

    #: A combination row's name is two OTHER codes joined, e.g. Suzuki C06 is
    #: "Y33 + Z0N" and BMW 813 is "314 + 303". They are two-tone pairings, not
    #: colours, and the operands are the real paint codes. 6,703 rows carry this
    #: shape across nearly every marque.
    #:
    #: Showing one to a customer prints "Y33 + Z0N" where a colour name belongs —
    #: worse than showing nothing, because it looks authoritative. It reaches the
    #: page whenever a provider returns a combination code, which happened twice
    #: on Suzuki (C01, C05) when partslink24's "Exterior color" field turned out
    #: to be TRIM COLOR in the dealer system.
    #:
    #: Guarded HERE rather than at the display site so every path is covered —
    #: results page, email and API each resolve names independently.
    _COMBINATION_NAME_RE = re.compile(r'^\s*\S+\s+\+\s+\S+\s*$')

    @classmethod
    def is_combination_name(cls, name):
        """True when a name is two codes joined rather than a colour.

        Deliberately anchored and whitespace-strict: it must be the WHOLE name,
        so a genuine colour containing a plus ("Black + Silver Trim", were one to
        exist) is left alone. 71 of the 6,703 carry a hex, which means the merge
        resolved a blended colour for them — the hex is still usable for a swatch
        even though the NAME is not usable as text.
        """
        return bool(name) and bool(cls._COMBINATION_NAME_RE.match(str(name)))

    @classmethod
    def expand_combination(cls, manufacturer, name, vdg_colour=None):
        """Turn "Z11 + Qab" into the two real colours it names (paint47).

        A combination row IS a real vehicle — a two-tone. Nissan XDF is a Pearl
        White Qashqai with a Black Metallic roof, and both halves exist as proper
        rows with names and hexes. Showing the raw "Z11 + Qab" tells a customer
        nothing; showing the expansion gives them two codes they can actually buy
        paint by.

        Returns a list of dicts [{code, name, hex, is_body}] ordered BODY FIRST,
        or None when either half cannot be resolved — in which case the caller
        keeps paint46's suppression, because half an answer is worse than none.

        Body is identified by matching the DVLA/VDG colour rather than by
        position. "Z11 + Qab" lists black first, but DVLA reports that car as
        White and QAB is the Pearl White — so position is not reliable, and we
        have the colour on every lookup anyway. With no colour to match on, the
        order is left as the data gives it and nothing is labelled.
        """
        if not cls.is_combination_name(name):
            return None
        parts = [p.strip().upper() for p in str(name).split('+')]
        if len(parts) != 2:
            return None
        out = []
        for code in parts:
            row = cls.lookup(manufacturer, code)
            if not row or not getattr(row, 'name', None):
                return None                      # half an answer is worse than none
            # paint48: lookup() falls back to a near match — asking for '398D'
            # returns row '398'. Harmless when they are the same paint, but the
            # expansion would then print a code the combination never named, and
            # nothing guarantees the next such pair agrees. A slash variant is
            # accepted ('398/D' answering for '398D') because that IS the same
            # code differently punctuated; anything else is refused.
            _got = (row.code or '').upper().replace('/', '').replace('-', '')
            if _got != code.replace('/', '').replace('-', ''):
                logger.warning(
                    'Combination operand mismatch: %s asked %r got %r — refusing',
                    manufacturer, code, row.code,
                )
                return None
            out.append({
                'code': row.code,
                'name': row.name,
                'hex': row.hex or None,
                'is_body': False,
                # Carried from the row we already hold. The body-colour match
                # below used to re-query for exactly this field, one SELECT per
                # operand, for rows that were in hand a few lines earlier. Safe
                # to reuse because (manufacturer, code) is UNIQUE
                # (lookup_paintlookup_uniq), so the re-query could only ever
                # return this same row — it was a duplicate read, not a
                # different lookup. Two queries saved on every two-tone result.
                '_group': (row.color_group or '').lower(),
            })
        want = (vdg_colour or '').strip().lower()
        if want:
            for item in out:
                grp = item['_group']
                if grp and grp == want:
                    item['is_body'] = True
            if any(i['is_body'] for i in out):
                out.sort(key=lambda i: not i['is_body'])
        for item in out:
            item.pop('_group', None)     # internal, never reaches the template
        return out

    @classmethod
    def two_tone_parts(cls, manufacturer, paint_code, vdg_colour=None):
        """The two halves of a two-tone code, for the template (paint50).

        A SEPARATE entry point rather than a fourth return value from
        lookup_with_canonical: that method's 3-tuple contract is consumed at six
        call sites (results page, email, API, admin), and widening it to serve
        one presentational feature would touch all of them for no benefit.

        Returns the same [{code, name, hex, is_body}] as expand_combination,
        body first when the DVLA colour identified one, or None.
        """
        try:
            row = cls.lookup(manufacturer, paint_code)
            if not row or not cls.is_combination_name(getattr(row, 'name', None)):
                return None
            return cls.expand_combination(manufacturer, row.name, vdg_colour=vdg_colour)
        except Exception:  # noqa: BLE001 — presentation must never break a lookup
            return None

    @staticmethod
    def _model_matches(vehicle_model, models_list, anywhere=True):
        """True if the looked-up vehicle's model corresponds to one of the model
        tags stored on a PaintLookup row.

        models_list holds short normalised tags ('ka', 'mondeo', 'fiestast'); the
        vehicle model is the full trim string ('Mondeo ST-Line X TDCi').

        PREFIX FIRST, then anywhere (paint45). Prefix alone assumed the model
        name always LEADS the trim string, which is true for Ford and most
        marques but not universally:

            'Range Rover Evoque Autobiography'  tag 'evoque'  -> prefix misses
            'Grand C-Max Titanium TDCi'         tag 'cmax'    -> prefix misses
            'A6 E-Tron S Line'                  tag 'etron'   -> prefix misses

        In each case the right code was one filter away and the name was declined
        instead. Falling back to a substring test rescues them. Measured across
        796 distinct (make, name, model) triples from real traffic: **3 newly
        resolved, 0 changed** — and each was verified to match the correct tag
        rather than coincidentally.

        Prefix is still tried FIRST so the cheaper, stricter test wins where it
        can. This remains only an ambiguity tiebreaker, so a near-miss leaves the
        name unresolved (declined), never mis-resolved.
        """
        if not vehicle_model or not models_list:
            return False
        vm = re.sub(r'[^a-z0-9]', '', str(vehicle_model).lower())
        if not vm:
            return False
        tags = [re.sub(r'[^a-z0-9]', '', str(t).lower()) for t in models_list]
        tags = [t for t in tags if t]
        if any(vm.startswith(t) for t in tags):
            return True
        if not anywhere:
            return False
        # Short tags are the false-positive risk when matching anywhere in the
        # string ('ka' would hit 'kodiaq'), so require a little length first.
        return any(len(t) >= 4 and t in vm for t in tags)

# =============================================================================
# SiteConfig — a single-row table holding site-wide runtime toggles that need to
# be flippable WITHOUT a redeploy (e.g. the maintenance / lookups-paused switch).
# Always accessed via SiteConfig.get() which returns (and lazily creates) the one
# row. Edited from the admin-stats dashboard.
# =============================================================================


class VrmCache(models.Model):
    """Cached successful lookup result, keyed by registration (A).

    Serves a repeat lookup of the same reg from storage instead of calling VDG,
    so repeated lookups — the exact abuse pattern observed — cost £0. Only
    SUCCESSFUL results (a paint code was delivered) are cached; a miss is never
    stored, so a previously-failed reg still gets a fresh live attempt.

    Stores the full results payload as JSON because several display fields
    (fuel_type, transmission, engine_description, all_paint_codes) live only in
    the session, not on the Search row — caching the whole payload means a cache
    hit reconstructs a pixel-identical results page, not a degraded one.

    A registration's factory paint code is effectively immutable, so serving a
    stored result is safe; freshness is bounded by VRM_CACHE_TTL_DAYS purely to
    cover the rare cherished-plate transfer (a reg moved to a different vehicle).
    Freshness is enforced at read time by comparing `updated_at`, so an expired
    entry is IGNORED rather than deleted — the next lookup goes live and
    update_or_create recycles the same row, so stale entries never accumulate as
    duplicates. prune_old_data deletes rows for registrations that are never
    looked up again (which also stops a VIN outliving the Search row it came
    from, per the retention policy).
    """

    registration = models.CharField(max_length=10, unique=True, db_index=True)
    # Full session 'vehicle_data' payload minus request-specific keys
    # (search_id / paint_pending are recomputed per request).
    payload = models.JSONField()
    hit_count = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True, db_index=True)

    class Meta:
        verbose_name = 'VRM cache entry'
        verbose_name_plural = 'VRM cache entries'

    def __str__(self):
        return f"VrmCache({self.registration}, hits={self.hit_count})"


class SiteConfig(models.Model):
    """Singleton holding runtime site toggles.

    Read on effectively every request (the homepage checks maintenance_mode), so
    get() is cached in the per-process 'local' cache to keep the DB asleep — see
    get() below. Any code path that MUTATES the row must call save(), which
    refreshes that cache so the change is visible immediately on the saving
    worker and within the cache TTL (60s) on the others.
    """

    # Pinned primary key for the singleton row.
    SINGLETON_PK = 1
    # Cache key + TTL for get(). 60s means a maintenance flip (or a blocklist /
    # budget edit) takes at most a minute to reach every worker — fine at the
    # frequency these are changed, and the saving worker sees it at once because
    # save() rewrites the cache.
    _CACHE_KEY = 'siteconfig:singleton'
    _CACHE_TTL = 60

    # When True: the homepage shows the "offline for maintenance" state (locked
    # field + notice) and the backend REFUSES to run any lookup — so no VDG spend
    # can occur even via a direct POST. Flip from /admin-stats/.
    maintenance_mode = models.BooleanField(default=False)

    # ORIGIN GATE (F2). Whether CF-Connecting-IP is trusted unconditionally, or
    # only when the Cloudflare Transform Rule's secret header proves the request
    # came through Cloudflare. Lives here rather than in an environment variable
    # so it can be flipped from the dashboard without a redeploy: enforcing has a
    # bad failure mode (if the Transform Rule stops firing, every visitor keys to
    # Railway's proxy address and shares ONE rate-limit bucket), and the whole
    # point of a switch is that reversing it is fast. A Railway variable change
    # restarts both workers; this takes effect within _CACHE_TTL.
    ORIGIN_GATE_OBSERVE = 'observe'
    ORIGIN_GATE_ENFORCE = 'enforce'
    #: Refuse anything that did not come through Cloudflare. Everything the
    #: enforce mode does, PLUS restoring Cloudflare's WAF and bot protection —
    #: neither of which can act on a request that goes round it. Enabled after
    #: the dashboard showed a clean bypass panel over several days: with no
    #: Cloudflare traffic bypassing and no Stripe webhook configured, there was
    #: nothing left for it to break.
    ORIGIN_GATE_BLOCK = 'block'
    ORIGIN_GATE_CHOICES = [
        (ORIGIN_GATE_OBSERVE, 'Observe — log only, trust headers as before'),
        (ORIGIN_GATE_ENFORCE, 'Enforce — only trust headers from Cloudflare'),
        (ORIGIN_GATE_BLOCK, 'Block — refuse anything not from Cloudflare'),
    ]
    origin_gate_mode = models.CharField(
        max_length=10, choices=ORIGIN_GATE_CHOICES, default=ORIGIN_GATE_OBSERVE,
    )
    # Set when the breaker drops enforcement automatically (see the middleware).
    # Durable and shown on the dashboard: an automatic revert that left no trace
    # would be worse than no revert at all, because the gate would silently be
    # off and the reason gone.
    origin_gate_auto_reverted_at = models.DateTimeField(null=True, blank=True)

    # --- Daily spend breaker (A) -------------------------------------------
    # Hard ceiling on VDG spend per calendar day (London time). When the sum of
    # today's real (refund-net) VDG cost reaches this, the backend refuses new
    # lookups for the rest of the day — a bounded worst case even if an abuser
    # gets past every other layer, or a bug loops. 0 disables the breaker.
    # Editable amount box in /admin-stats/.
    daily_budget_gbp = models.DecimalField(
        max_digits=8, decimal_places=2, default=Decimal('50.00'),
        help_text='Max VDG spend per day (London time). 0 = no limit.'
    )
    # Set True by the breaker when today's spend crossed the budget, so the
    # admin alert email is sent only ONCE per trip rather than every blocked
    # request. Reset automatically when a new day rolls over.
    budget_tripped = models.BooleanField(default=False)
    budget_tripped_date = models.DateField(null=True, blank=True)

    # --- Blocklists (A) -----------------------------------------------------
    # Newline- or comma-separated lists, edited in /admin-stats/. Checked at the
    # very top of the lookup POST, before any spend. Both are best-effort:
    # a determined abuser rotates IPs and UAs (we have direct evidence of both),
    # so the reg list is the strong one (abusers reuse regs); IP is a scalpel
    # for the lazy case. Matching is exact for both.
    blocked_regs = models.TextField(
        blank=True, default='',
        help_text='Registrations to refuse (one per line or comma-separated).'
    )
    blocked_ips = models.TextField(
        blank=True, default='',
        help_text='IP addresses to refuse (one per line or comma-separated).'
    )
    access_keys = models.TextField(
        blank=True, default='',
        help_text=(
            'Trade / unlimited access. One per line as key:label, e.g. '
            '"a7f3k9:Daves Bodyshop". Share coloureg.com/?key=a7f3k9 — clicking '
            'it once exempts that browser from the hourly search limit. Delete a '
            'line to revoke just that person. The label is recorded on each '
            'lookup so you can see who used it.'
        )
    )

    unsupported_makes = models.TextField(
        blank=True, default='',
        help_text=(
            'Makes we cannot resolve a paint code for — one per line or '
            'comma-separated. A lookup for these is refused BEFORE any paid '
            'VDG call, and the visitor is told immediately instead of waiting '
            'for a lookup that cannot succeed. Matching ignores case, spaces '
            'and hyphens, so "MERCEDES-BENZ", "Mercedes Benz" and '
            '"mercedesbenz" are the same entry. Leave empty to disable.'
        )
    )

    # NOTE: user-agent blocking was deliberately removed. It was the weakest of
    # the three (an attacker rewrites a UA string in one line — we watched that
    # happen twice) AND the most dangerous, because an over-broad fragment like
    # "Chrome" would silently block most real browsers with no validation and no
    # obvious symptom. Where it genuinely earned its keep was at Cloudflare, at
    # the edge, before traffic reaches Django at all — which is the right layer
    # for it. Duplicating it here added risk without adding capability.

    # --- Payments (F) -------------------------------------------------------
    # Master switch for the paid-lookup flow. Defaults False so the payment code
    # can ship (fully built + tested against Stripe test mode) WITHOUT going live
    # or changing the current free behaviour. Flip to True only once Stripe is
    # activated and the public address/email are set. While False, the site
    # behaves exactly as it does today (free lookups, 3/h limit enforced).
    payments_enabled = models.BooleanField(default=False)

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Site configuration'
        verbose_name_plural = 'Site configuration'

    def __str__(self):
        return f"SiteConfig(maintenance_mode={self.maintenance_mode})"

    # ---- helpers for the blocklists ---------------------------------------
    @staticmethod
    def _parse_list(raw):
        """Split a textarea value into a clean list of tokens (by newline or
        comma), stripped and empties removed. Used for all three blocklists."""
        if not raw:
            return []
        parts = re.split(r'[\n,]+', raw)
        return [p.strip() for p in parts if p.strip()]

    def blocked_reg_set(self):
        """Uppercased, whitespace-stripped set of blocked registrations."""
        return {r.upper().replace(' ', '') for r in self._parse_list(self.blocked_regs)}

    def blocked_ip_set(self):
        return set(self._parse_list(self.blocked_ips))

    @staticmethod
    def _norm_make(value):
        """Fold a make to a comparison key.

        DVLA returns MITSUBISHI, VDG returns Mitsubishi, and a human typing the
        admin box might write "Mercedes-Benz" or "mercedes benz". All three
        sources feed this check, so all three normalise the same way — a
        mismatch here fails SILENTLY, leaving you convinced a make is blocked
        while still paying for every lookup of it.
        """
        # DIACRITICS FOLD TOO. Uppercasing alone leaves ŠKODA != SKODA and
        # CITROËN != CITROEN, so an admin who types "citroen" — or copies
        # "Citroën" off the Help page while DVLA sends "CITROEN" — gets a list
        # that silently matches nothing and keeps paying for every lookup.
        # Found 20 Aug 2026 when "skoda" in the admin box left Škoda sitting in
        # the supported column on Help, blocked and unblocked at the same time.
        folded = unicodedata.normalize('NFKD', value or '')
        folded = ''.join(ch for ch in folded if not unicodedata.combining(ch))
        return folded.upper().replace(' ', '').replace('-', '').strip()

    #: A section header in the unsupported list marks everything under it as
    #: gated but NOT published on the Help page. Motorcycles are the case this
    #: exists for: the pipeline should skip them exactly as it skips an
    #: unsupported car, but a paint-code site for cars has no reason to
    #: advertise a motorcycle list to the people reading its coverage.
    _HIDDEN_SECTION = re.compile(r'^\s*#.*\b(hidden|unlisted|hide)\b', re.I)

    @classmethod
    def _parse_sections(cls, raw):
        """Split the list into (make, published) pairs.

        Lines beginning with # are section headers, never makes. Without this a
        header typed as "#CARS" would itself be blocked as a make called
        "#CARS" — silently, since nothing would ever match it.

        A header containing "hidden", "unlisted" or "hide" turns publishing off
        for the entries beneath it, until the next header. Everything is gated
        either way; only visibility changes.
        """
        out, published = [], True
        for line in (raw or '').splitlines():
            if line.strip().startswith('#'):
                published = not cls._HIDDEN_SECTION.match(line)
                continue
            for token in cls._parse_list(line):
                if token.strip():
                    out.append((token.strip(), published))
        return out

    def unsupported_make_set(self):
        """Every gated make, published or not. The gate ignores sections."""
        return {self._norm_make(m) for m, _pub in
                self._parse_sections(self.unsupported_makes)
                if self._norm_make(m)}

    def published_unsupported_makes(self):
        """Only the gated makes the Help page should list."""
        seen, out = set(), []
        for m, pub in self._parse_sections(self.unsupported_makes):
            key = self._norm_make(m)
            if pub and key and key not in seen:
                seen.add(key)
                out.append(m)
        return out

    def is_make_unsupported(self, make):
        if not make:
            return False
        return self._norm_make(make) in self.unsupported_make_set()

    def access_key_map(self):
        """{key: label} from the admin textarea. Malformed lines are ignored."""
        out = {}
        for line in self._parse_list(self.access_keys):
            if ':' not in line:
                continue
            key, _, label = line.partition(':')
            key = key.strip()
            if key:
                out[key] = label.strip() or key
        return out

    #: Must match Search.access_label's max_length. Labels come from a free-text
    #: admin box, so an over-long one would raise DataError on Postgres when the
    #: Search row saves — AFTER VDG has already been billed. SQLite truncates
    #: silently, so the battery would never see it. Same class of bug as the
    #: unvalidated CF-Connecting-IP in paint17.
    ACCESS_LABEL_MAX = 60

    def access_label_for(self, key):
        """Label for a key, or '' if it is not a live key.

        Truncated HERE rather than at the call site: this is the only way a
        label reaches a Search row, so capping it at the source means no future
        caller can get it wrong.
        """
        if not key:
            return ''
        return self.access_key_map().get(key.strip(), '')[:self.ACCESS_LABEL_MAX]

    def is_reg_blocked(self, registration):
        if not registration:
            return False
        return registration.upper().replace(' ', '') in self.blocked_reg_set()

    def is_ip_blocked(self, ip):
        if not ip:
            return False
        return ip in self.blocked_ip_set()

    def save(self, *args, **kwargs):
        """Persist, then refresh the get() cache so mutations are visible
        immediately on this worker (and within _CACHE_TTL elsewhere)."""
        super().save(*args, **kwargs)
        try:
            caches['local'].set(self._CACHE_KEY, self, self._CACHE_TTL)
        except Exception:
            # Cache write must never break a save; a stale read self-heals in
            # at most _CACHE_TTL seconds anyway.
            pass

    @classmethod
    def get(cls):
        """Return the singleton config row, cached in the per-process 'local'
        cache for _CACHE_TTL seconds.

        The point is zero DB queries on the hot path: the homepage GET calls this
        on every request, and without the cache each call is a Postgres round-trip
        that resets Neon's idle timer and keeps the compute awake 24/7 (this was
        the compute-exhaustion root cause). With the cache, a warm worker answers
        from memory and never touches the DB, so Neon can suspend between real
        lookups.

        Falls back to a direct fetch (and populates the cache) on a miss. Never
        raises for a missing row — pinned at pk=SINGLETON_PK, created on first use.
        """
        try:
            cached = caches['local'].get(cls._CACHE_KEY)
            if cached is not None:
                return cached
        except Exception:
            pass  # cache backend issue -> just hit the DB

        obj, _ = cls.objects.get_or_create(pk=cls.SINGLETON_PK)
        try:
            caches['local'].set(cls._CACHE_KEY, obj, cls._CACHE_TTL)
        except Exception:
            pass
        return obj