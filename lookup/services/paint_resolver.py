"""
Paint fallback resolution.

When the initial VDG bundle call returns a vehicle but NO paint code, this
module tries to recover the paint code two ways IN PARALLEL and returns
whichever produces a code first:

  1. VDG paint retry   -- an immediate second paint_lookup() call.
     Empirically this sometimes recovers paint that the first call missed
     (VDG's upstream paint source is intermittent/slow on the first hit).
     Fast (~a few seconds) and cheap-ish (~£0.15-0.50), so it's worth racing.

  2. pl24 scrape  -- call the pl24 service's /lookup-paint with the VIN we
     already got from the first VDG call. pl24 scrapes partslink24's catalogue,
     which carries codes VDG often doesn't (esp. commercial vehicles). Slower
     (~3-60s depending on routing) but a different data source, so it catches
     misses the VDG retry can't.

Design notes:
  - Both run on threads and we take the FIRST that returns a usable paint code,
    with a deterministic preference for the VDG retry when both produce a code
    (it's cheaper — already paid for — and faster). See resolve_paint for how
    the preference is enforced even when both finish in the same wait() batch.
  - This function is SYNCHRONOUS and may block for up to ~PL24_TIMEOUT seconds.
    It is intended to be called from the background/status path (where the user
    is already looking at their vehicle data), NOT inline in the main lookup
    request.
  - Nothing here raises on a miss; failures degrade to "no paint found" so the
    caller always gets a clean result dict (or None).
"""

import colorsys
import concurrent.futures
import logging
import os
import functools
import re
import threading
import time

import requests

from .http import failure_chain, failure_kind, get_session

from .vdg import (paint_lookup, VdgError, VdgNotFoundError, VdgNotSentError,
                  VdgTimeoutError, _log_reg)
# paint302: VDG's list of finish words that are not paint codes, read here for
# every provider's answer (see _is_finish_word_not_code).
from .vdg import _FINISH_WORD_CODES
# paint95: One Auto is NO LONGER SUBMITTED as a race leg, but the import and
# _oneauto_leg below are deliberately kept. Re-enabling it is then one
# ex.submit line rather than a rebuild, and its battery coverage — the coverage
# skip list, the second chance, the billing sink — stays live rather than
# rotting. The decision to drop it rests on 24 days of data; that is enough to
# act on and not enough to burn the bridge.
from . import ezyvin
from . import oneauto


_DE_COLOUR = re.compile(r'(rot|grau|blau|schwarz|wei(?:ss|\u00df)|silber|gr(?:ue|\u00fc)n|gelb|braun)\b', re.I)
_EN_COLOUR = re.compile(r'\b(red|gr[ae]y|blue|black|white|silver|green|yellow|brown|orange|purple|gold|beige|bronze)\b', re.I)


def _stated_colours(name, words):
    """paint302: the colours a name states through the colour words of ONE
    language (`words` is _DE_COLOUR or _EN_COLOUR), as the families of
    _COLOUR_FAMILY, so "rot" and "red" are one colour, and "grau", "grey",
    "silber" and "silver" another. The German pattern finds its word at the
    end of a longer one, which is how German names carry it ("Tornadorot",
    "Phantomschwarz"); reading the name a word at a time cannot see those."""
    found = (w.lower().replace('\u00df', 'ss').replace('\u00fc', 'ue') for w in words.findall(name or ''))
    return {_COLOUR_FAMILY[w] for w in found if w in _COLOUR_FAMILY}


def _display_tidy(result, make):
    """paint284: the name as it leaves for the page. Capitals for a name given all
    in lower case or all in capitals ("bright blue", "BRIGHT BLUE"), and the code's
    English name in place of a German one when its row has one ("Elixir-Rot
    Metallic" became "Elixir Red..."). Combination names and mixed case are left as
    they are. Never raises.

    paint302: ONLY AN ENGLISH NAME OF THE SAME COLOUR. The English name was
    the one sharing most words with the German, whatever colour it stated, and
    a row can list the names of two paints: BMW 209 "Nachtblau" became
    "Orange", Mercedes 334 "Hellblau" became "Cardinal Red", Opel 22T "Schwarz"
    became "Ocean Blue Perleffekt". Measured on the repository's catalogue: of
    the 7,079 German names a row lists beside an English one, 386 went to a
    name of another colour (the audit counted 105: its test reads whole words,
    and so cannot see the colour inside "Nachtblau"). Now 72 of those go to
    the row's English name of the right colour, and 314 stay German, because
    the row has none. In the lookups to 7 Oct the swap had changed 58
    different names (161 answers), every one to its own colour; all 58 read
    as they did."""
    try:
        from lookup.models import PaintLookup
        name = (result or {}).get('paint_description') or ''
        if not name or name.startswith('Two-tone'):
            return result
        letters = re.sub(r'[^A-Za-z]', '', name)
        if letters and (letters.islower() or letters.isupper()):
            name = ' '.join(w.capitalize() if w.isalpha() else w for w in name.split())
        code = (result.get('paint_code') or '').strip()
        if code and _DE_COLOUR.search(name) and not _EN_COLOUR.search(name):
            row = PaintLookup.all_objects.filter(manufacturer=PaintLookup.normalize_manufacturer(make or ''), code=code).first()
            words = set(re.findall(r'[a-z]+', name.lower()))
            english = [n for n in (getattr(row, 'all_names', None) or []) if _EN_COLOUR.search(n) and not _DE_COLOUR.search(n)]
            # paint302: of those, only the ones that state the German name's colour.
            _colour = _stated_colours(name, _DE_COLOUR)
            english = [n for n in english if _colour & _stated_colours(n, _EN_COLOUR)]
            if english:
                name = max(english, key=lambda n: len(words & set(re.findall(r'[a-z]+', n.lower()))))
        if name != result.get('paint_description'):
            result = dict(result, paint_description=name)
    except Exception:
        logger.warning('display name tidy failed', exc_info=True)
    return result


def _tidy_on_exit(fn):
    """paint284: the display tidy on whatever the catalogue check returns (it has
    several ways out). functools.wraps keeps the check's own source visible to
    inspect, which older battery checks read."""
    @functools.wraps(fn)
    def wrapper(result, make, *args, **kwargs):
        return _display_tidy(fn(result, make, *args, **kwargs), make)
    return wrapper


@_tidy_on_exit
def _enrich_from_lookup(result, make, model=None, vdg_colour=None,
                        telemetry=None):
    """Fill gaps in a provider result from the PaintLookup table, BEHIND the
    live race (we only fill what VDG/pl24 didn't return).

    Two directions:
      - code but blank description  -> fill the colour name  (reliable: a code
        is 1:1 with a colour within a make)
      - colour name but blank code  -> fill the code, but ONLY if the name maps
        to exactly one code within the make (names are 1:many, so ambiguous
        names are left as-is — a wrong code is worse than none); this also
        clears the name_only flag so it counts as a full result

    Always attaches a hex swatch when one is available for the resolved code.
    Never raises; enrichment failure must never break the result. Returns the
    (possibly mutated) result dict, or the original unchanged on miss/None.
    """
    if not result or not make:
        return result
    try:
        # Import here to avoid a circular import at module load.
        from lookup.models import PaintLookup, OperatorPaintCode

        code = (result.get('paint_code') or '').strip()
        # paint162: a slash-joined answer is resolved to its paint code HERE,
        # where the provider's answer arrives, so the corrected code flows to
        # the row, the page and the email alike.
        _slashed = resolve_slashed_code(make, code, vdg_colour)
        if _slashed != code:
            logger.info('slashed code %s resolved to %s for %s',
                        code, _slashed, (make or '')[:30])
            code = _slashed
            result['paint_code'] = code
        if is_placeholder_code(make, code):
            # paint184: A PLACEHOLDER IS REFUSED WHERE IT ARRIVES, not after.
            #
            # paint146 refused it in _record_paint_hit, which cleans the ROW —
            # and nothing else. The caller had already taken the code from
            # this result, and went on to write it to the session, cache it for
            # seven days and return it to the customer as status "found". An
            # external audit found it by reading; running it confirmed all
            # four destinations: the row was '' and the other three were XXX.
            # [car 38], a 2026 Audi, was shown XXX on 22 Sep while the log line
            # recorded the refusal as a success.
            #
            # Refused HERE because every provider answer passes through this
            # function — the initial lookup (views, VDG) and every return path
            # of resolve_paint — so no destination can be missed again. The
            # same reasoning put the slash and special-order rules here.
            #
            # BOTH FIELDS CLEARED. A placeholder means the source has no
            # answer, so a name travelling with it is not evidence either:
            # [car 5] was delivered XXX with "Blue" on a car registered GREY.
            # paint146's intent was that a failed lookup at least offers the
            # customer a free manual one, and an empty result does exactly
            # that. Returned before the catalogue is consulted, because the
            # catalogue holds a junk row for XXX that would name it.
            #
            # Manual fulfilments never reach this function, so an operator's
            # deliberate N/A ([car 29], an AJS motorcycle) is untouched.
            logger.info('placeholder code refused at source: %s %s',
                        (make or '')[:30], code)
            result['paint_code'] = ''
            result['paint_description'] = ''
            result['placeholder_refused'] = True
            return result
        if _INTERIOR_NAME.match(result.get('paint_description') or ''):
            # paint293: AN INTERIOR IS NOT A PAINT, WHOEVER SAYS SO. Ezyvin put
            # "Interior :Ivory (034EZ)" in the exterior field of a 1998 Jaguar
            # XK8 and it was served as paint 034EZ on 5 Oct. paint284 refused it
            # in Ezyvin's own reader, which stops the next one from Ezyvin and
            # nothing else: the answer already given sits on its row, and a
            # remembered answer (paint288) is read back through THIS function,
            # where nothing objected, so that car would have been told 034EZ
            # again for three months. Refused here, where every provider's
            # answer and every remembered one passes.
            #
            # Both fields cleared and routed as not found, the way a placeholder
            # is: the code beside such a name is a trim code, not a paint.
            logger.info('interior named as a paint, refused: %s', (make or '')[:30])
            result['paint_code'] = ''
            result['paint_description'] = ''
            result['placeholder_refused'] = True
            result['interior_refused'] = True
            return result
        if marks_special_order(make, code):
            # paint159: never name a special-order code. Whatever the catalogue
            # holds against it is somebody else's bespoke car, picked up by a
            # scraper that found the name sitting next to the placeholder.
            # The CODE is kept: it really is on the sticker.
            #
            # paint299: BMW'S 490 TOO. This asked is_special_order_code, which
            # knows 999 and its spellings only. So 490 kept the name it arrived
            # with ("Sonderlackierung", German for special paint) on all six
            # lookups that were given it (25 Jul to 1 Sep, on five cars
            # registered bronze, purple, maroon, green, and red and black), and
            # that name went to the row, the page and the email as if it were
            # the colour's. It now asks marks_special_order, the one definition
            # the search, the page, the email form and the replays all use.
            result['paint_description'] = ''
            result['special_order'] = True
            return result
        if _is_colour_word_not_code(make, code):
            # paint197 (audit #3, P3): the word is the colour's NAME. Refused
            # as a code and marked name_only. With the manufacturer's own name
            # beside it ('Nero'), the rest of this function may still find the
            # real code by THAT name, under its exactly-one-code rule.
            logger.info('colour word refused as a code: %s %s',
                        (make or '')[:30], code)
            result['paint_code'] = ''
            result['name_only'] = True
            result['colour_word_refused'] = True
            if not (result.get('paint_description') or '').strip():
                # No name at all: the word WAS the name. Kept as that and
                # returned now, because a generic word is not the maker's name
                # for a paint, and looking a code up by it would be a guess. A
                # Ferrari 'Black' matches 910 Black Metallic as the ONLY hit,
                # but only because the catalogue calls Ferrari's other blacks
                # Nero: unique by language, not by paint.
                result['paint_description'] = code.title()
                return result
            code = ''
        if _is_finish_word_not_code(make, code):
            # paint200 (old list #6): the code says how the paint LOOKS, not which
            # paint it is. Refused; a real name beside it survives as name_only,
            # and the rest of this function may still find the code by that name.
            logger.info('finish word refused as a code: %s %s',
                        (make or '')[:30], code)
            result['paint_code'] = ''
            result['finish_word_refused'] = True
            if not (result.get('paint_description') or '').strip():
                # Nothing usable is left. Routed as NOT FOUND the way a
                # placeholder is (placeholder_refused is what the status view
                # reads), so an empty answer can never be recorded as found.
                result['paint_description'] = ''
                result['placeholder_refused'] = True
                return result
            result['name_only'] = True
            code = ''
        # A provider can return a real code that no retailer sells (paint85).
        # Rewritten HERE, where the provider's answer arrives, so the mapped
        # code flows to the row, the page and the email alike — mapping later
        # would leave the customer looking at the unpurchasable one.
        mapped = PaintLookup.map_code(make, code)
        if mapped != code:
            logger.info('code %s mapped to %s for %s', code, mapped, make)
            code = mapped
            result['paint_code'] = mapped
        desc = (result.get('paint_description') or '').strip()

        # A NAME THAT IS JUST THE CODE IS NOT A NAME. One Auto returned
        # "(MG2)" as the colour of a Hyundai i20 on 21 Aug, so the customer saw
        # "MG2 (MG2)" and learned nothing — the catalogue had MG2 as Mangrove
        # Green Metallic all along. Same shape as the SEAT "(M6M6)".
        # Treated as blank so the fill below runs; if the catalogue has no name
        # either, we are no worse off than before.
        if desc and code and desc.strip('()').strip().upper() == code.upper():
            logger.info('description %r was just the code for %s; refilling',
                        desc, make)
            desc = ''
            result['paint_description'] = ''
        # paint273: NOR IS ONE ENDING IN ITS OWN CODE. VDG writes Land Rover names
        # as "Luxor (869)" or "Santorini Black (820)" (10 answers so far, all
        # VDG, all Land Rover), so the page showed the code twice. The bracket
        # goes; the name before it stays.
        if desc and code:
            _m = re.match(r'^(.*\S)\s*\(([^()]*)\)\s*$', desc)
            if _m and re.sub(r'[\s-]', '', _m.group(2)).upper() == re.sub(r'[\s-]', '', code).upper():
                desc = _m.group(1).strip()
                result['paint_description'] = desc
        # paint243: TIDY A PROVIDER'S NAME. Ezyvin writes one name twice in two
        # spellings ("Wolf Gray Metallic/Wolf Grey Metallic") and adds a word
        # that is not part of it ("Pearl White Paint"). Only provider answers
        # pass through here; a manual answer is the operator's and never does.
        if desc:
            _tidy = tidy_provider_name(desc)
            if _tidy != desc:
                logger.info('provider name tidied for %s: %r -> %r', (make or '')[:30], desc, _tidy)
                desc = _tidy
                result['paint_description'] = _tidy
        # paint243: A NAME THAT IS ONLY A COLOUR WORD gives way to the
        # catalogue's own name for the code, when that name states the same
        # colour. A 2015 Renault Captur came back from pl24 as TED69 "Grey"; the
        # catalogue calls TED69 "Gris Platine". Measured before shipping: 100
        # answers so far had a colour word for a name (pl24 58, VDG 34), and the
        # catalogue had a proper name of the same colour for 46 (TEGNE "Black"
        # -> Noir Etoile, W2Y "Orange" -> Eclipse Orange).
        if code and desc and is_bare_colour_name(desc):
            _h, _n, _c = PaintLookup.lookup_with_canonical(
                manufacturer=make, paint_code=code,
                vdg_colour=vdg_colour or result.get('colour') or '',
            )
            if _n and not is_bare_colour_name(_n) and (_colour_families(_n) & _colour_families(desc)):
                logger.info('bare colour name %r replaced by the catalogue\'s %r for %s %s',
                            desc, _n, (make or '')[:30], code)
                desc = _n
                result['paint_description'] = _n
                result['enriched_from'] = 'name'
                if _h and not result.get('paint_hex'):
                    result['paint_hex'] = _h
        # paint256: A NAME YOU LOCKED IS THE NAME CUSTOMERS SEE. A 2019 Citroen
        # C3 Aircross came back from pl24 and VDG as KVG "Black Meet Kettle
        # Paint", a garbled translation, while the catalogue has KVG as "Ink
        # Black Metallic". No rule can spot a garbled name (they are real words),
        # but a name the operator has locked is a decision: it replaces the
        # provider's, unless the provider's name states a DIFFERENT colour (a
        # code can cover two paints, and then the provider is describing the
        # other one). A provider name that states no colour ("Sonderlackierung")
        # gives way too.
        if code and desc:
            _locked = PaintLookup.lookup(make, code)
            if (_locked is not None and 'name' in (_locked.locked_fields or [])
                    and _locked.name and _locked.name != desc):
                _pf = _colour_families(desc)
                if not _pf or (_pf & _colour_families(_locked.name)):
                    logger.info('provider name %r gives way to the locked %r for %s %s',
                                desc, _locked.name, (make or '')[:30], code)
                    desc = _locked.name
                    result['paint_description'] = _locked.name
                    result['enriched_from'] = 'name'

        if code and not desc:
            # code -> name (+ swatch)
            # vdg_colour lets a two-tone row order its halves BODY FIRST
            # (paint47). Without it "Z11 + Qab" expands black-first even though
            # DVLA reports the car White and QAB is the Pearl White.
            hex_val, name, _canon = PaintLookup.lookup_with_canonical(
                manufacturer=make, paint_code=code,
                # paint49: passed in, NOT read off the result. pl24 results have
                # no 'colour' key, and pl24 is where combination codes come from.
                vdg_colour=vdg_colour or result.get('colour') or '',
            )
            # paint92: the operator's own table, consulted ONLY on a catalogue
            # miss. Additive by construction — it can fill a blank, never
            # overwrite an answer 120,594 merged rows already produced.
            if not name:
                name = OperatorPaintCode.name_for_code(make, code)
            if not name:
                # paint156: LAST RESORT — trim one trailing LETTER off an
                # alphanumeric base and try again.
                #
                # Three Mitsubishis in three days arrived with a code the
                # catalogue holds one character shorter, and every one lost its
                # name for it:
                #
                #   C06A -> C06  Quartz Brown Metallic   DVLA Brown
                #   A67B -> A67  Dark Grey Pearl         DVLA Grey
                #   A39A -> A39  Graphite Grey Pearl     DVLA Grey
                #
                # MEASURED, AND THE SHAPE IS THE GUARD. Trimming a trailing
                # LETTER from a base that holds both letters and digits agrees
                # on colour 167 times of 172 — 97%. Trimming a DIGIT from an
                # all-numeric code agrees 18% of the time, because Mitsubishi
                # and Volvo number colours sequentially and unrelated shades sit
                # next to each other (6054 Regatta Blue beside 605 Gris
                # Espumante). So digits are excluded outright.
                #
                # This was built into mmw's gate on 13 Sep and never into this
                # path, so a pl24 answer got no trim at all.
                _stem = code[:-1] if len(code) > 2 else ''
                if (_stem and code[-1].isalpha()
                        and any(ch.isdigit() for ch in _stem)
                        and any(ch.isalpha() for ch in _stem)):
                    _h2, _n2, _c2 = PaintLookup.lookup_with_canonical(
                        manufacturer=make, paint_code=_stem,
                        vdg_colour=vdg_colour or result.get('colour') or '',
                    )
                    # CORROBORATE IT. 97% is a good rate, not a certainty, and
                    # the 3% would put a wrong NAME beside a correct code. The
                    # registered colour is free and already to hand, so the trim
                    # only stands if the trimmed row describes the same kind of
                    # colour — the same test paint143 applies to mmw.
                    #
                    # Silent when the colour cannot decide: no registered
                    # colour, or a name and hex that name no family. Unknown is
                    # not approval, so the name stays blank rather than being
                    # taken on the trim alone.
                    if _n2:
                        _want = _colour_families(
                            vdg_colour or result.get('colour') or '')
                        _got = _colour_families(_n2) or {
                            _hex_family(_h2)} - {None}
                        if _want and _got and (_want & _got):
                            name, hex_val = _n2, hex_val or _h2
            if name:
                result['paint_description'] = name
                # the NAME was supplied by our database, not the provider
                result['enriched_from'] = 'name'
            if hex_val and not result.get('paint_hex'):
                result['paint_hex'] = hex_val

        elif desc and not code:
            # name -> code (conservative: unique match only)
            # paint136: clear this THREAD's slot first, so a decline recorded
            # on an earlier lookup on the same thread is not read as belonging
            # to this one. Thread-local, so a CONCURRENT lookup in another
            # thread can no longer leak into it — see models.py.
            PaintLookup.take_last_ambiguity()
            found_code, hex_val, _canon_name = PaintLookup.code_from_name(
                manufacturer=make, colour_name=desc, model=model,
            )
            # ONLY WHEN THE LOOKUP ACTUALLY DECLINED.
            #
            # _collapse_to_single_code runs more than once per resolution — the
            # decorated name is tried before the stripped one — so an early
            # decline leaves the stash set even when a later attempt wins. Ford
            # 'Ink Blue (Metallic)' resolves cleanly to 3CYCWWA and was being
            # recorded as a 2-code ambiguity, which is exactly the kind of
            # false positive that would make the whole measurement worthless.
            # Read-and-clear in one call, so the slot cannot be left set for
            # the next lookup on this thread.
            _amb = PaintLookup.take_last_ambiguity()
            if _amb and len(_amb) > 1 and not found_code and telemetry is not None:
                # Into TELEMETRY, not the result dict. _apply_recovery_telemetry
                # is handed telemetry only, so anything written to `result` here
                # never reaches the Search row — it would look recorded and
                # persist nothing.
                telemetry['name_match_count'] = len(_amb)
                telemetry['name_match_codes'] = ', '.join(_amb)[:200]
            # paint92: same fallback, other direction. This is the one that
            # pays — a hand-researched code exists precisely BECAUSE the
            # catalogue could not resolve that name the first time.
            if not found_code:
                found_code = OperatorPaintCode.code_for_name(make, desc)
            # paint254: SEVERAL CODES, ONE PAINT. A 2026 Ford Kuga came back from
            # pl24 and Ezyvin as "Desert Island Blue" only, and the name matched
            # 2Z1, 5JDC, FC1 and JDCEWHA, so the customer got no code, tried three
            # times, and was answered by hand with JDCEWHA. All four are one paint:
            # three cross-reference rows from one source with no models, and
            # JDCEWHA with three sources and a model list. When every match is the
            # same paint, the best-evidenced one is given; a tie stays unknown.
            # After the operator's own codes (paint92), which always come first.
            if _amb and len(_amb) > 1 and not found_code:
                _pick = _best_of_same_paint(make, _amb, desc)
                if _pick is not None:
                    found_code = _pick.code
                    hex_val = _pick.hex or hex_val
                    logger.info('name %r matched %s, all one paint: %s given', desc, _amb, found_code)
            if found_code:
                result['paint_code'] = found_code
                # the CODE was supplied by our database, not the provider
                result['enriched_from'] = 'code'
                if hex_val and not result.get('paint_hex'):
                    result['paint_hex'] = hex_val
                # a code was found → no longer a name-only result
                if result.get('name_only'):
                    result['name_only'] = False
    except Exception:
        # paint158: LOG IT. This wraps the whole enrichment block, so anything
        # raised here silently costs the customer a name or a code, and nothing
        # anywhere would say so. paint155 found the origin-gate breaker swallowing
        # a NameError on every request for weeks in exactly this shape.
        # Still swallowed: enrichment is an improvement on the provider answer,
        # never a precondition for it.
        logger.exception('enrichment failed for %s', (make or '')[:40])
        pass
    return result


