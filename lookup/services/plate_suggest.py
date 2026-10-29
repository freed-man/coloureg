"""paint372: "Did you mean ...?" for a plate typed with a letter for a number.

WHAT IT IS FOR. A plate that no supplier knows is usually a typing slip, and the
commonest slip is the letter O for the number 0 (or 0 for O), or the letter I
for the number 1 (or 1 for I). On the export of 27 Oct, 29 lookups were turned
away as not found although one such swap turned what was typed into a real
plate's shape; 15 of those people typed that plate themselves within four
minutes and found their car, and the other 14 gave up.

THE RULE, every part of which must hold (the operator's word of 28 Oct: only if
the logic is 100 percent correct):
  1. the plate was not found: the suppliers answered, and DVLA's answer for the
     plate as typed was no (the caller decides this, at the not found exit);
  2. the plate as typed fits no UK plate format (UK_FORMATS below), so it cannot
     be a real plate that is simply missing from the records;
  3. exactly one swap of I and 1, or of O and 0, makes a current style plate
     (two letters, two numbers, three letters);
  4. DVLA says that plate exists (one more call, which costs nothing), and the
     plate is not on the blocked list.
Anything less certain: no suggestion, and the customer is told what they were
told before. Nothing runs until the customer presses the button, and pressing
it is an ordinary lookup with every gate in front of it.

WHAT IT CANNOT KNOW is whose car the suggested plate is. One visitor in the
export typed two wrong plates before the right one, and for the second the rule
would have offered a plate one swap away that was not theirs. So the box shows
DVLA's colour and make beside the plate: a wrong suggestion shows itself before
anyone presses anything.

No plate is ever written to a log line here (a plate is personal data; the log
store sits outside the retention promise, paint262).
"""
import logging
import re

logger = logging.getLogger(__name__)

#: Every shape a UK plate can take once its space is gone, with no leading zero
#: on a number (UK plates never start a number with 0; paint206). A plate that
#: fits any of these may be real, so it is never "corrected". Every plate that
#: found a car in the export of 27 Oct (4,238 of them, as normalised today) fits
#: one of these.
UK_FORMATS = (
    re.compile(r'[A-Z]{2}[0-9]{2}[A-Z]{3}'),        # current style, since September 2001: two letters, two numbers, three letters
    re.compile(r'[A-Z][1-9][0-9]{0,2}[A-Z]{3}'),    # prefix, 1983 to 2001: a letter, one to three numbers, three letters
    re.compile(r'[A-Z]{3}[1-9][0-9]{0,2}[A-Z]'),    # suffix, 1963 to 1983: three letters, one to three numbers, a letter
    re.compile(r'[A-Z]{1,3}[1-9][0-9]{0,3}'),       # dateless, and Northern Ireland: one to three letters, then up to four numbers
    re.compile(r'[1-9][0-9]{0,3}[A-Z]{1,3}'),       # dateless the other way round: up to four numbers, then one to three letters
    re.compile(r'[0-9]{3}[DX][0-9]{3}'),            # diplomatic: three numbers, then D or X, then three numbers
)

#: The only shape a suggestion may take.
CURRENT_STYLE = re.compile(r'[A-Z]{2}[0-9]{2}[A-Z]{3}')

#: The two look alike pairs, both ways round. Nothing else is swapped.
SWAPS = {'I': '1', '1': 'I', 'O': '0', '0': 'O'}

#: DVLA colour words that are not a colour.
_NO_COLOUR = {'', 'NOT STATED', 'UNKNOWN', 'NOT KNOWN'}

_CACHE_PREFIX = 'plate_suggest:'


def fits_uk_format(plate):
    """True if the plate (upper case, no spaces) fits any UK plate format."""
    return any(rx.fullmatch(plate or '') for rx in UK_FORMATS)


def swap_candidates(plate):
    """Every current style plate one swap (I and 1, O and 0) away, as
    (plate, position of the swapped character)."""
    out = []
    for i, ch in enumerate(plate or ''):
        other = SWAPS.get(ch)
        if other:
            q = plate[:i] + other + plate[i + 1:]
            if CURRENT_STYLE.fullmatch(q):
                out.append((q, i))
    return out


