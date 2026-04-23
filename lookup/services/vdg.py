"""Vehicle Data Global API client for VRM -> VIN lookups."""
import os
import requests


VDG_BASE_URL = "https://uk.api.vehicledataglobal.com/r2/lookup"


class VdgError(Exception):
    """Raised when a VDG lookup fails."""


class VdgNotFoundError(VdgError):
    """Raised when the registration is not found."""


def get_vin(vrm, timeout=10):
    """Look up a VIN from a UK registration. Returns the VIN string."""
    api_key = os.environ.get('VDG_API_KEY')
    if not api_key:
        raise VdgError("VDG_API_KEY not set in environment")

    params = {
        "apikey": api_key,
        "packagename": "VehicleDetails",
        "vrm": vrm.upper().replace(" ", ""),
    }

    try:
        response = requests.get(VDG_BASE_URL, params=params, timeout=timeout)
        response.raise_for_status()
    except requests.RequestException as e:
        raise VdgError(f"Network error: {e}") from e

    data = response.json()
    status = data.get("ResponseInformation", {}).get("StatusCode")

    if status in (2, 25, 26):
        raise VdgNotFoundError("Vehicle not found")

    if status not in (0, 1):
        message = data.get("ResponseInformation", {}).get("StatusMessage", "Unknown")
        raise VdgError(f"VDG lookup failed ({status}): {message}")

    return data.get("Results", {}).get("VehicleDetails", {}).get(
        "VehicleIdentification", {}).get("Vin")