# pl24 service base URL + auth. In production PL24_BASE_URL is set to the private
# Railway address (http://pl24.railway.internal:8080) — pl24's public domain has
# been removed, so the default below points at the private address too (a missing
# env var should fail toward the real, private service, not a dead public URL).
PL24_BASE_URL = os.environ.get(
    'PL24_BASE_URL', 'http://pl24.railway.internal:8080'
).rstrip('/')
PL24_API_KEY = os.environ.get('PL24_API_KEY', '')

# paint140: the mmw service. Same shape as pl24 — private Railway hostname in
# production, X-API-Key, /lookup-paint. Unset key means the service has no key
# either, which is only true in local development.
MMW_BASE_URL = os.environ.get(
    'MMW_BASE_URL', 'http://mmw.railway.internal:8080'
).rstrip('/')
MMW_API_KEY = os.environ.get('MMW_API_KEY', '')

#: mmw answers in about a second warm (measured 14 Sep: 1.5s cold opening the
#: session, then 1.2s and 1.0s). This is a hang detector, not a patience
#: setting — it starts at t=0 and nothing waits on it, so a generous value
#: costs nothing and a tight one throws away answers.
#: (paint294: the END of a search may now wait on it, for MMW_SETTLE_WAIT_S at
#: most, whatever this is set to.)
_MMW_HTTP_TIMEOUT = (5.0, 12.0)

# How long resolve_paint waits for pl24 before giving up. Set just ABOVE pl24's
# own internal ceiling (~60s worst case, when it walks the full fallback chain:
# catalog -> commercial sibling -> Classic sibling -> dashboard). Matching it
# this way means pl24 always gets to finish its own attempt before we abandon
# it — capping lower would truncate exactly the slow commercial-vehicle lookups
# pl24 exists to rescue, throwing away codes it would have found seconds later.
# The long wait is acceptable because resolve_paint runs in the background while
# the user already sees their vehicle data; the results page communicates the
# wait ("checking manufacturer database...") rather than appearing frozen.
logger = logging.getLogger(__name__)

# The ceiling on how long a customer waits. pl24 is unaffected by the exact
# value: the backstop guarantees it STARTS by 10s, and its worst observed run is
# 29s (p50 1.0s, p90 8.0s), so it has ample room either way.
PL24_TIMEOUT = float(os.environ.get('PL24_CLIENT_TIMEOUT_S', '60'))

# How long the two PAID legs get before pl24 is brought in regardless (paint68).
# 10s: One Auto's quick answers land at ~6s (6.06-6.38 on 42 of 45 calls) and a
# VDG paint MISS refunds fast, so by 10s either leg that was going to fail has
# usually said so — while a VDG paint HIT can take 10-26s, which is why this is
# a backstop and not a deadline. pl24 joins the race; it does not end it.
#
# Worth being honest that the earlier "~6s" reading was optimistic: the same
# coverage run had vehicles still returning 202 at 21-31s. The backstop exists
# precisely because a leg can be slow rather than failed.
PL24_BACKSTOP_S = float(os.environ.get('PL24_BACKSTOP_S', '10'))
#: paint105. How long the reserve waits before firing on its own.
#:
#: A SAFETY NET FOR A HUNG LEG, NOT A COMPETITOR. It can only ever pre-empt a
#: FREE answer: it fires while a leg is still running, and a running leg usually
#: still answers, from VDG or pl24, at no cost. Every second earlier costs money
#: and buys time only for a leg that has genuinely stopped.
#:
#: Shipped at 15s and moved to 20s on 10 Sep, on the first evidence from the
#: pipeline as it now runs rather than as it used to:
#:
#:   [car 25] completed in 16.7s. The backstop fired at 15, pl24 answered
#:   shortly after with the same C3Y, and 5 credits bought 1.7 seconds. At 20s
#:   that call never happens. It was the first backstop firing in production
#:   and it was pure waste.
#:
#: The distribution moved too, because paint96 starts pl24 at zero instead of
#: summoning it when VDG drops out. Deliveries since: p50 9.8s (was 11.8s), and
#: NOTHING past 20s against 10.3% before. So 20s now sits above the whole
#: observed tail while 15s sits inside it.
#:
#: Six deliveries is a thin sample and a slow day will produce a 25s outlier —
#: which is fine, because that is the case this exists for. THE NUMBER TO WATCH
#: is ezyvin_started_because: if 'backstop' is more than a rarity against
#: 'both_empty', the answer is not to move this again, it is to find out which
#: leg is hanging.
EZYVIN_BACKSTOP_S = float(os.environ.get('EZYVIN_BACKSTOP_S', '20'))

# SECOND-CHANCE STAGE (paint73). When a paid leg finishes with nothing, ask it
# once more — but only briefly.
#
# The two are second chances for different reasons:
#   VDG      — a failed first call has WARMED their upstream cache, so the
#              second read is sub-second. [car 12] timed out twice at 40s and
#              then returned B85 in 0.74s. Before the vehicle/paint split this
#              mechanism supplied 214 of 866 answers (25%); the split removed it
#              because nothing warms the paint route any more.
#   One Auto — not a retry at all but a COLLECTION. 'still_fetching' means their
#              job was running server-side when we stopped polling, and results
#              are held 24 hours. [car 22] recorded still_fetching in coloureg
#              and then answered in 685ms on the next call.
#
# 5s, because the call should be fast OR NOT AT ALL: a warm read is sub-second,
# so anything slower is a cold fetch that will not finish inside the ceiling
# anyway. Giving up at 5s rather than 30 means a FAILED lookup — where the
# customer is waiting on the last leg to finish — resolves that much sooner.
SECOND_CHANCE_S = float(os.environ.get('SECOND_CHANCE_S', '5'))

# paint272: WHAT AN ABANDONED PAINT CALL COSTS. When we stop waiting on a paint
# call (our 35s timeout, a dropped connection, a 5xx from VDG's gateway) there is
# no receipt, so nothing used to be recorded. But VDG keeps working on it: its
# usage log for September shows every paint request still unanswered after 30s
# re-sent on VDG's side (a twin request exactly 30s later, from our address), and
# both charged at £0.27 when the paint is found. 117 slow first calls that month,
# every one found paint and was charged twice; 109 lookups lost £0.81-£1.08 each
# this way, about £100, recorded as £0.06. So an abandoned paint call is booked at
# two paint prices. It errs high when VDG finds nothing (an empty answer is
# refunded), which is the safe side for the daily budget breaker.
ABANDONED_PAINT_ESTIMATE = 0.54

# paint273: VDG'S SECOND CALL WAITS 23s, NOT 5s. It exists to collect an answer
# VDG has already prepared, usually under a second. Measured on VDG's own
# September log, 116 second calls fired at 35s: 12 were answered inside 5s, 12
# more WITH PAINT after 5s but inside VDG's 30s (we had hung up), 78 not inside
# 30s at all (VDG re-sent them; nothing comes back after that) and 14 empty. A
# longer wait doubles its wins and costs nothing extra: VDG charges that call
# whether we wait or not. 23s keeps it inside the race's 60s deadline (the
# first call's 35s plus this, with a second and a half to spare), so an answer
# still reaches the customer's page. It only fires when nobody has a code yet,
# mmw included (see _mmw_has_code). Separate from SECOND_CHANCE_S, which One
# Auto's dormant leg still reads.
VDG_SECOND_CHANCE_S = float(os.environ.get('VDG_SECOND_CHANCE_S', '23'))


def _abandoned(error):
    """True when a paint call failed without a receipt but VDG may still charge it.

    paint296: NOT A CALL THAT WAS NEVER SENT. When no connection to VDG could
    be made, VDG never saw the request, so there is nothing for it to charge.
    That used to count as abandoned, because a refused connection and a
    dropped one both read "VDG request failed". Measured against local
    sockets on 9 Oct: a refused connection, a name that does not resolve and
    a connect timeout were each booked at 54p, twice per paint search. While
    VDG could not be reached every search booked 1.08 pounds that was never
    spent, and a 30 pound daily budget would have paused the site after 28.
    """
    if error is None or isinstance(error, (VdgNotFoundError, VdgNotSentError)):
        return False
    if isinstance(error, VdgTimeoutError):
        return True
    text = str(error)
    return isinstance(error, VdgError) and (text.startswith('VDG request failed')
                                            or text.startswith('VDG returned 5'))

# Concurrency cap on the recovery race (paint19).
#
# resolve_paint parks its calling thread for up to PL24_TIMEOUT seconds. Gunicorn
# runs 2 workers x 8 threads = 16 concurrent requests, so 16 simultaneous
# paint-miss lookups park EVERY thread — including the one that would answer
# Railway's healthcheck. Failing healthchecks get the container restarted, which
# kills whatever lookups are in flight; once payments are live that means a
# customer charged mid-fulfilment.
#
# A plain semaphore would not help: a thread blocked waiting on it is just as
# parked. So callers TRY to acquire and are told to come back if they cannot,
# leaving the thread free immediately.
#
# paint294: THE LIMIT IS PER WORKER, AND 10 COULD NEVER BIND. This module is
# loaded once in each gunicorn worker, so each worker has its own slots, and a
# worker has 8 threads (WEB_THREADS, which the Dockerfile's start command reads
# too). Ten slots against eight threads meant the limit was never reached: all
# eight threads of a worker could park in paint searches at once, which is the
# very state this cap exists to prevent. The default is now two fewer than the
# worker's threads, so two stay free in every worker for ordinary pages and the
# healthcheck. MAX_CONCURRENT_RECOVERIES in the environment still wins.
def _recovery_slots_for(threads):
    """How many paint searches one worker may run at once, given its threads."""
    try:
        return max(1, int(threads) - 2)
    except (TypeError, ValueError):
        return 6


def _recovery_slots_from(environ):
    """The limit as the environment sets it: MAX_CONCURRENT_RECOVERIES when it
    is there, else two fewer than WEB_THREADS (8 when that is not set)."""
    return int(environ.get('MAX_CONCURRENT_RECOVERIES')
               or _recovery_slots_for(environ.get('WEB_THREADS') or 8))


MAX_CONCURRENT_RECOVERIES = _recovery_slots_from(os.environ)
_recovery_slots = threading.BoundedSemaphore(MAX_CONCURRENT_RECOVERIES)


def acquire_recovery_slot():
    """Non-blocking. True if a slot was taken (caller MUST release it)."""
    return _recovery_slots.acquire(blocking=False)


def release_recovery_slot():
    try:
        _recovery_slots.release()
    except ValueError:
        # BoundedSemaphore raises if released more times than acquired. Never
        # let bookkeeping take down a request that has already done its work.
        pass

# requests timeout as (connect, read): cap connection setup tightly (the pl24
# service is on the same platform/region, so a slow connect means trouble), and
# allow the read to run up to the overall budget for the scrape itself.
_PL24_HTTP_TIMEOUT = (5.0, PL24_TIMEOUT)
_PL24_RETRY_PAUSE_S = 1.0       # paint258: the pause before the one retry
_PL24_RETRY_WITHIN_S = 10.0     # paint258: only a failure this fast is retried


# Counter of retry-billing writes still in flight. Production does not need
# this — the write lands whenever it lands, and nothing reads the cost that
# quickly. It exists so tests can wait for the asynchronous write deterministically
# instead of racing it.
_pending_lock = threading.Lock()
_pending_count = 0


def _recovery_writes_pending():
    """True while a retry-billing write has not finished. Test support only."""
    with _pending_lock:
        return _pending_count > 0


def _record_worker_result(search_id, **fields):
    """Write a column straight to the Search row from a worker thread (paint26).

    Same problem and same shape as _record_retry_billing: resolve_paint returns
    as soon as one path wins, so the losing worker finishes AFTER the caller has
    read its telemetry and saved. Anything it learned has to be written by the
    worker itself or it is lost.

    An atomic UPDATE on named columns only, so it cannot lose a race against the
    caller's save and cannot clobber a column it does not own. Best-effort
    throughout — recording an observation must never break a lookup a customer
    is waiting on.
    """
    if search_id is None or not fields:
        return
    global _pending_count
    with _pending_lock:
        _pending_count += 1
    try:
        from lookup.models import Search
        Search.objects.filter(pk=search_id).update(**fields)
    except Exception:  # noqa: BLE001
        logger.warning('worker result not recorded for search=%s', search_id,
                       exc_info=True)
    finally:
        with _pending_lock:
            _pending_count -= 1
        # Worker threads get their own Django connection and nothing else
        # closes it; without this each recovery would strand one on Neon.
        try:
            from django.db import connections as _c
            _c.close_all()
        except Exception:  # noqa: BLE001
            pass


