import re

from django.db import models


class Search(models.Model):
    """Logs every paint code lookup."""

    PROVIDER_VDG = 'vdg'
    PROVIDER_VDG_RETRY = 'vdg_retry'
    PROVIDER_PARTSLINK24 = 'partslink24'
    PROVIDER_MANUAL = 'manual'
    PROVIDER_NONE = 'none'
    PROVIDER_CHOICES = [
        (PROVIDER_VDG, 'VDG'),
        (PROVIDER_VDG_RETRY, 'VDG (retry)'),
        (PROVIDER_PARTSLINK24, 'Partslink24'),
        (PROVIDER_MANUAL, 'Manual'),
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
    # vdg_vehicle_returned  — did Results.VehicleDetails return successfully? (£0.15 charged)
    # vdg_paint_returned    — did Results.PaintCodeDetails return ≥1 paint code? (£0.35 charged
    #                          if False, refunded by VDG; if True, kept)
    vdg_vehicle_returned = models.BooleanField(default=False)
    vdg_paint_returned = models.BooleanField(default=False)
    vdg_balance_after_call = models.DecimalField(
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
    pl24_attempted = models.BooleanField(default=False)
    pl24_returned = models.BooleanField(default=False)
    recovery_name_only = models.BooleanField(default=False)
    recovery_duration_ms = models.IntegerField(null=True, blank=True)

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
    }

    @staticmethod
    def normalize_manufacturer(text):
        """Match the normalisation used at merge time, with aliasing."""
        if not text:
            return ''
        norm = text.strip().lower().replace('-', '').replace(' ', '').replace('.', '')
        return PaintLookup.MANUFACTURER_ALIASES.get(norm, norm)

    @staticmethod
    def normalize_name(text):
        """Normalise a colour NAME for the name->code direction.

        MUST match the rules paintscraper used to build `normalized_names`
        (lowercase, punctuation stripped, finish words removed) — otherwise
        name->code lookups will silently miss. The merge pipeline is the single
        source of truth; this mirrors it. If that pipeline's rules change,
        update this to match.
        """
        if not text:
            return ''
        t = text.strip().lower()
        # strip finish/qualifier words
        for w in ('metallic', 'mica', 'pearl', 'pearlescent', 'matt', 'matte',
                  'gloss', 'solid', 'effect', 'met.', 'met'):
            t = re.sub(r'\b' + re.escape(w) + r'\b', ' ', t)
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
        for code in variants:
            match = cls.objects.filter(manufacturer=mfr_norm, code=code).first()
            if match:
                return match

        # 2) VW/Audi leading-L fallback (only when the plain form is absent).
        if mfr_norm in cls.LEADING_L_MAKES:
            for code in variants:
                # Prepend one 'L'. This is safe even when `code` already starts
                # with 'L' (the body code itself can be e.g. 'L5M', stored at the
                # factory as 'LL5M'): step 1 already confirmed the plain form has
                # no row, and there are no cases where both 'LX' and 'LLX' exist
                # as different colours — so the plain-absent check is the real
                # guard, not a no-double-L rule.
                match = cls.objects.filter(
                    manufacturer=mfr_norm, code='L' + code
                ).first()
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
                return None, None, None
            canonical = cls.find_canonical_code(
                manufacturer=manufacturer,
                paint_code=paint_code,
                swatch=swatch,
            )
            # hex may be '' (name-only rows) — normalise to None for the caller
            return (swatch.hex or None), (swatch.name or None), canonical
        except Exception:
            return None, None, None

    # ------------------------------------------------------------------
    # name -> code  [the conservative direction]
    # ------------------------------------------------------------------

    @classmethod
    def code_from_name(cls, manufacturer, colour_name):
        """Given a colour NAME (and make), return a single paint code — but ONLY
        when it is unambiguous.

        Colour names are 1:many with codes (one make can have many 'grey's), so
        this returns a code ONLY when exactly one code matches the normalised
        name within the make (optionally after collapsing dash-suffix variants
        of a single base code, e.g. 'B554P-L'/'B554P-S' -> 'B554P'). If the name
        maps to several genuinely different codes, returns None — a wrong code
        is worse than none.

        Returns (code, hex, canonical_name) or (None, None, None).
        """
        if not manufacturer or not colour_name:
            return None, None, None
        try:
            mfr_norm = cls.normalize_manufacturer(manufacturer)
            name_norm = cls.normalize_name(colour_name)
            if not name_norm:
                return None, None, None

            # Match the name in Python rather than via a JSONField `contains`
            # lookup: `contains` isn't supported on SQLite at all and its
            # semantics vary by backend, which would make this silently return
            # None. Filtering by make is indexed and cheap (a make has at most a
            # couple thousand rows), so scanning those in Python is fast and
            # behaves identically on every database.
            rows = [
                r for r in cls.objects.filter(manufacturer=mfr_norm)
                if name_norm in (r.normalized_names or [])
            ]
            if not rows:
                return None, None, None

            codes = {r.code for r in rows}

            if len(codes) == 1:
                r = rows[0]
                return r.code, (r.hex or None), r.name

            # Try collapsing dash-suffix variants to a single base code
            bases = {c.split('-')[0] for c in codes}
            if len(bases) == 1:
                base = next(iter(bases))
                # prefer an exact row for the base code if present, else any row
                exact = next((r for r in rows if r.code == base), rows[0])
                return base, (exact.hex or None), exact.name

            # Genuinely ambiguous — decline
            return None, None, None
        except Exception:
            return None, None, None

# =============================================================================
# SiteConfig — a single-row table holding site-wide runtime toggles that need to
# be flippable WITHOUT a redeploy (e.g. the maintenance / lookups-paused switch).
# Always accessed via SiteConfig.get() which returns (and lazily creates) the one
# row. Edited from the admin-stats dashboard.
# =============================================================================


class SiteConfig(models.Model):
    """Singleton holding runtime site toggles."""

    # When True: the homepage shows the "offline for maintenance" state (locked
    # field + notice) and the backend REFUSES to run any lookup — so no VDG spend
    # can occur even via a direct POST. Flip from /admin-stats/.
    maintenance_mode = models.BooleanField(default=False)

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Site configuration'
        verbose_name_plural = 'Site configuration'

    def __str__(self):
        return f"SiteConfig(maintenance_mode={self.maintenance_mode})"

    @classmethod
    def get(cls):
        """Return the single config row, creating it on first use.

        Never raises for a missing row. Cheap (single PK fetch); fine to call on
        every request. We pin pk=1 so there's only ever one row.
        """
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj