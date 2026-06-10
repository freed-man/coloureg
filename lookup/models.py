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
# PaintSwatch — runtime lookup table for colour swatches on the results page.
#
# Populated by `python manage.py load_paint_swatches` from a paint_swatches.json
# committed to the repo (the canonical source). One row per unique (manufacturer,
# code, hex) combination; multiple rows can exist for the same (manufacturer, code)
# when different models or eras use the same paint code for different colours.
# =============================================================================


class PaintSwatch(models.Model):
    """A single paint colour swatch keyed by manufacturer + code + hex."""

    # Lookup keys (normalised, lowercase mfr / uppercase code)
    manufacturer = models.CharField(max_length=64, db_index=True)
    code = models.CharField(max_length=32, db_index=True)
    hex = models.CharField(max_length=7)
    name = models.CharField(max_length=200, blank=True, default='')

    # Disambiguators (used when multiple rows exist for the same mfr+code)
    applicable_models = models.JSONField(default=list)   # ["roomster", "fabia"]
    model_families = models.JSONField(default=list)      # first-word fragments (computed at load time)
    color_group = models.CharField(max_length=20, blank=True, default='', db_index=True)
    year_min = models.IntegerField(null=True, blank=True)
    year_max = models.IntegerField(null=True, blank=True)

    # Provenance
    sources_count = models.IntegerField(default=1)
    sources = models.JSONField(default=list)           # ["atu", "chipex"]

    # Bookkeeping
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['manufacturer', 'code', 'hex'],
                name='lookup_paintswatch_uniq',
            ),
        ]
        indexes = [
            models.Index(fields=['manufacturer', 'code']),
            models.Index(fields=['manufacturer', 'code', 'color_group']),
        ]
        verbose_name = 'Paint swatch'
        verbose_name_plural = 'Paint swatches'

    def __str__(self):
        return f"{self.manufacturer} {self.code} {self.hex} ({self.name})"

    # ------------------------------------------------------------------
    # Lookup logic
    # ------------------------------------------------------------------

    # Manufacturer aliases — handle cases where VDG/DVLA returns a brand name
    # that differs from how it's stored in our scraped data. Maps the normalised
    # input to the normalised DB key. Only added when a real mismatch is known.
    MANUFACTURER_ALIASES = {
        'mercedesbenz': 'mercedes',  # VDG: 'Mercedes-Benz' → DB stores as 'mercedes'
    }

    @staticmethod
    def normalize_manufacturer(text):
        """Match the normalisation used at prep time, with aliasing for known
        VDG/DB mismatches (e.g. 'Mercedes-Benz' → 'mercedes')."""
        if not text:
            return ''
        norm = text.strip().lower().replace('-', '').replace(' ', '').replace('.', '')
        return PaintSwatch.MANUFACTURER_ALIASES.get(norm, norm)

    @staticmethod
    def normalize_model(text):
        """Match the normalisation used at prep time."""
        if not text:
            return ''
        return text.strip().lower().replace('.', '').replace('-', '').replace(' ', '')

    @staticmethod
    def normalize_code_variants(paint_code):
        """Generate all reasonable variants of a paint code to try.

        VDG often returns codes like '8E8E/A7W' which are TWO codes joined
        by a slash, but slashes also appear inside legitimate single codes
        like Fiat '102/F'. So we try the full string first, then fall back
        to splitting.

        Also yields suffix-stripped fallbacks (e.g. '197U' → '197') as a
        last resort so codes with manufacturer-specific variant suffixes
        (Mercedes 'U' for uni, Audi 'PA'/'SF' for process variants, etc.)
        can match the base code when no exact match exists.
        """
        if not paint_code:
            return []

        variants = []
        seen = set()

        full = paint_code.strip().upper()
        if full and full not in seen:
            variants.append(full)
            seen.add(full)

        # Split on / and , and try each fragment
        for part in re.split(r'[/,]', full):
            part = part.strip()
            if part and part not in seen:
                variants.append(part)
                seen.add(part)

        # Last-resort: strip common 1-2 char suffixes from numeric codes.
        # Only applies when the base would still be a meaningful code
        # (≥3 chars and starts with a digit, e.g. '197U' → '197').
        # We append these LAST so they only match if exact lookups fail.
        suffix_candidates = []
        for v in list(variants):
            if len(v) >= 4 and v[0].isdigit():
                # Try stripping 1-char alphabetic suffix
                if v[-1].isalpha():
                    base = v[:-1]
                    if base not in seen:
                        suffix_candidates.append(base)
                        seen.add(base)
                # Try stripping 2-char alphabetic suffix
                if len(v) >= 5 and v[-1].isalpha() and v[-2].isalpha():
                    base = v[:-2]
                    if base not in seen:
                        suffix_candidates.append(base)
                        seen.add(base)
        variants.extend(suffix_candidates)

        return variants

    # Map VDG colour names to our color_group taxonomy
    VDG_COLOUR_TO_GROUP = {
        'silver': 'grey',
        'grey': 'grey',
        'gray': 'grey',
        'black': 'black',
        'white': 'white',
        'blue': 'blue',
        'red': 'red',
        'green': 'green',
        'yellow': 'yellow',
        'orange': 'orange',
        'brown': 'brown',
        'gold': 'gold',
        'beige': 'beige',
        'cream': 'beige',
        'ivory': 'beige',
        'purple': 'purple',
        'maroon': 'red',
        'pink': 'red',
        'turquoise': 'blue',
    }

    @classmethod
    def lookup(cls, manufacturer, paint_code, model=None, year=None, vdg_colour=None):
        """Find the best swatch for a given vehicle.

        Returns a PaintSwatch instance or None.

        Decision tree (each tier returns immediately on a unique match):

            Tier 1: (mfr, code) → if 1 candidate, return it
            Tier 2: filter by exact normalised model
            Tier 3: narrow Tier 2 by year (within ±2 years)
            Tier 4: fuzzy model match (model_family startswith first word)
            Tier 5: VDG colour matches color_group
            Tier 6: pick swatch with highest sources_count (deterministic
                    tiebreak: alphabetical hex)
        """
        if not manufacturer or not paint_code:
            return None

        mfr_norm = cls.normalize_manufacturer(manufacturer)

        for code in cls.normalize_code_variants(paint_code):
            candidates = list(
                cls.objects.filter(manufacturer=mfr_norm, code=code)
            )

            if not candidates:
                continue

            # Tier 1: only one candidate
            if len(candidates) == 1:
                return candidates[0]

            # Tier 2: model prefix match
            # The user's model string is typically more detailed than the DB
            # entry — e.g. 'a3sportbacktdi138slinespecialedition' vs DB 'a3sportback'.
            # Prefix-match so longer user input still matches shorter DB entries,
            # picking the most specific (longest) DB match.
            #
            # If Tier 2 narrows to nothing (no applicable_model prefix matched),
            # we deliberately keep the wider `candidates` set and let Tiers 4-6
            # try to disambiguate. This is intentional: Tier 4's fuzzy first-
            # token match is more forgiving than Tier 2's full-prefix and often
            # rescues these cases.
            if model:
                model_norm = cls.normalize_model(model)
                tier2 = [
                    c for c in candidates
                    if any(model_norm.startswith(m) for m in c.applicable_models if m)
                ]
                if len(tier2) == 1:
                    return tier2[0]

                if tier2 and year:
                    # Tier 3: year filter on top of model (±2 years tolerance)
                    tier3 = [
                        c for c in tier2
                        if c.year_min and c.year_max
                        and c.year_min - 2 <= year <= c.year_max + 2
                    ]
                    if len(tier3) == 1:
                        return tier3[0]
                    # Tier 3 narrows tier2 if it found anything, else keep tier2
                    candidates = tier3 if tier3 else tier2
                elif tier2:
                    # No year — just use tier2's model-narrowed set
                    candidates = tier2
                # else: tier2 is empty — keep the wider candidates set unchanged
                # (deliberate fallthrough to Tiers 4-6, see comment above)

            # Tier 4: fuzzy model (first-token startswith)
            # The first token combines leading letters with any directly
            # following digits, so 'a3sportbacktdi...' yields 'a3' (not 'a'),
            # giving meaningful disambiguation for alphanumeric model series.
            if len(candidates) > 1 and model:
                first = re.match(r'^[a-z]+\d*', cls.normalize_model(model))
                if first:
                    first_word = first.group(0)
                    tier4 = [
                        c for c in candidates
                        if any(fam.startswith(first_word) for fam in c.model_families)
                    ]
                    if len(tier4) == 1:
                        return tier4[0]
                    if tier4:
                        candidates = tier4

            # Tier 5: VDG colour disambiguation
            if len(candidates) > 1 and vdg_colour:
                target_group = cls.VDG_COLOUR_TO_GROUP.get(vdg_colour.lower().strip())
                if target_group:
                    tier5 = [c for c in candidates if c.color_group == target_group]
                    if len(tier5) == 1:
                        return tier5[0]
                    if tier5:
                        candidates = tier5

            # Tier 6: most-sourced wins, alphabetical hex tiebreak
            best = max(candidates, key=lambda c: (c.sources_count, c.hex))
            return best

        return None

    @classmethod
    def find_canonical_code(cls, manufacturer, paint_code, swatch=None):
        """If paint_code is a short/abbreviated form, find the canonical long code.

        Many paint code databases (including some VDG responses) return abbreviated
        codes like 'L8' when the actual factory code on the car is 'LZ9Y'. This
        method searches the database for the most-cited longer code that points
        to the same hex, allowing the UI to display a more authoritative code.

        Returns a string (the canonical code) or None if no meaningful expansion
        exists. Never expands a code that's already long (>3 chars) — those are
        already canonical.

        Conservative criteria:
          - Input code must be ≤3 chars (short codes only)
          - Candidate must be strictly longer than input
          - Candidate must NOT be the input plus a 1-2 char suffix
            (those are process variants like L8PA, L8SF — not the canonical form)
          - Candidate must NOT be the input doubled (e.g. A2 → A2A2)
          - Candidate must have ≥50 source records (filters out one-off noise)
        """
        if not manufacturer or not paint_code:
            return None

        original = paint_code.strip().upper()
        if len(original) > 3:
            return None  # Already long enough — don't try to "expand"

        mfr_norm = cls.normalize_manufacturer(manufacturer)

        # Find the swatch for the input (or use the one already looked up)
        if swatch is None:
            swatch = cls.lookup(manufacturer, paint_code)
        if swatch is None:
            return None

        target_hex = swatch.hex

        # Search for siblings — same (mfr, hex), different code, longer, well-supported
        siblings = cls.objects.filter(
            manufacturer=mfr_norm, hex=target_hex
        ).exclude(code=original)

        candidates = []
        for s in siblings:
            c = s.code
            if len(c) <= len(original):
                continue
            if c == original + original:
                continue  # trivial doubling
            if c.startswith(original) and len(c) - len(original) <= 2:
                continue  # process variant (e.g. L8PA, L8SF)
            if s.sources_count < 50:
                continue
            candidates.append(s)

        if not candidates:
            return None

        # Pick the most-cited candidate
        best = max(candidates, key=lambda s: s.sources_count)
        return best.code

    @classmethod
    def lookup_with_canonical(cls, manufacturer, paint_code, model=None, year=None, vdg_colour=None):
        """Convenience: do a swatch lookup AND find any canonical expansion.

        Returns a tuple (paint_hex, paint_name, canonical_code) — all None on
        miss or exception. Used by the results page, manual-lookup admin
        endpoint, and email send paths so the same try/except dance isn't
        repeated four times across views.py.

        Never raises; a swatch failure should never break the calling page.
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
            return swatch.hex, swatch.name, canonical
        except Exception:
            return None, None, None