from django.shortcuts import render, redirect
from django.contrib import messages
from datetime import datetime, date
from .models import PaintColor
from .tax_rates import get_annual_tax
import requests
import os


def normalize(text):
    """Normalize text for database matching."""
    return text.strip().lower().replace(
        "-", "").replace(" ", "").replace(".", "")


def format_date(date_string):
    """Convert API date strings to dd/mm/yyyy format."""
    if not date_string:
        return None
    try:
        if 'T' in date_string:
            dt = datetime.strptime(date_string[:10], '%Y-%m-%d')
        else:
            dt = datetime.strptime(date_string, '%Y-%m-%d')
        return dt.strftime('%d/%m/%Y')
    except ValueError:
        return date_string


def format_mileage(value):
    """Format mileage with commas e.g. 103449 -> 103,449"""
    if not value:
        return None
    try:
        return f"{int(value):,}"
    except (ValueError, TypeError):
        return value


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
        response = requests.post(url, json=payload, headers=headers)
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
        response = requests.post(token_url, data=data)
        if response.status_code == 200:
            return response.json().get('access_token')
    except requests.exceptions.RequestException:
        return None
    return None


def get_mot_data(registration):
    """Fetch MOT history from the DVSA API using OAuth2 token."""
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
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            return response.json()
    except requests.exceptions.RequestException:
        return None
    return None


