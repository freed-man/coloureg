import os
import time
import requests
from django.shortcuts import render, redirect
from django.contrib import messages
from django.views.decorators.http import require_POST
from django_ratelimit.core import is_ratelimited
from .models import Search
from .services.vdg import (
    get_vehicle_details,
    get_vin,
    get_paint_code,
    VdgError,
    VdgNotFoundError,
)
from .services.email import (
    send_user_paint_code,
    send_admin_failure_notification,
    send_user_pending_notification,
    send_admin_contact_message,
    send_user_contact_confirmation,
)


def get_client_ip(request):
    x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
    if x_forwarded_for:
        return x_forwarded_for.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR')


def extract_mot_field(mot_data, field_name):
    if mot_data and isinstance(mot_data, dict):
        return mot_data.get(field_name)
    elif mot_data and isinstance(mot_data, list) and len(mot_data) > 0:
        return mot_data[0].get(field_name)
    return None


def get_dvla_data(registration):
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


def normalize_fuel_type(fuel):
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
        'LPG': 'LPG',
        'CNG': 'CNG',
        'HYDROGEN': 'Hydrogen',
    }
    return mapping.get(fuel, fuel.title())


def mask_vin(vin):
    if not vin or len(vin) < 6:
        return vin or ''
    return vin[:3] + '*' * (len(vin) - 6) + vin[-3:]


def index(request):
    if request.method == 'POST':
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

        if registration == 'PNZ282':
            return redirect('paige')

        search = Search(
            registration=registration,
            ip_address=get_client_ip(request),
            user_agent=request.META.get('HTTP_USER_AGENT', ''),
        )

        # --- VDG VehicleDetails (PRIMARY) ---
        vdg_details = None
        try:
            vdg_details = get_vehicle_details(registration)
            search.vdg_vehicle_called = True
        except (VdgError, VdgNotFoundError) as e:
            search.vdg_vehicle_called = True
            search.error_message = f'VDG Vehicle: {str(e)[:200]}'

        make = ''
        model = ''
        year = None
        colour = ''
        vin = None
        fuel_type = ''
        transmission = ''
        engine_description = ''

        if vdg_details:
            make = vdg_details.get('make', '')
            model = vdg_details.get('model', '')
            year = vdg_details.get('year')
            colour = vdg_details.get('colour', '')
            vin = vdg_details.get('vin', '')
            fuel_type = vdg_details.get('fuel_type', '')
            transmission = vdg_details.get('transmission', '')
            engine_description = vdg_details.get('engine_description', '')
        else:
            # --- FALLBACK: DVLA + MOT ---
            dvla = get_dvla_data(registration)
            if not dvla:
                search.success = False
                search.lookup_duration_ms = int((time.time() - start_time) * 1000)
                if not search.error_message:
                    search.error_message = 'DVLA + VDG: vehicle not found'
                else:
                    search.error_message += ' | DVLA: not found'
                search.save()
                messages.error(
                    request,
                    'Vehicle not found. Please check the registration '
                    'number is correct and try again.'
                )
                return redirect('index')

            make = (dvla.get('make', '') or '').title()
            year = dvla.get('yearOfManufacture')
            colour = (dvla.get('colour', '') or '').title()
            fuel_type = normalize_fuel_type(dvla.get('fuelType', ''))

            mot = get_mot_data(registration)
            mot_model = extract_mot_field(mot, 'model') or ''
            model = mot_model.title()

        search.make = make
        search.model = model
        search.year = year
        search.colour = colour
        search.vin = vin or ''

        # --- VDG Paint Package ---
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
            existing = search.error_message or ''
            search.error_message = f'{existing} | VDG Paint: {str(e)[:200]}'.strip(' |')

        search.success = True
        search.lookup_duration_ms = int((time.time() - start_time) * 1000)
        search.save()

        make_raw = make.lower().replace('-', '_').replace(' ', '_')
        make_logo_map = {
            'mercedes': 'mercedes_benz',
            'vw': 'volkswagen',
            'landrover': 'land_rover',
            'alfaromeo': 'alfa_romeo',
        }
        make_logo = make_logo_map.get(make_raw, make_raw)

        request.session['vehicle_data'] = {
            'make': make,
            'model': model,
            'year': year,
            'colour': colour,
            'fuel_type': fuel_type,
            'transmission': transmission,
            'engine_description': engine_description,
            'registration': registration,
            'vin': vin,
            'vin_masked': mask_vin(vin),
            'paint_code': paint_code,
            'paint_description': paint_description,
            'make_logo': make_logo,
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

    email_submitted = request.session.pop('email_submitted', None)

    context = {
        'registration': vehicle_data.get('registration', ''),
        'make': vehicle_data.get('make', ''),
        'model': vehicle_data.get('model', ''),
        'year': vehicle_data.get('year'),
        'colour': vehicle_data.get('colour', ''),
        'fuel_type': vehicle_data.get('fuel_type', ''),
        'transmission': vehicle_data.get('transmission', ''),
        'engine_description': vehicle_data.get('engine_description', ''),
        'vin_masked': vehicle_data.get('vin_masked', ''),
        'make_logo': vehicle_data.get('make_logo', ''),
        'paint_code': vehicle_data.get('paint_code'),
        'paint_description': vehicle_data.get('paint_description'),
        'search_id': vehicle_data.get('search_id'),
        'email_submitted': email_submitted,
    }

    return render(request, 'lookup/results.html', context)


@require_POST
def submit_email(request):
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

    if search.email_sent:
        request.session['email_submitted'] = search.email
        return redirect('results')

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

    request.session['email_submitted'] = email
    return redirect('results')


def about(request):
    contact_submitted = request.session.pop('contact_submitted', None)
    return render(request, 'lookup/about.html', {
        'contact_submitted': contact_submitted,
    })


@require_POST
def submit_contact(request):
    contact_type = request.POST.get('type', 'general').strip()
    email = request.POST.get('email', '').strip()
    message = request.POST.get('message', '').strip()

    if not email or not message:
        messages.error(request, 'Email and message are required.')
        return redirect('about')

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
        return redirect('about')

    admin_sent = send_admin_contact_message(contact_type, email, message)
    user_sent = send_user_contact_confirmation(email)

    if admin_sent and user_sent:
        request.session['contact_submitted'] = email

    return redirect('about')