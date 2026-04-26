import os
import time
import requests
from django.shortcuts import render, redirect
from django.contrib import messages
from django.views.decorators.http import require_POST
from django_ratelimit.core import is_ratelimited
from .models import Search
from .services.vdg import get_vin, get_paint_code, VdgError, VdgNotFoundError
from .services.email import (
    send_user_paint_code,
    send_admin_failure_notification,
    send_user_pending_notification,
    send_admin_contact_message,
    send_user_contact_confirmation,
)


def get_client_ip(request):
    """Extract the client IP address, handling proxy headers."""
    x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
    if x_forwarded_for:
        return x_forwarded_for.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR')


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
        # --- Rate limit check: 10 lookups per hour per IP ---
        was_limited = is_ratelimited(
            request,
            group='lookup',
            key='ip',
            rate='10/h',
            method='POST',
            increment=True,
        )
        if was_limited:
            messages.error(
                request,
                'Too many searches. Please wait an hour before trying again.'
            )
            return render(request, 'lookup/index.html')

        start_time = time.time()
        registration = request.POST.get('registration', '').strip().upper()
        registration = registration.replace(' ', '')

        if not registration:
            messages.error(request, 'Please enter a registration number.')
            return redirect('index')

        # Easter egg for Paige
        if registration == 'PNZ282':
            return redirect('paige')

        # Start a Search log entry
        search = Search(
            registration=registration,
            ip_address=get_client_ip(request),
            user_agent=request.META.get('HTTP_USER_AGENT', ''),
        )

        # Call DVLA API
        dvla = get_dvla_data(registration)
        if not dvla:
            search.success = False
            search.error_message = 'DVLA API: vehicle not found'
            search.lookup_duration_ms = int((time.time() - start_time) * 1000)
            search.save()
            messages.error(
                request,
                'Vehicle not found. Please check the registration '
                'number is correct and try again.'
            )
            return redirect('index')

        # Populate vehicle data from DVLA
        search.make = dvla.get('make', '')
        search.year = dvla.get('yearOfManufacture')
        search.colour = dvla.get('colour', '')

        # Call DVSA MOT API for model name
        mot = get_mot_data(registration)
        model = extract_mot_field(mot, 'model') or ''
        search.model = model

        # --- VDG Paint Package (PRIMARY) ---
        paint_code = None
        paint_description = None
        try:
            paint_result = get_paint_code(registration)
            search.vdg_paint_called = True
            if paint_result:
                paint_code = paint_result['code']
                paint_description = paint_result['description']
                search.paint_code = paint_code
                search.paint_description = paint_description
                search.provider = Search.PROVIDER_VDG
        except (VdgError, VdgNotFoundError) as e:
            search.vdg_paint_called = True
            search.error_message = f'VDG Paint: {str(e)[:200]}'

        # --- VDG VehicleDetails for VIN (FALLBACK only if no paint code) ---
        vin = None
        if not paint_code:
            try:
                vin = get_vin(registration)
                search.vdg_vehicle_called = True
                search.vin = vin or ''
            except (VdgError, VdgNotFoundError) as e:
                search.vdg_vehicle_called = True
                existing = search.error_message or ''
                search.error_message = f'{existing} | VDG Vehicle: {str(e)[:200]}'.strip(' |')

        # Mark successful (vehicle found, even if paint code didn't come through)
        search.success = True
        search.lookup_duration_ms = int((time.time() - start_time) * 1000)
        search.save()

        # Store in session for the results page
        request.session['vehicle_data'] = {
            'dvla': dvla,
            'mot': mot,
            'vin': vin,
            'paint_code': paint_code,
            'paint_description': paint_description,
            'registration': registration,
            'search_id': search.id,
        }

        return redirect('results')

    return render(request, 'lookup/index.html')


def paige(request):
    return render(request, 'lookup/paige.html')


def results(request):
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
    paint_code = vehicle_data.get('paint_code')
    paint_description = vehicle_data.get('paint_description')

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

    # Check if email was already submitted for this search
    email_submitted = request.session.pop('email_submitted', None)

    context = {
        'registration': registration,
        'dvla': dvla,
        'model': model,
        'make_logo': make_logo,
        'make': dvla.get('make', ''),
        'year': dvla.get('yearOfManufacture'),
        'colour': dvla.get('colour', ''),
        'paint_code': paint_code,
        'paint_description': paint_description,
        'search_id': vehicle_data.get('search_id'),
        'email_submitted': email_submitted,
    }

    return render(request, 'lookup/results.html', context)


@require_POST
def submit_email(request):
    """Handle email submission from results page."""
    search_id = request.POST.get('search_id')
    email = request.POST.get('email', '').strip()

    if not search_id or not email:
        messages.error(request, 'Email address is required.')
        return redirect('results')

    try:
        search = Search.objects.get(id=search_id)
    except Search.DoesNotExist:
        messages.error(request, 'Search record not found.')
        return redirect('index')

    # Prevent duplicate submissions (refresh spam)
    if search.email_sent:
        request.session['email_submitted'] = search.email
        return redirect('results')

    # Rate limit email submissions: 5 per hour per IP
    was_limited = is_ratelimited(
        request,
        group='email_submit',
        key='ip',
        rate='5/h',
        method='POST',
        increment=True,
    )
    if was_limited:
        messages.error(
            request,
            'Too many email requests. Please wait an hour before trying again.'
        )
        return redirect('results')

    # Save email to Search log
    search.email = email
    search.save()

    if search.paint_code:
        sent = send_user_paint_code(
            email,
            search.registration,
            search.make,
            search.model,
            search.year,
            search.paint_code,
            search.paint_description,
        )
        if sent:
            search.email_sent = True
            search.save()
    else:
        admin_sent = send_admin_failure_notification(
            search.registration,
            search.make,
            search.model,
            search.year,
            search.colour,
            search.vin,
            email,
        )
        user_sent = send_user_pending_notification(email, search.registration, search.make)
        if admin_sent and user_sent:
            search.email_sent = True
            search.save()

    # Store confirmation in session so results page knows to hide form
    request.session['email_submitted'] = email

    return redirect('results')

def info(request):
    """Info/about page with contact form."""
    contact_submitted = request.session.pop('contact_submitted', None)
    return render(request, 'lookup/info.html', {
        'contact_submitted': contact_submitted,
    })


@require_POST
def submit_contact(request):
    """Handle contact form submission from info page."""
    contact_type = request.POST.get('type', 'general').strip()
    email = request.POST.get('email', '').strip()
    message = request.POST.get('message', '').strip()

    if not email or not message:
        messages.error(request, 'Email and message are required.')
        return redirect('info')

    # Rate limit: 3 per hour per IP
    was_limited = is_ratelimited(
        request,
        group='contact',
        key='ip',
        rate='3/h',
        method='POST',
        increment=True,
    )
    if was_limited:
        messages.error(
            request,
            'Too many messages. Please wait an hour before trying again.'
        )
        return redirect('info')

    # Send admin notification
    from .services.email import send_admin_contact_message, send_user_contact_confirmation
    admin_sent = send_admin_contact_message(contact_type, email, message)
    user_sent = send_user_contact_confirmation(email)

    if admin_sent and user_sent:
        request.session['contact_submitted'] = email

    return redirect('info')