def _record_retry_billing(search_id, cost, balance, retry_code, retry_name=''):
    """Write the retry's own spend straight to its Search row (paint21).

    The retry used to hand its cost back through the telemetry dict, which the
    caller read AFTER resolve_paint returned. That works only if the retry
    finishes first. When pl24 wins the race, resolve_paint returns immediately
    and the retry is still in flight — cancel_futures cannot stop it, because
    with max_workers=2 it already started — so it completes, VDG bills us, and
    the cost lands in a dict nobody reads again.

    Measured on real traffic: every partslink24 row recording 0.08 was followed
    by exactly the abandoned retry's charge appearing on the NEXT balance
    reading. 5 of 5, no false positives, about GBP1/day and always under.

    Writing from here fixes it regardless of timing: whenever this worker
    finishes, it adds its own cost to the row. An atomic UPDATE, so it cannot
    lose against the caller's save, and Coalesce because the column is nullable
    and NULL + x is NULL in SQL.

    Best-effort by design: bookkeeping must never break a lookup the customer
    is waiting on.
    """
    if search_id is None:
        return
    global _pending_count
    with _pending_lock:
        _pending_count += 1
    try:
        from decimal import Decimal
        from django.db.models import DecimalField, F, Value
        from django.db.models.functions import Coalesce
        from lookup.models import Search

        updates = {}
        if cost is not None:
            updates['vdg_transaction_cost'] = Coalesce(
                F('vdg_transaction_cost'), Value(Decimal('0')),
                output_field=DecimalField(max_digits=10, decimal_places=2),
            ) + Decimal(str(cost))
        if balance is not None:
            # The retry is the newer call, so its balance is the fresher truth.
            updates['vdg_balance_after_call'] = Decimal(str(balance))
        if retry_code is not None:
            updates['vdg_retry_code'] = (retry_code or '')[:100]
        # Only write a NAME we actually have. A blank must not clobber a name
        # already on the row: the first pass and the retry both write here, and
        # the second one arriving empty should leave the first one's answer
        # alone rather than erase it.
        if retry_name:
            updates['vdg_paint_name'] = retry_name[:120]
        if updates:
            Search.objects.filter(pk=search_id).update(**updates)
    except Exception:  # noqa: BLE001 — never let bookkeeping break a lookup
        logger.warning('retry billing not recorded for search=%s', search_id,
                       exc_info=True)
    finally:
        with _pending_lock:
            _pending_count -= 1
        # This runs on a pool thread. Django opens a connection per thread and
        # nothing here closes it, so without this each recovery would strand one
        # on Neon (conn_max_age=200 keeps it alive well past the thread's life).
        try:
            from django.db import connections as _c
            _c.close_all()
        except Exception:  # noqa: BLE001
            pass


def _vdg_retry(registration, telemetry=None, search_id=None, race_over=None,
               mmw_future=None, make='', vdg_colour=''):
    """Second VDG bundle call. Returns a paint dict if paint came back, else
    None. Never raises — VDG errors degrade to None (no recovery).

    COST. Measured 8 Sep across 473 paint-less lookups since paint66 split the
    packages. A refund is CONDITIONAL, and the condition is what the paint
    document contained rather than whether we could use it:

        genuinely empty PaintCodeList -> refunded, ~£0.06 (vehicle only)   73%
        a colour NAME but no code     -> charged in full, ~£0.33           26%

    The discriminator is stark: of the 343 refunded, ZERO carried a colour name
    from VDG; of the 125 charged, 117 did. VDG refunds an empty document, not a
    disappointing one — a list with a name in it is not empty, so we got data,
    it just was not a code.

    This replaces the paint15 note, which read "VDG bills this call whether or
    not it returns paint — a paint-less call is partially refunded and nets
    ~£0.12, a hit costs the full ~£0.45". Those were BUNDLE-era figures, true
    until 15 Aug and wrong every day since, and they overstate the retry's cost
    by roughly 4x. They were quoted as current on 8 Sep and produced a wrong
    answer about the pipeline; the numbers above carry their measurement date
    for that reason.

    WHY THE SPEND IS RECORDED AT ALL (paint15, still true). It used to vanish:
    this function only surfaced a value on a hit, so the retry's cost never
    reached the Search row. Since ~58% of lookups trigger a retry, every
    downstream total undercounted — and the daily budget breaker, which sums
    vdg_transaction_cost, would have seen only ~60% of real spend, letting a
    £30 budget run to ~£50.

    So we now stash the NET cost and the latest balance into the telemetry dict
    on EVERY outcome — hit, miss, or error — and the caller adds them to the row.
    Writing two keys into a plain dict from this worker thread is safe (single
    assignments under the GIL, and the caller only reads them after the future
    resolves or the deadline passes).
    """
    _t = telemetry if telemetry is not None else {}
    # billing_sink is populated by vdg.py on EVERY call that reached VDG,
    # including ones that then raise or report not-found (paint18). Without it
    # a retry that came back empty was billed and recorded nothing — which is
    # exactly the common case here, since we only retry when the first call
    # found no paint. That is why partslink24 rows were storing £0.08 (one
    # call) when the account had actually been charged £0.16 (two).
    sink = {}
    # paint195: the second chance gets its OWN record. Both calls wrote into
    # `sink` and vdg.py overwrites rather than adds, so when the second chance
    # ran it replaced the first call's charge before anything was recorded.
    # That was real money. The second chance fires when the first retry comes
    # back empty, and VDG only PARTLY refunds an empty reply: it nets ~£0.06,
    # the vehicle part (see the COST note above). So every lookup reaching the
    # second chance lost its first retry's charge from the row and from the
    # budget breaker. Each call is now recorded and summed.
    second_sink = {}
    second = None
    data = None
    first_error = second_error = None       # paint272: why a call left no receipt
    try:
        # PAINT package only (paint66). The retry never needed the vehicle
        # half — it exists because a cold first call warms VDG's upstream cache,
        # so the second read is fast. Asking for the vehicle documents again
        # would pay for identity we already hold.
        data = paint_lookup(registration, billing_sink=sink)
    except Exception as e:  # noqa: BLE001 — see below; this must never escape
        # A TIMEOUT LANDS HERE, and the second chance below must still run
        # (paint84). It previously sat inside this try, so an exception jumped
        # straight past it — leaving the stage unreachable on the one case that
        # justified building it. [car 12] timed out twice at 40s and then
        # returned B85 in 0.74s; that is a timeout followed by a warm read, and
        # the code could not reach the warm read.
        #
        # CATCHES EVERYTHING, not just VdgError (F5). `sink` is populated by
        # _make_request the moment VDG answers, BEFORE the response is parsed —
        # so a parse-side failure (_parse_paint_fields meeting an unexpected
        # shape, e.g. a list of strings in PaintCodeList) arrives with the
        # charge ALREADY INCURRED. Catching only VdgError let that escape past
        # the cost read and _record_retry_billing below, losing a real charge:
        # the exact blind spot paint18 exists to close, and invisible spend is
        # what lets the daily budget breaker read low. views.py added the same
        # trust-boundary catch on the first pass; the retry lacked it.
        if not isinstance(e, VdgError):
            logger.exception('VDG paint call failed unexpectedly for %s',
                             _log_reg(registration))
        first_error = e
        data = None

    # SECOND CHANCE (paint73). The first call has warmed VDG's upstream cache
    # whether it succeeded, came back empty, or timed out — that is the whole
    # mechanism behind the 214 answers the old bundle-retry supplied. A short
    # timeout because a warm read is sub-second; anything slower is a cold fetch
    # that will not finish inside the ceiling anyway.
    #
    # Only when the first call produced NO PAINT. A hit needs no second call,
    # and a hit is the only outcome that has already cost the full price — a
    # paint-less call is refunded.
    first_data = data
    # paint272: NOT ONCE ANOTHER PROVIDER HAS A CODE. race_over is set when a
    # usable code exists (pl24, Ezyvin or VDG), so a second call then cannot help
    # the customer, yet it is charged whenever VDG finds paint: in September 85 of
    # the 109 lookups that paid for four paint calls already had pl24's code. A
    # warm-read rescue still runs whenever nobody has answered yet.
    # paint294: it is set as well once the search has ENDED, whatever it ended
    # with, because nobody is left to read a second call's answer then either.
    skip_second = (bool(race_over is not None and race_over.is_set())
                   or _mmw_has_code(mmw_future, make, vdg_colour))      # paint273
    if not (data and data.get('paint_returned')) and not skip_second:
        # RECORDED, not just done. Until now `data = second` overwrote silently,
        # so a row won on the second attempt looked identical to one won on the
        # first — and the question "does this £0.27 earn its keep" had no answer
        # in the data. Captured BEFORE the call so a raise still leaves a trace.
        from lookup.models import Search   # lazy: circular import at module load
        second_chance = Search.SECOND_CHANCE_EMPTY
        # Was the race already decided when this fired? Every True here is spend
        # a race-over flag would have prevented.
        after_race = bool(race_over is not None and race_over.is_set())
        try:
            second = paint_lookup(registration, billing_sink=second_sink,
                                  timeout=VDG_SECOND_CHANCE_S)          # paint273
        except Exception as e:  # noqa: BLE001 — a second chance must never raise
            second_error = e
            second = None
        if second and second.get('paint_returned'):
            logger.info('VDG second chance recovered paint for %s',
                        _log_reg(registration))
            data = second
            second_chance = Search.SECOND_CHANCE_WON
        if search_id is not None:
            _record_worker_result(search_id, vdg_second_chance=second_chance,
                                  second_chance_after_race=after_race)
    # Take the cost from whichever source has it. The sink is the only source
    # on the not-found and error paths (where `data` is None), but `data`
    # carries it on the success path — and reading BOTH means this keeps
    # working if either mechanism changes, rather than silently recording
    # nothing. Losing this figure is not a visible failure: it just makes the
    # budget breaker read low, which is precisely how the original bug went
    # unnoticed.
    first_cost = sink.get('transaction_cost')
    if first_cost is None and first_data:
        first_cost = first_data.get('transaction_cost')
    second_cost = second_sink.get('transaction_cost')
    if second_cost is None and second:
        second_cost = second.get('transaction_cost')
    # paint272: a call we abandoned left no receipt; book what VDG goes on to charge.
    estimated = False
    if first_cost is None and _abandoned(first_error):
        first_cost, estimated = ABANDONED_PAINT_ESTIMATE, True
    if second_cost is None and _abandoned(second_error):
        second_cost, estimated = ABANDONED_PAINT_ESTIMATE, True
    if estimated:
        _t['vdg_retry_cost_estimated'] = True
        logger.warning('VDG paint call abandoned without a receipt for %s; booking an estimate',
                       _log_reg(registration))
    # paint296: a call that never left is said so, and nothing is booked for it.
    _unsent = [e for e in (first_error, second_error) if isinstance(e, VdgNotSentError)]
    if _unsent:
        _t['vdg_retry_not_sent'] = True
        logger.warning('VDG paint call not sent for %s (%s); nothing booked for it',
                       _log_reg(registration), str(_unsent[-1])[:120])
    _costs = [c for c in (first_cost, second_cost) if c is not None]
    retry_cost = sum(_costs) if _costs else None
    if retry_cost is not None:
        _t['vdg_retry_cost'] = retry_cost

    # The balance is a snapshot, not a sum: the later call's is the current one.
    retry_balance = second_sink.get('balance')
    if retry_balance is None:
        retry_balance = sink.get('balance')
    if retry_balance is None and data:
        retry_balance = data.get('balance')
    if retry_balance is not None:
        _t['vdg_retry_balance'] = retry_balance

    # Record straight to the row, so this survives the caller having already
    # returned and saved (paint21). The telemetry keys above are kept for the
    # existing tests and for the case where the caller is still waiting, but
    # they are no longer what the cost DEPENDS on — see _record_retry_billing.
    retry_code = ''
    retry_name = ''
    if data:
        retry_code = (data.get('paint_code') or '')
        # The NAME too (paint69). VDG can return a colour name with no code, and
        # until now that was thrown away — so a row could not show that VDG had
        # said anything at all. It is also what makes source comparison
        # possible: 18 of 27 observed disagreements were the same paint at
        # different completeness, which only the names reveal.
        retry_name = (data.get('paint_description') or '')
    _t['vdg_retry_code'] = retry_code
    _t['vdg_paint_name'] = retry_name
    _record_retry_billing(search_id, retry_cost, retry_balance, retry_code,
                          retry_name=retry_name)
    if data is None:
        return None
    if not data or not data.get('paint_returned'):
        return None
    return {
        'source': 'vdg_retry',
        'paint_code': data.get('paint_code', ''),
        'paint_description': data.get('paint_description', ''),
        'all_paint_codes': data.get('all_paint_codes', []),
        'balance': data.get('balance'),
        # The retry calls the SAME bundle endpoint as the first pass, so `data`
        # carries the vehicle identity too — VIN included. This used to be
        # dropped: only the paint fields were surfaced (paint61).
        #
        # It matters when the FIRST call returned nothing at all. A VDG timeout
        # yields no vehicle and no VIN, but the retry (which needs only the
        # registration) comes back with the full bundle. [car 30] on 12 Aug is
        # the case: first pass died at 46s with nothing, retry returned C31 —
        # so a complete response was in hand, and the row still shows vin=''.
        # The VIN then reads blank on the results page and in the email.
        #
        # DOES NOT change which lookups succeed, and it is worth being exact
        # about why: pl24 is submitted to the executor at the SAME instant as
        # this retry, with the `vin` variable as it stands then — empty. It
        # no-ops at its own `if not vin` guard before this value could exist.
        # So this is data completeness and honest telemetry, not a recovery
        # improvement. It is also the precondition for sequencing the recovery
        # (retry first behind a short fuse, then pl24 with the VIN it produced),
        # which is where it WOULD change outcomes.
        'vin': data.get('vin', ''),
    }


# VW model lines whose paint data lives in partslink24's COMMERCIAL catalogue
# regardless of the EU type-approval class VDG reports. The Caddy is the
# canonical case: a Caddy Life is type-approved M1 (passenger MPV), so VDG's
# category is "correct" — but partslink24 files every Caddy under Volkswagen
# Commercial Vehicles, so an M1 routing sends pl24 to a catalogue that cannot
# resolve it. Matched as a prefix of the model string ("Caddy Maxi C20 Life"
# starts with "caddy"). Model name is the primary signal; the WV1 VIN prefix
# (VW Commercial Vehicles' WMI — data-confirmed 16/16 commercial in our
# traffic) is a belt-and-braces catch for commercial VWs with unusual model
# strings. WV2/WV3 are deliberately NOT used: WV2 is ambiguous (car-derived
# vans) and WV3 is unverified.
_VW_COMMERCIAL_MODELS = (
    'transporter', 'caddy', 'crafter', 'amarok', 'caravelle', 'multivan',
)


# VDG make strings that pl24's MAKE_TO_BRAND has no key for, mapped to one it
# does (paint65).
#
# pl24 turns make strings into catalogues — that IS its job, and its map already
# carries 46 make strings. The reason this translation lives HERE rather than
# there is narrower: "Mercedes-AMG" is what VDG calls the car, and VDG's
# vocabulary is coloureg's business. Put it in both places and two systems are
# each half-responsible for the same rewrite, with the one that knows why not
# doing it. Same boundary, same reasoning, same file as _route_category.
#
# EVIDENCE, not guesswork: pl24's resolve_brand('Mercedes-AMG') returns
# "unknown make", so the lookup dies at its routing gate before a browser
# opens. Mercedes-Benz resolves and returns paint (6 of 54 lookups); every
# Mercedes-AMG lookup has failed (3 of 3), and partslink24 was confirmed by
# hand to hold the code for [car 27].
#
# DELIBERATELY NOT HERE — checked, and the ownership guess was wrong:
#   Cupra   pl24 has its OWN Cupra catalogue, not SEAT. 10/10 resolved anyway.
#   Dacia   pl24 has its OWN Dacia catalogue, not Renault. 9/9 resolved.
#   Alpine, smart  both already keys in pl24's map.
# Mapping those to a parent would break routing that works. A shared corporate
# owner is not evidence of a shared catalogue.
#
# Renault is a different problem and NOT an alias: it routes cleanly and still
# never returns (0 of 25 attempted). That belongs to pl24's extractor, and
# diagnosing it needs a debug dump, not an entry here.
_PL24_MAKE_ALIASES = {
    'mercedes-amg': 'Mercedes-Benz',
}


def route_make(make):
    """The make string pl24 should receive. Only rewrites known-unroutable ones.

    Returns `make` untouched when there is no alias, so an unknown string still
    reaches pl24 and fails visibly rather than being silently swallowed here.
    """
    return _PL24_MAKE_ALIASES.get((make or '').strip().lower(), make)


def _route_category(make, model, vin, category):
    """The category pl24 should receive for this vehicle.

    Fixes the one known misroute: VW commercial lines that VDG classes as M1
    (or leaves unclassed), which sends pl24 to the passenger catalogue where
    their paint doesn't exist. Only ever upgrades ''/M1 to N1, and only for
    Volkswagen: an explicit non-passenger class (N1/N2/N3) from VDG is trusted
    as-is, and other makes are untouched. The Search row keeps VDG's raw
    category — this routing applies solely at the pl24 boundary.
    """
    cat = (category or '').strip().upper()
    if cat and cat != 'M1':
        return category
    mk = (make or '').strip().lower()
    if not (mk.startswith('volkswagen') or mk == 'vw'):
        return category
    m = (model or '').strip().lower()
    if any(m.startswith(t) for t in _VW_COMMERCIAL_MODELS) \
            or (vin or '').strip().upper().startswith('WV1'):
        return 'N1'
    return category


