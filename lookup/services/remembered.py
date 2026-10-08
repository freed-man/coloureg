"""Remembered answers (paint288).

The 7-day cache forgets a car after a week, and the next lookup of the same
plate then paid for the whole search again. Measured on the lookups from 2 Aug
(when the cache began) to 7 Oct: 54 lookups were of a plate answered 7 to 90
days earlier. They cost 19.04 pounds; 48 got the same answer again, and 3
customers got nothing for a car already answered.

So an answer is remembered for REMEMBER_DAYS, from the lookups table itself
(the cache table is emptied of old entries by prune_old_data). Nothing here
calls a supplier or writes anything: this module only decides.

WHICH EARLIER ANSWER COUNTS (find):
  * Only an answer GIVEN in the window: a provider's, or the operator's. A
    cache copy or a remembered copy is not a new answer, so a car is searched
    afresh at least every REMEMBER_DAYS however often it is looked up.
  * The operator's answer is the one that counts when there is one: it stands
    exactly as typed, and lookups before it are ignored.
  * A provider's answer is read by TODAY'S rules, the ones every new answer
    passes (_enrich_from_lookup). 9 of the 54 held a slash-joined code from
    before paint162 ("2T2T/C9X"); read today it is the C9X a new lookup gives.
  * ONLY WHAT THE PROVIDER SAID IS KEPT (paint293). A lookup's row holds the
    provider's half of the answer and, often, a half our own catalogue supplied
    at the time (`enriched_from`): the name for a provider's code, or the code
    for a provider's name. paint288 read both halves back together, so the
    catalogue's half was frozen as it stood that day: Vauxhall 4CU stayed
    "Power Red" on blue cars after paint239 taught a new lookup to say Ultra
    Blue Pearl, and five black Audis kept "Dark Grey Matt". Now the catalogue's
    half is derived again, and where it was the CODE and today's rules give a
    different one (or none), nothing is remembered.
  * A provider's answer whose name states a colour the car's registered colour
    does not share is not served from memory (paint293): the normal lookup
    runs. Replayed on the lookups of 10 Jul to 7 Oct, that is 29 of 2,359
    plates, and it costs those what every lookup cost before paint288. The
    operator's answer is never second-guessed this way.
  * Every lookup that counts must agree on the code. If two disagree (two
    sources, or a correction made on one lookup and not the other) nothing is
    remembered and the normal lookup runs, exactly as before this existed.
    Measured: 7 of 250 plates with two or more coded lookups.

IS IT STILL THE SAME CAR (same_car): DVLA is asked, free, on every lookup
anyway. Its make, year of manufacture and colour today must all equal the
remembered lookup's. A plate moved to another car fails this; so does DVLA not
answering. Across 225 plates looked up more than once, none ever changed.

WHAT A LOOKUP SAVES (details_of): the car details the results page shows that
the row has no column for, so a remembered answer needs no paid call to draw
its page. Never the registration or VIN (the row's own columns hold those, and
the 12-month scrub clears the VIN there), never the paint answer (the row's
own columns are the answer, so a correction is never contradicted), and never
MOT, tax or ULEZ (asked afresh every time).
"""
import logging
import re
from datetime import timedelta

from django.utils import timezone

logger = logging.getLogger(__name__)

#: How long an answer is remembered, counted from the lookup that got it.
REMEMBER_DAYS = 90

#: The longest a lookup waits for DVLA's reply before giving up on the
#: remembered answer (the vehicle checks wait the same for theirs).
DVLA_WAIT_SECONDS = 4

#: Keys of the results payload that are never saved with a lookup (see above).
NOT_SAVED = frozenset({
    'search_id', 'paint_pending',
    'vehicle_status',
    'registration', 'vin', 'vin_masked',
    'paint_code', 'paint_description', 'all_paint_codes', 'paint_name_only',
})


def details_of(payload):
    """The part of a results payload saved on its lookup."""
    return {k: v for k, v in dict(payload or {}).items() if k not in NOT_SAVED}


def _code_key(code):
    return re.sub(r'[\s-]', '', str(code or '')).upper()


def _make_key(make):
    from lookup.models import PaintLookup
    return PaintLookup.normalize_manufacturer(str(make or ''))


def _colour_key(colour):
    """'Grey And Black', 'GREY/BLACK' and 'grey and black' are one colour."""
    return frozenset(w for w in re.sub(r'[^a-z]+', ' ', str(colour or '').lower()).split() if w != 'and')


def vin_agrees(remembered_vin, vin_now):
    """False only when both VINs are known and differ: another car."""
    a, b = str(remembered_vin or '').strip().upper(), str(vin_now or '').strip().upper()
    return not (a and b and a != b)


