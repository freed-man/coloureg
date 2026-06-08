import os
import time
import requests
from datetime import timedelta
from django.shortcuts import render, redirect
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.db.models import Count, Avg, Q
from django.db.models.functions import TruncDate
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_POST, require_GET
from django_ratelimit.core import is_ratelimited
from .models import Search, PaintSwatch
from .services.vdg import (
    get_combined_lookup,
    smart_title,
    normalize_fuel_type,
    VdgError,
    VdgNotFoundError,
)
from .services.paint_resolver import resolve_paint
from .services.email import (
    send_user_paint_code,
    send_admin_failure_notification,
    send_user_pending_notification,
    send_admin_contact_message,
    send_user_contact_confirmation,
    send_custom_message,
)


def get_client_ip(request):
    x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
    if x_forwarded_for:
        return x_forwarded_for.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR')


def extract_mot_field(mot_data, field_name):
    """Pull a field from the DVLA MOT API response.

    DVLA's /v1/trade/vehicles/registration/{vrm} endpoint returns a single dict
    for one VRM (it never returns a list for the per-VRM endpoint).
    """
    if mot_data and isinstance(mot_data, dict):
        return mot_data.get(field_name)
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


def mask_vin(vin):
    """Censor middle of VIN for display: WAU***********456"""
    if not vin or len(vin) < 6:
        return vin or ''
    return vin[:3] + '*' * (len(vin) - 6) + vin[-3:]


# Make → logo filename overrides. Default behaviour is "lowercase, spaces and
# hyphens become underscores" (e.g. 'Volkswagen' → 'volkswagen.png'). This
# table covers cases where the natural slugification doesn't match the file
# we have on disk under static/images/logos/.
MAKE_LOGO_OVERRIDES = {
    'mercedes': 'mercedes_benz',
    'vw': 'volkswagen',
    'landrover': 'land_rover',
    'alfaromeo': 'alfa_romeo',
}


def make_to_logo(make):
    """Translate a make string into the logo filename stem (no .png extension)."""
    slug = (make or '').lower().replace('-', '_').replace(' ', '_')
    return MAKE_LOGO_OVERRIDES.get(slug, slug)


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
            device=parse_device(request.META.get('HTTP_USER_AGENT', '')),
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
            # EU type-approval category (M1/N1/N2/N3) — needed by pl24 to route
            # commercial vehicles to the right catalogue in the paint-miss
            # fallback. Carried through the session so the status endpoint can
            # pass it on without a second VDG call.
            category = vdg_data.get('category', '')
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

        # success = "we found a paint code for this lookup"
        # (vehicle-found-but-no-paint is recorded with success=False so the
        # admin filter and stats reflect end-user value, not just whether VDG
        # returned any data at all). The dashboard's success_rate metric
        # already used `paint_code != ''` to count real successes — this just
        # makes the underlying field match.
        search.success = bool(paint_code)
        search.lookup_duration_ms = int((time.time() - start_time) * 1000)
        search.save()

        make_logo = make_to_logo(make)

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
            # EU category for pl24 routing in the fallback (see status endpoint).
            'category': category,
            # paint_pending: True when VDG returned a vehicle but no paint, so
            # the results page should poll /lookup-status to try the parallel
            # VDG-retry + pl24 fallback. False when we already have paint (or no
            # vehicle at all) — nothing to resolve.
            'paint_pending': bool(vin) and not paint_code,
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

    make = vehicle_data.get('make', '')
    model = vehicle_data.get('model', '')
    year = vehicle_data.get('year')
    colour_for_lookup = vehicle_data.get('colour', '')

    if paint_code:
        paint_hex, paint_name, canonical_code = PaintSwatch.lookup_with_canonical(
            manufacturer=make,
            paint_code=paint_code,
            model=model,
            year=year,
            vdg_colour=colour_for_lookup,
        )

    # When VDG returns multiple codes (e.g. "8E8E/A7W"), look up each so the
    # multi-code template path can show a swatch bar per code. If the item's
    # code matches the top-level paint_code we already looked up, reuse the
    # result instead of re-querying.
    for item in all_paint_codes:
        if not isinstance(item, dict):
            continue
        if 'hex' in item and item['hex']:
            continue  # already has hex
        item_code = item.get('code')
        if not item_code:
            item['hex'] = None
            continue
        # Reuse the top-level lookup when it's the same code
        if item_code == paint_code:
            item['hex'] = paint_hex
            item['canonical'] = canonical_code
            continue
        item_hex, _item_name, item_canonical = PaintSwatch.lookup_with_canonical(
            manufacturer=make,
            paint_code=item_code,
            model=model,
            year=year,
            vdg_colour=colour_for_lookup,
        )
        item['hex'] = item_hex
        item['canonical'] = item_canonical

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
        # True when VDG returned a vehicle but no paint: the template shows the
        # "finding your paint code" state and polls /lookup-status. Only true if
        # we don't already have a paint_code here.
        'paint_pending': bool(vehicle_data.get('paint_pending')) and not paint_code,
    }

    return render(request, 'lookup/results.html', context)


