"""VDG (Vehicle Data Global) API client.

Handles calls to VDG for:
- VIN lookup (VehicleDetails package)
- Paint code lookup (Paint Package — launches May 2026)
"""
import os
import requests


VDG_BASE_URL = 'https://uk.api.vehicledataglobal.com/r2'
VDG_VEHICLE_ENDPOINT = f'{VDG_BASE_URL}/lookup'
VDG_PAINT_ENDPOINT = f'{VDG_BASE_URL}/lookup'  # same endpoint, different package


class VdgError(Exception):
    """Generic VDG API error."""


class VdgNotFoundError(VdgError):
    """Vehicle not found in VDG."""


def _make_request(endpoint, package_name, registration):
    """Shared VDG API request handler."""
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

    # Check response status
    response_data = data.get('Response', {})
    status_code = response_data.get('StatusCode', '')
    if status_code == 'KeyInvalid':
        raise VdgError('VDG API key invalid')
    if status_code == 'ItemNotFound':
        raise VdgNotFoundError(f'Vehicle not found: {registration}')
    if status_code != 'Success':
        raise VdgError(f'VDG status: {status_code}')

    return response_data.get('DataItems', {})


def get_vin(registration):
    """Fetch VIN from VDG VehicleDetails package."""
    data_items = _make_request(
        VDG_VEHICLE_ENDPOINT,
        'VehicleDetails',
        registration,
    )
    vehicle_details = data_items.get('VehicleDetails', {})
    vin = vehicle_details.get('VehicleIdentification', {}).get('VIN')
    return vin


def get_paint_code(registration):
    """Fetch paint code from VDG Paint Package.

    Returns dict with 'code' and 'description', or None if no paint code found.
    Example: {'code': '775U', 'description': 'IRIDIUM SILVER - METALLIC FINISH'}

    The Paint Package launches May 2026. Until then this will return None
    (VDG will respond with ItemNotFound or similar for unknown packages).
    """
    try:
        data_items = _make_request(
            VDG_PAINT_ENDPOINT,
            'PaintCodeDetails',
            registration,
        )
    except VdgNotFoundError:
        return None

    paint_details = data_items.get('PaintCodeDetails', {})
    paint_list = paint_details.get('PaintCodeList', [])

    if not paint_list:
        return None

    # Take the first paint code (usually the primary colour)
    first = paint_list[0]
    return {
        'code': first.get('Code', ''),
        'description': first.get('Description', ''),
    }