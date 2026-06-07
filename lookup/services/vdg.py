"""VDG (Vehicle Data Global) API client.

Single combined-package call: PaintCodeDetails has been extended on the VDG
side to also include VehicleDetails + ModelDetails as components, so a single
HTTP request returns vehicle data, model spec, and paint code(s) in one shot.

Cost is unchanged at £0.50 per successful lookup (£0.15 + £0.35 + £0.00 for
the dependency-included Model Details), but latency is roughly halved compared
to the previous two-sequential-call pattern.

Per-document StatusCode is exposed in the response so the caller can record
which documents returned data.
"""
import os
import re

import requests


VDG_BASE_URL = 'https://uk.api.vehicledataglobal.com/r2'
VDG_LOOKUP_ENDPOINT = f'{VDG_BASE_URL}/lookup'

# The single package name used for every lookup. Configured on the VDG side
# to include VehicleDetails + ModelDetails + PaintCodeDetails.
VDG_PACKAGE_NAME = 'PaintCodeDetails'


class VdgError(Exception):
    pass


class VdgNotFoundError(VdgError):
    pass


class VdgTimeoutError(VdgError):
    """The VDG request exceeded the client-side timeout.

    Raised separately from generic VdgError so callers (and the error_message
    column on Search) can distinguish 'VDG was slow' from 'VDG was unreachable'
    or 'VDG returned a 5xx'. All three previously surfaced as the same VdgError.
    """
    pass


def _make_request(registration):
    """Single combined call. Returns the parsed JSON dict, or raises."""
    api_key = os.environ.get('VDG_API_KEY')
    if not api_key:
        raise VdgError('VDG_API_KEY not configured')

    params = {
        'packagename': VDG_PACKAGE_NAME,
        'apikey': api_key,
        'vrm': registration,
    }

    try:
        response = requests.get(VDG_LOOKUP_ENDPOINT, params=params, timeout=30)
    except requests.exceptions.Timeout as e:
        # Raise the timeout-specific subclass so views.py / Sentry can tell
        # this apart from a generic transport failure or VDG 500.
        raise VdgTimeoutError(f'VDG request timed out: {e}')
    except requests.exceptions.RequestException as e:
        raise VdgError(f'VDG request failed: {e}')

    if response.status_code != 200:
        raise VdgError(f'VDG returned {response.status_code}')

    try:
        data = response.json()
    except ValueError:
        raise VdgError('VDG returned invalid JSON')

    response_info = data.get('ResponseInformation', {})
    is_success = response_info.get('IsSuccessStatusCode', False)
    status_message = response_info.get('StatusMessage', '')

    if not is_success:
        if 'not found' in status_message.lower():
            raise VdgNotFoundError(f'Vehicle not found: {registration}')
        if 'Invalid' in status_message and 'Key' in status_message:
            raise VdgError('VDG API key invalid')
        raise VdgError(f'VDG status: {status_message}')

    return data


def _extract_balance(data):
    """Extract account balance from VDG response BillingInformation block."""
    if not data:
        return None
    billing = data.get('BillingInformation', {}) or {}
    return billing.get('AccountBalance')


def smart_title(text):
    """Title-case a string for paint descriptions.

    VDG returns paint descriptions in mixed cases:
      - Sentence case: 'Glacier white-metallic' → 'Glacier White-Metallic'
      - All-caps SHOUTING: 'BLUE IRON' → 'Blue Iron'
      - Already-correct title: 'Limestone Grey' → unchanged

    The function preserves the first character of each word (uppercase)
    and otherwise leaves word internals alone — *unless* the whole input
    is all-caps, in which case we treat it as shouting and lowercase
    the rest of each word.

    Used only on paint descriptions (never makes/models), so we don't
    need to worry about acronymic brand tokens like 'BMW', 'AMG', 'GTI'
    appearing inside the input.

    Splits on whitespace, hyphens, parentheses, and slashes so multi-token
    paint names capitalise correctly: 'metallic deep-blue (sapphire)'
    becomes 'Metallic Deep-Blue (Sapphire)'.
    """
    if not text:
        return text
    # If the whole string is uppercase (no lowercase chars anywhere), it's
    # shouting case — lowercase first so the per-word logic below produces
    # proper Title Case rather than preserving every word as if it were an
    # acronym. 'BLUE IRON' → 'blue iron' here, → 'Blue Iron' below.
    if text.isupper():
        text = text.lower()
    # Split on word boundaries that should trigger capitalisation: whitespace,
    # hyphens, opening/closing parens, slashes. Keep separators by using a
    # capturing group so we can rejoin.
    parts = re.split(r'([\s\-()/])', text)
    out = []
    for p in parts:
        if not p or p.isspace() or p in '-()/':
            out.append(p)
            continue
        # Uppercase first character only — leave rest of word alone so that
        # already-correct casings like 'McLaren' or 'eBoost' survive intact.
        out.append(p[0].upper() + p[1:])
    return ''.join(out)


