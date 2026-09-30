"""Year, V5C, MOT and tax on the results page (paint244), ported from motoreg.

FETCHED ALONGSIDE THE LOOKUP. When VDG returns the vehicle, coloureg calls
DVLA only if VDG left the make or category blank, and the MOT service only if
it left the model blank, so on most lookups neither is called. These checks
need both, so `start()` asks them in the background as the lookup begins and
`collect()` picks the replies up when the answer is stored, a few seconds
later: in the usual case they are back by then and the page is no slower.

ONLY RAW FACTS ARE STORED with the answer (dates and statuses as DVLA and the
MOT service give them), because a cached answer is replayed for up to 7 days:
"Valid, 104 days remaining" is worked out when the page is shown, so a cached
page still says the right thing on the day it is shown. Answers stored before
this release have no facts, and simply show no new rows.
"""
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime

from django.db import close_old_connections

from lookup.services.http import get_session
from lookup.services.tax_rates import get_annual_tax

logger = logging.getLogger(__name__)

_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix='vehicle-checks')
WAIT_SECONDS = 4          # the most collect() waits, once the lookup reaches it
ASKMID_URL = 'https://ownvehicle.askmid.com/'
MAJOR_TYPES = ('DANGEROUS', 'MAJOR', 'FAIL', 'PRS')     # motoreg's grouping; the rest are advisories

# paint245: ULEZ, as motoreg answers it: Transport Scotland's emissions checker,
# asked the same way its own page asks. Scotland's LEZs and London's ULEZ set the
# same standard for cars and vans (Euro 4 petrol, Euro 6 diesel), so its
# compliant / not compliant answers London's question too; anything else links
# to TfL's checker with the reason.
LEZ_URL = 'https://vehicleemissionscheck.service.gov.scot/api'
LEZ_TIMEOUT = 8
TFL_ULEZ_URL = 'https://tfl.gov.uk/modes/driving/check-your-vehicle/'
ULEZ_NOTICES = {
    'u': 'not recognised by the emissions checker',
    'busy': 'emissions checker busy',
    'error': "emissions checker didn't answer",
    'unclear': 'no automatic answer for this vehicle',
}


def fetch_lez(registration):
    """(status, letter): ('ok', 'c'|'n'|'e'), ('not_found', 'u'), ('busy', ''), ('error', '')."""
    try:
        r = get_session().post(LEZ_URL, json={'vrn': registration}, timeout=LEZ_TIMEOUT,
                               headers={'User-Agent': 'coloureg (vehicle details)'})
    except Exception as exc:
        logger.warning('LEZ checker unreachable: %s', exc)
        return 'error', ''
    if r.status_code == 429:
        return 'busy', ''
    if r.status_code != 200:
        logger.warning('LEZ checker returned HTTP %s', r.status_code)
        return 'error', ''
    try:
        letter = r.json()['vehicleResult'][0]['s']
    except (ValueError, KeyError, IndexError, TypeError):
        logger.warning('LEZ checker sent an unexpected reply')
        return 'error', ''
    if letter == 'u':
        return 'not_found', 'u'
    if letter not in ('c', 'e', 'n'):
        logger.warning('LEZ checker sent an unknown status %r', letter)
        return 'error', ''
    return 'ok', letter


def _in_thread(fn, registration):
    try:
        return fn(registration)
    except Exception:
        logger.warning('vehicle check failed', exc_info=True)
        return None
    finally:
        close_old_connections()


def start(registration, get_dvla, get_mot, get_lez=None):
    """Ask DVLA, the MOT service and (paint245) the emissions checker in the
    background; returns the handles."""
    try:
        return (_POOL.submit(_in_thread, get_dvla, registration),
                _POOL.submit(_in_thread, get_mot, registration),
                _POOL.submit(_in_thread, get_lez, registration) if get_lez else None)
    except Exception:
        logger.warning('vehicle checks could not start', exc_info=True)
        return None


def _result(future, deadline):
    if future is None:
        return None
    try:
        return future.result(timeout=max(0.0, deadline - datetime.now().timestamp()))
    except Exception:
        return None