#: paint140. Colour words to families, for the mmw validation gate.
#:
#: DELIBERATELY COARSE. It answers one question — is the catalogue's name for
#: this code the same KIND of colour as the car's registered one — and a
#: finer-grained map would reject honest answers over shade. Measured at a 1.4%
#: false-reject rate across 1,547 known-good paid answers.
#:
#: Multilingual because the catalogue is: it holds Phantomschwarz beside
#: Phantom Black and Ljusbla beside Light Blue, from three scraped sources.
_COLOUR_FAMILY = {
    'red': 'red', 'rosso': 'red', 'rouge': 'red', 'rot': 'red',
    'infrared': 'red', 'rood': 'red', 'rojo': 'red',
    'blue': 'blue', 'bleu': 'blue', 'blau': 'blue', 'blu': 'blue',
    'azul': 'blue', 'bla': 'blue', 'turquoise': 'blue', 'teal': 'blue',
    'green': 'green', 'vert': 'green', 'verde': 'green', 'grun': 'green',
    'gruen': 'green', 'moss': 'green', 'olive': 'green',
    'yellow': 'yellow', 'jaune': 'yellow', 'gelb': 'yellow',
    'amarillo': 'yellow',
    'black': 'black', 'noir': 'black', 'nero': 'black', 'schwarz': 'black',
    'preto': 'black', 'negro': 'black', 'svart': 'black',
    'white': 'white', 'blanc': 'white', 'bianco': 'white', 'weiss': 'white',
    'weis': 'white', 'branco': 'white', 'blanco': 'white', 'vit': 'white',
    # Silver and grey are ONE family. DVLA registers many metallic greys as
    # Silver and the catalogue names them Grey, or the reverse; splitting them
    # would reject correct answers on a naming convention.
    'grey': 'grey', 'gray': 'grey', 'gris': 'grey', 'grigio': 'grey',
    'grau': 'grey', 'silver': 'grey', 'silber': 'grey', 'cinza': 'grey',
    'plata': 'grey', 'titanium': 'grey', 'graphite': 'grey', 'anthracite': 'grey',
    'orange': 'orange', 'arancio': 'orange',
    'purple': 'purple', 'violet': 'purple', 'viola': 'purple', 'lilac': 'purple',
    'brown': 'brown', 'braun': 'brown', 'marron': 'brown', 'beige': 'brown',
    'bronze': 'brown', 'sand': 'brown', 'tan': 'brown',
    'gold': 'gold', 'or': 'gold',
    'pink': 'pink', 'rose': 'pink',

    # paint168: WIDER VOCABULARY, SAME RULE.
    #
    # 42% of rows with no hex had a name this map could not read, so nothing
    # could be verified against them — and the names were mostly colour words
    # in languages it did not know: giallo 219, azzurro 186, vermelho 143,
    # brun 75, marrone 56, avorio 54.
    #
    # This map gates the mmw check, the Mitsubishi letter-trim corroboration
    # and the hex proposals, so every addition was tested against all 163
    # historical mmw decisions first: NONE changed. That is the point — a
    # wider vocabulary lets more answers be CHECKED, it does not let more
    # through.
    #
    # DELIBERATELY EXCLUDED: `perla`. Pearl is a FINISH, not a colour, and
    # `citroen/KTV Noir Perla Nera` is black. Mapping it to white would have
    # made a black car's name read white. Accented forms are excluded too —
    # _colour_families strips non-letters, so `doré` arrives as `dor`.
    'giallo': 'yellow', 'amarelo': 'yellow', 'geel': 'yellow',
    'azzurro': 'blue', 'celeste': 'blue', 'blauw': 'blue', 'niebieski': 'blue',
    'vermelho': 'red', 'czerwony': 'red',
    'brun': 'brown', 'bruin': 'brown', 'marrone': 'brown', 'castanho': 'brown',
    'bordeaux': 'red', 'burgundy': 'red', 'maroon': 'red', 'crimson': 'red',
    'scarlet': 'red', 'ruby': 'red', 'cherry': 'red', 'claret': 'red',
    'avorio': 'white', 'ivory': 'white', 'creme': 'white', 'cream': 'white',
    'argento': 'grey', 'argent': 'grey', 'zilver': 'grey', 'grijs': 'grey',
    'quicksilver': 'grey', 'gunmetal': 'grey', 'pewter': 'grey',
    'slate': 'grey', 'charcoal': 'grey', 'platinum': 'grey',
    'musta': 'black', 'zwart': 'black', 'czarny': 'black',
    'ebony': 'black', 'onyx': 'black',
    'oro': 'gold', 'dore': 'gold', 'goud': 'gold', 'champagne': 'gold',
    'amber': 'gold', 'brass': 'gold',
    'arancione': 'orange', 'naranja': 'orange', 'laranja': 'orange',
    'oranje': 'orange',
    'lila': 'purple', 'morado': 'purple', 'roxo': 'purple', 'porpora': 'purple',
    'rosado': 'pink', 'roze': 'pink',
    'emerald': 'green', 'jade': 'green', 'lime': 'green', 'sage': 'green',
    'navy': 'blue', 'cobalt': 'blue', 'sapphire': 'blue', 'indigo': 'blue',
    'aqua': 'blue', 'cyan': 'blue',
    'copper': 'brown', 'chocolate': 'brown', 'mocha': 'brown', 'taupe': 'brown',
    'khaki': 'brown', 'caramel': 'brown', 'bronzo': 'brown',
}


#: Codes that are a placeholder rather than an answer. Shape only — whether one
#: is REJECTED depends on evidence, not on matching this (see is_placeholder_code).
_PLACEHOLDER_CODE = re.compile(
    r'^(X{2,}|N\.?/?A\.?|NONE|UNKNOWN|TBC|TBA|\?+|-+)$', re.I)


#: Codes that mean "painted to special order", not a colour. The code IS on the
#: car's sticker, so it is kept and shown — it just does not identify a paint.
_SPECIAL_ORDER_CODES = {'999', 'L999', '0999'}

#: paint293: a "paint name" that says it is the interior ("Interior :Ivory").
_INTERIOR_NAME = re.compile(r'\s*interior\b', re.I)


# paint243: a name that is only a colour word, with at most a shade before it
# and a finish after it: "Grey", "Blue Metallic", "Black Pearl", "Pearl White".
_BARE_COLOUR_NAME = re.compile(
    r'^(?:(?:dark|light|metallic|pearl)\s+)?'
    r'(?:white|black|grey|gray|silver|blue|red|green|yellow|orange|brown|beige|gold|purple|maroon|bronze)'
    r'(?:\s+(?:metallic|pearl|mica|solid|paint))?$', re.I)
# A trailing "Paint" is part of the name after these words ("Special Paint").
_KEEP_PAINT_AFTER = {'special', 'custom', 'individual', 'exclusive', 'bespoke'}


def _same_paint(a, b):
    """paint243: two catalogue rows for the same paint: the same name (one
    containing the other counts, after normalising) or swatches within 8 on
    every channel."""
    from lookup.models import PaintLookup
    na, nb = PaintLookup.normalize_name(a.name or ''), PaintLookup.normalize_name(b.name or '')
    if na and nb and (na == nb or na in nb or nb in na):
        return True
    ha, hb = (a.hex or '').lstrip('#'), (b.hex or '').lstrip('#')
    if len(ha) == 6 and len(hb) == 6:
        try:
            return max(abs(int(ha[i:i + 2], 16) - int(hb[i:i + 2], 16)) for i in (0, 2, 4)) <= 8
        except ValueError:
            return False
    return False


def _best_of_same_paint(make, codes, name):
    """paint254: the one code to give when a name matches several codes of one
    paint, or None.

    The Kuga's "Desert Island Blue" matched 2Z1, 5JDC, FC1 and JDCEWHA: three
    cross-reference rows with no models from one source, and JDCEWHA, the only
    one with a model list. That is the shape that is safe to resolve: exactly
    ONE candidate is attested on cars; the rest are cross-references. Where
    several candidates carry models, the name spans real codes from different
    eras (Ford "Race Red", 13 codes; "Smoke", YHR and BMU), and choosing between
    them is a guess the older rule (paint68) rightly refuses; the battery caught
    my first version doing exactly that. All of these must hold:
      * the provider's name is specific, not a bare colour word;
      * no candidate's own names state different colours;
      * every candidate is the same paint (same name, or practically the same
        swatch);
      * exactly one candidate has a model list;
      * its colour agrees with the provider's name."""
    from lookup.models import PaintLookup
    if not name or is_bare_colour_name(name):
        return None
    rows = [PaintLookup.lookup(make, c) for c in codes]
    if not rows or not all(rows):
        return None
    for r in rows:
        fams = [f for f in (_colour_families(n) for n in (r.all_names or []) + [r.name or '']) if f]
        if len(fams) > 1 and not set.intersection(*fams):
            return None
    if not all(_same_paint(rows[0], r) for r in rows[1:]):
        return None
    attested = [r for r in rows if r.models_list]
    if len(attested) != 1:
        return None
    best = attested[0]
    want, got = _colour_families(name), _colour_families(best.name or '')
    if want and got and not (want & got):
        return None
    return best


def is_bare_colour_name(name):
    return bool(_BARE_COLOUR_NAME.match((name or '').strip()))


def tidy_provider_name(name):
    """paint243. One name written twice in two spellings becomes one ("Wolf
    Gray Metallic/Wolf Grey Metallic" -> "Wolf Grey Metallic", the British
    spelling kept), and a trailing "Paint" that is not part of the name goes
    ("Pearl White Paint" -> "Pearl White"). Anything else is left as sent."""
    out = (name or '').strip()
    # paint278: a bracketed note is not part of the name ("Steel Grey [India:Steel
    # Silver", its bracket never closed, from Ezyvin on 3 Oct), and neither is
    # trailing punctuation ("Titanium Grey Paint-", the same day).
    out = re.sub(r'\s*\[[^\]]*(?:\]|$)', '', out).strip()
    out = re.sub(r'[\s,;:\-\u2013\u2014]+$', '', out).strip()
    parts = [p.strip() for p in out.split('/') if p.strip()]
    key = lambda p: re.sub(r'\s+', ' ', p.lower().replace('gray', 'grey'))
    if len(parts) > 1 and len({key(p) for p in parts}) == 1:
        out = next((p for p in parts if 'grey' in p.lower()), parts[0])
    # paint279: "Paintwork" too ("Elixir Red Paintwork", pl24 and Ezyvin, 3 Oct).
    stripped = re.sub(r'\s+paint(?:work)?$', '', out, flags=re.I).strip()
    if stripped and stripped != out and stripped.split()[-1].lower() not in _KEEP_PAINT_AFTER:
        out = stripped
    return out


def resolve_slashed_code(make, code, dvla_colour=None):
    """Pick the paint code out of a slash-joined string, or return it unchanged.

    paint162. 283 delivered codes have contained a slash, and a customer cannot
    order paint from any of them. They are four different conventions joined by
    the same character:

        Ford        2431C/2PJE/ZJNC  -> 2PJE   cross-references
        Chrysler    PS3/QS3S         -> PS3    body / trim
        Jeep        PW6/QW6S         -> PW6
        Audi        T9/Y9C           -> Y9C    interior / exterior
        Volkswagen  0Q0Q/C9A         -> C9A

    THE GUARD IS THE CATALOGUE, not the shape. Exactly one part must resolve as
    a code for that make. If several do, we cannot tell which is the paint and
    it is left alone; if none does, we know nothing and it is left alone.

    Measured over the full history: 266 of 283 have exactly one resolving part,
    13 have several, 4 have none. No delivered slashed code was itself a
    catalogue entry, but the whole string is still tried FIRST — abarth 103/B
    and its 4,565 siblings carry a slash inside a legitimate code, and must
    never be split.
    """
    code = (code or '').strip()
    if '/' not in code:
        return code
    from lookup.models import PaintLookup
    # The whole string wins if it is ITSELF a row. Checked against the raw
    # table, NOT through lookup(), because lookup() already splits on '/'
    # internally — so asking it would always say yes and nothing would ever be
    # resolved. That is exactly what happened on the first attempt here.
    # (paint302: lookup() no longer splits a slash. The raw table is still the
    # place to ask: lookup() also tries a shortened form and the L prefix.)
    #
    # Ordering matters: this is what protects abarth 103/B and its 4,565
    # siblings, where the slash is part of a legitimate code.
    mfr = PaintLookup.normalize_manufacturer(make)
    if PaintLookup.objects.filter(manufacturer=mfr, code__iexact=code).exists():
        return code
    # Each part is tried as sent AND `L`-prefixed, because VDG returns the VAG
    # exterior code without the L that the catalogue carries: `T9/Y9C` is Ibis
    # White under `LY9C`, and testing `Y9C` alone finds nothing. The interior
    # halves — T9, 0E, 2T2T — resolve under neither form, which is what makes
    # the pair separable at all.
    #
    # The part is returned AS SENT, not as the variant that matched: the
    # customer's sticker says Y9C, and the L is our catalogue's notation.
    hits, found = [], {}      # found: part -> (its row, matched only through the L prefix)
    for part in (x.strip() for x in code.split('/')):
        # paint176: A SINGLE CHARACTER IS NOT A PAINT CODE. Some VAG codes carry
        # the slash INSIDE them — `L8/2` is one code, not two — and splitting
        # those produced a confident wrong answer: `L8/2` became `2`, which the
        # L-prefix variant then matched to `L2`, Sequoia Green Metallic. Phantom
        # Black served as a green, through three individually reasonable steps.
        #
        # Costs nothing: all 266 delivered codes that resolved still resolve.
        if not part or len(part) < 2:
            continue
        _plain = PaintLookup.objects.filter(manufacturer=mfr, code__iexact=part).first()
        _lrow = None if _plain else PaintLookup.objects.filter(manufacturer=mfr, code__iexact='L' + part).first()
        if _plain or _lrow:
            hits.append(part)
            found[part] = (_plain or _lrow, _lrow is not None)
    if len(hits) > 1:
        # paint243: SEVERAL HALVES ARE CODES, BUT FOR THE SAME PAINT. Audi
        # `2T/C9X` (29 Sep, an RS Q8 from VDG's second try): production holds
        # 2T "Deep Black Metallic" and LC9X "Orcaschwarz Perleffekt" with the
        # same swatch; 2T is VW's two-character short code for it. When every
        # half is the SAME PAINT, the full code is kept (the longest; the first
        # of equal length). Same paint means the same name (one containing the
        # other counts) or practically the same swatch. NOT the same colour
        # family: Ford 0210 Ermine White and 0691 Diamond White are both white
        # and different paints, and the older check P4 caught my first version
        # merging them. Anything else still leaves the string alone.
        # Which half to keep: the VAG exterior code where there is one (the
        # half found only as L + part: C9X over 2T, N1K over 9141), else the
        # longest.
        _rows = [found[part][0] for part in hits]
        if not all(_same_paint(_rows[0], r) for r in _rows[1:]):
            # paint302: BEFORE THE STRING IS LEFT ALONE, THE TOP-UP HALVES STAND
            # ASIDE. A row the catalogue top-up added answers only where no
            # established row would (paint236), and until paint302 lookup()
            # kept that promise for a slash string by splitting it itself: the
            # established half went on answering as it had before the top-up.
            # With that split gone (see PaintLookup.lookup) the promise is
            # kept here. Halves that are different paints, one of them known
            # only through a top-up row: the string resolves as it did before
            # that row existed, by its established halves alone.
            #
            # NOT WHEN THE HALVES ARE ALL ONE PAINT (the lines below): there
            # the longest is still given, top-up row or not, and Ford
            # "2431C/2PJE/ZJNC" is why. In production 2431C and ZJNC are top-up
            # rows named Moondust Silver and 2PJE is the established row, named
            # Satin Silver by a single source; the string counts as one paint
            # and gives 2431C. Setting the top-up rows aside there as well
            # (the audit's W12) would give 2PJE "Satin Silver", and mmw sent
            # that string for 10 cars, every one of which pl24 named Moondust
            # Silver (VDG and Ezyvin, when they had a code of their own for
            # one, said PNZJB, which is Moondust Silver too). So that part is
            # left as it was, for the operator to decide.
            hits = [part for part in hits if not PaintLookup.is_topup_only(found[part][0])]
            _rows = [found[part][0] for part in hits]
            if not hits or not all(_same_paint(_rows[0], r) for r in _rows[1:]):
                return code
        if len(hits) > 1:
            _vag = [part for part in hits if found[part][1]]
            hits = [max(_vag or hits, key=len)]
    if len(hits) != 1:
        return code
    # AND IT MUST AGREE WITH THE REGISTERED COLOUR, the same guard the mmw gate
    # and the Mitsubishi letter-trim use. `T9/1` is Ibis White, but Skoda's `T9`
    # is Atoll Gruen — a real code for a different paint, so length alone does
    # not catch it. Refuses 1 of the 266 in history (`L8/Z9Y` reading Dark Grey
    # Matt on a car DVLA calls Black), which is a defensible answer rather than
    # a wrong one; that is the price of catching the T9 class.
    if dvla_colour:
        # paint293: read as the page will read it, by this car's colour. `L8/Z9Y`
        # on a black Audi was left unsplit because the short Z9Y row says Dark
        # Grey Matt; the row that names this car's paint is LZ9Y, Phantom Black.
        _row = PaintLookup.lookup(make, hits[0], vdg_colour=dvla_colour)
        _want = _colour_families(dvla_colour)
        _got = _colour_families(_row.name) if _row else set()
        if _want and _got and not (_want & _got):
            return code
    return hits[0]


def is_special_order_code(code):
    """True for a code that marks bespoke paint rather than naming one.

    paint159. DIFFERENT FROM is_placeholder_code. `XXX` is a wildcard a source
    returns when it has nothing, and is worthless to the customer. `999` is
    genuinely stamped on the car: it means the paint was ordered outside the
    catalogue, and the colour lives only on the build record.

    So the code is KEPT and shown. Only the name is suppressed, because every
    name attached to it is a different car's bespoke colour that a scraper
    happened to find next to the placeholder. The catalogue proves it — eight
    makes carry `999` with eight unrelated colours:

        bmw Medium Grey · dodge Orange · plymouth Orange · porsche Dunkelgrau
        renault Vert Tyrol Metallic · isuzu, mazda, nissan Trans Blue

    and mazda's Trans Blue ALSO exists under its real code, A6A.

    Two groups use it the same way: VW Group as `L999` (Bentley has used VW
    paint codes since 1998, and the L is often dropped on the sticker), and
    Chrysler as plain `999` on 1969-70 Dodge and Plymouth.

    NOT evidence-based, unlike is_placeholder_code. `renault/999` has a hex and
    six models, so an evidence test would keep it — but six models is six cars
    that had special-order paint, not six cars sharing a colour.

    paint299: NOTHING ON THE SITE ASKS THIS ANY MORE. It knows the codes every
    make uses and not the ones a single make uses (BMW's 490), and while the
    page asked this and the search asked marks_special_order, the two
    disagreed about a BMW given 490. Everything now asks marks_special_order,
    below. This is kept as the any-make half of that rule, which the
    battery's paint159 checks still read on its own.
    """
    return (code or '').strip().upper() in _SPECIAL_ORDER_CODES


