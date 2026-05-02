"""VDG (Vehicle Data Global) API client."""
import os
import requests


VDG_BASE_URL = 'https://uk.api.vehicledataglobal.com/r2'
VDG_LOOKUP_ENDPOINT = f'{VDG_BASE_URL}/lookup'


class VdgError(Exception):
    pass


class VdgNotFoundError(VdgError):
    pass


def _make_request(endpoint, package_name, registration):
    api_key = os.environ.get('VDG_API_KEY')
    if not api_key:
        raise VdgError('VDG_API_KEY not configured')

    params = {
        'packagename': package_name,
        'apikey': api_key,
        'vrm': registration,
    }

    try:
        response = requests.get(endpoint, params=params, timeout=30)
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
        if 'NotFound' in status_message or 'not found' in status_message.lower():
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
    """Title-case a string while preserving all-uppercase tokens.

    VDG returns paint descriptions in sentence case ('Glacier white-metallic'),
    which we want to display as 'Glacier White-Metallic'. Python's built-in
    .title() would also wrongly lowercase already-uppercase tokens like 'BMW',
    so this function only capitalises the first letter of each word and leaves
    the rest of each word untouched.

    Splits on whitespace, hyphens, and parentheses so multi-token paint names
    capitalise correctly: 'metallic deep-blue (sapphire)' becomes
    'Metallic Deep-Blue (Sapphire)'.
    """
    if not text:
        return text
    import re
    # Split on word boundaries that should trigger capitalisation: whitespace,
    # hyphens, opening/closing parens, slashes. Keep separators by using a
    # capturing group so we can rejoin.
    parts = re.split(r'([\s\-()/])', text)
    out = []
    for p in parts:
        if not p or p.isspace() or p in '-()/':
            out.append(p)
            continue
        # Skip already all-uppercase tokens (e.g. 'BMW', 'AMG', 'GTI')
        if p.isupper():
            out.append(p)
            continue
        # Otherwise, uppercase first character only — leave rest of word alone
        out.append(p[0].upper() + p[1:])
    return ''.join(out)


def _normalize_fuel_type(fuel):
    """Normalize fuel type to consumer-friendly format."""
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


def get_vehicle_details(registration):
    """Fetch full vehicle details from VDG VehicleDetails package.

    Returns dict with: make, model, year, colour, vin, fuel_type,
    transmission, engine_description, balance.
    Or None if vehicle not found.

    fuel_type and engine_description are pre-composed display strings:
      ICE: engine_description = '1.6 TDI BLUEMOTION (103bhp)'
           fuel_type          = 'Diesel (4 cylinders)'
      EV:  engine_description = '150 kW Electric Motor (201bhp)'
           fuel_type          = 'Electric (58kWh battery)'
    """
    try:
        data = _make_request(
            VDG_LOOKUP_ENDPOINT,
            'VehicleDetails',
            registration,
        )
    except VdgNotFoundError:
        return None

    results = data.get('Results', {})

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

    make = model_identification.get('Make') or identification.get('DvlaMake', '')
    model = model_identification.get('Model') or identification.get('DvlaModel', '')

    make = (make or '').strip()
    if make and make.isupper():
        make = make.title()
    model = (model or '').strip()
    if model and model.isupper():
        model = model.title()

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
        'balance': _extract_balance(data),
    }


def get_vin(registration):
    """Fetch just the VIN. Compatibility wrapper."""
    details = get_vehicle_details(registration)
    if details:
        return details.get('vin')
    return None


def get_paint_code(registration):
    """Fetch paint code(s) from VDG Paint Package.

    Returns dict with:
      - 'code': primary paint code (first in list)
      - 'description': primary paint description
      - 'all_codes': list of all paint codes [{code, description}, ...]
      - 'balance': latest VDG balance
      - 'found': True/False
    """
    try:
        data = _make_request(
            VDG_LOOKUP_ENDPOINT,
            'PaintCodeDetails',
            registration,
        )
    except VdgNotFoundError:
        return None

    balance = _extract_balance(data)

    results = data.get('Results', {})
    paint_details = results.get('PaintCodeDetails', {})
    paint_list = paint_details.get('PaintCodeList', [])

    if not paint_list:
        return {
            'code': '',
            'description': '',
            'all_codes': [],
            'balance': balance,
            'found': False,
        }

    all_codes = [
        {'code': p.get('Code', ''), 'description': smart_title(p.get('Description', ''))}
        for p in paint_list
    ]

    first = paint_list[0]
    return {
        'code': first.get('Code', ''),
        'description': smart_title(first.get('Description', '')),
        'all_codes': all_codes,
        'balance': balance,
        'found': True,
    }