def index(request):
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

        # Call DVSA MOT API for model name and MOT history
        mot = get_mot_data(registration)

        # Store in session for the results page
        request.session['vehicle_data'] = {
            'dvla': dvla,
            'mot': mot,
            'registration': registration,
        }

        return redirect('results')

    return render(request, 'lookup/index.html')


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

    # Extract model and MOT tests
    model = extract_mot_field(mot, 'model') or ''
    mot_tests = extract_mot_field(mot, 'motTests') or []

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

    # Clean up fuel type display
    fuel_display = dvla.get('fuelType', '')
    if fuel_display.upper() == 'ELECTRICITY':
        fuel_display = 'ELECTRIC'

    # Vehicle age
    vehicle_age_text = ''
    reg_date_raw = extract_mot_field(mot, 'registrationDate')
    if reg_date_raw:
        try:
            manufacture_date = datetime.strptime(
                reg_date_raw[:10], '%Y-%m-%d').date()
            delta = (date.today() - manufacture_date).days
            years = delta // 365
            months = (delta % 365) // 30
            if years > 0 and months > 0:
                vehicle_age_text = (
                    f"{years} year{'s' if years != 1 else ''}, "
                    f"{months} month{'s' if months != 1 else ''} old"
                )
            elif years > 0:
                vehicle_age_text = (
                    f"{years} year{'s' if years != 1 else ''} old"
                )
            else:
                vehicle_age_text = (
                    f"{months} month{'s' if months != 1 else ''} old"
                )
        except (ValueError, TypeError):
            pass
    if not vehicle_age_text:
        year_of_manufacture = dvla.get('yearOfManufacture')
        if year_of_manufacture:
            try:
                manufacture_date = date(int(year_of_manufacture), 1, 1)
                delta = (date.today() - manufacture_date).days
                years = delta // 365
                months = (delta % 365) // 30
                if years > 0 and months > 0:
                    vehicle_age_text = (
                        f"{years} year{'s' if years != 1 else ''}, "
                        f"{months} month{'s' if months != 1 else ''} old"
                    )
                elif years > 0:
                    vehicle_age_text = (
                        f"{years} year{'s' if years != 1 else ''} old"
                    )
                else:
                    vehicle_age_text = (
                        f"{months} month{'s' if months != 1 else ''} old"
                    )
            except (ValueError, TypeError):
                pass

    # MOT days remaining/overdue
    mot_days_text = ''
    mot_expiry = dvla.get('motExpiryDate')
    if mot_expiry:
        try:
            expiry_date = datetime.strptime(
                mot_expiry, '%Y-%m-%d').date()
            delta = (expiry_date - date.today()).days
            if delta > 0:
                mot_days_text = f"{delta} days remaining"
            elif delta == 0:
                mot_days_text = "expires today"
            else:
                mot_days_text = f"{abs(delta)} days overdue"
        except ValueError:
            pass

    # New car MOT handling
    new_car_mot = False
    new_car_mot_date = ''
    new_car_mot_days = ''
    if dvla.get('motStatus') == 'No details held by DVLA':
        mot_due_raw = extract_mot_field(mot, 'motTestDueDate')
        if mot_due_raw:
            try:
                mot_due = datetime.strptime(
                    mot_due_raw[:10], '%Y-%m-%d').date()
                new_car_mot_date = mot_due.strftime('%d/%m/%Y')
                delta = (mot_due - date.today()).days
                if delta > 0:
                    new_car_mot_days = f"{delta} days remaining"
                else:
                    new_car_mot_days = f"{abs(delta)} days overdue"
                new_car_mot = True
            except (ValueError, TypeError):
                pass

    # Tax days remaining/overdue
    tax_days_text = ''
    tax_due = dvla.get('taxDueDate')
    if tax_due:
        try:
            due_date = datetime.strptime(tax_due, '%Y-%m-%d').date()
            delta = (due_date - date.today()).days
            if delta > 0:
                tax_days_text = f"{delta} days remaining"
            elif delta == 0:
                tax_days_text = "due today"
            else:
                tax_days_text = f"{abs(delta)} days overdue"
        except ValueError:
            pass

    # V5C keeper duration
    v5c_days_text = ''
    v5c_date = dvla.get('dateOfLastV5CIssued')
    if v5c_date:
        try:
            issued_date = datetime.strptime(
                v5c_date, '%Y-%m-%d').date()
            delta = (date.today() - issued_date).days
            years = delta // 365
            months = (delta % 365) // 30
            if years > 0 and months > 0:
                v5c_days_text = (
                    f"{years} year{'s' if years != 1 else ''}, "
                    f"{months} month{'s' if months != 1 else ''}"
                )
            elif years > 0:
                v5c_days_text = (
                    f"{years} year{'s' if years != 1 else ''}"
                )
            else:
                v5c_days_text = (
                    f"{months} month{'s' if months != 1 else ''}"
                )
        except ValueError:
            pass

    # Tax estimate
    co2 = dvla.get('co2Emissions')
    reg_month_raw = dvla.get('monthOfFirstRegistration', '')
    reg_year = None
    reg_month = None
    if reg_month_raw:
        try:
            parts = reg_month_raw.split('-')
            reg_year = int(parts[0])
            reg_month = int(parts[1])
        except (ValueError, IndexError):
            reg_year = dvla.get('yearOfManufacture')
    else:
        reg_year = dvla.get('yearOfManufacture')

    tax_estimate = get_annual_tax(
        co2, dvla.get('fuelType', ''), reg_year, reg_month,
        dvla.get('engineCapacity')
    )

    # Format dates for display
    if dvla.get('motExpiryDate'):
        dvla['motExpiryDateFormatted'] = format_date(
            dvla['motExpiryDate'])
    if dvla.get('taxDueDate'):
        dvla['taxDueDateFormatted'] = format_date(dvla['taxDueDate'])
    if dvla.get('dateOfLastV5CIssued'):
        dvla['v5cDateFormatted'] = format_date(
            dvla['dateOfLastV5CIssued'])

    # Format MOT tests
    for test in mot_tests:
        test['completedDateFormatted'] = format_date(
            test.get('completedDate', ''))
        if test.get('odometerValue'):
            test['mileageFormatted'] = format_mileage(
                test['odometerValue'])
        if test.get('odometerUnit'):
            if test['odometerUnit'].upper() == 'MI':
                test['odometerUnit'] = 'miles'
        if test.get('defects'):
            major_types = ['DANGEROUS', 'MAJOR', 'FAIL', 'PRS']
            test['majors'] = [
                d for d in test['defects']
                if d.get('type') in major_types
            ]
            test['advisories'] = [
                d for d in test['defects']
                if d.get('type') not in major_types
            ]
            for d in test['majors']:
                if d.get('text'):
                    d['text'] = d['text'][0].upper() + d['text'][1:]
            for d in test['advisories']:
                if d.get('text'):
                    d['text'] = d['text'][0].upper() + d['text'][1:]

    # Mileage differences between tests
    for i in range(len(mot_tests)):
        if (mot_tests[i].get('odometerValue')
                and i < len(mot_tests) - 1):
            next_test = mot_tests[i + 1]
            if next_test.get('odometerValue'):
                try:
                    current = int(mot_tests[i]['odometerValue'])
                    previous = int(next_test['odometerValue'])
                    diff = current - previous
                    mot_tests[i]['mileage_diff'] = diff
                    mot_tests[i]['mileage_diff_formatted'] = (
                        f"{abs(diff):,}"
                    )
                except (ValueError, TypeError):
                    pass

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
            colors = PaintColor.objects.filter(
                normalized_model__startswith=norm_model[:3],
                **filters
            )

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
        'fuel_display': fuel_display,
        'vehicle_age_text': vehicle_age_text,
        'mot_tests': mot_tests,
        'mot_days_text': mot_days_text,
        'tax_days_text': tax_days_text,
        'v5c_days_text': v5c_days_text,
        'new_car_mot': new_car_mot,
        'new_car_mot_date': new_car_mot_date,
        'new_car_mot_days': new_car_mot_days,
        'tax_estimate': tax_estimate,
        'colors': colors,
    }

    return render(request, 'lookup/results.html', context)