def normalize_fuel_type(fuel):
    """Normalize fuel type to consumer-friendly format.

    Used both by the VDG parser (when ModelDetails.Powertrain.FuelType is
    missing and we fall back to DvlaFuelType) and by views.py's DVLA fallback
    path. Single source of truth so 'PLUG-IN HYBRID ELECTRIC' etc. stay
    consistent across both code paths.
    """
    if not fuel:
        return ''
    fuel = fuel.upper().strip()
    mapping = {
        'ELECTRICITY': 'Electric',
        'ELECTRIC': 'Electric',
        'PETROL': 'Petrol',
        'GASOLINE': 'Petrol',
        'DIESEL': 'Diesel',
        'HEAVY OIL': 'Diesel',
        'HYBRID ELECTRIC': 'Hybrid',
        'HYBRID': 'Hybrid',
        'PLUG-IN HYBRID': 'Plug-in Hybrid',
        'PLUG-IN HYBRID ELECTRIC': 'Plug-in Hybrid',
        'LPG': 'LPG',
        'CNG': 'CNG',
        'HYDROGEN': 'Hydrogen',
    }
    return mapping.get(fuel, fuel.title())


# Internal alias so existing _parse_vehicle_fields callers keep working
_normalize_fuel_type = normalize_fuel_type


def _clean_case(s):
    """Strip + title-case ALL-CAPS strings, leave mixed-case alone.

    VDG's ModelDetails returns clean names ('Volkswagen', 'Golf SE BlueMotion'),
    but DVLA fallbacks return ALL-CAPS ('VOLKSWAGEN'). This normalises only
    the all-caps case so 'BMW' or 'TDI' tokens within a properly-cased model
    name are preserved.
    """
    s = (s or '').strip()
    return s.title() if s and s.isupper() else s


def _doc_succeeded(doc):
    """A document inside Results.* is considered to have returned data when
    its StatusCode is 0 (Success). VDG occasionally returns a top-level
    success but with one document partially failed — this is the per-doc check.
    """
    if not doc:
        return False
    return doc.get('StatusCode', -1) == 0


