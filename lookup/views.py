import os
import time
import requests
from datetime import timedelta
from django.shortcuts import render, redirect
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.db.models import Count, Avg
from django.db.models.functions import TruncDate
from django.utils import timezone
from django.views.decorators.http import require_POST
from django_ratelimit.core import is_ratelimited
from .models import Search, PaintSwatch
from .services.vdg import (
    get_combined_lookup,
    smart_title,
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
    """Censor middle of VIN for display: WAU***********456"""
    if not vin or len(vin) < 6:
        return vin or ''
    return vin[:3] + '*' * (len(vin) - 6) + vin[-3:]


def build_vehicle_title(year, make, model):
    """Build a vehicle title string: '2014 Volkswagen Golf SE BlueMotion Technology TDI'"""
    parts = []
    if year:
        parts.append(str(year))
    if make:
        parts.append(make)
    if model:
        parts.append(model)
    return ' '.join(parts).strip()


def parse_device(user_agent):
    """Classify user agent as 'mobile', 'tablet', or 'desktop'."""
    if not user_agent:
        return 'unknown'
    ua_lower = user_agent.lower()
    if any(kw in ua_lower for kw in ['ipad', 'tablet']):
        return 'tablet'
    if any(kw in ua_lower for kw in ['mobile', 'android', 'iphone', 'ipod']):
        return 'mobile'
    return 'desktop'


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

        # --- VDG combined lookup (single API call) ---
        # The VDG PaintCodeDetails package now bundles VehicleDetails +
        # ModelDetails + PaintCodeDetails, so one HTTP request returns
        # everything we used to fetch in two. Latency roughly halved at the
        # same £0.50 cost.
        vdg_data = None
        latest_balance = None
        try:
            vdg_data = get_combined_lookup(registration)
            search.vdg_combined_called = True
            if vdg_data:
                # Per-document tracking: each doc has its own StatusCode
                # inside the response, exposed by vdg.py as boolean flags.
                search.vdg_vehicle_returned = vdg_data.get('vehicle_returned', False)
                search.vdg_paint_returned = vdg_data.get('paint_returned', False)
                if vdg_data.get('balance') is not None:
                    latest_balance = vdg_data.get('balance')
        except (VdgError, VdgNotFoundError) as e:
            search.vdg_combined_called = True
            search.error_message = f'VDG: {str(e)[:200]}'

        make = ''
        model = ''
        year = None
        colour = ''
        vin = None
        fuel_type = ''
        transmission = ''
        engine_description = ''

        # Use VDG vehicle data if it returned successfully, else fall back to DVLA+MOT
        if vdg_data and vdg_data.get('vehicle_returned'):
            make = vdg_data.get('make', '')
            model = vdg_data.get('model', '')
            year = vdg_data.get('year')
            colour = vdg_data.get('colour', '')
            vin = vdg_data.get('vin', '')
            fuel_type = vdg_data.get('fuel_type', '')
            transmission = vdg_data.get('transmission', '')
            engine_description = vdg_data.get('engine_description', '')
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

        vehicle_title = build_vehicle_title(year, make, model)

        search.make = make
        search.model = model
        search.year = year
        search.colour = colour
        search.vin = vin or ''
        search.vehicle_title = vehicle_title

        # --- Paint code (from same combined response) ---
        paint_code = None
        paint_description = None
        all_paint_codes = []
        if vdg_data and vdg_data.get('paint_returned'):
            paint_code = vdg_data.get('paint_code', '')
            paint_description = vdg_data.get('paint_description', '')
            all_paint_codes = vdg_data.get('all_paint_codes', [])
            search.paint_code = paint_code
            search.paint_description = paint_description
            search.provider = Search.PROVIDER_VDG

        # Save latest VDG balance
        if latest_balance is not None:
            search.vdg_balance_after_call = latest_balance

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
            'vehicle_title': vehicle_title,
            'paint_code': paint_code,
            'paint_description': paint_description,
            'all_paint_codes': all_paint_codes,
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

    # Look up paint swatch (hex + name) for the result page swatch bar.
    # Returns None if no match found, in which case the swatch bar stays dormant.
    paint_hex = None
    paint_name = None
    canonical_code = None  # If VDG returned a short code, the canonical long form
    paint_code = vehicle_data.get('paint_code')
    all_paint_codes = list(vehicle_data.get('all_paint_codes', []))

    if paint_code:
        try:
            swatch = PaintSwatch.lookup(
                manufacturer=vehicle_data.get('make', ''),
                paint_code=paint_code,
                model=vehicle_data.get('model', ''),
                year=vehicle_data.get('year'),
                vdg_colour=vehicle_data.get('colour', ''),
            )
            if swatch:
                paint_hex = swatch.hex
                paint_name = swatch.name
                # If VDG returned an abbreviated code, surface the canonical long form
                canonical_code = PaintSwatch.find_canonical_code(
                    manufacturer=vehicle_data.get('make', ''),
                    paint_code=paint_code,
                    swatch=swatch,
                )
        except Exception:
            # Never let a swatch lookup failure break the results page
            pass

    # When VDG returns multiple codes (e.g. "8E8E/A7W"), look up each individually
    # so the multi-code template path can show a swatch bar per code.
    for item in all_paint_codes:
        if not isinstance(item, dict):
            continue
        if 'hex' in item and item['hex']:
            continue  # already has hex
        item_code = item.get('code')
        if not item_code:
            item['hex'] = None
            continue
        try:
            swatch = PaintSwatch.lookup(
                manufacturer=vehicle_data.get('make', ''),
                paint_code=item_code,
                model=vehicle_data.get('model', ''),
                year=vehicle_data.get('year'),
                vdg_colour=vehicle_data.get('colour', ''),
            )
            item['hex'] = swatch.hex if swatch else None
            # Per-item canonical expansion (multi-code template can show it)
            if swatch:
                item['canonical'] = PaintSwatch.find_canonical_code(
                    manufacturer=vehicle_data.get('make', ''),
                    paint_code=item_code,
                    swatch=swatch,
                )
            else:
                item['canonical'] = None
        except Exception:
            item['hex'] = None
            item['canonical'] = None

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
        'vehicle_title': vehicle_data.get('vehicle_title', ''),
        'paint_code': paint_code,
        'paint_description': vehicle_data.get('paint_description'),
        'all_paint_codes': all_paint_codes,
        'paint_hex': paint_hex,
        'paint_name': paint_name,
        'canonical_code': canonical_code,
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

    vin_masked = mask_vin(search.vin)

    if search.paint_code:
        # Look up swatch (hex) and canonical code so the email matches the website UI
        canonical_code = None
        paint_hex = None
        try:
            swatch = PaintSwatch.lookup(
                manufacturer=search.make,
                paint_code=search.paint_code,
                model=search.model,
                year=search.year,
                vdg_colour=search.colour,
            )
            if swatch:
                paint_hex = swatch.hex
                canonical_code = PaintSwatch.find_canonical_code(
                    manufacturer=search.make,
                    paint_code=search.paint_code,
                    swatch=swatch,
                )
        except Exception:
            pass

        sent = send_user_paint_code(
            to_email=email,
            registration=search.registration,
            vehicle_title=search.vehicle_title,
            vin_masked=vin_masked,
            colour=search.colour,
            paint_code=search.paint_code,
            paint_description=search.paint_description,
            canonical_code=canonical_code,
            paint_hex=paint_hex,
        )
        if sent:
            search.email_sent = True
            search.save()
    else:
        admin_sent = send_admin_failure_notification(
            registration=search.registration,
            vehicle_title=search.vehicle_title,
            vin_full=search.vin,
            colour=search.colour,
            user_email=email,
        )
        user_sent = send_user_pending_notification(
            to_email=email,
            registration=search.registration,
            vehicle_title=search.vehicle_title,
            vin_masked=vin_masked,
            colour=search.colour,
        )
        if admin_sent and user_sent:
            search.email_sent = True
            search.save()

    request.session['email_submitted'] = email
    return redirect('results')


def about(request):
    return render(request, 'lookup/about.html')


def privacy(request):
    return render(request, 'lookup/privacy.html')


def disclaimer(request):
    return render(request, 'lookup/disclaimer.html')


def help_page(request):
    contact_submitted = request.session.pop('contact_submitted', None)
    return render(request, 'lookup/help.html', {
        'contact_submitted': contact_submitted,
    })


@require_POST
def submit_contact(request):
    contact_type = request.POST.get('type', 'general').strip()
    email = request.POST.get('email', '').strip()
    message = request.POST.get('message', '').strip()

    if not email or not message:
        messages.error(request, 'Email and message are required.')
        return redirect('help')

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
        return redirect('help')

    admin_sent = send_admin_contact_message(contact_type, email, message)
    user_sent = send_user_contact_confirmation(email)

    if admin_sent and user_sent:
        request.session['contact_submitted'] = email

    return redirect('help')


@staff_member_required
def admin_stats(request):
    """Admin-only stats dashboard."""
    now = timezone.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_ago = now - timedelta(days=7)
    month_ago = now - timedelta(days=30)

    # Top metrics
    total_searches = Search.objects.count()
    today_searches = Search.objects.filter(timestamp__gte=today_start).count()
    week_searches = Search.objects.filter(timestamp__gte=week_ago).count()
    month_searches = Search.objects.filter(timestamp__gte=month_ago).count()
    success_with_code = Search.objects.exclude(paint_code='').count()
    success_rate = (success_with_code / total_searches * 100) if total_searches > 0 else 0

    # Avg lookup duration
    avg_duration_ms = Search.objects.exclude(
        lookup_duration_ms__isnull=True
    ).aggregate(avg=Avg('lookup_duration_ms'))['avg'] or 0
    avg_duration_s = round(avg_duration_ms / 1000, 2)

    # Daily counts for last 30 days
    daily_counts = (
        Search.objects.filter(timestamp__gte=month_ago)
        .annotate(date=TruncDate('timestamp'))
        .values('date')
        .annotate(count=Count('id'))
        .order_by('date')
    )
    chart_labels = []
    chart_data = []
    daily_dict = {item['date']: item['count'] for item in daily_counts}
    for i in range(30, -1, -1):
        d = (now - timedelta(days=i)).date()
        chart_labels.append(d.strftime('%b %d'))
        chart_data.append(daily_dict.get(d, 0))

    # Top searched makes
    top_makes = (
        Search.objects.exclude(make='')
        .values('make')
        .annotate(count=Count('id'))
        .order_by('-count')[:10]
    )

    # Top searched registrations
    top_regs = (
        Search.objects.values('registration')
        .annotate(count=Count('id'))
        .order_by('-count')[:10]
    )

    # Top makes with NO paint code
    failed_makes = (
        Search.objects.filter(paint_code='').exclude(make='')
        .values('make')
        .annotate(count=Count('id'))
        .order_by('-count')[:10]
    )

    # Recent failures with email (manual lookup pipeline)
    recent_failures_with_email = (
        Search.objects.filter(paint_code='', email__gt='', manual_lookup_completed=False)
        .order_by('-timestamp')[:10]
    )

    # All recent failures (no paint code)
    recent_all_failures = (
        Search.objects.filter(paint_code='')
        .exclude(make='')
        .order_by('-timestamp')[:20]
    )

    # All recent lookups (success + failure) for the unified history table
    recent_all_lookups = (
        Search.objects.exclude(make='')
        .order_by('-timestamp')[:20]
    )

    # Email submissions
    total_emails = Search.objects.exclude(email='').count()
    emails_sent = Search.objects.filter(email_sent=True).count()
    conversion_rate = (total_emails / total_searches * 100) if total_searches > 0 else 0

    # Device breakdown (mobile / tablet / desktop)
    device_counts = {'mobile': 0, 'tablet': 0, 'desktop': 0, 'unknown': 0}
    for s in Search.objects.exclude(user_agent='').only('user_agent').iterator():
        device = parse_device(s.user_agent)
        device_counts[device] = device_counts.get(device, 0) + 1
    device_total = sum(device_counts.values())

    # VDG cost tracker
    # £0.15 per VehicleDetails (always charged when returned)
    # £0.35 per PaintCodeDetails (refunded by VDG if no paint data returned)
    # As of the combined-call refactor, each lookup makes ONE API call that
    # contains both documents. We track which documents *returned* data
    # (vdg_vehicle_returned, vdg_paint_returned), which is what determines
    # billing and refunds. Old call-flags (vdg_paint_called, vdg_vehicle_called)
    # were backfilled into the returned-flags by migration 0006.
    #
    # Variable naming kept as vdg_vehicle_calls / vdg_paint_calls for backward
    # compatibility with admin_stats.html — semantically these are now "how many
    # times we were billed for X", which equals "how many times the doc returned"
    # (vehicle: always charged when returned) or "how many combined calls"
    # (paint: charged then refunded if no code).
    vdg_vehicle_calls = Search.objects.filter(vdg_vehicle_returned=True).count()
    # Paint is initially billed on every combined call where the package was
    # requested. The refund happens after if no paint code came back.
    vdg_paint_calls = Search.objects.filter(vdg_combined_called=True).count()
    # Of those billed, how many actually returned paint data (kept) vs got refunded
    paint_calls_returned = Search.objects.filter(vdg_paint_returned=True).count()
    paint_refunds_count = vdg_paint_calls - paint_calls_returned

    vehicle_cost = round(vdg_vehicle_calls * 0.15, 2)
    paint_charged_total = round(vdg_paint_calls * 0.35, 2)
    paint_refunds_amount = round(paint_refunds_count * 0.35, 2)
    estimated_cost = round(vehicle_cost + paint_charged_total - paint_refunds_amount, 2)

    # Latest VDG balance (captured opportunistically from any API call)
    latest_with_balance = (
        Search.objects.exclude(vdg_balance_after_call__isnull=True)
        .order_by('-timestamp')
        .first()
    )
    vdg_balance = latest_with_balance.vdg_balance_after_call if latest_with_balance else None
    vdg_balance_at = latest_with_balance.timestamp if latest_with_balance else None

    context = {
        'total_searches': total_searches,
        'today_searches': today_searches,
        'week_searches': week_searches,
        'month_searches': month_searches,
        'success_rate': round(success_rate, 1),
        'success_with_code': success_with_code,
        'avg_duration_s': avg_duration_s,
        'chart_labels': chart_labels,
        'chart_data': chart_data,
        'top_makes': top_makes,
        'top_regs': top_regs,
        'failed_makes': failed_makes,
        'recent_failures': recent_failures_with_email,
        'recent_all_failures': recent_all_failures,
        'recent_all_lookups': recent_all_lookups,
        'total_emails': total_emails,
        'emails_sent': emails_sent,
        'conversion_rate': round(conversion_rate, 1),
        'device_counts': device_counts,
        'device_total': device_total,
        'vdg_vehicle_calls': vdg_vehicle_calls,
        'vdg_paint_calls': vdg_paint_calls,
        'paint_calls_charged': paint_calls_returned,
        'paint_refunds_count': paint_refunds_count,
        'paint_refunds_amount': paint_refunds_amount,
        'vehicle_cost': vehicle_cost,
        'paint_charged_total': paint_charged_total,
        'estimated_cost': estimated_cost,
        'vdg_balance': vdg_balance,
        'vdg_balance_at': vdg_balance_at,
    }

    return render(request, 'lookup/admin_stats.html', context)

@staff_member_required
@require_POST
def submit_manual_lookup(request):
    """Admin endpoint for fulfilling a pending manual paint code lookup.

    Expected POST fields: search_id, paint_code, paint_description.
    Returns JSON so the dashboard can animate the row in-place without
    a full page reload.
    """
    from django.http import JsonResponse

    search_id = request.POST.get('search_id')
    paint_code = (request.POST.get('paint_code') or '').strip()
    paint_description = (request.POST.get('paint_description') or '').strip()

    if not search_id or not paint_code:
        return JsonResponse({'success': False, 'error': 'Search ID and paint code are required.'}, status=400)

    try:
        search = Search.objects.get(id=search_id)
    except Search.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'Search record not found.'}, status=404)

    if search.manual_lookup_completed:
        return JsonResponse({'success': False, 'error': f'Already completed for {search.registration}.'}, status=409)

    if not search.email:
        return JsonResponse({'success': False, 'error': f'No email on file for {search.registration}.'}, status=400)

    # Title-case the description so '  glacier white-metallic ' becomes
    # 'Glacier White-Metallic' before saving and sending.
    paint_description_clean = smart_title(paint_description) if paint_description else ''

    # Look up swatch (hex) and canonical code so the email matches the website UI
    canonical_code = None
    paint_hex = None
    try:
        swatch = PaintSwatch.lookup(
            manufacturer=search.make,
            paint_code=paint_code,
            model=search.model,
            year=search.year,
            vdg_colour=search.colour,
        )
        if swatch:
            paint_hex = swatch.hex
            canonical_code = PaintSwatch.find_canonical_code(
                manufacturer=search.make,
                paint_code=paint_code,
                swatch=swatch,
            )
    except Exception:
        pass

    sent = send_user_paint_code(
        to_email=search.email,
        registration=search.registration,
        vehicle_title=search.vehicle_title,
        vin_masked=mask_vin(search.vin),
        colour=search.colour,
        paint_code=paint_code,
        paint_description=paint_description_clean,
        canonical_code=canonical_code,
        paint_hex=paint_hex,
    )

    if not sent:
        return JsonResponse({
            'success': False,
            'error': f'Failed to send email to {search.email}. Please try again.',
        }, status=502)

    # Update DB only after successful send so a failed email doesn't mark complete
    search.paint_code = paint_code
    search.paint_description = paint_description_clean
    search.manual_lookup_completed = True
    search.email_sent = True
    search.save()

    return JsonResponse({
        'success': True,
        'message': f'Sent paint code {paint_code} to {search.email} for {search.registration}.',
        'paint_code': paint_code,
        'registration': search.registration,
        'email': search.email,
    })


@staff_member_required
@require_POST
def dismiss_manual_lookup(request):
    """Admin endpoint to dismiss a manual lookup request without sending.

    Marks the search as 'manual_lookup_completed' so it falls off the queue,
    but does not email the customer or mark email_sent. Used for spam,
    test entries, or otherwise unactionable requests. Returns JSON so the
    dashboard can animate the row out without a page reload.
    """
    from django.http import JsonResponse

    search_id = request.POST.get('search_id')
    if not search_id:
        return JsonResponse({'success': False, 'error': 'Search ID is required.'}, status=400)

    try:
        search = Search.objects.get(id=search_id)
    except Search.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'Search record not found.'}, status=404)

    if search.manual_lookup_completed:
        return JsonResponse({'success': False, 'error': f'Already actioned for {search.registration}.'}, status=409)

    search.manual_lookup_completed = True
    search.save()

    return JsonResponse({
        'success': True,
        'message': f'Dismissed manual lookup request for {search.registration}.',
        'registration': search.registration,
    })