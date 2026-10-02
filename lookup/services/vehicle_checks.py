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
_NICE_STEPS = (100, 200, 250, 500, 1000, 2000, 2500, 5000, 10000, 20000, 25000, 50000, 100000, 200000)


def mileage_chart(tests):
    """paint263: positions as PERCENTAGES of the plot, not pixels. The page gives
    the plot a fixed height (150px) and lets only its width follow the screen; the
    line is a stretchable drawing (preserveAspectRatio="none", strokes that do not
    stretch), while the points and labels are ordinary page elements placed by
    percentage, so their size never changes. paint261 drew everything at 340x190
    and scaled it, which on a wide screen made the chart and its labels huge."""
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
        pts.append({'when': when, 'miles': round(reading * KM_TO_MILES) if km else reading, 'km': km,
                    'passed': t.get('r') == 'PASSED', 'recorded': f"{reading:,} {'km' if km else 'miles'}"})
    pts.sort(key=lambda p: p['when'])
    if len(pts) < 2:
        return None
    lo, hi = pts[0]['when'] - timedelta(days=45), pts[-1]['when'] + timedelta(days=45)
    days = max((hi - lo).days, 1)
    highest = max(p['miles'] for p in pts) or 1
    step = next((s_ for s_ in _NICE_STEPS if highest / s_ <= 4), _NICE_STEPS[-1])
    ymax = step * max(1, -(-highest // step))
    XP = lambda d: (d - lo).days / days * 100
    YP = lambda v: 100 - v / ymax * 100
    pct = lambda v: f'{v:.2f}'
    last_by_unit = {}
    for p in pts:
        p['xv'], p['yv'] = XP(p['when']), YP(p['miles'])
        p['x'], p['y'], p['date'] = pct(p['xv']), pct(p['yv']), _shown(p['when'])
        before = last_by_unit.get(p['km'])
        p['drop'] = before is not None and p['miles'] < before
        last_by_unit[p['km']] = p['miles']
        p['label'] = f"{p['date']}, {'passed' if p['passed'] else 'failed'}, {p['recorded']}"
        if p['drop']:
            p['label'] += ', lower than the test before'
    # The drawing's own units: 1000 wide, 100 high, stretched to the plot's size.
    line = ' '.join(f"{p['xv'] * 10:.1f},{p['yv']:.2f}" for p in pts)
    area = (f"M{pts[0]['xv'] * 10:.1f},100 L" + ' L'.join(f"{p['xv'] * 10:.1f},{p['yv']:.2f}" for p in pts)
            + f" L{pts[-1]['xv'] * 10:.1f},100 Z")
    years = list(range(lo.year + 1, hi.year + 1))
    span_years = (hi - lo).days / 365.25
    every = 1 if span_years <= 6 else 2 if span_years <= 12 else 5
    xticks = [{'label': str(y), 'x': XP(date(y, 1, 1))} for y in years[::every]]
    xticks = [{'label': t['label'], 'x': pct(t['x'])} for t in xticks if 4 <= t['x'] <= 96]
    if not xticks:
        xticks = [{'label': str(pts[0]['when'].year), 'x': pts[0]['x']}]
    fmt = lambda v: '0' if v == 0 else (f'{v / 1000:g}k' if v >= 1000 else str(v))
    yticks = [{'label': fmt(v), 'y': pct(YP(v))} for v in range(0, ymax + 1, step)]
    gap = (pts[-1]['when'] - pts[0]['when']).days / 365.25
    rise = pts[-1]['miles'] - pts[0]['miles']
    avg = round(rise / gap / 100) * 100 if gap >= 0.5 and rise > 0 else 0
    for p in pts:
        del p['when']
    return {
        'points': pts, 'line': line, 'area': area, 'xticks': xticks, 'yticks': yticks,
        'avg': f'{avg:,}' if avg else '', 'has_fail': any(not p['passed'] for p in pts),
        'summary': (f"Mileage at {len(pts)} MOT tests, from {pts[0]['recorded']} in {pts[0]['date'][-4:]} "
                    f"to {pts[-1]['recorded']} in {pts[-1]['date'][-4:]}"),
    }


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
        if letter == 'c':
            out['vc_ulez'] = {'ok': True, 'label': 'Compliant'}
        elif letter == 'n':
            out['vc_ulez'] = {'ok': False, 'label': 'Not compliant'}
        else:
            notice = (ULEZ_NOTICES['u'] if letter == 'u' else ULEZ_NOTICES['unclear'] if letter
                      else ULEZ_NOTICES.get(f.get('lez_error', ''), ''))
            out['vc_ulez'] = {'ok': None, 'link': TFL_ULEZ_URL, 'notice': f'({notice})' if notice else ''}
        out['vc_insurance_url'] = INSURANCE_URL
    return out