def same_car(row, dvla):
    """(True, '') when DVLA's make, year and colour today all equal the
    remembered lookup's; otherwise (False, why). Unknown is not agreement."""
    if not isinstance(dvla, dict):
        return False, 'DVLA gave no answer'
    make = _make_key(row.make)
    if not make or make != _make_key(dvla.get('make')):
        return False, 'the make differs'
    try:
        if int(row.year) != int(dvla.get('yearOfManufacture')):
            return False, 'the year differs'
    except (TypeError, ValueError):
        return False, 'the year is not known'
    colour = _colour_key(row.colour)
    if not colour or colour != _colour_key(dvla.get('colour')):
        return False, 'the colour differs'
    return True, ''


class Remembered:
    """An earlier answer for a plate: the lookup it came from, the code and
    name as they would be given today, and the saved car details if any
    lookup of the plate has them."""

    def __init__(self, source, paint_code, paint_description, details_row):
        self.source = source
        self.paint_code = paint_code
        self.paint_description = paint_description
        self.details_row = details_row
        self.confirmed = None          # set by confirm()
        self.why_not = ''

    @property
    def has_details(self):
        return self.details_row is not None

    def confirm(self, dvla):
        """Is it still the same car? Asked once; the answer is kept."""
        if self.confirmed is None:
            self.confirmed, self.why_not = same_car(self.source, dvla)
        return self.confirmed


def _reading(row):
    """(code, name) as this lookup's answer would be given today, or None when
    today's rules refuse it. The operator's answer is given as typed.

    paint293: only the provider's half is read back; the half our catalogue
    supplied at the time is derived again (see the note at the top)."""
    from lookup.models import Search
    code = (row.paint_code or '').strip()
    name = (row.paint_description or '').strip()
    if row.provider == Search.PROVIDER_MANUAL:
        return (code, name) if code else None
    from lookup.services.paint_resolver import _enrich_from_lookup
    ours = (row.enriched_from or '').strip()      # the half our catalogue supplied on the day, if any
    out = _enrich_from_lookup({'paint_code': '' if ours == Search.ENRICHED_CODE else code,
                               'paint_description': '' if ours == Search.ENRICHED_NAME else name},
                              row.make, row.model, vdg_colour=row.colour) or {}
    today = (out.get('paint_code') or '').strip()
    if not today or out.get('placeholder_refused'):
        return None
    if ours == Search.ENRICHED_CODE and _code_key(today) != _code_key(code):
        return None                    # the name gives a different code today: search afresh
    return today, (out.get('paint_description') or '').strip()


def contradicts(name, colour):
    """True when a paint's name states a colour and the car's registered colour
    states another. Unknown on either side is not a contradiction."""
    from lookup.services.paint_resolver import _colour_families
    said, registered = _colour_families(name or ''), _colour_families(colour or '')
    return bool(said and registered and not (said & registered))


def find(registration, now=None):
    """The remembered answer for this plate, or None. Reads only; never raises."""
    try:
        return _find(registration, now or timezone.now())
    except Exception:
        logger.warning('remembered answer could not be read', exc_info=True)
        return None


def _find(registration, now):
    from lookup.models import Search
    if not registration:
        return None
    rows = list(Search.objects
                .filter(registration=registration, timestamp__gte=now - timedelta(days=REMEMBER_DAYS))
                .order_by('timestamp', 'id'))
    coded = [r for r in rows if (r.paint_code or '').strip()]
    if not coded:
        return None
    manual = [r for r in coded if r.provider == Search.PROVIDER_MANUAL]
    if manual:
        source = manual[-1]
        counted = coded[coded.index(source):]
    else:
        given = [r for r in coded if r.provider not in Search.COPIED_PROVIDERS]
        if not given:
            return None                # only copies are left: the answer itself is older than the window
        source = given[-1]
        counted = coded
    answer = _reading(source)
    if answer is None:
        return None
    if source.provider != Search.PROVIDER_MANUAL and contradicts(answer[1], source.colour):
        return None                    # paint293: something is off; let the normal lookup run
    for row in counted:
        if row is source or _code_key(row.paint_code) == _code_key(source.paint_code):
            continue
        other = _reading(row)
        if other is None or _code_key(other[0]) != _code_key(answer[0]):
            return None                # two lookups of this plate disagree
    after = rows[rows.index(source) + 1:]
    if any(r.no_code_available for r in after):
        return None                    # the operator has since found that no code exists
    # The saved details of this same car: the fullest set any lookup of the plate
    # holds (a hand answer's cached copy can be thinner than the lookup before
    # it), the newest among equals.
    with_details = [r for r in rows
                    if isinstance(r.details, dict) and r.details
                    and _make_key(r.make) == _make_key(source.make) and r.year == source.year
                    and vin_agrees(source.vin, r.vin)]
    details_row = max(with_details, default=None,
                      key=lambda r: (sum(1 for v in r.details.values() if v), r.timestamp, r.id))
    return Remembered(source, answer[0], answer[1], details_row)
