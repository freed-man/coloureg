"""VDG (Vehicle Data Global) API client."""
import os
import requests


VDG_BASE_URL = 'https://uk.api.vehicledataglobal.com/r2'
VDG_VEHICLE_ENDPOINT = f'{VDG_BASE_URL}/lookup'
VDG_PAINT_ENDPOINT = f'{VDG_BASE_URL}/lookup'


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
        response = requests.get(endpoint, params=params, timeout=15)
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
    """
    try:
        data = _make_request(
            VDG_VEHICLE_ENDPOINT,
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
    engine_number = (identification.get('EngineNumber', '') or '').strip()

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
    fuel_type = powertrain.get('FuelType') or dvla_fuel
    fuel_type = _normalize_fuel_type(fuel_type)

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
            transmission = f'{transmission_type} ({number_of_gears}-speed)'
        else:
            transmission = transmission_type

    engine_description = ''
    ice_details = powertrain.get('IceDetails')
    if ice_details:
        engine_description = ice_details.get('EngineDescription', '') or ''
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

    if engine_description and engine_number:
        engine_description = f'{engine_description} ({engine_number})'
    elif not engine_description and engine_number:
        engine_description = f'({engine_number})'

    return {
        'make': make,
        'model': model,
        'year': year,
        'colour': colour,
        'vin': vin,
        'fuel_type': fuel_type,
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
    """Fetch paint code from VDG Paint Package.

    Returns dict with 'code', 'description', 'balance', or None if no paint code found.
    """
    try:
        data = _make_request(
            VDG_PAINT_ENDPOINT,
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
        # Return balance even when no paint data (so we can still update it)
        return {'code': '', 'description': '', 'balance': balance, 'found': False}

    first = paint_list[0]
    return {
        'code': first.get('Code', ''),
        'description': first.get('Description', ''),
        'balance': balance,
        'found': True,
    }