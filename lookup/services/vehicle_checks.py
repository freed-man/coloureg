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
from datetime import date, datetime, timedelta

from django.db import close_old_connections

from lookup.services.http import get_session
from lookup.services.tax_rates import TAX_YEAR, get_annual_tax

logger = logging.getLogger(__name__)

_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix='vehicle-checks')
WAIT_SECONDS = 4          # the most collect() waits, once the lookup reaches it
# paint247: the Motor Insurers' Bureau's own vehicle check (MIB Navigate), in
# place of the old askMID address.
INSURANCE_URL = 'https://enquiry.navigate.mib.org.uk/checkyourvehicle'
MAJOR_TYPES = ('DANGEROUS', 'MAJOR', 'FAIL', 'PRS')     # motoreg's grouping (kept for reference; paint265 below)
# paint265: grouped by what happened at the test, as the operator chose. FAIL:
# dangerous, major, and the "fail" items of tests before 20 May 2018 (each failed
# the test; a dangerous one says so). REPAIRED: a failing defect fixed at the
# test centre within an hour, so the test passed (PRS). ADVISORY: minor defects,
# advisories and the tester's own notes, none of which fail a test. Counted in the
# cached histories on 2 Oct: 791 advisory, 154 major, 45 minor, 41 repaired, 39
# old fail, 36 tester's notes, 34 dangerous.
FAIL_TYPES = ('DANGEROUS', 'MAJOR', 'FAIL')
REPAIRED_TYPES = ('PRS',)

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
        'type_approval': dvla.get('typeApproval') or '',          # paint248: M1 is a car
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


# -- the tax estimate (paint248) -----------------------------------------------
#
# CHECKED AGAINST DVLA's V149 FOR 1 APRIL 2026 (every figure in tax_rates.py,
# and the rules below). Shown only when every rule that decides it is known;
# otherwise nothing is shown rather than a figure that might be wrong:
#   * cars only (type approval M1): vans and motorcycles have their own rates;
#   * only within the table's own tax year, so it never goes stale after April;
#   * the dates that move a car between systems need the month (1 March 2001,
#     1 April 2017) or the day (23 March 2006, for band K); the MOT service
#     gives the day, DVLA only the month;
#   * a vehicle built before 1 January of the tax year's start less 40 years
#     is historic: exempt;
#   * a car first registered on or after 1 April 2017 pays £440 more for 5
#     years from its second licence if its list price was over £40,000 (£50,000
#     for a zero-emission car first registered on or after 1 April 2025). The
#     list price is not known here, so the higher figure is shown beside it
#     while that can apply.
SUPPLEMENT_12, SUPPLEMENT_6 = 640, 352          # V149: the standard rate with the £440 added


def _money(value):
    return f'£{value:,.2f}'.replace('.00', '')


def tax_estimate(f, today):
    parts = _tax_parts(f, today)
    if not parts:
        return ''
    if parts.get('historic'):
        return '(historic vehicle: exempt from tax)'
    text = f"annual tax: {_money(parts['annual'])}"          # paint250: DVLA's rate, not an estimate
    if parts.get('six'):
        text += f", 6 months: {_money(parts['six'])}"
    if parts.get('limit'):
        # paint251: "or" and "when new" make the alternative read as one.
        text += (f"; or {_money(SUPPLEMENT_12)}, 6 months: {_money(SUPPLEMENT_6)}, "
                 f"if its list price was over {_money(parts['limit'])} when new")
    return f'({text})'


def tax_table(f, today):
    """paint261: the same facts as tax_estimate(), as the small table opened with
    the Tax row's "+": {'cols': [...], 'rows': [[label, 12 months, 6 months]]},
    {'note': ...} for a historic vehicle, or None. The second row is the
    expensive-car rate; DVLA's rule is the LIST PRICE (the published price with
    factory options, before any discount), not what the owner paid."""
    parts = _tax_parts(f, today)
    if not parts:
        return None
    if parts.get('historic'):
        return {'note': 'Historic vehicle: exempt from tax'}
    rows = [['Standard', _money(parts['annual']), _money(parts['six']) if parts.get('six') else '']]
    if parts.get('limit'):
        rows.append([f"List price over £{parts['limit'] // 1000}k", _money(SUPPLEMENT_12), _money(SUPPLEMENT_6)])
    cols = ['12 months', '6 months'] if all(r[2] for r in rows) else ['12 months']
    return {'cols': cols, 'rows': [r[:1 + len(cols)] for r in rows]}