@require_GET
def health(request):
    """Liveness endpoint for Railway's healthcheck. Returns a plain 200 and is
    exempt from the HTTPS redirect (SECURE_SSL_REDIRECT would otherwise turn a
    plain-HTTP probe into a 301, which the healthcheck reads as a failure). Kept
    deliberately trivial — it does NOT touch the database, so it reflects "the
    web process is up and serving", which is what a liveness probe wants."""
    return JsonResponse({'status': 'ok'})


@require_GET
def lookup_status(request, search_id):
    """Background paint-resolution endpoint, polled by the results page when the
    initial VDG call returned a vehicle but no paint.

    Runs the parallel VDG-retry + pl24 fallback (resolve_paint) and returns the
    outcome as JSON. Because resolve_paint can take up to ~65s (pl24's worst
    case), this is a single, potentially long-held request — viable on Railway,
    which has no 30s router timeout. The results page shows the vehicle data
    immediately and only this request waits, so the user is never blocked.

    Response shapes:
      {"status": "found",     "source": "...", "paint_code": "...",
       "paint_description": "...", "paint_hex": "...", "canonical_code": "..."}
      {"status": "not_found"}                      -- both paths missed
      {"status": "already_resolved", ...}          -- paint was found earlier
      {"status": "error"}                          -- unexpected failure

    Idempotency: if the Search row already has a paint_code (a previous poll
    resolved it, or it was never actually pending), we return it without
    re-running the fallback or re-charging VDG.
    """
    # Pull the vehicle context from the session, not query params: the VIN/make
    # /category came from VDG and we don't want them spoofable via the URL, and
    # it ties the request to this user's own just-completed lookup.
    vehicle_data = request.session.get('vehicle_data') or {}

    # Validate that this status request matches the session's current lookup.
    if str(vehicle_data.get('search_id')) != str(search_id):
        return JsonResponse({'status': 'error', 'detail': 'unknown search'},
                            status=404)

    # Idempotency / guard: if we already have paint (resolved on a prior poll,
    # or this was never a paint-miss), return it; never re-run the fallback.
    if vehicle_data.get('paint_code'):
        return JsonResponse({
            'status': 'already_resolved',
            'paint_code': vehicle_data.get('paint_code'),
            'paint_description': vehicle_data.get('paint_description', ''),
        })

    if not vehicle_data.get('paint_pending'):
        return JsonResponse({'status': 'not_found'})

    vin = vehicle_data.get('vin', '')
    make = vehicle_data.get('make', '')
    category = vehicle_data.get('category', '')
    registration = vehicle_data.get('registration', '')

    try:
        result = resolve_paint(registration, vin, make, category)
    except Exception:  # noqa: BLE001 — never let a fallback failure 500 the poll
        return JsonResponse({'status': 'error'}, status=200)

    if not result:
        # Both paths missed. Mark the Search row so analytics reflect the miss,
        # and clear the pending flag so further polls short-circuit.
        _record_paint_miss(search_id)
        vehicle_data['paint_pending'] = False
        request.session['vehicle_data'] = vehicle_data
        request.session.modified = True
        return JsonResponse({'status': 'not_found'})

    # Paint recovered. Persist to the Search row, update the session so the page
    # (and any reload) now shows it, and return it with a swatch lookup.
    paint_code = result.get('paint_code', '')
    paint_description = result.get('paint_description', '')
    source = result.get('source', '')
    all_paint_codes = result.get('all_paint_codes', [])

    paint_hex, paint_name, canonical_code = PaintSwatch.lookup_with_canonical(
        manufacturer=make,
        paint_code=paint_code,
        model=vehicle_data.get('model', ''),
        year=vehicle_data.get('year'),
        vdg_colour=vehicle_data.get('colour', ''),
    )

    _record_paint_hit(search_id, paint_code, paint_description, source)

    vehicle_data['paint_code'] = paint_code
    vehicle_data['paint_description'] = paint_description
    vehicle_data['all_paint_codes'] = all_paint_codes
    vehicle_data['paint_pending'] = False
    request.session['vehicle_data'] = vehicle_data
    request.session.modified = True

    return JsonResponse({
        'status': 'found',
        'source': source,
        'paint_code': paint_code,
        'paint_description': paint_description,
        'paint_hex': paint_hex,
        'paint_name': paint_name,
        'canonical_code': canonical_code,
        'all_paint_codes': all_paint_codes,
    })