def collect(handles, wait=WAIT_SECONDS):
    """The raw facts, or {} when nothing came back in time."""
    if not handles:
        return {}
    deadline = datetime.now().timestamp() + wait
    dvla, mot, lez = (list(_result(f, deadline) for f in handles) + [None, None, None])[:3]
    return facts(dvla if isinstance(dvla, dict) else None, mot if isinstance(mot, dict) else None,
                 lez if isinstance(lez, tuple) else None)


def facts(dvla, mot, lez=None):
    """What is kept with the answer: DVLA's and the MOT service's own values."""
    dvla, mot = dvla or {}, mot or {}
    out = {
        'year': dvla.get('yearOfManufacture'),
        'first_registered': mot.get('registrationDate') or '',
        'v5c': dvla.get('dateOfLastV5CIssued') or '',
        'mot_status': dvla.get('motStatus') or '',
        'mot_expiry': dvla.get('motExpiryDate') or '',
        'mot_due': mot.get('motTestDueDate') or '',
        'tax_status': dvla.get('taxStatus') or '',
        'tax_due': dvla.get('taxDueDate') or '',
        'co2': dvla.get('co2Emissions'),
        'fuel': dvla.get('fuelType') or '',
        'reg_month': dvla.get('monthOfFirstRegistration') or '',
        'engine_cc': dvla.get('engineCapacity'),
    }
    # paint246: the MOT tests, kept small: date, result, mileage, expiry, and each
    # defect's type and text.
    tests = []
    for t in (mot.get('motTests') or [])[:60]:
        if not isinstance(t, dict):
            continue
        tests.append({
            'd': str(t.get('completedDate') or '')[:10], 'r': t.get('testResult') or '',
            'o': str(t.get('odometerValue') or ''), 'u': t.get('odometerUnit') or '',
            'x': str(t.get('expiryDate') or '')[:10],
            'f': [[str(d.get('type') or ''), str(d.get('text') or '')[:300]]
                  for d in (t.get('defects') or []) if isinstance(d, dict) and d.get('text')],
        })
    if tests:
        out['mot_tests'] = tests
    if lez:
        status, letter = lez
        if letter:
            out['lez'] = letter
        elif status in ('busy', 'error'):
            out['lez_error'] = status
    return {k: v for k, v in out.items() if v not in (None, '')}


# -- display (motoreg's wording) ----------------------------------------------

def _date(value):
    """DVLA writes 2027-01-12; the MOT service 2017.06.30 or an ISO timestamp."""
    text = str(value or '').strip()[:10].replace('.', '-')
    try:
        return datetime.strptime(text, '%Y-%m-%d').date()
    except ValueError:
        return None


def _shown(d):
    return d.strftime('%d/%m/%Y')


def _plural(n, word):
    return f"{n} {word}{'s' if n != 1 else ''}"


def span(start, today):
    """Time since start, e.g. '3 years, 8 months'."""
    days = (today - start).days
    years, months = days // 365, (days % 365) // 30
    if years and months:
        return f"{_plural(years, 'year')}, {_plural(months, 'month')}"
    if years:
        return _plural(years, 'year')
    return _plural(months, 'month')


def countdown(target, today, on_the_day):
    days = (target - today).days
    if days > 0:
        return f"{_plural(days, 'day')} remaining"
    if days == 0:
        return on_the_day
    return f"{_plural(-days, 'day')} overdue"