#: paint294: codes ONE make uses for "painted to special order". BMW's 490 comes
#: with the name "Sonderlackierung" (German for special paint) on every car it
#: was given to: six lookups on five cars from 25 Jul to 1 Sep, registered
#: bronze, purple, maroon, green, and red and black. Limited to BMW because 16
#: other makes in the catalogue use 490 for a real colour (Volvo's is Chameleon
#: Blue). Keyed by the catalogue's own spelling of the make.
_SPECIAL_ORDER_BY_MAKE = {'bmw': frozenset({'490'})}


def marks_special_order(make, code):
    """True when a code says the car was painted to special order: `999` and its
    spellings for any make, and the codes a single make uses that way.

    paint294, for the paint search. Such a code is true and still not an
    answer: it tells the customer the paint exists, not which one it is. So
    the search no longer stops on it (see resolve_paint). A yellow BMW on
    7 Oct shows what that is worth: partslink24 said 490, mmw said 490, and
    VDG held the car's real code, C4H. VDG happened to answer first; had
    partslink24 been quicker, the customer would have been given 490.

    paint299: THE ONE DEFINITION, FOR EVERYTHING. The page, the status call,
    the email form, the emails, the replays (7-day cache and remembered
    answers), the car picture and the dashboard's manual queue all ask this
    function, as the search does. Until then the page asked
    is_special_order_code, which does not know 490, so a BMW given 490 was
    shown it with a name and a swatch as if it were a colour. What each of
    them does with the answer: no colour name and no swatch (the catalogue's
    row for such a code is some other car's bespoke paint), the note that
    offers a free manual lookup, and an email request that goes to the
    operator's manual queue the way a not found request does (decided 9 Oct).
    """
    code = (code or '').strip().upper()
    if not code:
        return False
    if code in _SPECIAL_ORDER_CODES:
        return True
    from lookup.models import PaintLookup
    return code in _SPECIAL_ORDER_BY_MAKE.get(
        PaintLookup.normalize_manufacturer(str(make or '')), ())


def special_order_code_spellings():
    """paint299: every code marks_special_order says yes to for at least one
    make (999, L999, 0999 and 490 today), sorted. It is NOT the test: 490 is
    in it and is a real colour on a Volvo. It is for narrowing a database
    query to the few rows worth reading (the dashboard's manual queue), each
    of which marks_special_order then decides with the row's make. Built from
    the two tables above, so a code added there is found here too.
    """
    return sorted(set(_SPECIAL_ORDER_CODES).union(*_SPECIAL_ORDER_BY_MAKE.values()))


def is_placeholder_code(make, code):
    """True when a code is a scraper artefact rather than a paint code.

    paint146. `[car 5]`, a 2013 Audi A8 registered GREY, was delivered paint
    code `XXX` with the description `Blue` — twice, once via One Auto in
    September and again via pl24 tonight. `XXX` is a wildcard the source uses
    where it has no answer, and the catalogue carries a junk row for it.

    A code is worthless to a customer whatever its name: `XXX` sends them to a
    paint counter with nothing, while a failed lookup at least offers them a
    free manual one.

    EVIDENCE, NOT A BLACKLIST. Shape alone would be wrong — three makes carry
    `NA` with a hex and a model list:

        fordamerica  NA  Dark Tourmaline Pearl   #003339  8 models
        mazda        NA  Dark Tourmaline Mica    #003339  1 model
        nissan       NA  Midnight Teal Metallic  #003339  1 model

    All the same colour, on makes that genuinely shared platforms and paints.
    `bedford XX` is Brilliant Ochre at #FCE903 with a model. So a placeholder is
    only refused when its catalogue row has NO hex AND NO models — nothing to
    suggest it is real. That keeps 7 of the 18 placeholder-shaped rows and
    refuses 11.

    Measured over four months: blocks 1 of 1,943 delivered answers, and loses
    ZERO whose description matched the registered colour.

    NOT APPLIED TO MANUAL FULFILMENTS — see the caller. `[car 29]` is an AJS
    motorcycle where the operator entered `N/A` with 'Metallic Blue': no code
    exists for that bike, the colour does, and that is a real answer.

    paint242: A SWATCH IS NO LONGER EVIDENCE, only a model list. Swatches can
    now be made after loading (propose_hexes names a colour from the name;
    the top-up fills gaps), so a junk row can gain one: production's Audi
    `XXX` "Blue" did, and a 2015 A5 registered MAROON was shown XXX "Blue"
    on 29 Sep. partslink24's own data for that car reads
    "Exterior color / Paint Code: Q0 / XXX": no catalogue paint at all. On
    the repository's catalogue the change keeps exactly the same 7
    placeholder-shaped rows as before; all 7 have models.
    """
    code = (code or '').strip()
    if not code or not _PLACEHOLDER_CODE.match(code):
        return False
    from lookup.models import PaintLookup
    row = PaintLookup.lookup(make, code)
    if row and (row.models_list or []):
        return False
    return True


def _hex_family(hex_value):
    """The colour family a hex sits in, or None when it cannot be read.

    paint143. The gate matched on COLOUR WORDS IN THE NAME, so a row whose name
    does not happen to say its colour was refused however obviously right it
    was. [car 24], 14 Sep: a Honda CR-V registered Red, mmw returned R-539P, the
    catalogue holds R539P as 'Molten Lava Pearl' at #8E1F13. Plainly red, and
    invisible — molten, lava and pearl are not colour words. That is 21,645
    rows, 18% of the catalogue, carrying a hex and no colour word.

    MEASURED, NOT GUESSED. Against 1,674 known-good delivered answers this
    accepts 151 of the 183 the name cannot judge, and against deliberately
    wrong codes it wrongly accepts 13.1% — slightly BETTER than the name gate's
    15.5%, so reach improves without the gate getting looser.

    AN EARLIER VERSION TREATED NEIGHBOURING FAMILIES AS COMPATIBLE and was
    dropped: it raised true-accept to 95% and false-accept to 44.8%, three
    times worse than the gate it was meant to help. A false reject costs an
    opportunity; a false accept sends someone the wrong paint.

    Neutrals are decided FIRST and generously, because that is where the
    boundaries bite: a bluish silver must read grey, not blue.
    """
    raw = (hex_value or '').lstrip('#')
    if len(raw) != 6:
        return None
    try:
        r, g, b = (int(raw[i:i + 2], 16) / 255 for i in (0, 2, 4))
    except ValueError:
        return None
    h, sat, val = colorsys.rgb_to_hsv(r, g, b)
    h *= 360
    if val < 0.10:
        return 'black'
    # 0.25, not 0.15. Metallic silvers carry a real blue cast — Meteor Silver
    # #97B0BF is 0.21 saturated — and DVLA records those cars as Silver or
    # Grey, so a lower threshold reads them blue and refuses a correct answer.
    # Measured across 1,674 known-good answers the choice is a WASH (86.2/11.8
    # against 86.1/12.1), so this is settled on being comprehensible rather
    # than on a difference that is inside the noise.
    if sat < 0.25:
        return 'black' if val < 0.18 else ('white' if val > 0.82 else 'grey')
    # Dark and washed out reads grey whatever the hue — 'Magnetic' #383838 is
    # a grey to DVLA, not a black.
    if sat < 0.30 and val < 0.45:
        return 'grey'
    if h < 18 or h >= 330:
        return 'red'
    if h < 42:
        return 'brown' if val < 0.55 else 'orange'
    if h < 70:
        return 'yellow'
    if h < 180:
        return 'green'
    if h < 260:
        return 'blue'
    return 'purple'


def _colour_families(text):
    """Every colour family named in a string. Empty set when it names none."""
    words = re.sub(r'[^a-z]+', ' ', (text or '').lower()).split()
    return {_COLOUR_FAMILY[w] for w in words if w in _COLOUR_FAMILY}


#: paint302: the words of _COLOUR_FAMILY that name a GEM or a METAL, for
#: _gate_colour_families below and nothing else. Silver, gold and bronze are
#: metals and are NOT here: they are words DVLA registers a car's colour by.
#: Nor are graphite, anthracite, slate and charcoal, which are greys by name:
#: a Volkswagen registered grey has "Blue Anthracite Pearl" (C7V), sent by mmw
#: on 1 of its 832 lookups, and it passes because anthracite says grey.
_GEM_AND_METAL_WORDS = frozenset({
    'sapphire', 'ruby', 'emerald', 'jade', 'onyx', 'amber',
    'titanium', 'platinum', 'cobalt', 'copper', 'brass', 'pewter', 'gunmetal',
    'quicksilver',
})


def _gate_colour_families(text):
    """paint302: the colour families a catalogue name states, AS THE MMW GATE
    READS IT: a gem or a metal word counts only when the name has no plain
    colour word.

    _colour_families reads every word alike, so "Black Sapphire" stated black
    AND blue (sapphire is in the table as a blue), and mmw's code for it
    passed the gate on a car registered blue. A black named after a gem is
    not a blue. When a name has a plain colour word, that word is the colour:
    "Black Sapphire" is black, "Platinum White" white, "Copper Red" red. A
    name with only the gem or the metal ("Sapphire", "Titanium Metallic") is
    read by it, as before.

    FOR THE GATE ONLY. _colour_families is shared with every other rule that
    compares colours (the slash rule's colour guard, the dashboard's "check"
    mark, what memory refuses, the catalogue commands) and reads as it did.
    Measured: the repository's catalogue has 466 names that pair a gem or a
    metal with a plain colour word of another family (sapphire with black 89,
    platinum with white 32, onyx with green 27, copper with orange 23). Of
    the 483 different codes mmw sent with a registered colour to 7 Oct, not
    one gets another verdict from this, and so none of the 104 answers that
    were mmw's own.

    IT MAKES THE GATE STRICTER, AND THAT CAN REFUSE A RIGHT CODE. Chrysler PS3
    is "Sapphire Silver", and a car with it was registered blue by DVLA: that
    name is now read as silver alone. A refusal costs a free answer (the
    search goes on without it); a wrong pass sends someone the wrong paint.
    """
    words = [w for w in re.sub(r'[^a-z]+', ' ', (text or '').lower()).split()
             if w in _COLOUR_FAMILY]
    plain = {_COLOUR_FAMILY[w] for w in words if w not in _GEM_AND_METAL_WORDS}
    return plain or {_COLOUR_FAMILY[w] for w in words}


def _is_colour_word_not_code(make, code):
    """paint197 (audit #3, P3): a colour word delivered as a paint code.

    'Nero (Black)' reached the customer as code BLACK. The bracket rule takes
    a single word in brackets as a code; only finish words (paint104) and
    phrases of two or more words (paint184) were refused. 'Bianco (White)',
    'Grigio (Silver)' and 'Rosso (Pearl-Red)' the same.

    A single word of letters (a hyphen joins words, a slash joins parts) that
    names a colour family is the colour's NAME, UNLESS the catalogue holds it
    as a code FOR THIS MAKE. Measured, not guessed: of 120,594 catalogued
    codes, 17 are also colour words, among them Kia BLA, Mitsubishi OR and
    Tesla ONYX, and a make-blind rule would refuse every one. Exempting any
    catalogued code fails the other way: five classic British makes list RED,
    so a Ferrari's '(Red)' would pass. Per make gets both right.
    """
    c = (code or '').strip()
    if not c:
        return False
    parts = [p.strip() for p in c.split('/')]
    if not all(p and ' ' not in p and p.replace('-', '').isalpha()
               and _colour_families(p) for p in parts):
        return False
    from lookup.models import PaintLookup
    mfr = PaintLookup.normalize_manufacturer(make)
    return not any(PaintLookup.objects.filter(manufacturer=mfr, code__iexact=x).exists()
                   for x in [c] + parts)


def _is_finish_word_not_code(make, code):
    """paint200 (old list #6): a FINISH word delivered as a paint code.

    Reproduced in production on 24 Sep: in the previous 30 days a Ford customer
    was handed METALLIC as their paint code. The Ezyvin and One Auto adapters
    refuse finish words at the bracket (paint104); every other leg could pass
    one straight through, because nothing between the legs and the customer
    asked the question.

    A code made ONLY of finish words (normalize_name empties it: METALLIC,
    MICA, MATT, SOLID, PEARL, GLOSS, PEARL METALLIC) says how a paint looks,
    not which paint it is. Real codes keep a word normalize_name does not
    drop (PN3BG, KTA). As with P3, a make whose catalogue holds the word as a
    code keeps it.

    paint302: AND THE WORDS VDG'S CLIENT REFUSES, WHOEVER SENDS THEM. vdg.py
    has a list of its own (_FINISH_WORD_CODES, 17 words) that it applies to
    VDG's answers alone. Five of those words are not finish words to
    normalize_name, so from pl24, Ezyvin or mmw they passed as a code of the
    provider's own, which ends the search: STANDARD, METAL, BASECOAT,
    NON-METALLIC and NONMETALLIC (the audit of 8 Oct, reproduced). None has
    arrived yet: of that list only METALLIC is in five months of lookups, and
    this rule already refused it. The list is read here the way VDG's client
    reads it, the whole word in any case, so there is one list and every
    provider's answer meets it. A make whose catalogue holds one of the words
    as a code would keep it, as above; none does.
    """
    c = (code or '').strip()
    if not c or not c.replace(' ', '').replace('-', '').isalpha():
        return False
    from lookup.models import PaintLookup
    if PaintLookup.normalize_name(c) and c.upper() not in _FINISH_WORD_CODES:
        return False
    mfr = PaintLookup.normalize_manufacturer(make)
    return not PaintLookup.objects.filter(manufacturer=mfr, code__iexact=c).exists()


#: paint302: the makes whose codes mmw writes with two extra digits.
_MMW_PADDED_MAKES = frozenset({'volvo', 'polestar'})


def mmw_unpadded_code(make, code):
    """paint302: mmw's code for a Volvo or a Polestar without the two extra
    digits it writes: "71700" is 717. Any other code, and any other make's,
    comes back as it was given.

    Measured on the lookups to 7 Oct: 16 Volvo and Polestar lookups hold a
    code from mmw beside the code that was given. In 7 the two are the same
    three digits. In the other 9, mmw's is those three digits and "00" (71700
    three times, 74000 twice, 72000, 73500, 73900, and 36800 on a Polestar).
    All 9 were recorded as mmw disagreeing with the answer, and the colour
    check would have thrown each of them away had mmw's code been needed: the
    catalogue holds 717, not 71700. (It has not been needed yet: of the 49
    paint searches on these two makes 3 ended without a code, and mmw had
    none for those.)

    ONLY FIVE DIGITS ENDING 00, AND ONLY THESE TWO MAKES, because that is all
    the evidence covers. Both callers try the code as sent FIRST: Volvo's own
    91300, a truck colour, is five digits ending 00 and still answers as
    itself.
    """
    from lookup.models import PaintLookup
    c = str(code or '').strip()
    if (len(c) == 5 and c.isdigit() and c.endswith('00')
            and PaintLookup.normalize_manufacturer(str(make or '')) in _MMW_PADDED_MAKES):
        return c[:3]
    return code


def mmw_code_validates(make, code, dvla_colour):
    """Should mmw's code be trusted enough to serve?

    paint140. mmw scrapes a third-party site of unknown provenance, so its
    answer is never served unverified. The check: does OUR catalogue's name for
    that code describe the same KIND of colour the car is registered as.

    `[car 28]` on 14 Sep is why this exists. mmw returned Z9Y for an Audi A3
    registered BLACK:

        Z9Y   Dark Grey Matt        no hex, no models, 1 source
        LZ9Y  Phantom Black Pearl   #0D0F13, 3 sources, 51 models

    The bare code is a GREY on a BLACK car. The prefixed one is black. So the
    registered colour picks the right row, and this returns the code that
    actually matched rather than the one mmw sent.

    NOTE mmw's own colour field is NOT used. It returns the DVLA-style word
    (GREY, BLACK), confirmed live across three vehicles, so comparing it to the
    registered colour would compare DVLA against DVLA and prove nothing.

    Returns the code to use, or None. UNKNOWN IS NOT APPROVAL: a code absent
    from the catalogue, or a colour naming no family, returns None — the whole
    point is corroboration, and there is none.
    """
    from lookup.models import PaintLookup
    if not code or not make:
        return None
    want = _colour_families(dvla_colour)
    if not want:
        return None

    # paint302: A SLASH STRING IS RESOLVED FIRST, BY THE SLASH RULE. mmw joins
    # some codes ("7236/BRQA", "2431C/2PJE/ZJNC": 4 different strings in 24 of
    # the 832 lookups it had a code for, to 7 Oct). The string used to be
    # handed whole to lookup(), which split it and answered with the first
    # half it knew, while the half GIVEN was chosen afterwards by
    # resolve_slashed_code: the colour could be checked on one half and
    # another given. lookup() no longer splits (see there). The rule picks the
    # half here, and that half is what is checked and what is returned (it was
    # the string as sent). A string the rule leaves alone, two halves that are
    # different paints or none it knows, finds no row below and is refused:
    # Skoda "F9R/F9E" on a black car passed on its first half, Black Magic,
    # though F9E is Candy White and the rule cannot tell which was meant.
    if '/' in code:
        code = resolve_slashed_code(make, code, dvla_colour)

    # Try the code as sent, then the notations the catalogue uses. mmw returns
    # whatever the SITE holds, and that differs from the catalogue in at least
    # two ways — refusing to look further rejects correct answers on
    # punctuation.
    #
    #   PREFIX   mmw sends A7N;      the catalogue has LA7N
    #   HYPHEN   mmw sends NH-731P;  the catalogue has NH731P
    #
    # The hyphen case cost a real answer on 14 Sep: [car 23], a Honda CR-V this
    # pipeline had failed three times, came back NH-731P and was refused as
    # unknown. NH731P is in the catalogue as Crystal Black Pearl (#030405) on a
    # car registered BLACK — it would have validated. The catalogue is
    # inconsistent about this by nature: 778 of 2,080 Honda codes carry a
    # hyphen and the rest do not.
    #
    # Order matters: as-sent first, so an exact match is never passed over for
    # a variant.
    _bare = code.replace('-', '').replace(' ', '')
    # paint159: B0N is mmw's notation for the Stellantis E codes. Three live
    # deliveries agree — B0NPR twice, B0NZR once, all answered Exx by pl24 —
    # and EWP, EZR and EPR each exist across Vauxhall, Opel, Peugeot and
    # Citroen with consistent colours, against 847 E+2 codes in total.
    #
    # WEAKER EVIDENCE THAN THE OTHERS, AND DELIBERATELY SO. `TE` was confirmed
    # at 69 of 69 inside the catalogue; B0N appears in ZERO catalogue rows, so
    # it cannot be corroborated that way, and one counter-example exists
    # (B0N9V answered KTV, where neither E9V nor 9V is in the catalogue). What
    # makes it safe is that the colour check still has to pass afterwards: the
    # prefix only earns a row a hearing, never an answer.
    _variants = [code, _bare, f'L{code}', f'L{_bare}']
    if _bare.upper().startswith('B0N') and len(_bare) > 3:
        _variants.append('E' + _bare[3:])
    # paint302: a Volvo or Polestar code with mmw's two extra digits is tried
    # without them, after the code as sent (see mmw_unpadded_code). 71700 found
    # nothing, so the gate refused it on a black Volvo where it passes 717.
    _unpadded = mmw_unpadded_code(make, _bare)
    if _unpadded != _bare:
        _variants.append(_unpadded)
    for candidate in _variants:
        row = PaintLookup.lookup(make, candidate)
        if not row:
            continue
        # THE NAME FIRST, ALWAYS. A colour word the manufacturer wrote is
        # better evidence than a hex we classified, so the hex never overrides
        # it and never gets a vote when the name has one.
        # paint302: and a gem or a metal word in the name gives way to a plain
        # colour word beside it (see _gate_colour_families).
        named = _gate_colour_families(row.name) if row.name else set()
        if named:
            if named & want:
                return candidate
            # The name spoke and disagreed. Do NOT then ask the hex for a
            # second opinion — that is how a gate turns into a search for any
            # reason to say yes.
            continue
        # paint143: the name says nothing. Fall back to the hex, which is what
        # made [car 24]'s 'Molten Lava Pearl' refusable despite being #8E1F13 on
        # a car registered Red.
        if _hex_family(row.hex) in want:
            return candidate
    return None