def _record_paint_hit(search_id, paint_code, paint_description, source):
    """Persist a recovered paint code to the Search row. Best-effort: a DB hiccup
    here must not break the user-facing response, so failures are swallowed."""
    try:
        search = Search.objects.get(id=search_id)
    except (Search.DoesNotExist, ValueError, TypeError):
        return
    search.paint_code = paint_code
    search.paint_description = paint_description
    search.success = bool(paint_code)
    if source == 'pl24':
        search.provider = Search.PROVIDER_PARTSLINK24
    elif source == 'vdg_retry':
        search.provider = Search.PROVIDER_VDG
    search.save(update_fields=['paint_code', 'paint_description', 'success',
                               'provider'])


def _record_paint_miss(search_id):
    """Record that the fallback ran and found nothing. Best-effort."""
    try:
        search = Search.objects.get(id=search_id)
    except (Search.DoesNotExist, ValueError, TypeError):
        return
    # success stays False (the default); nothing to set unless you later add a
    # 'fallback_attempted' field. Touch nothing destructive.
    return


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
        paint_hex, _paint_name, canonical_code = PaintSwatch.lookup_with_canonical(
            manufacturer=search.make,
            paint_code=search.paint_code,
            model=search.model,
            year=search.year,
            vdg_colour=search.colour,
        )

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

    # Top metrics + email + cost — collapsed into a single aggregate so the
    # dashboard does ONE round-trip to Postgres instead of ~10. Each metric is
    # a conditional Count over the same Search table, so they fit naturally
    # into one SELECT with FILTER clauses.
    top_metrics = Search.objects.aggregate(
        # Volume / time windows
        total=Count('id'),
        today=Count('id', filter=Q(timestamp__gte=today_start)),
        week=Count('id', filter=Q(timestamp__gte=week_ago)),
        month=Count('id', filter=Q(timestamp__gte=month_ago)),
        # Success / paint hit rate
        with_code=Count('id', filter=~Q(paint_code='')),
        # Email pipeline
        with_email=Count('id', filter=~Q(email='')),
        emails_sent_count=Count('id', filter=Q(email_sent=True)),
        # VDG cost flags (per-document returned counts, plus combined-call count)
        vdg_vehicle_returned_count=Count('id', filter=Q(vdg_vehicle_returned=True)),
        vdg_combined_count=Count('id', filter=Q(vdg_combined_called=True)),
        vdg_paint_returned_count=Count('id', filter=Q(vdg_paint_returned=True)),
        # Average lookup duration (filtered nulls handled by Avg)
        avg_duration_ms=Avg('lookup_duration_ms'),
    )
    total_searches = top_metrics['total']
    today_searches = top_metrics['today']
    week_searches = top_metrics['week']
    month_searches = top_metrics['month']
    success_with_code = top_metrics['with_code']
    success_rate = (success_with_code / total_searches * 100) if total_searches > 0 else 0
    total_emails = top_metrics['with_email']
    emails_sent = top_metrics['emails_sent_count']
    conversion_rate = (total_emails / total_searches * 100) if total_searches > 0 else 0
    avg_duration_s = round((top_metrics['avg_duration_ms'] or 0) / 1000, 2)

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

    # Pending manual lookups (the actual to-do list — shows ALL unfulfilled
    # requests, no [:10] cap. In practice this is small; if your backlog grew
    # to thousands it'd be worth paginating, but you'd notice that long before
    # the page slowed down).
    recent_failures_with_email = (
        Search.objects.filter(paint_code='', email__gt='', manual_lookup_completed=False)
        .order_by('-timestamp')
    )

    # All recent lookups (success + failure) for the unified history table
    recent_all_lookups = (
        Search.objects.exclude(make='')
        .order_by('-timestamp')[:20]
    )

    # (total_emails, emails_sent, conversion_rate computed in top_metrics above)

    # Device breakdown (mobile / tablet / desktop)
    # Stored at write time on Search.device, so this is one fast aggregate
    # instead of iterating every Search row in Python on every render.
    device_counts = {'mobile': 0, 'tablet': 0, 'desktop': 0, 'unknown': 0}
    for row in (
        Search.objects.exclude(device='')
        .values('device')
        .annotate(count=Count('id'))
    ):
        device_counts[row['device']] = row['count']
    device_total = sum(device_counts.values())

    # VDG cost tracker
    # £0.15 per VehicleDetails (always charged when returned)
    # £0.35 per PaintCodeDetails (refunded by VDG if no paint data returned)
    # As of the combined-call refactor, each lookup makes ONE API call that
    # contains both documents. Counts come from the single aggregate above.
    #
    # Variable naming kept as vdg_vehicle_calls / vdg_paint_calls for backward
    # compatibility with admin_stats.html — semantically these are now "how many
    # times we were billed for X".
    vdg_vehicle_calls = top_metrics['vdg_vehicle_returned_count']
    # Paint is initially billed on every combined call where the package was
    # requested. The refund happens after if no paint code came back.
    vdg_paint_calls = top_metrics['vdg_combined_count']
    paint_calls_returned = top_metrics['vdg_paint_returned_count']
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
        'recent_all_lookups': recent_all_lookups,
        'total_emails': total_emails,
        'emails_sent': emails_sent,
        'conversion_rate': round(conversion_rate, 1),
        'device_counts': device_counts,
        'device_total': device_total,
        'vdg_vehicle_calls': vdg_vehicle_calls,
        'vdg_paint_calls': vdg_paint_calls,
        'paint_calls_returned': paint_calls_returned,
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
def send_compose_email(request):
    """Admin endpoint for sending a one-off branded email from the compose form
    on /admin-stats/.

    Returns JSON so the frontend can show success/error inline without a full
    page reload. Mirrors the AJAX pattern already used by submit_manual_lookup.

    Posts: to (email), subject (str, max 200), body (markdown, max 5000).

    The compose form is staff-only (the decorator enforces auth), so we don't
    sanitise the body — staff input is trusted. The body is rendered through
    markdown -> HTML -> brand wrapper in send_custom_message.
    """
    to_email = (request.POST.get('to') or '').strip()
    subject = (request.POST.get('subject') or '').strip()
    body = (request.POST.get('body') or '').strip()

    # Basic validation. Reject before any work happens (same pattern as
    # submit_manual_lookup) so we don't half-send.
    if not to_email or '@' not in to_email or '.' not in to_email:
        return JsonResponse({'success': False, 'error': 'Recipient must be a valid email address.'}, status=400)
    if not subject:
        return JsonResponse({'success': False, 'error': 'Subject is required.'}, status=400)
    if len(subject) > 200:
        return JsonResponse({'success': False, 'error': f'Subject too long ({len(subject)} chars, max 200).'}, status=400)
    if not body:
        return JsonResponse({'success': False, 'error': 'Message body is required.'}, status=400)
    if len(body) > 5000:
        return JsonResponse({'success': False, 'error': f'Message body too long ({len(body)} chars, max 5000).'}, status=400)

    sent = send_custom_message(to_email, subject, body)
    if sent:
        return JsonResponse({'success': True, 'message': f'Email sent to {to_email}.'})
    return JsonResponse({'success': False, 'error': f'Failed to send email to {to_email}. Check Resend logs.'}, status=502)


