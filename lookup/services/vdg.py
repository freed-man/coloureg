"""VDG (Vehicle Data Global) API client.

Handles calls to VDG for:
- Vehicle details (VehicleDetails package) — make, model, year, colour, VIN
- Paint code lookup (Paint Package — launches May 2026)
"""
import os
import requests


VDG_BASE_URL = 'https://uk.api.vehicledataglobal.com/r2'
VDG_VEHICLE_ENDPOINT = f'{VDG_BASE_URL}/lookup'
VDG_PAINT_ENDPOINT = f'{VDG_BASE_URL}/lookup'


class VdgError(Exception):
    """Generic VDG API error."""


class VdgNotFoundError(VdgError):
    """Vehicle not found in VDG."""


def _make_request(endpoint, package_name, registration):
    """Shared VDG API request handler. Returns the parsed JSON response."""
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

    # Check ResponseInformation for status
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


def get_vehicle_details(registration):
    """Fetch full vehicle details from VDG VehicleDetails package.

    Returns dict with: make, model, year, colour, vin
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
    vehicle_details = results.get('VehicleDetails', {})

    # Vehicle identification (VIN, year, basic info)
    identification = vehicle_details.get('VehicleIdentification', {}) or {}
    vin = identification.get('Vin', '')
    year = identification.get('YearOfManufacture')

    # Model details (cleaner make/model than DVLA all-caps)
    model_details = vehicle_details.get('ModelDetails', {}) or {}
    model_identification = model_details.get('ModelIdentification', {}) or {}
    make = model_identification.get('Make', '') or identification.get('DvlaMake', '')
    model = model_identification.get('Model', '') or identification.get('DvlaModel', '')

    # Colour from VehicleHistory
    vehicle_history = vehicle_details.get('VehicleHistory', {}) or {}
    colour_details = vehicle_history.get('ColourDetails', {}) or {}
    colour = colour_details.get('CurrentColour', '')

    return {
        'make': make,
        'model': model,
        'year': year,
        'colour': colour,
        'vin': vin,
    }


def get_vin(registration):
    """Fetch just the VIN. Compatibility wrapper around get_vehicle_details."""
    details = get_vehicle_details(registration)
    if details:
        return details.get('vin')
    return None


def get_paint_code(registration):
    """Fetch paint code from VDG Paint Package.

    Returns dict with 'code' and 'description', or None if no paint code found.
    Example: {'code': '775U', 'description': 'IRIDIUM SILVER - METALLIC FINISH'}

    The Paint Package launches May 2026. Until then this will fail
    (VDG will respond with NotFound or similar for unknown packages).
    """
    try:
        data = _make_request(
            VDG_PAINT_ENDPOINT,
            'PaintCodeDetails',
            registration,
        )
    except VdgNotFoundError:
        return None

    results = data.get('Results', {})
    paint_details = results.get('PaintCodeDetails', {})
    paint_list = paint_details.get('PaintCodeList', [])

    if not paint_list:
        return None

    first = paint_list[0]
    return {
        'code': first.get('Code', ''),
        'description': first.get('Description', ''),
    }