def _tax_parts(f, today):
    """The rules behind both: {'historic': True}, or {'annual', 'six', 'limit'}
    (limit is 40000 or 50000 while the expensive-car rate can apply, else None),
    or None when any deciding fact is missing (paint248)."""
    year_start = int(TAX_YEAR[:4])
    if not (date(year_start, 4, 1) <= today <= date(year_start + 1, 3, 31)):
        return None
    try:
        built = int(f.get('year'))
    except (TypeError, ValueError):
        built = None
    if built and built < year_start - 40:
        return {'historic': True}
    if str(f.get('type_approval', '')).upper() != 'M1':
        return None
    exact = _date(f.get('first_registered'))
    if exact:
        reg_year, reg_month = exact.year, exact.month
    else:
        try:
            reg_year, reg_month = (int(x) for x in str(f.get('reg_month', '')).split('-')[:2])
        except (TypeError, ValueError):
            return None
    co2 = f.get('co2')
    if reg_year < 2017 and co2 is not None and co2 > 225 and (reg_year, reg_month) == (2006, 3):
        if not exact:
            return None                         # band K turns on the day in March 2006
        if exact < date(2006, 3, 23):
            co2 = 225
    try:
        est = get_annual_tax(co2, f.get('fuel', ''), reg_year, reg_month, f.get('engine_cc'))
    except Exception:
        return None
    if not est or not est.get('annual_rate'):
        return None
    parts = {'annual': est['annual_rate'], 'six': est.get('six_month_rate'), 'limit': None}
    registered = exact or date(reg_year, reg_month, 1)
    if registered >= date(2017, 4, 1):
        try:
            ends = registered.replace(year=registered.year + 6)
        except ValueError:                      # 29 February
            ends = registered.replace(year=registered.year + 6, day=28)
        if today < ends:
            parts['limit'] = 50000 if (str(f.get('fuel', '')).upper() == 'ELECTRICITY' and registered >= date(2025, 4, 1)) else 40000
    return parts


# -- details in brackets (paint250) ---------------------------------------------
#
# VDG's engine and transmission carry a detail in brackets ("2.0L (148 bhp)",
# "Manual (6 speed)"); the page shows it in the lighter style of the MOT and tax
# details, on the same line.

def split_bracket(text):
    """('2.0L', '(148 bhp)'); ('', '(148 bhp)') for a bracket alone; (text, '')."""
    t = (text or '').strip()
    if t.startswith('(') and t.endswith(')'):
        return '', t
    i = t.find(' (')
    if i > 0 and t.endswith(')'):
        return t[:i], t[i + 1:]
    return t, ''


def split_details(vehicle_data):
    out = {}
    for key, name in (('engine_description', 'engine'), ('fuel_type', 'fuel'), ('transmission', 'transmission')):
        out[f'{name}_main'], out[f'{name}_extra'] = split_bracket((vehicle_data or {}).get(key, ''))
    # paint251: DVLA's exact engine size goes first in the engine's bracket,
    # "2.0L (1968cc, 148 bhp)". Not for an electric car, which has no cc.
    facts_ = (vehicle_data or {}).get('vehicle_status') or {}
    try:
        cc = int(facts_.get('engine_cc') or 0)
    except (TypeError, ValueError):
        cc = 0
    electric = (str(facts_.get('fuel', '')).upper() == 'ELECTRICITY'
                or 'electric motor' in (out['engine_main'] + out['engine_extra']).lower())
    if cc > 0 and not electric and (out['engine_main'] or out['engine_extra']):
        inner = out['engine_extra'][1:-1] if out['engine_extra'] else ''
        out['engine_extra'] = f"({cc}cc{', ' + inner if inner else ''})"
    return out


# -- display (motoreg's wording) ----------------------------------------------

# -- the mileage chart (paint261) ----------------------------------------------
#
# Every MOT inspection with a readable mileage, pass or fail, on a line drawn by
# the site itself (an inline SVG; no charting library on the results page).
# Time is to scale, so two tests in one year sit apart by their months. A
# reading in km is drawn in miles; its tap label says what was recorded. A
# fall against the test before (in the same unit) is flagged in the label, as
# the test card flags it. Fewer than two readings: no chart.