@staff_member_required
@require_POST
def submit_manual_lookup(request):
    """Admin endpoint for fulfilling a pending manual paint code lookup.

    Expected POST fields: search_id, paint_code, paint_description.
    Returns JSON so the dashboard can animate the row in-place without
    a full page reload.
    """

    search_id = request.POST.get('search_id')
    # Paint codes are always upper-case in practice (manufacturer convention).
    # We .upper() here so admin typos don't end up as mixed-case in the DB.
    paint_code = (request.POST.get('paint_code') or '').strip().upper()
    paint_description = (request.POST.get('paint_description') or '').strip()

    if not search_id or not paint_code:
        return JsonResponse({'success': False, 'error': 'Search ID and paint code are required.'}, status=400)

    # Validate field lengths against the underlying DB column sizes BEFORE doing
    # any work (DB lookup, swatch lookup, email send). Otherwise an over-long
    # description silently sends the email then crashes on save, leaving the
    # row marked as not-completed despite the user having received the email.
    # Limits match the Search model: paint_code=50, paint_description=200.
    if len(paint_code) > 50:
        return JsonResponse({
            'success': False,
            'error': f'Paint code too long ({len(paint_code)} chars, max 50).',
        }, status=400)
    if len(paint_description) > 200:
        return JsonResponse({
            'success': False,
            'error': f'Paint description too long ({len(paint_description)} chars, max 200).',
        }, status=400)

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
    paint_hex, _paint_name, canonical_code = PaintSwatch.lookup_with_canonical(
        manufacturer=search.make,
        paint_code=paint_code,
        model=search.model,
        year=search.year,
        vdg_colour=search.colour,
    )

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
    search.provider = Search.PROVIDER_MANUAL
    search.success = True  # paint code was found (manually) and emailed
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