def candidate_for(plate):
    """The one plate to ask DVLA about, as (plate, position), or None.

    Only for seven characters of A to Z and 0 to 9 (a swap never changes the
    length, and a current style plate has seven) that fit no UK format, and only
    when exactly one swap gives a current style plate."""
    if not isinstance(plate, str) or not re.fullmatch(r'[A-Z0-9]{7}', plate):
        return None
    if fits_uk_format(plate):
        return None
    found = swap_candidates(plate)
    return found[0] if len(found) == 1 else None


def _car_line(dvla):
    """DVLA's colour and make as the box shows them: "Blue Ford", or the make
    alone when DVLA gives no colour. '' when DVLA gives no make."""
    from .vdg import fix_make_case
    make = fix_make_case((dvla.get('make', '') or '').strip().title())
    if not make:
        return ''
    colour = (dvla.get('colour', '') or '').strip()
    line = make if colour.upper() in _NO_COLOUR else f'{colour.title()} {make}'
    return line[:60]


def suggest(typed, ask_dvla, is_blocked=None):
    """The suggestion for a plate no supplier knows, or None. Never raises.

    `ask_dvla(plate)` is DVLA's vehicle enquiry: a dict when DVLA knows the
    plate, None when it does not or could not be asked. `is_blocked(plate)` is
    the blocked plates list. The suggestion is a small dict the session and the
    hour's memory can hold: the plate as typed, the plate suggested, the
    position swapped and the car line."""
    try:
        cand = candidate_for(typed)
        if not cand:
            return None
        plate, at = cand
        if is_blocked is not None and is_blocked(plate):
            logger.info('plate suggestion (paint372): the one swap plate is blocked; none offered')
            return None
        dvla = ask_dvla(plate)
        if not isinstance(dvla, dict):
            logger.info('plate suggestion (paint372): DVLA does not know the one swap plate; none offered')
            return None
        # DVLA names the plate it answered for; when it does, it must be this one.
        named = re.sub(r'\s+', '', str(dvla.get('registrationNumber') or '')).upper()
        if named and named != plate:
            logger.warning('plate suggestion (paint372): DVLA answered for another plate; none offered')
            return None
        car = _car_line(dvla)
        if not car:
            logger.info('plate suggestion (paint372): DVLA gave no make; none offered')
            return None
        logger.info('plate suggestion (paint372): offered (one swap, position %d)', at)
        return {'typed': typed, 'reg': plate, 'at': at, 'car': car}
    except Exception:  # noqa: BLE001 — a suggestion must never break the not found answer
        logger.exception('plate suggestion (paint372) failed; none offered')
        return None


def remember(typed, suggestion):
    """Keep the suggestion for as long as the plate's miss is remembered, so a
    repeat inside the hour (served without asking anyone) shows it again.
    Never raises."""
    from django.core.cache import caches
    from .protection import VRM_NEGATIVE_TTL_SECONDS
    try:
        caches['default'].set(_CACHE_PREFIX + typed, dict(suggestion), VRM_NEGATIVE_TTL_SECONDS)
    except Exception:  # noqa: BLE001
        logger.warning('plate suggestion (paint372) could not be remembered for the hour', exc_info=True)


def recall(typed):
    """The suggestion remembered for this plate, or None. Never raises."""
    from django.core.cache import caches
    try:
        found = caches['default'].get(_CACHE_PREFIX + typed)
    except Exception:  # noqa: BLE001
        return None
    return dict(found) if isinstance(found, dict) else None


def for_page(suggestion):
    """What the home page draws, or None when there is nothing sound to draw.

    The candidate is worked out again from the plate as typed, so the page can
    only ever show the one plate the rule gives, with the swapped character
    marked in both."""
    try:
        if not isinstance(suggestion, dict):
            return None
        typed = suggestion.get('typed')
        cand = candidate_for(typed)
        if not cand or cand != (suggestion.get('reg'), suggestion.get('at')):
            return None
        car = suggestion.get('car')
        if not isinstance(car, str) or not car.strip():
            return None
        plate, at = cand
        shown = plate[:4] + ' ' + plate[4:]
        shown_at = at if at < 4 else at + 1
        return {
            'typed_before': typed[:at], 'typed_swapped': typed[at], 'typed_after': typed[at + 1:],
            'before': shown[:shown_at], 'swapped': shown[shown_at], 'after': shown[shown_at + 1:],
            'shown': shown,
            'car': car.strip()[:60],
        }
    except Exception:  # noqa: BLE001
        logger.exception('plate suggestion (paint372) could not be drawn')
        return None