def _mmw_has_code(f_mmw, make, vdg_colour):
    """paint273: True when mmw already holds a code the race would serve.

    The race only serves mmw's code at the end, after VDG and pl24, so a lookup
    mmw can answer used to wait for VDG's second call too: since 13 Sep it fired
    in all 85 lookups mmw answered, mmw having replied within 1-6s. With that
    call now waiting up to 23s, those customers would wait longer for the same
    answer. The colour check _mmw_settle makes, then the reading the end of the
    search gives that code (paint294, below). Never blocks.
    """
    if f_mmw is None or not f_mmw.done():
        return False
    try:
        row = f_mmw.result()
        code = row and row.get('code') and mmw_code_validates(make, row['code'], vdg_colour)
        if not code:
            return False
        # paint294: BY THE READING THE SEARCH WILL GIVE IT. Passing the colour
        # check is not the whole of it: at the end of the search mmw's code is
        # read by the refusal rules like any other answer. A special order
        # code is held back there, and a placeholder that slipped past the
        # colour check (a junk XXX row can carry a swatch) is refused.
        # Neither is a reason to skip the call that may find the real code.
        read = _enrich_from_lookup({'paint_code': code, 'paint_description': '',
                                    'all_paint_codes': [], 'source': 'mmw'},
                                   make, None, vdg_colour=vdg_colour)
        return _weigh(make, read) == 'code'
    except Exception:  # noqa: BLE001 — a free leg's answer must never break VDG's worker
        return False


#: paint294: how long the end of a search waits for mmw when mmw is the only
#: source still running. Measured on 1,045 answers since 14 Sep: half within
#: 1.5s, 99% within 4.6s, 3 over 8s. The search's own deadline still caps it.
MMW_SETTLE_WAIT_S = float(os.environ.get('MMW_SETTLE_WAIT_S', '8'))


#: paint298: when the reserve is about to be bought and a supplier's colour
#: name is in hand, mmw is given until THIS many seconds after the search
#: began to answer, so its code can be tested against that name. Not a wait
#: from the moment of asking: a search already past this mark does not wait
#: at all, and most are (mmw is asked at the very start and 99% of its
#: answers are back within 4.6s; VDG and pl24 are rarely both done sooner).
#: The cost, when mmw is slow or not answering and a name is in hand: the
#: reserve is asked up to this many seconds later than it used to be.
MMW_NAME_TEST_BY_S = float(os.environ.get('MMW_NAME_TEST_BY_S', '5'))


def mmw_code_matches_name(make, model, vdg_colour, mmw_code, names):
    """paint298, THE OPERATOR'S RULE FOR THE RESERVE. True when mmw's code may
    stand in for the paid reserve: a supplier named the colour, and our
    catalogue lists mmw's code under that name.

    WHY. The reserve (Ezyvin, 5 credits, about 45p a call) was bought whenever
    VDG and pl24 had both finished without a code of their own, before mmw's
    free answer was looked at. Measured on production, 22 Sep to 9 Oct: 259
    reserve calls, about 145 pounds a month. The operator does not trust mmw
    on its own word ("i don't fully trust mwm answers"), so the saving he
    chose is the narrow one, in his words: "if we have a name from vdg or pl24
    and code from mmw, compare if that code matches that name, if not then we
    do ezyvin and if that fails only then do mmw". Two witnesses, then: a
    supplier's NAME for this car, and mmw's CODE, joined by our catalogue.

    THE TEST, all of which must hold:
      1. `names`: what VDG or pl24 called the colour, having no code of their
         own for it. No name, no test. A name that is only a colour word
         ("Blue Metallic") is not one: it says no more than the registered
         colour does, which test 2 has already used, so it is no second
         witness. One of the 47 searches the rule first held on, replayed on
         the lookups of 22 Sep to 7 Oct, held on such a name alone (a Ford,
         "Blue Metallic"); there the reserve is bought as before.
      2. mmw's code passes the colour check every mmw answer has to pass
         before it is used (mmw_code_validates: our catalogue's row for it is
         the colour the car is registered as).
      3. Read by the refusal rules, it is a code to give (not a placeholder,
         not a special order code).
      4. The code that would be GIVEN (after the slash rules have chosen a
         half of "7236/BRQA"), or the notation of it the colour check
         matched, is among the codes the catalogue lists under one of the
         names (PaintLookup.codes_listed_under: the hand table, the
         catalogue's rows, the operator's own table).

    MEASURED ON PRODUCTION (measure298.py, read only, 9 Oct): the rule held in
    56 of the 259 searches that bought the reserve (46 with both suppliers
    empty, 10 at the backstop), about 43 pounds a month. In those 56 the
    reserve had answered with only a name 35 times, the same code 17 times,
    nothing twice and another code twice (Ford "Sea Grey (Metallic)": 6DYE
    here, PN3FV from the reserve). In 37 more, mmw's code passed the colour
    check and the catalogue did not list it under the name (Ford "Frozen
    White" and 7VTA, "Moondust Silver (Metallic)" and 2431C/2PJE/ZJNC): there
    the reserve is still bought, which is the rule doing its job.

    TWO THINGS STRICTER THAN WHAT WAS MEASURED, both on the side of buying the
    reserve as before. The script counted a match on ANY half of a slash
    string mmw sent; here the half that is given must be the one listed (in
    all 9 slash strings production saw, it was). And the script took a bare
    colour word as a name (test 1 above); an independent review of this
    change found that one, on the replay.

    Never raises: any failure is "no match", and the reserve is bought.
    """
    try:
        from lookup.models import PaintLookup
        names = [n.strip() for n in (names or ()) if isinstance(n, str) and n.strip()
                 and not is_bare_colour_name(n.strip())]
        raw = (mmw_code or '').strip() if isinstance(mmw_code, str) else ''
        if not names or not raw or not make:
            return False
        checked = mmw_code_validates(make, raw, vdg_colour)
        if not checked:
            return False
        read = _enrich_from_lookup({'paint_code': checked, 'paint_description': '',
                                    'all_paint_codes': [], 'source': 'mmw'},
                                   make, model, vdg_colour=vdg_colour)
        if _weigh(make, read) != 'code':
            return False
        ours = {PaintLookup.code_key(checked), PaintLookup.code_key(read.get('paint_code'))} - {''}
        return any(ours & PaintLookup.codes_listed_under(make, name) for name in names)
    except Exception:  # noqa: BLE001 - a test that fails must cost a reserve call, not a search
        logger.warning('mmw name test failed', exc_info=True)
        return False


def _mmw_settle(f_mmw, make, vdg_colour, telemetry, wait=0.0):
    """Read mmw's held answer. Records agreement; returns a usable code or None.

    paint140, corrected in paint195. Called in ONE place: at the end, where a
    validated code may be returned and served.

    It used to take a `delivered_code` and record whether mmw agreed. Nothing
    ever passed one, and the agreement it computed was an older, weaker copy
    of the check in views._record_paint_hit, which also handles hyphens
    (B-570M against B570M), the L and TE prefixes, Stellantis B0N codes and
    Ford suffixes. The battery was testing the dead copy. Removed, so a
    future fix cannot land in it and pass while changing nothing.

    WAITS ONLY AS LONG AS IT IS TOLD TO (paint294). This never blocked, on the
    reasoning that mmw started at t=0 and everything else had finished by now.
    That holds for an ordinary search and fails for the quickest ones: a car
    with no usable VIN asks neither pl24 nor Ezyvin, VDG's paint call for it
    comes back in a fraction of a second, and the search ended "not found"
    while mmw, which needs about a second and a half, was still on its way.
    Since 14 Sep, 9 of the 18 searches on such cars finished within 0.4s. The
    caller now passes how long it will wait; with wait=0 this is as it was.
    """
    if f_mmw is None:
        return None
    try:
        row = f_mmw.result(timeout=max(0.0, wait or 0.0))
    except Exception:  # noqa: BLE001 (not back in time, or a free leg that raised)
        return None
    if not row or not row.get('code'):
        return None

    validated = mmw_code_validates(make, row['code'], vdg_colour)


    if validated and telemetry is not None:
        telemetry['mmw_used'] = True
    return validated


def _mmw_lookup(registration, search_id=None):
    """Call the mmw service. Returns its row dict, or None on any failure.

    paint140. UNLIKE EVERY OTHER LEG, THIS NEEDS ONLY THE REGISTRATION. pl24
    and Ezyvin both need the VIN, which arrives from the £0.06 VDG vehicle
    call, so they cannot start until it returns. mmw can start at t=0.

    Free, so there is no spend guard and no budget check: the only cost of
    calling it is someone else's bandwidth, which the service paces.

    Never raises. A leg that cannot make things worse must not be able to break
    a lookup either.
    """
    if not registration:
        return None
    headers = {'X-API-Key': MMW_API_KEY} if MMW_API_KEY else {}
    try:
        resp = get_session().get(
            f'{MMW_BASE_URL}/lookup-paint',
            params={'reg': registration}, headers=headers,
            timeout=_MMW_HTTP_TIMEOUT,
        )
    except requests.exceptions.RequestException:
        return None
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None

    row = {
        'code': (data.get('paint_code') or '').strip()[:100],
        'colour': (data.get('paint_description') or '').strip()[:60],
        'outcome': (data.get('outcome') or '').strip()[:40],
        'ms': int((data.get('elapsed_s') or 0) * 1000),
    }
    # Recorded whether or not the answer is ever used — that is the entire
    # reason the columns exist. A leg used last still teaches you its accuracy.
    if search_id is not None and (row['code'] or row['outcome']):
        _record_worker_result(
            search_id,
            mmw_attempted=True,
            **({'mmw_code': row['code']} if row['code'] else {}),
            **({'mmw_colour': row['colour']} if row['colour'] else {}),
            **({'mmw_outcome': row['outcome']} if row['outcome'] else {}),
            mmw_ms=row['ms'],
        )
    return row


def _pl24_lookup(vin, make, category=None, search_id=None):
    """Call the pl24 service. Returns a paint dict if pl24 found a code, else
    None. Never raises — network/HTTP/timeout errors degrade to None."""
    # paint213: EVERY CALL NOW LEAVES A REASON ON THE ROW. 25 pl24 calls
    # since 14 Aug ended with no outcome recorded, and they were read as pl24
    # being busy. They were not: pl24 queues a busy request rather than
    # refusing it, and says why any call failed (its HTTP status, `outcome` and
    # `error`). coloureg simply never kept the reply unless it was a 200, and
    # never recorded its own timeouts. 24 of the 25 were cars with no real VIN.
    vin = (vin or '').strip()
    if not make or not vin:
        # Nothing to send; the row says so.
        _record_worker_result(search_id, pl24_outcome='client_skipped',
                              pl24_error='no make' if not make else 'no VIN')
        return None
    # paint214: A SHORT VIN IS SENT, AND PL24 DECIDES. paint213 stopped sending
    # anything but 17 characters (42 such calls ever, 0 answers), but what
    # partslink24 will take is pl24's call, not coloureg's: its classic
    # catalogues may accept a chassis number, now or later. A refusal is fast
    # and costs nothing, and pl24's reply is now kept (status, outcome, error),
    # so the day pl24 starts answering one, the rows will show it.
    params = {'vin': vin, 'make': make}
    if category:
        params['category'] = category
    headers = {'X-API-Key': PL24_API_KEY} if PL24_API_KEY else {}
    _started = time.monotonic()
    for _attempt in (1, 2):
        try:
            resp = get_session().get(
                f'{PL24_BASE_URL}/lookup-paint',
                params=params, headers=headers, timeout=_PL24_HTTP_TIMEOUT,
            )
            break
        except requests.exceptions.RequestException as exc:
            # paint296: ONE CLAUSE, AND THE KIND IS READ FROM THE FAILURE.
            # There were three: Timeout, ConnectionError, anything else. On the
            # shared session a read timeout is a ConnectionError (see
            # http.failure_kind), so pl24 taking the full 60 seconds was stored
            # as "client_connection_error", the name a refused connection has.
            # Each case now gets its own name and says what happened.
            _kind = failure_kind(exc)
            _cause = type(failure_chain(exc)[-1]).__name__
            _connect_s, _read_s = _PL24_HTTP_TIMEOUT
            if _kind == 'timeout':
                # coloureg's own timeout (PL24_TIMEOUT), shorter than pl24's 120s, so
                # pl24's 504 and its reason can never arrive in this case.
                _record_worker_result(
                    search_id, pl24_outcome='client_timeout',
                    pl24_error=(f'{_cause}: no reply, coloureg gave up after '
                                f'{_read_s:g}s')[:200])
                return None
            if isinstance(exc, requests.exceptions.ConnectTimeout):
                # Connecting took the whole connect allowance. Not retried: the
                # retry is for a failure that comes back at once.
                _record_worker_result(
                    search_id, pl24_outcome='client_timeout',
                    pl24_error=(f'{_cause}: could not connect within '
                                f'{_connect_s:g}s')[:200])
                return None
            if not isinstance(exc, requests.exceptions.ConnectionError):
                _record_worker_result(search_id, pl24_outcome='client_error',
                                      pl24_error=type(exc).__name__[:200])
                return None
            # paint258: ONE QUICK RETRY after a connection that failed fast. pl24
            # is our own service on Railway's private network; a failed
            # connection there is a restart or a dropped socket, and it cost
            # three customers their answer in a fortnight (29 Sep: a Punto,
            # with Ezyvin timing out too). Not the session-level retry that F12
            # forbids: one explicit, logged attempt, only for pl24 (free, ours),
            # only for a fast failure, never for a timeout.
            if _attempt == 1 and time.monotonic() - _started < _PL24_RETRY_WITHIN_S:
                logger.warning('pl24 connection failed (%s); retrying once', type(exc).__name__)
                time.sleep(_PL24_RETRY_PAUSE_S)
                continue
            # paint296: the cause by name (it always said "ConnectionError").
            _record_worker_result(
                search_id, pl24_outcome='client_connection_error',
                pl24_error=(_cause + (' (after one retry)' if _attempt == 2 else ''))[:200])
            return None
    # paint144: READ THE BODY BEFORE GIVING UP ON THE STATUS. pl24 puts `slot`
    # on its 502 and 504 bodies too, and that is where it is most informative —
    # a failure tells you nothing until you know WHICH session failed. Bailing
    # on the status code alone threw exactly that away.
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}                       # a list or a string must not raise here
    _err = str(data.get('error') or '').strip()[:200]
    _slot = data.get('slot')
    _via = (data.get('via') or '').strip()[:40]
    # `is not None`, NOT truthiness: slot 0 is the first account, and `if
    # _slot:` would silently drop every answer from it.
    if search_id is not None and (_slot is not None or _via):
        _record_worker_result(
            search_id,
            **({'pl24_slot': _slot} if isinstance(_slot, int)
               and not isinstance(_slot, bool) else {}),
            **({'pl24_via': _via} if _via else {}),
        )
    if resp.status_code != 200 or not data:
        # paint213: a refusal or failure says why; keep it. pl24's own outcome
        # when the body carries one, else the status itself.
        _record_worker_result(
            search_id, pl24_http_status=resp.status_code,
            pl24_outcome=(str(data.get('outcome') or '').strip()
                          or ('empty_reply' if resp.status_code == 200
                              else f'http_{resp.status_code}'))[:40],
            **({'pl24_error': _err} if _err else {}))
        return None
    code = (data.get('paint_code') or '').strip()
    desc = (data.get('paint_description') or '').strip()

    # Record what pl24 found regardless of whether this answer gets used
    # (paint26). When the VDG retry wins the race, resolve_paint has already
    # returned by the time we get here and nothing would otherwise keep this.
    # Compare against paint_code afterwards to see whether the two sources
    # agree; vdg_retry_code covers the mirror case.
    # Record the outcome REGARDLESS of whether a code came back — a failure's
    # reason is the whole point of storing it (paint65). Capped at the column
    # width because _record_worker_result writes via .update(), which bypasses
    # Model.save() and its truncation guard.
    outcome = (data.get('outcome') or '').strip()[:40]
    name = (data.get('paint_description') or '').strip()[:120]
    if search_id is not None:
        _record_worker_result(search_id, pl24_http_status=200,
                              **({'pl24_code': code[:100]} if code else {}),
                              **({'pl24_name': name} if name else {}),
                              **({'pl24_outcome': outcome} if outcome else {}),
                              **({'pl24_error': _err} if _err else {}))
    # Keep the result if pl24 returned EITHER a code OR a colour name. The
    # name-only case (code == '' but desc set) covers brands partslink24 carries
    # a colour name but no code for (Ford passenger, Jaguar, older Land Rover,
    # some Kia) — pl24's `name_only` outcome. It's a confident match (pl24 read
    # the right vehicle's colour row; the brand just has no code field), so the
    # name is worth surfacing even without a code. Only a result with neither is
    # a true miss.
    if not code and not desc:
        return None
    name_only = (code == '' and bool(desc))
    return {
        'source': 'pl24',
        'paint_code': code,                 # may be '' for name-only
        'paint_description': desc,
        'name_only': name_only,
        # pl24 returns one code; keep the shape consistent with VDG's list form.
        # A name-only result has no code, so it contributes no code block.
        'all_paint_codes': [{
            'code': code,
            'description': desc,
        }] if code else [],
        'via': data.get('via', ''),
    }