KM_TO_MILES = 0.621371
# paint269: 400, 4,000 and 40,000 too, so one high reading wastes less of the chart
# (150,008 miles: 0 to 160k in 40k steps, not 0 to 200k in 50k).
_NICE_STEPS = (100, 200, 250, 400, 500, 1000, 2000, 2500, 4000, 5000, 10000, 20000, 25000, 40000, 50000, 100000, 200000)


VISIT_DAYS = 31        # paint264: tests within a month of the one before are one visit


def mileage_chart(tests):
    """The MOT mileage chart: positions as PERCENTAGES of a fixed-height plot
    (paint263), with one marker per VISIT (paint264). Tests within a month of the
    one before are one visit (a fail and its retest, the same day or weeks
    later); the marker sits on the visit's last test and shows a green tick or a
    red cross for each test in it, in order, so no key is needed. A month, not
    the 10 working days of the free retest: repairs can wait for parts, and on a
    long history tests weeks apart would sit on top of each other anyway; normal
    MOTs are a year apart, so nothing unrelated is joined."""
    pts = []
    for t in tests or []:
        when = _date(t.get('d'))
        try:
            reading = int(str(t.get('o', '')).replace(',', '').strip())
        except ValueError:
            continue                      # no reading, or "unreadable": nothing to plot
        if when is None or reading < 0:
            continue
        km = str(t.get('u', '')).upper() == 'KM'
        pts.append({'when': when, 'stamp': str(t.get('d', '')), 'miles': round(reading * KM_TO_MILES) if km else reading,
                    'km': km, 'passed': t.get('r') == 'PASSED', 'recorded': f"{reading:,} {'km' if km else 'miles'}"})
    # Oldest first; on the same day a fail comes before the pass that followed it
    # (unless the stored times say otherwise).
    pts.sort(key=lambda p: (p['when'], p['stamp'], p['passed']))
    if len(pts) < 2:
        return None
    visits = []
    for p in pts:
        if visits and (p['when'] - visits[-1]['tests'][-1]['when']).days <= VISIT_DAYS:
            visits[-1]['tests'].append(p)
        else:
            visits.append({'tests': [p]})
    lo, hi = pts[0]['when'] - timedelta(days=45), pts[-1]['when'] + timedelta(days=45)
    days = max((hi - lo).days, 1)
    highest = max(p['miles'] for p in pts) or 1
    step = next((s_ for s_ in _NICE_STEPS if highest / s_ <= 4), _NICE_STEPS[-1])
    ymax = step * max(1, -(-highest // step))
    XP = lambda d: (d - lo).days / days * 100
    YP = lambda v: 100 - v / ymax * 100
    pct = lambda v: f'{v:.2f}'
    last_by_unit = {}
    for v in visits:
        tests_ = v['tests']
        last = tests_[-1]
        v['xv'], v['yv'] = XP(last['when']), YP(last['miles'])
        v['x'], v['y'] = pct(v['xv']), pct(v['yv'])
        v['results'] = [t['passed'] for t in tests_]
        before = last_by_unit.get(last['km'])
        v['drop'] = before is not None and last['miles'] < before
        last_by_unit[last['km']] = last['miles']
        if all(t['when'] == tests_[0]['when'] for t in tests_):
            first_line = f"{_shown(last['when'])} · " + ', then '.join('passed' if t['passed'] else 'failed' for t in tests_)
        else:
            first_line = ' · '.join(f"{_shown(t['when'])} {'passed' if t['passed'] else 'failed'}" for t in tests_)
        v['lines'] = [first_line, last['recorded']] + (['Lower than the test before'] if v['drop'] else [])
        v['label'] = ', '.join(v['lines'])
    line = ' '.join(f"{v['xv'] * 10:.1f},{v['yv']:.2f}" for v in visits)
    area = (f"M{visits[0]['xv'] * 10:.1f},100 L" + ' L'.join(f"{v['xv'] * 10:.1f},{v['yv']:.2f}" for v in visits)
            + f" L{visits[-1]['xv'] * 10:.1f},100 Z")
    years = list(range(lo.year + 1, hi.year + 1))
    span_years = (hi - lo).days / 365.25
    every = 1 if span_years <= 6 else 2 if span_years <= 12 else 5
    marks_every = 1 if span_years <= 12 else 2
    year_marks = [pct(XP(date(y, 1, 1))) for y in years[::marks_every]]
    xticks = [{'label': str(y), 'x': XP(date(y, 1, 1))} for y in years[::every]]
    xticks = [{'label': t['label'], 'x': pct(t['x'])} for t in xticks if 4 <= t['x'] <= 96]
    if not xticks:
        xticks = [{'label': str(pts[0]['when'].year), 'x': visits[0]['x']}]
    fmt = lambda v: '0' if v == 0 else (f'{v / 1000:g}k' if v >= 1000 else str(v))
    yticks = [{'label': fmt(v), 'y': pct(YP(v))} for v in range(0, ymax + 1, step)]
    gap = (pts[-1]['when'] - pts[0]['when']).days / 365.25
    rise = pts[-1]['miles'] - pts[0]['miles']
    avg = round(rise / gap / 100) * 100 if gap >= 0.5 and rise > 0 else 0
    for v in visits:
        for k in ('xv', 'yv', 'tests'):
            del v[k]
    return {
        'visits': visits, 'tests': len(pts), 'line': line, 'area': area, 'years': year_marks,
        'xticks': xticks, 'yticks': yticks, 'avg': f'{avg:,}' if avg else '',
        'summary': (f"Mileage at {len(pts)} MOT tests, from {pts[0]['recorded']} in {pts[0]['when'].year} "
                    f"to {pts[-1]['recorded']} in {pts[-1]['when'].year}"),
    }


# paint265: when Transport Scotland's checker answers "exempt" (its codes are
# not published; on 2 Oct its 3 exempt answers were all new cars, a 2024 hybrid,
# a 2024 and a 2026 petrol), the car's own DVLA details decide, by the legal
# cut-offs both London and Scotland use: new petrol cars have had to meet Euro 4
# since 2006, new diesels Euro 6 since September 2015 (hybrids are held to the
# diesel date, since "hybrid electric" can be a diesel hybrid), and a fully
# electric car always passes. Anything older or unclear still links to TfL, so
# this can never turn an exempt but non-compliant car into a tick.
_PETROL_FROM, _DIESEL_FROM = date(2006, 1, 1), date(2015, 9, 1)
_VAN_PETROL_FROM, _VAN_DIESEL_FROM = date(2007, 1, 1), date(2016, 9, 1)      # paint267


_BIKE_FROM = date(2007, 7, 1)     # paint268: Euro 3 motorbikes, as TfL dates them


def _first_registered(f):
    first = _date(f.get('first_registered'))
    if first is None:
        try:
            year, month = (int(x) for x in str(f.get('reg_month', '')).split('-')[:2])
            first = date(year, month, 1)
        except (TypeError, ValueError):
            first = None
    return first


def _bike_by_age(f):
    """paint268: London's own answer for a motorbike, moped or quadricycle."""
    if str(f.get('fuel', '')).upper().strip() == 'ELECTRICITY':
        return True
    first = _first_registered(f)
    if first is not None:
        return first >= _BIKE_FROM
    try:
        return int(f.get('year')) > _BIKE_FROM.year     # only the year known: a full year past it
    except (TypeError, ValueError):
        return False


def _ulez_by_age(f):
    fuel = str(f.get('fuel', '')).upper().strip()
    if fuel == 'ELECTRICITY':
        return True
    # paint267: CARS AND LIGHT VANS ONLY. Motorbikes are outside Scotland's zones
    # (so its checker calls them exempt), but London holds them to Euro 3, a
    # different cut-off (generally bikes from July 2007); lorries, buses and
    # coaches to Euro VI. Those, and anything whose type DVLA did not give, go
    # to TfL.
    kind = str(f.get('type_approval', '')).upper()
    if kind not in ('M1', 'N1'):
        return False
    # Vans got there later: Euro 4 was required of new petrol vans of every size
    # from January 2007, Euro 6 of new diesel vans from September 2016.
    petrol_from, diesel_from = (_VAN_PETROL_FROM, _VAN_DIESEL_FROM) if kind == 'N1' else (_PETROL_FROM, _DIESEL_FROM)
    if fuel == 'PETROL':
        cut = petrol_from
    elif fuel == 'DIESEL' or 'HYBRID' in fuel or fuel == 'ELECTRIC DIESEL':
        cut = diesel_from
    else:
        return False                      # gas, bi-fuel, steam, unknown: TfL decides
    first = _date(f.get('first_registered'))
    if first is None:
        try:
            year, month = (int(x) for x in str(f.get('reg_month', '')).split('-')[:2])
            first = date(year, month, 1)
        except (TypeError, ValueError):
            first = None
    if first is not None:
        return first >= cut
    try:
        return int(f.get('year')) > cut.year     # only the year known: a full year past the cut-off
    except (TypeError, ValueError):
        return False


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
    """Everything the results page shows, from the stored facts. paint248: it
    never raises: a fact it cannot read hides the new rows, never the page."""
    try:
        return _display(f, today)
    except Exception:
        logger.exception('vehicle checks could not be shown')
        return {}


def _display(f, today=None):
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
        out['vc_v5c_ago'] = f'({span(v5c, today)} ago)'          # paint249: no "approx.": the date is exact
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
            # paint261: "expires", like the MOT: the date is when this tax runs out.
            out['vc_tax'] = {'ok': True, 'label': 'Taxed',
                             'detail': f'(expires {_shown(tdue)}, {countdown(tdue, today, "expires today")})' if tdue else ''}
        else:
            out['vc_tax'] = {'ok': False, 'label': tstatus,
                             'detail': f'(expired {_shown(tdue)}, {countdown(tdue, today, "due today")})' if tdue else ''}
        try:
            estimate = tax_estimate(f, today)
        except Exception:
            logger.exception('tax estimate failed')
            estimate = ''
        if estimate:
            out['vc_tax']['estimate'] = estimate
        try:
            table = tax_table(f, today)
        except Exception:
            logger.exception('tax table failed')
            table = None
        if table:
            out['vc_tax']['table'] = table
    # paint261: what the "+" beside MOT and Tax opens: the detail without its
    # brackets, "Expires 27/05/2027, 238 days remaining".
    for key in ('vc_mot', 'vc_tax'):
        detail = (out.get(key) or {}).get('detail') or ''
        if detail:
            inner = detail[1:-1] if detail.startswith('(') and detail.endswith(')') else detail
            out[key]['more'] = inner[:1].upper() + inner[1:]
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
            'fails': [{'text': caps(x), 'dangerous': k == 'DANGEROUS'} for k, x in t.get('f', []) if k in FAIL_TYPES],
            'repaired': [caps(x) for k, x in t.get('f', []) if k in REPAIRED_TYPES],
            'advisories': [caps(x) for k, x in t.get('f', []) if k not in FAIL_TYPES + REPAIRED_TYPES],
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
        try:
            chart = mileage_chart(f.get('mot_tests'))
        except Exception:
            logger.exception('mileage chart failed')
            chart = None
        if chart:
            out['vc_mileage_chart'] = chart
    if out:
        # paint245: ULEZ. An answer stored before paint245 carries no letter, so
        # it links to TfL without a reason.
        letter = f.get('lez', '')
        motorbike = str(f.get('type_approval', '')).upper().startswith('L')
        if motorbike and letter in ('c', 'e', 'n'):
            # paint268: Scotland's zones do not cover motorbikes, mopeds or
            # quadricycles at all, so its answer for one says nothing about London
            # (on 3 Oct it had called 2 motorbikes "compliant"). London holds them
            # to Euro 3, generally those first registered from July 2007: decided
            # from the bike's own details, anything earlier or unclear to TfL.
            if _bike_by_age(f):
                out['vc_ulez'] = {'ok': True, 'label': 'Compliant'}
            else:
                out['vc_ulez'] = {'ok': None, 'link': TFL_ULEZ_URL, 'notice': f"({ULEZ_NOTICES['unclear']})"}
        elif letter == 'c' or (letter == 'e' and _ulez_by_age(f)):
            out['vc_ulez'] = {'ok': True, 'label': 'Compliant'}
        elif letter == 'n':
            out['vc_ulez'] = {'ok': False, 'label': 'Not compliant'}
        else:
            notice = (ULEZ_NOTICES['u'] if letter == 'u' else ULEZ_NOTICES['unclear'] if letter
                      else ULEZ_NOTICES.get(f.get('lez_error', ''), ''))
            out['vc_ulez'] = {'ok': None, 'link': TFL_ULEZ_URL, 'notice': f'({notice})' if notice else ''}
        out['vc_insurance_url'] = INSURANCE_URL
    return out