def _parse_vehicle_fields(results):
    """Pull vehicle fields out of the combined response.

    Reads from Results.VehicleDetails (DVLA-derived fields like VIN, year,
    colour, body type) and Results.ModelDetails (manufacturer-spec fields
    like clean make/model, transmission, engine description, BHP).

    Returns a dict with the same shape the old get_vehicle_details() used.
    """
    vehicle_details = results.get('VehicleDetails', {}) or {}
    identification = vehicle_details.get('VehicleIdentification', {}) or {}
    vin = identification.get('Vin', '')
    year = identification.get('YearOfManufacture')
    dvla_fuel = identification.get('DvlaFuelType', '')

    vehicle_history = vehicle_details.get('VehicleHistory', {}) or {}
    colour_details = vehicle_history.get('ColourDetails', {}) or {}
    colour = (colour_details.get('CurrentColour', '') or '').title()

    model_details = results.get('ModelDetails', {}) or {}
    model_identification = model_details.get('ModelIdentification', {}) or {}

    # EU type-approval category (M1 passenger, N1/N2/N3 commercial). pl24 needs
    # this to route commercial vehicles (Sprinters, Transits) to the right
    # catalogue — without it they mis-route and burn the full fallback chain
    # (observed ~60s timeout vs ~3s when the category is supplied). Lives at
    # ModelDetails.ModelClassification.TypeApprovalCategory; may be absent on
    # vehicles VDG has no ModelDetails for, in which case we pass '' and pl24
    # falls back to treating it as passenger.
    model_classification = model_details.get('ModelClassification', {}) or {}
    category = (model_classification.get('TypeApprovalCategory', '') or '').strip()

    # Make and model: prefer VDG's curated ModelDetails (clean casing like
    # 'BMW', 'Volkswagen', '730Ld SE Auto'). Only fall back to DvlaMake/DvlaModel
    # when ModelDetails has no value, and clean those because DVLA always
    # returns SHOUTING CASE.
    make_from_model = model_identification.get('Make')
    model_from_model = model_identification.get('Model')

    if make_from_model:
        make = make_from_model.strip()
    else:
        make = _clean_case(identification.get('DvlaMake', ''))

    if model_from_model:
        model = model_from_model.strip()
    else:
        model = _clean_case(identification.get('DvlaModel', ''))

    powertrain = model_details.get('Powertrain', {}) or {}
    fuel_type_raw = powertrain.get('FuelType') or dvla_fuel
    fuel_type_normalised = _normalize_fuel_type(fuel_type_raw)

    # Performance.Power.Bhp lives at ModelDetails.Performance, NOT under Powertrain
    performance = model_details.get('Performance', {}) or {}
    power = performance.get('Power', {}) or {}
    bhp_raw = power.get('Bhp')
    bhp = int(round(bhp_raw)) if bhp_raw is not None else None

    # Transmission — check Powertrain first, then ModelDetails (older docs sometimes
    # nest it differently), then EV TransmissionDetailsList as last resort.
    transmission_data = (
        powertrain.get('Transmission')
        or model_details.get('Transmission')
        or {}
    )
    transmission_type = transmission_data.get('TransmissionType', '') or ''
    number_of_gears = transmission_data.get('NumberOfGears')

    if not transmission_type:
        ev_details_check = powertrain.get('EvDetails') or {}
        ev_tech_check = ev_details_check.get('TechnicalDetails', {}) or {}
        trans_list = ev_tech_check.get('TransmissionDetailsList', []) or []
        if trans_list:
            first_trans = trans_list[0]
            transmission_type = first_trans.get('TransmissionType', '') or ''
            number_of_gears = first_trans.get('NumberOfGears')

    transmission = ''
    if transmission_type:
        if number_of_gears:
            transmission = f'{transmission_type} ({number_of_gears} speed)'
        else:
            transmission = transmission_type

    # Engine description — ICE vs EV branching
    engine_description = ''
    cylinders = None
    battery_kwh = None

    ice_details = powertrain.get('IceDetails')
    if ice_details:
        # Prefer EngineCapacityLitres for a clean simple display ("1.6L")
        capacity_litres = ice_details.get('EngineCapacityLitres')
        if capacity_litres is not None:
            # Format: 1.6 -> "1.6L", 2.0 -> "2.0L". Strip trailing .0 only if exact integer-like.
            if capacity_litres == int(capacity_litres):
                engine_description = f'{capacity_litres:.1f}L'
            else:
                engine_description = f'{capacity_litres}L'
        else:
            # Fallback: raw EngineDescription as VDG returns it (no cleaning)
            engine_description = ice_details.get('EngineDescription', '') or ''
        cylinders = ice_details.get('NumberOfCylinders')
    else:
        ev_details = powertrain.get('EvDetails') or {}
        ev_tech = ev_details.get('TechnicalDetails', {}) or {}
        motor_list = ev_tech.get('MotorDetailsList', []) or []
        if motor_list:
            motor = motor_list[0]
            power_kw = motor.get('PowerKw')
            if power_kw:
                engine_description = f'{int(power_kw)} kW Electric Motor'
            else:
                engine_description = 'Electric Motor'
        battery_list = ev_tech.get('BatteryDetailsList', []) or []
        if battery_list:
            usable_kwh = battery_list[0].get('UsableCapacityKwh')
            if usable_kwh is not None:
                # Drop trailing .0 if it's a whole number
                battery_kwh = int(usable_kwh) if usable_kwh == int(usable_kwh) else usable_kwh

    # Append BHP to engine description if available
    if engine_description and bhp is not None:
        engine_description = f'{engine_description} ({bhp} bhp)'
    elif not engine_description and bhp is not None:
        engine_description = f'({bhp} bhp)'

    # Compose fuel display string with cylinder count (ICE) or battery (EV)
    fuel_display = fuel_type_normalised
    if fuel_display:
        if cylinders is not None:
            unit = 'cylinder' if cylinders == 1 else 'cylinders'
            fuel_display = f'{fuel_display} ({cylinders} {unit})'
        elif battery_kwh is not None:
            fuel_display = f'{fuel_display} ({battery_kwh}kWh battery)'

    return {
        'make': make,
        'model': model,
        'year': year,
        'colour': colour,
        'vin': vin,
        'fuel_type': fuel_display,
        'transmission': transmission,
        'engine_description': engine_description,
        'category': category,
    }