def _oneauto_leg(vin, make, model, year, search_id, sink, race_over=None):
    """Run the One Auto call and WRITE ITS OWN COST, whoever wins the race.

    Same problem and same fix as _record_worker_result for pl24 (paint26):
    resolve_paint returns the instant any path produces a code, so a leg that is
    still in flight finishes AFTER the caller has read its telemetry and saved.
    Anything it learned — including what it SPENT — is lost unless the worker
    writes it itself.

    That mattered immediately. [car 13], the first BMW through the new pool, had
    pl24 answer in 1.64s while One Auto needs ~6s; the row recorded
    oneauto_cost NULL even though the call was made. An unrecorded charge is
    invisible to the daily budget breaker, which is the one thing standing
    between a bug and a £30 day.

    Best-effort throughout: recording an observation must never break a lookup
    a customer is waiting on.
    """
    result = oneauto.lookup(
        vin=vin, make=make, model=model, year=year,
        search_id=search_id, cost_sink=sink,
    )
    # SECOND CHANCE (paint73) — a COLLECTION rather than a retry. 'still_fetching'
    # means their job was running server-side when we stopped polling, and One
    # Auto hold a result for 24 hours. [car 22] recorded still_fetching in
    # coloureg and then answered in 685ms on the very next call.
    #
    # ONLY on still_fetching. A 206 is a settled "no data" and a 200 has already
    # answered; asking again would spend time on a question already decided.
    # Measured to be free: billing is per VIN, not per call — a repeat call on
    # the same VIN moved the balance not at all.
    if result is None and sink.get('outcome') == 'still_fetching':
        # Recorded for the same reason as VDG's, though this one is FREE —
        # One Auto bills per VIN, not per call. Worth measuring anyway: if it
        # rarely collects anything, the wait it adds is not buying much.
        from lookup.models import Search   # lazy: circular import at module load
        oa_second = Search.SECOND_CHANCE_EMPTY
        oa_after_race = bool(race_over is not None and race_over.is_set())
        try:
            again = oneauto.lookup(
                vin=vin, make=make, model=model, year=year,
                search_id=search_id, cost_sink=sink,
                budget=SECOND_CHANCE_S,
            )
        except Exception:  # noqa: BLE001 — a second chance must never raise
            again = None
        if again:
            logger.info('One Auto second chance collected a result')
            result = again
            oa_second = Search.SECOND_CHANCE_WON
        if search_id is not None:
            _record_worker_result(search_id, oneauto_second_chance=oa_second)
            if oa_after_race:
                _record_worker_result(search_id, second_chance_after_race=True)
    if search_id is not None:
        fields = {}
        if sink.get('cost') is not None:
            fields['oneauto_cost'] = sink['cost']
        if sink.get('outcome'):
            fields['oneauto_outcome'] = str(sink['outcome'])[:40]
        # What it SAID, win or lose. A losing answer is what makes source
        # comparison possible, and a name we cannot resolve today may resolve
        # once the table grows.
        if result:
            if result.get('code'):
                fields['oneauto_code'] = str(result['code'])[:100]
            if result.get('description'):
                fields['oneauto_name'] = str(result['description'])[:120]
        if fields:
            _record_worker_result(search_id, **fields)
    return result


# paint255: a real VIN: 17 characters, digits and capital letters except I, O
# and Q. Measured across all lookups to 2 Oct: 36 had anything else (13-character
# Japanese chassis numbers, short numbers on classics); pl24 refused 6, Ezyvin
# answered "not found" 12 times and timed out once after a minute, and not one
# got a code from either. mmw works from the registration and still runs.
_REAL_VIN = re.compile(r'[A-HJ-NPR-Z0-9]{17}')


def vin_is_real(vin):
    return bool(_REAL_VIN.fullmatch((vin or '').strip().upper()))


def _weigh(make, answer):
    """paint294: what a provider's answer is worth once the refusal rules
    (_enrich_from_lookup) have read it. Decides whether the search may stop.

        'code'     a code of the provider's own. The search stops; this is the
                   answer.
        'special'  a special order code (marks_special_order). True, and no
                   use if another source can say which paint it was: held.
        'name'     a colour name, with or without a code OUR catalogue worked
                   out from it (enriched_from 'code'). Held: a provider's own
                   code for this very car is better than a match on a name.
        ''         nothing usable: empty, or refused (a placeholder, an
                   interior). The search carries on as if nothing had arrived.
    """
    if not answer or answer.get('placeholder_refused'):
        return ''
    code = (answer.get('paint_code') or '').strip()
    if code and marks_special_order(make, code):
        return 'special'
    if code and answer.get('enriched_from') != 'code':
        return 'code'
    if code or (answer.get('paint_description') or '').strip():
        return 'name'
    return ''


def _ezyvin_leg(vin, sink, race_over, budget, search_id):
    """Run the reserve and WRITE WHAT IT LEARNED TO THE ROW, whoever wins.

    paint294. The same problem and the same fix as _oneauto_leg and pl24's own
    worker (paint26): the search returns the moment a source has a code, so a
    reserve still in flight finishes after the caller has read its telemetry
    and saved. Its outcome and its credits stayed in a dict nobody read again.
    Measured: 16 of the 356 times the reserve was started, the row shows no
    outcome and no credits (15 were started by the backstop while a slow leg
    was about to answer). Up to 80 credits the dashboard never counted.

    The caller still copies the same values when it is there to read them; it
    writes them only when it has them, so it cannot blank what is written here.
    Best-effort throughout: recording must never break a search.
    """
    result = None
    try:
        result = ezyvin.lookup(vin, sink, race_over, budget)
    except Exception:  # noqa: BLE001 - a reserve that raises is a miss, and the row says so
        logger.warning('Ezyvin leg failed', exc_info=True)
        if not sink.get('outcome'):
            sink['outcome'] = 'client_error'
    if search_id is not None:
        fields = {}
        if sink.get('credits') is not None:
            fields['ezyvin_credits'] = sink['credits']
        if sink.get('outcome'):
            fields['ezyvin_outcome'] = str(sink['outcome'])[:40]
        if result:
            if result.get('code'):
                fields['ezyvin_code'] = str(result['code'])[:50]
            if result.get('description'):
                fields['ezyvin_name'] = str(result['description'])[:200]
        if fields:
            _record_worker_result(search_id, **fields)
    return result


