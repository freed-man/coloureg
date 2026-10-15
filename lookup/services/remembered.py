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
the 5-year scrub clears the VIN there), never the paint answer (the row's
own columns are the answer, so a correction is never contradicted), and never
MOT, tax or ULEZ (asked afresh every time).

paint306, seven changes from the audit of 8 Oct, measured first on the lookups
of 2 May to 10 Oct (2,495 plates with a coded lookup in the window, 2,435 of
them remembered before this release):
  * NOTHING IS REMEMBERED WHEN ANY LOOKUP IN THE WINDOW SAYS NO CODE EXISTS,
    wherever that lookup sits. The operator's answer is written on the
    customer's own row, which keeps the time of the lookup, not of the answer:
    "no code exists" typed today on a lookup of last week sat BEFORE a
    provider's code of yesterday and was not seen. And two hand answers of one
    plate that give different codes remember nothing (the older was ignored).
    No plate was in either state.
  * A CUSTOMER'S "WRONG CODE" REPORT (reports_for). While a report is waiting
    to be judged, or was upheld with no correction typed, nothing is
    remembered for the plate. A correction typed on a report is the operator's
    answer for that lookup and is read as one: exactly as typed. A report he
    ignored changes nothing.
  * A SPECIAL ORDER CODE IS NOT SERVED FROM MEMORY (999 in its spellings, a
    BMW's 490): the search runs, because another source may now hold the car's
    real code. 7 plates held one, each from a supplier. One the operator
    typed himself stands, as every answer of his does.
  * A HAND ANSWER TYPED WITHOUT A NAME gets the catalogue's name for its code,
    as a 7-day replay does. No hand answer has been typed without one.
  * AN OLDER LOOKUP WHOSE CODE OUR CATALOGUE WORKED OUT FROM A NAME is read,
    when it is not the answer itself, by what that name gives today. It used
    to count as a disagreement whenever today's code was not the stored one,
    even when today's code WAS the answer, and blocked the plate for 90 days.
  * THE SAME CAR, MORE STRICTLY (identity_of). A lookup now also saves DVLA's
    engine size, fuel and month of first registration, and where the saved
    details hold them DVLA must say the same today. Lookups saved before this
    release hold none and are confirmed by make, year and colour as before.

paint312, decided by the operator on 11 Oct: HIS WORD IS KEPT FOR GOOD, A
SUPPLIER'S ANSWER FOR 30 DAYS.
  * An answer a supplier gave is remembered for REMEMBER_DAYS, now 30 (it was
    90). Measured on the lookups of 2 May to 10 Oct, the operator's own left
    out: of the repeats searched afresh 7 to 30 days after an answer, 35 got
    the same code and 4 another; 30 to 90 days after, 7 and 5.
  * The operator's own word on a plate has no end date: a code he typed (a
    manual answer, the dashboard's Answer box, a correction on a report) and
    his verdict that no code exists. It was counted from the day the customer
    LOOKED, so a car he answered in July failed again in November.
  * HIS NEWEST WORD STANDS. Each is dated by when he gave it
    (Search.operator_answered_at; on older rows, the lookup's own time, and
    for a report's correction the time the report was resolved). When the
    newest is "no code exists", nothing is remembered, whatever a supplier
    said before or since. When it is a code, that code is the answer, and an
    older verdict or an older code of his no longer counts. (Until now any
    "no code exists" in the window, and two hand answers that differed,
    remembered nothing.)
  * A verdict never stops a search: it only stops memory. The plate is
    searched afresh on every lookup, and the customer may ask again.
  * A customer's report that pauses memory (waiting, or upheld with nothing
    typed) pauses it for REMEMBER_DAYS, as before, and for as long as it is
    newer than the operator's word it would otherwise serve.
  * Lookups by suppliers that disagree with his code still stop it, while
    they are inside REMEMBER_DAYS and came after the lookup he answered.
"""
import logging
import re
from datetime import timedelta

from django.utils import timezone

logger = logging.getLogger(__name__)

#: How long a SUPPLIER'S answer is remembered, counted from the lookup that got
#: it. paint312: 30 days (it was 90). The operator's own word has no end date.
REMEMBER_DAYS = 30

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


#: paint306: where the saved details keep DVLA's own marks of the car.
IDENTITY_KEY = 'dvla'


def _mark(value):
    """One of DVLA's values as it is compared: text, trimmed, lower case; '' for none."""
    return '' if value is None else str(value).strip().lower()


def identity_of(status):
    """paint306: DVLA's engine size, fuel and month of first registration from
    the vehicle checks' facts of a lookup, or None when DVLA did not answer
    (its year is the sign that it did). A value DVLA does not hold for the car
    (an electric car has no engine size) is kept as '', so that "none then and
    none now" is agreement."""
    if not isinstance(status, dict) or not status.get('year'):
        return None
    return {'engine_cc': _mark(status.get('engine_cc')), 'fuel': _mark(status.get('fuel')),
            'reg_month': _mark(status.get('reg_month'))}


def _identity_now(dvla):
    return {'engine_cc': _mark(dvla.get('engineCapacity')), 'fuel': _mark(dvla.get('fuelType')),
            'reg_month': _mark(dvla.get('monthOfFirstRegistration'))}


def details_of(payload):
    """The part of a results payload saved on its lookup. paint306: with DVLA's
    marks of the car, when this lookup's own checks hold them; a payload drawn
    from saved details keeps the marks those details held."""
    payload = dict(payload or {})
    out = {k: v for k, v in payload.items() if k not in NOT_SAVED}
    marks = identity_of(payload.get('vehicle_status'))
    if marks:
        out[IDENTITY_KEY] = marks
    return out


def _code_key(code):
    return re.sub(r'[\s-]', '', str(code or '')).upper()


def _make_key(make):
    from lookup.models import PaintLookup
    return PaintLookup.normalize_manufacturer(str(make or ''))


def _car_make_key(make, vin):
    """paint327: a make as the search now reads it for this car: the VIN's
    maker when the make is one the catalogue does not know (make_from_vin).
    A lookup saved since paint327 holds "Ford" where DVLA, and every lookup
    before it, still say "Dria A"; read the same way, they are one make."""
    from lookup.services.paint_resolver import make_from_vin
    return _make_key(make_from_vin(make, vin))


def _colour_key(colour):
    """'Grey And Black', 'GREY/BLACK' and 'grey and black' are one colour."""
    return frozenset(w for w in re.sub(r'[^a-z]+', ' ', str(colour or '').lower()).split() if w != 'and')


def vin_agrees(remembered_vin, vin_now):
    """False only when both VINs are known and differ: another car."""
    a, b = str(remembered_vin or '').strip().upper(), str(vin_now or '').strip().upper()
    return not (a and b and a != b)


def same_car(row, dvla, marks=None):
    """(True, '') when DVLA's make, year and colour today all equal the
    remembered lookup's; otherwise (False, why). Unknown is not agreement.

    paint306: and, when the saved details hold DVLA's marks of the car
    (`marks`, see identity_of), its engine size, fuel and month of first
    registration today must equal them too. A plate moved to another car of
    the same make, year and colour passed the three alone."""
    if not isinstance(dvla, dict):
        return False, 'DVLA gave no answer'
    # paint327: both read by this car's VIN, so "Ford" on a lookup saved since
    # paint327 and DVLA's "Dria A" for the same van agree, as before they did.
    make = _car_make_key(row.make, row.vin)
    if not make or make != _car_make_key(dvla.get('make'), row.vin):
        return False, 'the make differs'
    try:
        if int(row.year) != int(dvla.get('yearOfManufacture')):
            return False, 'the year differs'
    except (TypeError, ValueError):
        return False, 'the year is not known'
    colour = _colour_key(row.colour)
    if not colour or colour != _colour_key(dvla.get('colour')):
        return False, 'the colour differs'
    if isinstance(marks, dict) and marks:
        now = _identity_now(dvla)
        for key, what in (('engine_cc', 'the engine size'), ('fuel', 'the fuel'), ('reg_month', 'the month of first registration')):
            if key in marks and _mark(marks.get(key)) != now[key]:
                return False, what + ' differs'
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
            marks = (self.details_row.details or {}).get(IDENTITY_KEY) if self.details_row is not None else None
            self.confirmed, self.why_not = same_car(self.source, dvla, marks)
        return self.confirmed


def _catalogue_name(row, code):
    """paint306: the catalogue's name for a hand answer typed without one, as
    a 7-day replay fills it (views._fill_cached_name). Never for a special
    order code, whose name is blank on purpose. '' when there is none."""
    try:
        from lookup.models import PaintLookup
        from lookup.services.paint_resolver import marks_special_order
        if marks_special_order(row.make, code):
            return ''
        _hex, name, _canonical = PaintLookup.lookup_with_canonical(
            manufacturer=row.make or '', paint_code=code, model=row.model or '',
            year=row.year, vdg_colour=row.colour or '')
        return (name or '').strip()
    except Exception:
        logger.warning('remembered answer: a hand answer\'s name could not be filled', exc_info=True)
        return ''


def _reading(row, by_hand=False):
    """(code, name) as this lookup's answer would be given today, or None when
    today's rules refuse it. The operator's answer is given as typed
    (`by_hand`: also a lookup he corrected from a customer's report).

    paint293: only the provider's half is read back; the half our catalogue
    supplied at the time is derived again (see the note at the top)."""
    from lookup.models import Search
    code = (row.paint_code or '').strip()
    name = (row.paint_description or '').strip()
    if by_hand or row.provider == Search.PROVIDER_MANUAL:
        if not code:
            return None
        return code, name or _catalogue_name(row, code)
    from lookup.services.paint_resolver import _enrich_from_lookup
    ours = (row.enriched_from or '').strip()      # the half our catalogue supplied on the day, if any
    out = _enrich_from_lookup({'paint_code': '' if ours == Search.ENRICHED_CODE else code,
                               'paint_description': '' if ours == Search.ENRICHED_NAME else name,
                               'source': row.provider},          # paint316: a VDG or Ezyvin name is cleaned
                              row.make, row.model, vdg_colour=row.colour) or {}
    today = (out.get('paint_code') or '').strip()
    if not today or out.get('placeholder_refused'):
        return None
    if ours == Search.ENRICHED_CODE and _code_key(today) != _code_key(code):
        return None                    # the name gives a different code today: search afresh
    return today, (out.get('paint_description') or '').strip()


def _name_gives_today(row):
    """paint306: the code a lookup's NAME gives today, for a lookup whose code
    our catalogue worked out from that name on the day; '' when it gives none
    or is refused. (What _reading works out before it compares the result with
    the stored code.)"""
    from lookup.models import Search
    from lookup.services.paint_resolver import _enrich_from_lookup
    if (row.enriched_from or '').strip() != Search.ENRICHED_CODE:
        return ''
    out = _enrich_from_lookup({'paint_code': '', 'paint_description': (row.paint_description or '').strip()},
                              row.make, row.model, vdg_colour=row.colour) or {}
    return '' if out.get('placeholder_refused') else (out.get('paint_code') or '').strip()


#: paint306: what a customer's report means for the plate's memory.
REPORT_PAUSES, REPORT_CORRECTED = 'pauses', 'corrected'
_CORRECTED_NOTE = 'corrected to '        # how views.admin_stats records a correction on the report


def reports_for(registration, since):
    """paint306: the "wrong code" reports on this plate's lookups in the
    window, as {lookup id: REPORT_PAUSES or REPORT_CORRECTED}. A report still
    waiting to be judged pauses; so does one upheld with no correction typed
    (the code was wrong and nothing replaced it). One upheld with a correction
    marks its lookup as answered by hand. An ignored report is left out. A
    report whose lookup is gone is filed under None.

    paint312: `since` None reads the plate's reports of every age."""
    out = {}
    for search_id, corrected, _created, _resolved in _report_list(registration, since):
        if corrected and out.get(search_id) != REPORT_PAUSES:
            out[search_id] = REPORT_CORRECTED
        elif not corrected:
            out[search_id] = REPORT_PAUSES
    return out


def _report_list(registration, since=None):
    """paint312: this plate's reports that were not ignored, oldest first, as
    (lookup id, was it a correction, when it was filed, when it was resolved)."""
    from lookup.models import PaintCodeReport
    reports = PaintCodeReport.objects.filter(registration=registration)
    if since is not None:
        reports = reports.filter(created_at__gte=since)
    out = []
    for search_id, status, note, created, resolved in (reports.order_by('created_at', 'id')
                                                       .values_list('search_id', 'status', 'operator_note',
                                                                    'created_at', 'resolved_at')):
        if status == PaintCodeReport.STATUS_IGNORED:
            continue
        corrected = status == PaintCodeReport.STATUS_ACTIONED and (note or '').startswith(_CORRECTED_NOTE)
        out.append((search_id, corrected, created, resolved))
    return out


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
    # paint312: EVERY lookup of the plate is read, because the operator's own
    # word has no end date. `recent` is the window a supplier's answer lives in.
    since = now - timedelta(days=REMEMBER_DAYS)
    rows = list(Search.objects.filter(registration=registration).order_by('timestamp', 'id'))
    recent = [r for r in rows if r.timestamp >= since]
    # paint306 (R2): customers' reports on these lookups. A correction typed on
    # a report is the operator's answer for that lookup, at any age (paint312).
    every_report = _report_list(registration)
    all_kinds = reports_for(registration, None)
    corrected = {r.id for r in rows if (r.paint_code or '').strip() and all_kinds.get(r.id) == REPORT_CORRECTED}
    corrected_at = {}
    for search_id, was_correction, created, resolved in every_report:
        if was_correction and search_id in corrected:
            corrected_at[search_id] = resolved or created
    by_hand = lambda r: r.provider == Search.PROVIDER_MANUAL or r.id in corrected
    said_at = lambda r: r.operator_answered_at or corrected_at.get(r.id) or r.timestamp

    # paint312: THE OPERATOR'S WORDS ON THIS PLATE, oldest first by when he
    # gave them: "no code exists", or a code of his own. The newest stands.
    words = []
    for place, r in enumerate(rows):
        if r.no_code_available:
            words.append((said_at(r), place, False, r))
        elif by_hand(r) and (r.paint_code or '').strip():
            words.append((said_at(r), place, True, r))
    words.sort(key=lambda w: (w[0], w[1]))

    coded = [r for r in recent if (r.paint_code or '').strip()]
    # A report waiting to be judged, or upheld with nothing typed, filed
    # inside the window pauses the plate (paint306), whoever gave the answer.
    if REPORT_PAUSES in reports_for(registration, since).values():
        return None
    if words:
        said, _place, is_code, source = words[-1]
        if not is_code:
            return None                # his newest word: no code exists
        # ...and such a report of any age pauses it while it is newer than
        # the word that would be served.
        if any(not was_correction and created >= said
               for _search_id, was_correction, created, _resolved in every_report):
            return None
        # His older words are behind him; what must still agree is every
        # other coded lookup inside the window that came after the one he
        # answered (a supplier's own answer since, say).
        superseded = {id(w[3]) for w in words[:-1]}
        counted = [r for r in coded
                   if r is source or (rows.index(r) > rows.index(source) and id(r) not in superseded)]
    else:
        if not coded:
            return None
        given = [r for r in coded if r.provider not in Search.COPIED_PROVIDERS]
        if not given:
            return None                # only copies are left: the answer itself is older than the window
        source = given[-1]
        counted = coded
    answer = _reading(source, by_hand(source))
    if answer is None:
        return None
    if not by_hand(source) and contradicts(answer[1], source.colour):
        return None                    # paint293: something is off; let the normal lookup run
    # paint306 (R5): a special order code names no paint. Served from memory it
    # kept the search from running for 90 days, and the search is what might
    # now find the car's real code. The operator's own answer is not
    # second-guessed here either: one he typed himself stands (none of the 7
    # was his).
    from lookup.services.paint_resolver import marks_special_order
    if not by_hand(source) and marks_special_order(source.make, answer[0]):
        return None
    # paint320: nor a code we could not confirm (no name, and nothing we hold
    # names it). Never remembered: the next lookup searches again, and may
    # find what it is. One the operator confirmed with the pencil stands.
    from lookup.services.paint_resolver import is_unconfirmed_code
    if (not by_hand(source) and not source.operator_answered_at
            and is_unconfirmed_code(source.make, answer[0], answer[1], source.model or None, source.colour or '')):
        return None
    for row in counted:
        if row is source or _code_key(row.paint_code) == _code_key(source.paint_code):
            continue
        other = _reading(row, by_hand(row))
        if other is None and not by_hand(row):
            # paint306 (R7): a lookup whose code our catalogue worked out from a
            # name, and whose name gives another code today, is read by what
            # the name gives today. When that is the answer, the two agree.
            today = _name_gives_today(row)
            if today and _code_key(today) == _code_key(answer[0]):
                continue
        if other is None or _code_key(other[0]) != _code_key(answer[0]):
            return None                # two lookups of this plate disagree
    # The saved details of this same car: the fullest set any lookup of the plate
    # holds (a hand answer's cached copy can be thinner than the lookup before
    # it), the newest among equals.
    with_details = [r for r in rows
                    if isinstance(r.details, dict) and r.details
                    and _car_make_key(r.make, r.vin or source.vin) == _car_make_key(source.make, source.vin)   # paint327
                    and r.year == source.year
                    and vin_agrees(source.vin, r.vin)]
    details_row = max(with_details, default=None,
                      key=lambda r: (sum(1 for v in r.details.values() if v), r.timestamp, r.id))
    return Remembered(source, answer[0], answer[1], details_row)
