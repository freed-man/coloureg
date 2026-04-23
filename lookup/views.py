from django.shortcuts import render, redirect
from django.contrib import messages
from .models import PaintColor
from .services.vdg import get_vin, VdgError, VdgNotFoundError
import requests
import os


def normalize(text):
    """Normalize text for database matching."""
    return text.strip().lower().replace(
        "-", "").replace(" ", "").replace(".", "")


def extract_mot_field(mot_data, field_name):
    """Extract a field from MOT data which can be dict or list."""
    if mot_data and isinstance(mot_data, dict):
        return mot_data.get(field_name)
    elif mot_data and isinstance(mot_data, list) and len(mot_data) > 0:
        return mot_data[0].get(field_name)
    return None


def get_dvla_data(registration):
    """Fetch vehicle data from the DVLA VES API."""
    url = os.environ.get('DVLA_API_URL')
    api_key = os.environ.get('DVLA_API_KEY')
    headers = {
        'x-api-key': api_key,
        'Content-Type': 'application/json',
    }
    payload = {'registrationNumber': registration}

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        if response.status_code == 200:
            return response.json()
    except requests.exceptions.RequestException:
        return None
    return None


def get_mot_access_token():
    """Get an OAuth2 access token from the DVSA MOT API."""
    token_url = os.environ.get('MOT_TOKEN_URL')
    client_id = os.environ.get('MOT_CLIENT_ID')
    client_secret = os.environ.get('MOT_CLIENT_SECRET')
    scope = os.environ.get('MOT_SCOPE')

    data = {
        'grant_type': 'client_credentials',
        'client_id': client_id,
        'client_secret': client_secret,
        'scope': scope,
    }

    try:
        response = requests.post(token_url, data=data, timeout=10)
        if response.status_code == 200:
            return response.json().get('access_token')
    except requests.exceptions.RequestException:
        return None
    return None


def get_mot_data(registration):
    """Fetch MOT data from the DVSA API — used only to get the model name."""
    access_token = get_mot_access_token()
    if not access_token:
        return None

    api_base = os.environ.get('MOT_API_BASE')
    api_key = os.environ.get('MOT_API_KEY')
    url = f"{api_base}/v1/trade/vehicles/registration/{registration}"

    headers = {
        'Authorization': f'Bearer {access_token}',
        'X-API-Key': api_key,
    }

    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            return response.json()
    except requests.exceptions.RequestException:
        return None
    return None


def index(request):
    if request.method == 'POST':
        return redirect('paige')
    return render(request, 'lookup/index.html')


def paige(request):
    return render(request, 'lookup/paige.html')

# def index(request):
    if request.method == 'POST':
        registration = request.POST.get('registration', '').strip().upper()
        registration = registration.replace(' ', '')

        if not registration:
            messages.error(request, 'Please enter a registration number.')
            return redirect('index')

        # Call DVLA API
        dvla = get_dvla_data(registration)
        if not dvla:
            messages.error(
                request,
                'Vehicle not found. Please check the registration '
                'number is correct and try again.'
            )
            return redirect('index')

        # Call DVSA MOT API for model name
        mot = get_mot_data(registration)

        # Call VDG for VIN (internal use only — not displayed)
        try:
            vin = get_vin(registration)
        except (VdgError, VdgNotFoundError):
            vin = None

        # Store in session for the results page
        request.session['vehicle_data'] = {
            'dvla': dvla,
            'mot': mot,
            'vin': vin,
            'registration': registration,
        }

        return redirect('results')

    return render(request, 'lookup/index.html')


# def results(request):
    vehicle_data = request.session.get('vehicle_data')

    if not vehicle_data:
        messages.error(
            request,
            'No vehicle data found. Please look up a vehicle first.'
        )
        return redirect('index')

    dvla = vehicle_data.get('dvla', {})
    mot = vehicle_data.get('mot')
    registration = vehicle_data.get('registration', '')

    # Extract model from MOT data
    model = extract_mot_field(mot, 'model') or ''

    # Make logo filename
    make_raw = dvla.get(
        'make', '').lower().replace('-', '_').replace(' ', '_')
    make_logo_map = {
        'mercedes': 'mercedes_benz',
        'vw': 'volkswagen',
        'landrover': 'land_rover',
        'alfaromeo': 'alfa_romeo',
    }
    make_logo = make_logo_map.get(make_raw, make_raw)

    # Query paint database
    make = dvla.get('make', '')
    year = dvla.get('yearOfManufacture')
    colour = dvla.get('colour', '')

    # Map DVLA colour to database color_group
    dvla_to_group = {
        'silver': 'grey',
        'maroon': 'red',
        'cream': 'white',
        'bronze': 'brown',
        'turquoise': 'blue',
        'navy': 'blue',
    }
    colour_group = colour.lower() if colour else ''
    colour_group = dvla_to_group.get(colour_group, colour_group)

    norm_make = normalize(make)
    norm_model = normalize(model) if model else ''

    filters = {
        'normalized_manufacturer': norm_make,
    }
    if year:
        filters['year'] = year
    if colour_group:
        filters['color_group'] = colour_group

    # Exact model match first
    if norm_model:
        colors = PaintColor.objects.filter(
            normalized_model=norm_model, **filters)

        # Partial match — DVSA returns 'ID3 FAMILY', db has 'id3'
        if not colors.exists():
            db_models = PaintColor.objects.filter(
                **filters
            ).values_list(
                'normalized_model', flat=True
            ).distinct()
            for db_model in db_models:
                if norm_model.startswith(db_model):
                    colors = PaintColor.objects.filter(
                        normalized_model=db_model, **filters)
                    break

        # Fall back to no model filter
        if not colors.exists():
            colors = PaintColor.objects.filter(**filters)
    else:
        colors = PaintColor.objects.filter(**filters)

    context = {
        'registration': registration,
        'dvla': dvla,
        'model': model,
        'make_logo': make_logo,
        'make': make,
        'year': year,
        'colour': colour,
        'colors': colors,
    }

    return render(request, 'lookup/results.html', context)