def _parse_paint_fields(results):
    """Extract paint code(s) from Results.PaintCodeDetails.

    Returns dict with:
      - 'code': primary paint code (first in list), '' if none
      - 'description': primary paint description, '' if none
      - 'all_codes': list of all paint codes [{code, description}, ...]

    Paint codes are .upper()'d on the way out — manufacturer convention is
    always upper-case, but VDG occasionally returns mixed case for some
    Ford codes (e.g. 'Pn4lr'). Uppercasing at this boundary means every
    downstream consumer (DB, email, display) sees consistent caps.
    """
    paint_details = results.get('PaintCodeDetails', {}) or {}
    paint_list = paint_details.get('PaintCodeList', []) or []

    if not paint_list:
        return {'code': '', 'description': '', 'all_codes': []}

    all_codes = [
        {
            'code': (p.get('Code', '') or '').strip().upper(),
            'description': smart_title(p.get('Description', '')),
        }
        for p in paint_list
    ]

    first = paint_list[0]
    return {
        'code': (first.get('Code', '') or '').strip().upper(),
        'description': smart_title(first.get('Description', '')),
        'all_codes': all_codes,
    }


def get_combined_lookup(registration):
    """Single combined VDG call. Returns vehicle + paint data + per-doc flags.

    Returns dict with:
      - 'make', 'model', 'year', 'colour', 'vin', 'fuel_type',
        'transmission', 'engine_description'  -- vehicle/model fields
      - 'paint_code', 'paint_description', 'all_paint_codes'  -- paint fields
      - 'vehicle_returned': bool — Results.VehicleDetails StatusCode == 0
      - 'paint_returned':   bool — Results.PaintCodeDetails returned ≥1 paint code
      - 'balance': float — VDG account balance after this call (or None)

    Returns None if VDG reports the vehicle was not found at all.
    Raises VdgError on HTTP / config / unexpected-payload errors.
    """
    try:
        data = _make_request(registration)
    except VdgNotFoundError:
        return None

    results = data.get('Results', {}) or {}
    vehicle_details_doc = results.get('VehicleDetails', {}) or {}
    paint_details_doc = results.get('PaintCodeDetails', {}) or {}

    vehicle_returned = _doc_succeeded(vehicle_details_doc)
    # Paint is "returned" only if both StatusCode is OK *and* there's at least
    # one paint code. An empty PaintCodeList is what triggers VDG's auto-refund.
    paint_list = paint_details_doc.get('PaintCodeList', []) or []
    paint_returned = _doc_succeeded(paint_details_doc) and len(paint_list) > 0

    out = {
        'vehicle_returned': vehicle_returned,
        'paint_returned': paint_returned,
        'balance': _extract_balance(data),
    }

    # Always pull what we can from each document, even on partial success.
    out.update(_parse_vehicle_fields(results))
    paint = _parse_paint_fields(results)
    out['paint_code'] = paint['code']
    out['paint_description'] = paint['description']
    out['all_paint_codes'] = paint['all_codes']

    return out