def resolve_paint(registration, vin, make, category=None, telemetry=None, model=None,
                  search_id=None, vdg_colour=None, year=None):
    """Race the VDG bundle-retry and the pl24 scrape; return the first usable
    paint result, or None if neither recovers a code.

    Returns a dict on success:
        {'source': 'vdg_retry'|'pl24', 'paint_code', 'paint_description',
         'all_paint_codes', ...}
    or None if no paint could be recovered by either path.

    Optional telemetry: if a dict is passed, it is populated (in place) with what
    each path did, for logging on the Search row:
        {'recovery_attempted': True,
         'vdg_retry_returned': bool,   # did the 2nd VDG call return paint?
         'pl24_attempted': True,       # pl24 is always queried in the race
         'pl24_returned': bool,        # did pl24 return a usable CODE?
         'pl24_name_only': bool,       # did pl24 return a name but NO code?
         'duration_ms': int}           # wall-clock time of the recovery
    The return value is unchanged whether or not telemetry is supplied.

    Preference / ordering of results (strongest first):
      1. A real CODE wins. When both paths produce a code, the VDG-retry result
         wins (cheaper, already paid for). This holds even if both futures
         complete in the same wait() batch — we inspect the VDG future before the
         pl24 future within a batch, not relying on set-iteration order.
      2. Anything less is a FALLBACK, whichever source sent it (paint294; this
         was true of pl24's name-only answer alone): a colour name with no
         code, a code our catalogue worked out from such a name, a special
         order code. It is held aside and returned ONLY if no source produces a
         real code before the deadline. A late real code must still be able to
         beat it, so a fallback does not short-circuit the wait. One exception:
         once VDG's name gives a code, a source still silent at the reserve's
         backstop is no longer waited for (see the top of the loop). The order
         among fallbacks is set out where the loop ends.
         paint298: and the paid reserve is not bought to improve on a fallback
         when mmw's code is listed in our catalogue under the colour name VDG
         or pl24 gave (mmw_code_matches_name, the operator's rule). With both
         suppliers finished that code is the answer; at the backstop it ends
         the search, as VDG's name code does.
      3. An answer the refusal rules reject (a placeholder, an interior) is no
         answer at all, and the search carries on (paint294).
      4. Otherwise None (a true miss).

    Timeout: the total wait is hard-bounded by PL24_TIMEOUT. We deliberately do
    NOT use the ThreadPoolExecutor as a context manager, because its __exit__
    blocks until all worker threads finish — which would let a slow/hung pl24
    thread make this function hang far past the timeout. Instead we wait on the
    futures with an explicit deadline, then shut the executor down WITHOUT
    waiting (wait=False, cancel_futures=True), abandoning any straggler. The
    abandoned pl24 thread's HTTP request has its own timeout and ends on its own.
    """
    # THREE workers for three legs (paint83). It was 2 when the pool held only
    # the VDG retry and pl24; a third leg made it three, and a third leg in a
    # two-worker pool does not run — it QUEUES.
    #
    # paint95 replaced One Auto with Ezyvin as that third leg, so the COUNT is
    # unchanged and the RELATIONSHIP is what matters: max_workers >= the number
    # of ex.submit sites below. Asserting the literal 3 would pass while a
    # fourth leg reintroduced the starvation this fixed.
    #
    # [car 16] showed exactly that: One Auto polled to its 30s budget while pl24,
    # submitted by the backstop at 10s, sat waiting for a free worker. It only
    # started once One Auto released one, and the lookup took 36.7s to return a
    # code pl24 could have supplied in about one. The backstop had fired
    # correctly; there was simply nothing to run it on.
    # paint140: 4, not 3 — mmw is a fourth leg. The RELATIONSHIP is what
    # matters (see above): max_workers >= the number of legs, or a leg silently
    # queues behind another and its timing measurements become fiction.
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=4)
    _t = telemetry if telemetry is not None else {}
    _start = time.monotonic()
    # Recovery telemetry, finalised in the `finally` block so it is written no
    # matter which return path (or timeout) we take. pl24 and vdg-retry are both
    # always submitted, so "attempted" is True for both once we get here.
    _t['recovery_attempted'] = True
    _t['vdg_retry_returned'] = False
    # pl24 is no longer started unconditionally, so 'attempted' is set when it
    # actually is (paint68).
    _t['pl24_attempted'] = False
    _t['pl24_started_because'] = ''
    _t['pl24_returned'] = False
    _t['pl24_name_only'] = False
    # paint95: Ezyvin replaces One Auto here. The oneauto_* keys are still
    # written, as False/None/'' — the Search columns and every admin chart read
    # them, and dropping them mid-flight would blank historical comparisons
    # rather than showing the leg stopping.
    _t['oneauto_attempted'] = False
    _t['oneauto_returned'] = False
    _t['oneauto_name_only'] = False
    _t['oneauto_cost'] = None
    _t['oneauto_outcome'] = ''
    _t['ezyvin_attempted'] = False
    _t['ezyvin_returned'] = False
    _t['ezyvin_name_only'] = False
    _t['ezyvin_credits'] = None
    _t['ezyvin_outcome'] = ''
    _t['ezyvin_started_because'] = ''
    # paint182: what it SAID, not just whether it said anything.
    _t['ezyvin_code'] = ''
    _t['ezyvin_name'] = ''
    # RACE FLAG. Set the moment a usable code is found, so a second chance that
    # fires afterwards can record that it did. Measurement only right now — it
    # cancels nothing, because nothing here CAN be cancelled: an HTTP call
    # already in flight completes and is billed whatever a flag says (paint21).
    #
    # What it makes answerable is whether a race-over flag is worth building at
    # all. The only call it could ever prevent is a second chance not yet
    # started, and until this field exists there is no way to know how often
    # that happens.
    race_over = threading.Event()
    try:
        # paint140: mmw starts HERE, at t=0, before anything waits on the VIN.
        #
        # It is the only leg that needs just the registration — pl24 and Ezyvin
        # both need the VIN from the £0.06 vehicle call. It is free, so there
        # is nothing to weigh against starting it early, and it answers in
        # about a second.
        #
        # ITS ANSWER IS HELD, NOT RACED. Everything below can beat it: the
        # order of preference is VDG, pl24, Ezyvin, then mmw. That is a trust
        # ordering, not an economic one — mmw is free and Ezyvin costs 45p, so
        # every lookup where mmw was right and Ezyvin was called is money spent
        # to avoid a scraper. Whether that ordering is correct is exactly what
        # mmw_agreed is recorded to find out.
        # Set on the MAIN THREAD at submit time, exactly as pl24 does in
        # _start_pl24. _mmw_lookup also records it via _record_worker_result,
        # but that is a direct DB write from a worker thread — it never reaches
        # this dict, so a caller reading telemetry would see the leg as never
        # attempted. Two channels reporting the same fact must agree.
        _t['mmw_attempted'] = True
        f_mmw = ex.submit(_mmw_lookup, registration, search_id)

        f_vdg = ex.submit(_vdg_retry, registration, _t, search_id, race_over,
                          f_mmw, make, vdg_colour)      # paint273: mmw's code counts
        # Category is routed (not raw): VW commercial lines misfiled as M1 by
        # VDG are sent to pl24 as N1 so the lookup hits the right catalogue
        # first time. See _route_category.
        # pl24 is NOT started here (paint68). It is the one source we do not own
        # — a partslink24 subscription with a browser session that can be locked
        # out, as it was on 13 Aug when amended T&Cs blocked the login and four
        # lookups failed until a human logged in by hand. So it is held back as
        # REINFORCEMENT and started only when a paid leg has already dropped
        # out, which keeps it on a fraction of lookups instead of all of them.
        #
        # Holding it back costs nothing in the common case because the two paid
        # legs start immediately and failure is FAST while success is slow: a
        # One Auto 206 lands in ~6s and a VDG paint miss refunds quickly, while
        # a VDG paint HIT can take 10-26s. So "one has dropped out" is a signal
        # that arrives early — exactly when reinforcement is useful.
        f_pl24 = None

        def _start_pl24(reason):
            """Bring pl24 into the race. Idempotent."""
            nonlocal f_pl24
            if f_pl24 is not None:
                return None
            # paint255: pl24 reads partslink24 by VIN, and refuses anything that
            # is not a real 17-character VIN (a Japanese import's chassis number,
            # a classic's short number). Not asked at all for those.
            if not vin_is_real(vin):
                _t['pl24_outcome'] = 'skipped_bad_vin' if (vin or '').strip() else 'skipped_no_vin'
                return None
            _t['pl24_attempted'] = True
            _t['pl24_started_because'] = reason
            # Make AND category are both routed (not raw) at this boundary. The
            # Search row keeps VDG's originals either way — this rewrite applies
            # solely to what pl24 receives.
            f_pl24 = ex.submit(
                _pl24_lookup, vin, route_make(make),
                _route_category(make, model, vin, category),
                search_id,
            )
            return f_pl24

        # paint96: STARTED IMMEDIATELY, alongside VDG.
        #
        # paint68 held it back as reinforcement — summoned by a paid leg
        # dropping out, or a 10s backstop — because it is the one source we do
        # not own: a partslink24 subscription whose session can be locked out,
        # as it was on 13 Aug when amended T&Cs blocked the login and four
        # lookups failed until a human logged in by hand. Holding it back kept
        # it on roughly 73% of lookups instead of all of them.
        #
        # That is now traded the other way. pl24 wins 52% of non-VAG
        # deliveries and is FREE, so every second it spends waiting for VDG to
        # fail is a second the customer waits for nothing. Starting it at zero
        # also pulls the reserve's both-empty trigger forward by about VDG's
        # median 4.5s, which is worth more than any backstop tuning.
        #
        # THE COST IS LOAD: ~37% more calls on the supplier we control least.
        # If partslink24 ever rate-limits or locks out, this is the change that
        # caused it. Reverting is one line — delete this call and the drop-out
        # triggers below come back to life on their own.
        _start_pl24('immediate')
        # paint255: recorded up front, because with pl24 skipped the "both empty"
        # trigger never fires, so Ezyvin's own refusal might never be reached.
        # paint294: and "no VIN at all" is recorded as well. Three searches
        # since 2 Oct left the reserve's column blank for exactly that reason.
        if not vin_is_real(vin):
            _t['ezyvin_outcome'] = 'skipped_bad_vin' if (vin or '').strip() else 'skipped_no_vin'

        # THIRD LEG (paint95) — EZYVIN, AND IT IS NOT IN THE RACE.
        #
        # One Auto used to sit here, started unconditionally alongside VDG. It
        # is gone. Measured over 24 days: it won 27 of 621 deliveries, 16 of
        # those were answers another leg also produced, and on 208 VAG lookups
        # it was the only source ZERO times. £115.20 spent, £102.30 of it on
        # calls whose answers were discarded.
        #
        # Ezyvin is the replacement and it is HELD BACK, for a different reason
        # than pl24 is. pl24 can start on a single drop-out because it is free.
        # Ezyvin charges 5 credits for any 200 — including one carrying no
        # colour — and in 44% of deliveries one leg comes back empty while the
        # other still delivers. Starting it on one drop-out would spend roughly
        # £188/month on answers that were already arriving.
        #
        # So the trigger is BOTH legs finished with no code. Measured on 18
        # recorded failures — the population that actually reaches here — it
        # answered 15, returned a free 404 on 3, and was charged-and-empty on
        # none: 75 credits, £8.25, for lookups the pipeline currently loses
        # outright.
        f_ezyvin = None
        def _start_ezyvin(reason):
            """Bring Ezyvin in. Idempotent."""
            nonlocal f_ezyvin
            if f_ezyvin is not None or not vin:
                return None
            # paint255: Ezyvin decodes the VIN too, so a chassis number or a
            # classic's short number cannot be answered; the Prius on 30 Sep
            # waited a minute for its timeout. Never started for those.
            if not vin_is_real(vin):
                _t['ezyvin_outcome'] = 'skipped_bad_vin'
                return None
            _t['ezyvin_attempted'] = True
            _t['ezyvin_started_because'] = reason
            # paint112: BUDGET = WHATEVER THE RACE HAS LEFT, not a fixed 20s.
            #
            # Ezyvin bills on SUBMISSION — the job runs on their side whether
            # or not we wait — so abandoning it early is pure loss: full price,
            # no code, and the customer gets the manual-lookup offer for a car
            # we already paid to identify.
            #
            # [car 36], a 2001 Fiat Punto on 10 Sep, is exactly that. The job
            # finished and returned a code when run by hand; the pipeline gave
            # up at 20s with roughly 20s of race deadline still unused, and was
            # charged 5 credits for nothing.
            #
            # The reserve starts LAST, so whatever remains is its natural
            # budget — there is nothing waiting behind it to protect. The floor
            # keeps a near-expired race from submitting a job it cannot
            # possibly hear back from, which would be the same waste again.
            _ez_budget = deadline - time.monotonic() - 0.5
            if _ez_budget < 3.0:
                _t['ezyvin_started_because'] = ''
                _t['ezyvin_attempted'] = False
                _t['ezyvin_outcome'] = 'skipped_no_time'
                f_ezyvin = None
                return None
            f_ezyvin = ex.submit(_ezyvin_leg, vin, _ez_sink, race_over,
                                 _ez_budget, search_id)
            return f_ezyvin
        _ez_sink = {}
        # A LATE BACKSTOP, deliberately. Its job is to catch a HUNG leg, not to
        # race: a leg that is merely slow will usually still answer, and buying
        # its answer from Ezyvin instead is money for nothing. Measured against
        # 24 days of deliveries, a backstop at 15s pre-empts 32% of them
        # (~£125/month) while one at 25s pre-empts 4% (~£15/month). Ezyvin adds
        # ~2.2s on a hit, so a 25s backstop still answers inside the 60s
        # deadline with room to spare.
        ezyvin_backstop_at = time.monotonic() + EZYVIN_BACKSTOP_S
        deadline = time.monotonic() + PL24_TIMEOUT
        pending = {f for f in (f_vdg, f_pl24) if f is not None}     # paint255: pl24 may be skipped
        # paint294: WHAT IS HELD WHILE THE SEARCH GOES ON, by source. An answer
        # that is not a code of the provider's own (a colour name, a code our
        # catalogue worked out from that name, a special order code) waits
        # here, and is given only if no source produces a code of its own.
        held = {}
        # paint294: the reserve has been started, or could not be. Either way
        # it is not tried again and its backstop stops shortening the waits.
        reserve_done = False
        # paint294: the VIN that VDG's paint call supplied, kept whichever
        # answer is given in the end (see _give).
        vdg_vin = ''
        # paint298: what VDG and pl24 CALLED the colour, as they sent it, when
        # they had no code of their own for it. The reserve rule tests mmw's
        # code against these (see _mmw_stands_in).
        said = {}

        def _result_or_none(fut):
            try:
                return fut.result()
            except Exception:  # noqa: BLE001  (any worker failure -> no paint)
                return None

        def _read(answer):
            """paint294: one answer as the refusal rules leave it, with what it
            is worth (see _weigh) and the notes the reading made for the row."""
            notes = {}
            read = _enrich_from_lookup(dict(answer), make, model,
                                       vdg_colour=vdg_colour, telemetry=notes)
            kind = _weigh(make, read)
            if kind == 'name':
                # A name with no code is a name-only answer WHOEVER sent it.
                # Only pl24's and Ezyvin's were marked; VDG's reached the page
                # as "found" with a blank code (two Fords on 12 Sep).
                read['name_only'] = not (read.get('paint_code') or '').strip()
            return kind, read, notes

        def _give(entry):
            """Hand an answer back. Its notes go to the row; mmw counts as used
            only when the answer given is mmw's."""
            _kind, read, notes = entry
            _t.update(notes)
            if read.get('source') != 'mmw':
                _t.pop('mmw_used', None)      # _mmw_settle set it; another answer is being given
            # A VDG paint call can supply a VIN the first pass never returned
            # (paint61), and the caller fills a blank one from the answer it is
            # given. When VDG's answer always ended the search, that answer WAS
            # the one given. Now it may be held and another given in its place,
            # so the VIN is carried across.
            if vdg_vin and not (read.get('vin') or '').strip():
                read['vin'] = vdg_vin
            return read

        def _gives_code(entry):
            """True for a held name our catalogue turned into one code."""
            return bool(entry and entry[0] == 'name'
                        and (entry[1].get('paint_code') or '').strip())

        def _mmw_stands_in():
            """paint298: the reserve is about to be bought. True when the
            operator's rule says mmw's code does instead (see
            mmw_code_matches_name), and the row is told why no reserve call
            was made. Asked at the two moments the reserve is started, and
            nowhere else: before them a supplier still running may yet give
            the car's own code, which beats a match on a name.

            mmw is waited for only until MMW_NAME_TEST_BY_S after the search
            began, and only when there is a name to test its code against."""
            names = [n for n in (said.get('pl24'), said.get('vdg_retry')) if n]
            if not names or f_mmw is None:
                return False
            try:
                row = f_mmw.result(timeout=max(
                    0.0, min(_start + MMW_NAME_TEST_BY_S, deadline) - time.monotonic()))
            except Exception:  # noqa: BLE001 - not back in time, or a free leg that raised
                return False
            if not mmw_code_matches_name(make, model, vdg_colour,
                                         (row or {}).get('code'), names):
                return False
            _t['ezyvin_outcome'] = 'skipped_name_match'
            return True

        while pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break  # deadline hit — stop waiting, abandon stragglers
            # paint294: ONCE VDG'S NAME GIVES A CODE, PL24 IS WAITED FOR ONLY
            # UNTIL THE BACKSTOP. A search like that used to end the moment
            # VDG answered. It now waits, so that pl24's own code can beat a
            # match on a name; but a pl24 still silent at the 20 second mark is
            # the hung leg the backstop exists for, and the remedy for it here
            # is the code already in hand, not a paid call and not the rest of
            # the minute. A reserve already called in is paid for, so that one
            # is heard out first.
            if (_gives_code(held.get('vdg_retry'))
                    and time.monotonic() >= ezyvin_backstop_at
                    and (f_ezyvin is None or f_ezyvin not in pending)):
                break
            # Wake at the reserve's backstop too, so a pair of slow-but-alive
            # legs cannot leave the reserve unstarted.
            if not reserve_done:
                remaining = min(remaining,
                                max(0.05, ezyvin_backstop_at - time.monotonic()))
            done, pending = concurrent.futures.wait(
                pending, timeout=remaining,
                return_when=concurrent.futures.FIRST_COMPLETED,
            )
            if not done:
                # Nothing completed in this slice: the backstop woke us, or
                # the deadline has passed (the top of the loop ends the search
                # then).
                #
                # paint294: A RESERVE THAT CANNOT START NO LONGER ENDS THE
                # SEARCH. This branch read "the reserve did not start" as "time
                # is up" and stopped, so a car with no usable VIN (the reserve
                # needs one) had its search cut at the 20 second backstop
                # with VDG's paint call still running: one such search stopped
                # at exactly 20.0s. The search now carries on to its deadline.
                if not reserve_done and time.monotonic() >= ezyvin_backstop_at:
                    reserve_done = True
                    # Not when VDG's name already gives a code: the check at
                    # the top of the loop ends the search with that code.
                    if not _gives_code(held.get('vdg_retry')):
                        # paint298: NOR WHEN MMW'S CODE MATCHES THE NAME ONE
                        # SUPPLIER HAS ALREADY GIVEN. Then the search ENDS
                        # here, with that code, for the reason given at the
                        # top of the loop: a supplier still silent at the
                        # backstop is the hung leg, and the remedy is the code
                        # in hand, not a paid call and not the rest of the
                        # minute. (A car with no usable VIN never gets here
                        # with a name: pl24 is not asked for it, so the only
                        # supplier that could name its colour is the one still
                        # running.)
                        if _mmw_stands_in():
                            break
                        started = _start_ezyvin('backstop')
                        if started is not None:
                            pending = pending | {started}
                continue

            # paint294: EVERY ANSWER IS READ BY THE REFUSAL RULES AS IT
            # ARRIVES, and only a code of the provider's own ends the search.
            #
            # It used to be read after the search had ended. So an answer that
            # was then refused (the placeholder XXX, an interior named as a
            # paint) had already stopped the other sources, and the customer was
            # told "not found" with VDG or the reserve never heard. And VDG's
            # answer ended the search whatever it held: 26 times since 11 Sep
            # it was a colour name with no code. pl24's name-only answer has
            # always been held back for exactly this reason; VDG's now is too.
            #
            # Enforce the VDG-over-pl24 preference within this batch: if the VDG
            # future is among the just-completed ones and produced a code, that
            # wins outright — regardless of whether pl24 also completed here.
            if f_vdg in done:
                vdg_result = _result_or_none(f_vdg)
                if vdg_result is not None:
                    _t['vdg_retry_returned'] = True
                    vdg_vin = (vdg_result.get('vin') or '').strip()
                    entry = _read(vdg_result)
                    if entry[0] == 'code':
                        race_over.set()   # a usable code exists from here on
                        return _give(entry)
                    if entry[0]:
                        held['vdg_retry'] = entry
                    if entry[0] == 'name':
                        said['vdg_retry'] = (vdg_result.get('paint_description') or '').strip()

            # VDG didn't (yet) yield a code. Inspect pl24 if it completed in
            # this batch. A real CODE wins immediately (subject only to a VDG
            # code, already handled above). Anything less is held aside as a
            # FALLBACK: we do NOT return it here, because a real code from a
            # still-pending leg must be able to beat it.
            if f_pl24 is not None and f_pl24 in done:
                p = _result_or_none(f_pl24)
                if p is not None:
                    entry = _read(p)
                    if entry[0] == 'code':
                        _t['pl24_returned'] = True
                        race_over.set()   # a usable code exists from here on
                        return _give(entry)
                    # pl24_returned is left alone for a held answer: on the row it
                    # has always meant "pl24's code was the answer", and the caller
                    # sets it if a held one of pl24's turns out to be.
                    if entry[0] == 'name':
                        _t['pl24_name_only'] = True
                        said['pl24'] = (p.get('paint_description') or '').strip()
                    if entry[0]:
                        held['pl24'] = entry

            # THE RESERVE. Started only once BOTH paid-and-free legs have
            # finished with nothing, so by the time it is inspected there is
            # nothing left to beat it — but it is read last regardless, because
            # a leg that costs credits should never pre-empt one that does not.
            if f_ezyvin is not None and f_ezyvin in done:
                _t['ezyvin_credits'] = _ez_sink.get('credits')
                _t['ezyvin_outcome'] = _ez_sink.get('outcome', '')
                ez = _result_or_none(f_ezyvin)
                # paint182: RECORDED BEFORE THE BRANCHES, so it is kept whether
                # Ezyvin wins with a code, offers a name that loses to another
                # leg, or is never used at all. Recording it inside a branch
                # would keep only the answers that happened to be taken, which
                # is the half that needs no auditing.
                if ez is not None:
                    _t['ezyvin_code'] = (ez.get('code') or '')[:50]
                    _t['ezyvin_name'] = (ez.get('description') or '')[:200]
                if ez is not None and (ez.get('code') or ez.get('description')):
                    entry = _read({'paint_code': ez.get('code') or '',
                                   'paint_description': ez.get('description') or '',
                                   'all_paint_codes': [],
                                   'source': 'ezyvin'})
                    if entry[0] == 'code':
                        _t['ezyvin_returned'] = True
                        race_over.set()
                        return _give(entry)
                    if entry[0] == 'name':
                        # A NAME with no code is still real manufacturer data,
                        # and four Mazdas measured on 8 Sep came back exactly
                        # that way: 'MARINER BLUE', 'DEEP CRYSTAL BLUE MICA'.
                        # Held as a fallback, same as pl24's.
                        _t['ezyvin_name_only'] = True
                    if entry[0]:
                        held['ezyvin'] = entry
            # BOTH LEGS DONE, NEITHER HAD A CODE — the trigger. Checked here
            # rather than on a single drop-out because Ezyvin is charged on any
            # 200: firing when one leg is empty while the other still delivers
            # would spend on 44% of deliveries that never needed it.
            #
            # paint294: NOT WHEN VDG'S NAME ALREADY GIVES A CODE. Holding VDG's
            # name back would otherwise have sent every one of those searches
            # to the reserve: 26 since 11 Sep, all Fords, for which pl24 had
            # the same name and no code either. For 24 of them our catalogue
            # turned the name into one code at no cost. (Where the reserve WAS
            # asked about a name the catalogue could also turn into a code, on
            # other searches, the two agreed on the paint 17 times of 22, on
            # the repository's copy of the catalogue.) So those get the answer
            # they got, once pl24 has had its say (see the top of the loop).
            # The reserve is now asked when the name gives NO code, which is
            # new: two Fords on 12 Sep ("Smoke") went without a code and the
            # reserve was never tried.
            #
            # paint298: NOR WHEN MMW'S CODE MATCHES THE NAME A SUPPLIER GAVE
            # (the operator's rule, see mmw_code_matches_name). Nothing is
            # pending then, so the loop ends and the order below gives mmw's
            # corroborated code, which is the code that was tested. Where the
            # test does not hold the reserve is bought exactly as before, and
            # mmw's code is given only if the reserve has no code of its own.
            if (not reserve_done and f_pl24 is not None
                    and f_vdg not in pending and f_pl24 not in pending
                    and not _gives_code(held.get('vdg_retry'))):
                reserve_done = True
                if not _mmw_stands_in():
                    started = _start_ezyvin('both_empty')
                    if started is not None:
                        pending = pending | {started}

        # NO SOURCE GAVE A CODE OF ITS OWN. What is left, best first (paint294
        # put these in one place):
        #
        #   1. VDG's name, when our catalogue turns it into one code. This is
        #      what those searches were given before, at once; now only after
        #      pl24 has had its chance to give the car's own code.
        #   2. mmw's code, corroborated against the registered colour.
        #   3. pl24's name, then Ezyvin's, when the catalogue turns it into a
        #      code.
        #   4. A bare colour name: VDG's, pl24's, Ezyvin's.
        #   5. A special order code. It is true, and it does not say which
        #      paint: a colour name from another source tells the customer
        #      more, and the name-only page it leads to offers the manual
        #      lookup such a car needs.
        #
        # The first three are in the order they had, with one difference:
        # where pl24's name gives no code and Ezyvin's does, Ezyvin's code is
        # now given, where pl24's bare name was.
        #
        # A name may be upgraded to a full code because _enrich_from_lookup
        # runs it through code_from_name, which returns a code ONLY when the
        # candidates collapse to a single paint. Names are 1:many with codes
        # ('Race Red' matches 13, 'Black Pearl' 481), so it declines far more
        # often than it resolves — deliberately, because a wrong code is worse
        # than none when the customer is about to buy paint.
        if _gives_code(held.get('vdg_retry')):
            return _give(held['vdg_retry'])

        # paint140: mmw is the LAST code source, after VDG, pl24 and Ezyvin have
        # all failed to produce one. Free, and only served if the catalogue
        # corroborates it against the registered colour.
        #
        # Placed above the name-only fallback deliberately: a validated CODE is
        # worth more to the customer than a colour name with no code, which is
        # what that fallback delivers.
        #
        # paint294: WAITED FOR, BRIEFLY, when it is still on its way (see
        # _mmw_settle), and never past the search's own deadline.
        _mmw_code = _mmw_settle(
            f_mmw, make, vdg_colour, _t,
            wait=min(MMW_SETTLE_WAIT_S, deadline - time.monotonic()))
        if _mmw_code:
            entry = _read({'paint_code': _mmw_code, 'paint_description': '',
                           'all_paint_codes': [], 'source': 'mmw'})
            if entry[0] == 'code':
                return _give(entry)
            if entry[0] == 'special':
                held['mmw'] = entry

        for _source in ('pl24', 'ezyvin'):
            if _gives_code(held.get(_source)):
                return _give(held[_source])
        for _source in ('vdg_retry', 'pl24', 'ezyvin'):
            if held.get(_source) and held[_source][0] == 'name':
                return _give(held[_source])
        for _source in ('vdg_retry', 'pl24', 'ezyvin', 'mmw'):
            if held.get(_source):
                return _give(held[_source])       # what is left is a special order code
        _t.pop('mmw_used', None)      # mmw answered, and its answer was not one to give
        return None
    finally:
        # paint294: THE RACE IS OVER ON EVERY WAY OUT, not only when a code
        # won. A search that ended with nothing left this flag unset, and VDG's
        # worker reads it to decide whether to make its second paid call: with
        # the flag unset it went ahead, for an answer nobody was left to read.
        race_over.set()
        # Do NOT block on stragglers. wait=False means we don't join running
        # threads; cancel_futures cancels any not-yet-started work. A pl24 thread
        # still mid-request is abandoned and ends when its own HTTP timeout fires.
        ex.shutdown(wait=False, cancel_futures=True)
        _t['duration_ms'] = int((time.monotonic() - _start) * 1000)