def display(f, today=None):
    """Everything the results page shows, from the stored facts."""
    today = today or date.today()
    f = f or {}
    out = {}
    if f.get('year'):
        out['vc_year'] = str(f['year'])
        started = _date(f.get('first_registered'))
        if not started:
            try:
                started = date(int(f['year']), 1, 1)
            except (TypeError, ValueError):
                started = None
        if started and started <= today:
            out['vc_age'] = f'({span(started, today)} old)'
    v5c = _date(f.get('v5c'))
    if v5c:
        out['vc_v5c'] = _shown(v5c)
        out['vc_v5c_ago'] = f'(approx. {span(v5c, today)} ago)'
    # MOT: motoreg's four cases.
    status, expiry, due = f.get('mot_status', ''), _date(f.get('mot_expiry')), _date(f.get('mot_due'))
    if status == 'Valid':
        out['vc_mot'] = {'ok': True, 'label': 'Valid',
                         'detail': f'(expires {_shown(expiry)}, {countdown(expiry, today, "expires today")})' if expiry else ''}
    elif status == 'No details held by DVLA' and due:
        left = countdown(due, today, 'due today')
        if 'overdue' in left:
            out['vc_mot'] = {'ok': False, 'label': 'Not valid', 'detail': f'(expired {_shown(due)}, {left})'}
        else:
            out['vc_mot'] = {'ok': True, 'label': 'Exempt (new vehicle)', 'detail': f'(first MOT due {_shown(due)}, {left})'}
    elif status == 'No details held by DVLA':
        out['vc_mot'] = {'ok': None, 'label': 'No details held', 'detail': ''}
    elif status:
        out['vc_mot'] = {'ok': False, 'label': status,
                         'detail': f'(expired {_shown(expiry)}, {countdown(expiry, today, "expired today")})' if expiry else ''}
    # Tax, with motoreg's estimate.
    tstatus, tdue = f.get('tax_status', ''), _date(f.get('tax_due'))
    if tstatus:
        if tstatus == 'Taxed':
            out['vc_tax'] = {'ok': True, 'label': 'Taxed',
                             'detail': f'(due {_shown(tdue)}, {countdown(tdue, today, "due today")})' if tdue else ''}
        else:
            out['vc_tax'] = {'ok': False, 'label': tstatus,
                             'detail': f'(expired {_shown(tdue)}, {countdown(tdue, today, "due today")})' if tdue else ''}
        reg_year, reg_month = None, None
        try:
            reg_year, reg_month = (int(x) for x in str(f.get('reg_month', '')).split('-')[:2])
        except (TypeError, ValueError):
            reg_year = f.get('year')
        try:
            est = get_annual_tax(f.get('co2'), f.get('fuel', ''), reg_year, reg_month, f.get('engine_cc'))
        except Exception:
            est = None
        if est and est.get('annual_rate'):
            six = f", 6 months: £{est['six_month_rate']}" if est.get('six_month_rate') else ''
            out['vc_tax']['estimate'] = f"(est. annual tax: £{est['annual_rate']}{six})"
    # paint246: the MOT history, as motoreg shows it: newest first, the mileage
    # and its change since the test before, then Major and Advisory items.
    history = []
    for t in sorted(f.get('mot_tests') or [], key=lambda t: t.get('d', ''), reverse=True):
        when = _date(t.get('d'))
        unit = {'MI': 'miles', 'KM': 'km'}.get(str(t.get('u', '')).upper(), str(t.get('u', '')).lower())
        try:
            miles = int(str(t.get('o', '')).replace(',', ''))
        except ValueError:
            miles = None
        caps = lambda text: text[:1].upper() + text[1:]
        history.append({
            'date': _shown(when) if when else t.get('d', ''), 'passed': t.get('r') == 'PASSED',
            'miles': miles, 'unit': unit, 'mileage': f'{miles:,} {unit}'.strip() if miles is not None else '',
            'majors': [caps(x) for k, x in t.get('f', []) if k in MAJOR_TYPES],
            'advisories': [caps(x) for k, x in t.get('f', []) if k not in MAJOR_TYPES],
        })
    for i, t in enumerate(history):
        older = history[i + 1] if i + 1 < len(history) else None
        # Only like with like: a test read in km against one in miles is no
        # discrepancy (motoreg compared the raw numbers).
        if older and t['miles'] is not None and older['miles'] is not None and t['unit'] == older['unit']:
            diff = t['miles'] - older['miles']
            t['diff'] = f'+{diff:,}' if diff >= 0 else f'-{-diff:,}'
            t['diff_negative'] = diff < 0
    if history:
        out['vc_mot_tests'] = history
    if out:
        # paint245: ULEZ. An answer stored before paint245 carries no letter, so
        # it links to TfL without a reason.
        letter = f.get('lez', '')
        if letter == 'c':
            out['vc_ulez'] = {'ok': True, 'label': 'Compliant'}
        elif letter == 'n':
            out['vc_ulez'] = {'ok': False, 'label': 'Not compliant'}
        else:
            notice = (ULEZ_NOTICES['u'] if letter == 'u' else ULEZ_NOTICES['unclear'] if letter
                      else ULEZ_NOTICES.get(f.get('lez_error', ''), ''))
            out['vc_ulez'] = {'ok': None, 'link': TFL_ULEZ_URL, 'notice': f'({notice})' if notice else ''}
        out['vc_insurance_url'] = ASKMID_URL
    return out
