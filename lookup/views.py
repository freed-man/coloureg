import os
import re
import time
import requests
from datetime import timedelta
from django.shortcuts import render, redirect
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db.models import Count, Avg, Q, Sum
from django.db.models.functions import TruncDate
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_POST, require_GET
from django_ratelimit.core import is_ratelimited
from .models import Search, PaintLookup, SiteConfig
from .services.vdg import (
    get_combined_lookup,
    smart_title,
    normalize_fuel_type,
    fix_make_case,
    VdgError,
    VdgNotFoundError,
)
from .services.paint_resolver import resolve_paint, _enrich_from_lookup, PL24_TIMEOUT
from .services.email import (
    send_user_paint_code,
    send_admin_failure_notification,
    send_user_pending_notification,
    send_admin_contact_message,
    send_user_contact_confirmation,
    send_custom_message,
)


def get_client_ip(request):
    # All traffic is proxied through Cloudflare (orange-cloud), which sets
    # CF-Connecting-IP to the single real client IP. Trust that first.
    # X-Forwarded-For is unreliable here: Cloudflare appends its edge IP and
    # Railway's proxy rewrites the chain, so the visitor isn't dependably the
    # first entry (that's why logs were showing 172.6x Cloudflare IPs).
    cf_ip = request.META.get('HTTP_CF_CONNECTING_IP')
    if cf_ip:
        return cf_ip.strip()
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
    # Maintenance switch (flippable from /admin-stats/, no redeploy). When on,
    # the homepage shows the locked "offline for maintenance" state AND the
    # backend refuses to run any lookup here — so a direct POST can't bypass the
    # greyed field or spend any VDG credit.
    maintenance = SiteConfig.get().maintenance_mode

    if request.method == 'POST':
        if maintenance:
            # Refuse silently-but-clearly: no VDG call, no Search row, just
            # re-render the maintenance page. (Covers direct/scripted POSTs.)
            return render(request, 'lookup/index.html', {'maintenance_mode': True})

        was_limited = is_ratelimited(
            request,
            group='lookup',
            key='ip',
            rate='3/h',
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

        # Reject anything that isn't a plausible UK plate (alphanumeric, <=8).
        # Runs BEFORE the VDG call (no spend on junk input), keeps the value
        # within the registration column (max_length=10 in the DB), and stops odd
        # characters reaching the MOT API URL path or the notification emails.
        # Real UK plates — current and older formats — are all A-Z / 0-9 and at
        # most 7-8 characters. The PNZ282 easter-egg below still matches (it's
        # alphanumeric), since this runs on the normalised value.
        if not re.fullmatch(r'[A-Z0-9]{1,8}', registration):
            messages.error(request, 'Please enter a valid registration number.')
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
        # We always attempt the combined call. The OUTCOME is captured by:
        #   success + paint     -> vehicle_returned=True,  paint_returned=True
        #   success + no paint  -> vehicle_returned=True,  paint_returned=False
        #   VDG failure/error   -> vehicle_returned=False, paint_returned=False,
        #                          error_message set (so a failure is distinguishable
        #                          from a successful-but-paintless lookup).
        try:
            vdg_data = get_combined_lookup(registration)
            if vdg_data:
                # Per-document tracking: each doc has its own StatusCode
                # inside the response, exposed by vdg.py as boolean flags.
                search.vdg_vehicle_returned = vdg_data.get('vehicle_returned', False)
                search.vdg_paint_returned = vdg_data.get('paint_returned', False)
                if vdg_data.get('balance') is not None:
                    latest_balance = vdg_data.get('balance')
                # Record the REAL amount VDG billed this call (tier-correct, net
                # of any refund) so admin can sum exact spend.
                if vdg_data.get('transaction_cost') is not None:
                    search.vdg_transaction_cost = vdg_data.get('transaction_cost')
        except (VdgError, VdgNotFoundError) as e:
            # vehicle_returned / paint_returned stay False (their defaults), and
            # the error is recorded — together these mark a VDG failure.
            search.error_message = f'VDG: {str(e)[:200]}'

        make = ''
        model = ''
        year = None
        colour = ''
        vin = None
        fuel_type = ''
        transmission = ''
        engine_description = ''
        # EU type-approval category (M1/N1/N2/N3). Only VDG provides it; default
        # empty so the DVLA fallback branch (which doesn't set it) is safe.
        category = ''

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

            make = fix_make_case((dvla.get('make', '') or '').title())
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
        search.category = category

        # --- Paint code (from same combined response) ---
        paint_code = None
        paint_description = None
        all_paint_codes = []
        if vdg_data and vdg_data.get('paint_returned'):
            paint_code = vdg_data.get('paint_code', '')
            paint_description = vdg_data.get('paint_description', '')
            all_paint_codes = vdg_data.get('all_paint_codes', [])
            # Behind the provider result: if VDG gave a code but no name (or a
            # name but no code), fill the gap from our PaintLookup database and
            # record which part we supplied. Most VDG hits return both, so this
            # usually does nothing.
            _enriched = _enrich_from_lookup(
                {'paint_code': paint_code, 'paint_description': paint_description},
                make,
                model,
            )
            paint_code = _enriched.get('paint_code', paint_code)
            paint_description = _enriched.get('paint_description', paint_description)
            search.paint_code = paint_code
            search.paint_description = paint_description
            search.provider = Search.PROVIDER_VDG
            search.enriched_from = _enriched.get('enriched_from', '')

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

    return render(request, 'lookup/index.html', {'maintenance_mode': maintenance})


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
        paint_hex, paint_name, canonical_code = PaintLookup.lookup_with_canonical(
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
        item_hex, _item_name, item_canonical = PaintLookup.lookup_with_canonical(
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
        # True when a prior recovery resolved to name-only (colour name, no code).
        # Lets a page reload re-render the name-only card instead of re-polling.
        # Only meaningful when there's no code and we're not pending.
        'paint_name_only': (
            bool(vehicle_data.get('paint_name_only'))
            and not paint_code
            and not bool(vehicle_data.get('paint_pending'))
        ),
    }

    return render(request, 'lookup/results.html', context)


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

    # Maintenance switch: if lookups are paused, do NOT run the recovery race
    # (VDG-retry / pl24) — that would spend money. Report no result.
    if SiteConfig.get().maintenance_mode:
        return JsonResponse({'status': 'not_found'})

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

    # --- Atomic claim: only ONE request may run the (paid) recovery ---------
    # Two CONCURRENT polls for the same search (two tabs on the results page,
    # or a refresh while the first poll is still in flight) would both pass
    # the session checks above and both run resolve_paint — a duplicate paid
    # VDG retry and a duplicate pl24 scrape for one vehicle. Same TOCTOU shape
    # as the manual-lookup double-click, so it gets the same fix: one filtered
    # UPDATE claims the row and exactly one request wins. The claim reuses
    # recovery_attempted (which completion writes anyway), shifting its
    # meaning from "a recovery ran" to "a recovery ran or is running". The
    # stats' definitive-outcome rule is unaffected: it also requires
    # recovery_duration_ms, which is still written only on completion. If the
    # winning process dies mid-recovery (kill/redeploy), the claim stays taken
    # and later polls fall through to the waiter below, time out, and surface
    # the manual-lookup offer — the row completes by hand rather than by a
    # fresh automatic spend.
    claimed = Search.objects.filter(
        id=search_id, paint_code='', recovery_attempted=False,
    ).update(recovery_attempted=True)
    if not claimed:
        return _wait_for_recovery_result(search_id)

    telemetry = {}
    try:
        result = resolve_paint(registration, vin, make, category, telemetry=telemetry, model=vehicle_data.get('model', ''))
    except Exception:  # noqa: BLE001 — never let a fallback failure 500 the poll
        _record_recovery(search_id, telemetry)
        return JsonResponse({'status': 'error'}, status=200)

    if not result:
        # Both paths missed. Log what the recovery did (for the dashboard), clear
        # the pending flag so further polls short-circuit, and tell the page to
        # surface the manual-lookup offer.
        _record_recovery(search_id, telemetry)
        vehicle_data['paint_pending'] = False
        request.session['vehicle_data'] = vehicle_data
        request.session.modified = True
        return JsonResponse({'status': 'not_found'})

    # Name-only: pl24 returned a colour NAME but no code (e.g. Ford passenger,
    # Jaguar, older Land Rover, some Kia — partslink24 carries the name, not a
    # code). This is a partial result: we show the customer the colour name and
    # offer to email the exact code once found manually. It is NOT a code
    # recovery, so `success` stays False and `provider` is left unset — only the
    # name-only telemetry flag records it (keeps admin stats honest).
    if result.get('name_only'):
        paint_description = result.get('paint_description', '')
        _record_name_only(search_id, paint_description, telemetry)
        # Persist the name to the session so a reload re-shows it, but keep
        # paint_code empty and clear pending so further polls short-circuit.
        vehicle_data['paint_description'] = paint_description
        vehicle_data['paint_name_only'] = True
        vehicle_data['paint_pending'] = False
        request.session['vehicle_data'] = vehicle_data
        request.session.modified = True
        return JsonResponse({
            'status': 'name_only',
            'paint_description': paint_description,
        })

    # Paint recovered. Persist to the Search row, update the session so the page
    # (and any reload) now shows it, and return it with a swatch lookup.
    paint_code = result.get('paint_code', '')
    paint_description = result.get('paint_description', '')
    source = result.get('source', '')
    all_paint_codes = result.get('all_paint_codes', [])
    enriched_from = result.get('enriched_from', '')

    paint_hex, paint_name, canonical_code = PaintLookup.lookup_with_canonical(
        manufacturer=make,
        paint_code=paint_code,
        model=vehicle_data.get('model', ''),
        year=vehicle_data.get('year'),
        vdg_colour=vehicle_data.get('colour', ''),
    )

    _record_paint_hit(search_id, paint_code, paint_description, source, telemetry,
                      enriched_from=enriched_from)

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


RECOVERY_WAIT_POLL_S = 2.0


def _wait_for_recovery_result(search_id):
    """A concurrent poll LOST the recovery claim: another request for this same
    search is already running resolve_paint. Instead of running a duplicate
    (paid) recovery, watch the Search row until the winner's outcome lands and
    return it in exactly the response shapes the winner's request produces, so
    the page's JS needs no new states. Deliberately read-only on the session:
    the winning request owns all session writes (saving a stale copy from here
    would clobber them; the winner has already persisted the outcome to the
    session by the time it lands on the row, so a later page reload is safe).

    If the winner never completes (its process was killed mid-recovery), the
    deadline lapses and we return status=error — the page then shows the
    manual-lookup offer, which is the correct fallback for a recovery that
    can no longer finish on its own."""
    deadline = time.monotonic() + PL24_TIMEOUT + 10
    while time.monotonic() < deadline:
        try:
            row = Search.objects.get(id=search_id)
        except (Search.DoesNotExist, ValueError, TypeError):
            return JsonResponse({'status': 'error'}, status=200)
        if row.paint_code:
            paint_hex, paint_name, canonical_code = PaintLookup.lookup_with_canonical(
                manufacturer=row.make,
                paint_code=row.paint_code,
                model=row.model,
                year=row.year,
                vdg_colour=row.colour,
            )
            if row.provider == Search.PROVIDER_PARTSLINK24:
                source = 'pl24'
            elif row.provider == Search.PROVIDER_VDG_RETRY:
                source = 'vdg_retry'
            else:
                source = row.provider or ''
            return JsonResponse({
                'status': 'found',
                'source': source,
                'paint_code': row.paint_code,
                'paint_description': row.paint_description,
                'paint_hex': paint_hex,
                'paint_name': paint_name,
                'canonical_code': canonical_code,
                # Multi-code detail lives in the winner's session write; the
                # single recovered code is what matters here and is complete.
                'all_paint_codes': [],
            })
        if row.recovery_duration_ms is not None:
            # Recovery finished without a code.
            if row.recovery_name_only and row.paint_description:
                return JsonResponse({
                    'status': 'name_only',
                    'paint_description': row.paint_description,
                })
            return JsonResponse({'status': 'not_found'})
        time.sleep(RECOVERY_WAIT_POLL_S)
    return JsonResponse({'status': 'error'}, status=200)


def _apply_recovery_telemetry(search, telemetry):
    """Copy recovery telemetry (from resolve_paint) onto a Search instance.
    Returns the list of field names touched, for a targeted update_fields save."""
    if not telemetry:
        return []
    search.recovery_attempted = bool(telemetry.get('recovery_attempted'))
    search.vdg_retry_returned = bool(telemetry.get('vdg_retry_returned'))
    search.pl24_attempted = bool(telemetry.get('pl24_attempted'))
    search.pl24_returned = bool(telemetry.get('pl24_returned'))
    search.recovery_name_only = bool(telemetry.get('pl24_name_only'))
    dur = telemetry.get('duration_ms')
    if dur is not None:
        search.recovery_duration_ms = int(dur)
    return ['recovery_attempted', 'vdg_retry_returned', 'pl24_attempted',
            'pl24_returned', 'recovery_name_only', 'recovery_duration_ms']


def _record_paint_hit(search_id, paint_code, paint_description, source, telemetry=None,
                      enriched_from=''):
    """Persist a recovered paint code (and the recovery telemetry) to the Search
    row in one save. Best-effort: a DB hiccup here must not break the user-facing
    response, so failures are swallowed."""
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
        search.provider = Search.PROVIDER_VDG_RETRY
    search.enriched_from = enriched_from or ''
    fields = ['paint_code', 'paint_description', 'success', 'provider', 'enriched_from']
    fields += _apply_recovery_telemetry(search, telemetry)
    # We have a real code here (this is the paint-hit path), so this is a FULL
    # result, not name-only — even if pl24 originally returned a name that our
    # PaintLookup enrichment then resolved to a code. The OUTCOME column checks
    # recovery_name_only BEFORE success, so it must be cleared or the row would
    # wrongly show ◐ instead of a green ✓.
    if paint_code:
        search.recovery_name_only = False
        if 'recovery_name_only' not in fields:
            fields.append('recovery_name_only')
        # If this coded result came from pl24, count it as a pl24 hit — including
        # the case where pl24 returned a NAME that our database then resolved to a
        # code. The raw telemetry flags that as pl24_name_only (no code), which
        # would otherwise leave pl24_returned False and undercount pl24's real
        # contribution in the recovery_pl24_hits stat.
        if source == 'pl24':
            search.pl24_returned = True
            if 'pl24_returned' not in fields:
                fields.append('pl24_returned')
    search.save(update_fields=fields)


def _record_recovery(search_id, telemetry):
    """Persist just the recovery telemetry (for the both-missed / error paths,
    where no paint was found). Best-effort."""
    try:
        search = Search.objects.get(id=search_id)
    except (Search.DoesNotExist, ValueError, TypeError):
        return
    fields = _apply_recovery_telemetry(search, telemetry)
    if fields:
        search.save(update_fields=fields)


def _record_name_only(search_id, paint_description, telemetry=None):
    """Persist a name-only recovery: a colour name with NO code (e.g. Ford
    passenger, Jaguar, some Kia — partslink24 carries the name, not a code).

    Counts as a SUCCESS for the customer's purposes — we found their colour — so
    `success=True` and `provider=partslink24` (the SOURCE the name came from).
    The `recovery_name_only` flag (set via the telemetry helper) is KEPT so the
    distinction "name only vs. real code" survives in the data: admin stats can
    separate them if needed, and a future learned code=name DB must only learn
    from rows that actually had a code. The OUTCOME column shows a plain green
    tick regardless. Best-effort — a DB hiccup must not break the response."""
    try:
        search = Search.objects.get(id=search_id)
    except (Search.DoesNotExist, ValueError, TypeError):
        return
    search.paint_description = paint_description
    search.success = True
    search.provider = Search.PROVIDER_PARTSLINK24
    fields = ['paint_description', 'success', 'provider']
    fields += _apply_recovery_telemetry(search, telemetry)
    search.save(update_fields=fields)


@require_POST
def submit_email(request):
    search_id = request.POST.get('search_id')
    email = request.POST.get('email', '').strip()

    if not search_id or not email:
        messages.error(request, 'Email address is required.')
        return redirect('results')

    # Validate the email format. EmailField does NOT run validators on .save(),
    # so without this an invalid address is stored, fails at the Resend send, and
    # is echoed into the admin notification.
    try:
        validate_email(email)
    except ValidationError:
        messages.error(request, 'Please enter a valid email address.')
        return redirect('results')

    # Tie this submit to the user's own session lookup — the same ownership guard
    # lookup_status uses. search_id is a guessable integer POST param with no
    # ownership check otherwise, so an attacker could POST enumerated ids to email
    # arbitrary searches (paint codes to attacker addresses, admin notifications
    # to our inbox) without doing any lookup. Reject a mismatch before any DB hit.
    session_search_id = (request.session.get('vehicle_data') or {}).get('search_id')
    if str(session_search_id) != str(search_id):
        messages.error(request, 'Your session has expired. Please look up your vehicle again.')
        return redirect('index')

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
        # Matched to the lookup limit (3/h). NOT redundant with it: although the
        # happy path reaches here only after a lookup, search_id arrives as a POST
        # param and Search.id is an auto-increment integer, so this endpoint is
        # reachable directly with enumerated ids and no lookup at all — the lookup
        # limit gates Search *creation*, not access here. The missed-result branch
        # below emails ADMIN_EMAIL, so leaving this open would let enumerated old
        # misses flood the admin inbox. A user capped at 3 lookups never needs a
        # 4th action here, so the cap only ever bites the abuse path.
        key='ip',
        rate='3/h',
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
        paint_hex, _paint_name, canonical_code = PaintLookup.lookup_with_canonical(
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
    # Whitelist the contact type — it is echoed into the admin email's subject
    # and body, so anything unexpected falls back to 'general'.
    contact_type = request.POST.get('type', 'general').strip().lower()
    if contact_type not in ('general', 'technical'):
        contact_type = 'general'
    email = request.POST.get('email', '').strip()
    message = request.POST.get('message', '').strip()

    if not email or not message:
        messages.error(request, 'Email and message are required.')
        return redirect('help')

    # Validate the email format (defence-in-depth; it also feeds the admin email).
    try:
        validate_email(email)
    except ValidationError:
        messages.error(request, 'Please enter a valid email address.')
        return redirect('help')

    # Cap the message so an oversized body can't be emailed to the admin.
    if len(message) > 5000:
        messages.error(request, 'Message is too long (max 5000 characters).')
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
    # Maintenance toggle: a POST from the dashboard's switch flips the
    # site-wide maintenance flag (lookups paused). Redirect-after-POST so a
    # refresh doesn't re-submit. staff-only via the decorator above.
    if request.method == 'POST' and request.POST.get('action') == 'toggle_maintenance':
        cfg = SiteConfig.get()
        cfg.maintenance_mode = not cfg.maintenance_mode
        cfg.save(update_fields=['maintenance_mode', 'updated_at'])
        messages.success(
            request,
            'Maintenance mode ON — lookups are paused.' if cfg.maintenance_mode
            else 'Maintenance mode OFF — lookups are live again.'
        )
        return redirect('admin_stats')

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
        # --- Outcome buckets for a fair success rate (see success_rate below) ---
        # vehicle_found: lookups where we actually identified a vehicle (VDG or
        # the DVLA fallback populate `make`; a not-found reg leaves it blank).
        # Mistyped / non-existent regs are excluded so they don't count as
        # paint-code failures. The success-rate denominator is narrower still —
        # see the definitive-outcome rule where success_rate is computed.
        vehicle_found=Count('id', filter=~Q(make='')),
        no_vehicle=Count('id', filter=Q(make='')),
        # Of the vehicle-found rows with no code: a "genuine miss" is one where the
        # recovery pipeline actually RAN to completion (attempted + a duration
        # recorded) and still found nothing; "incomplete" is everything else —
        # recovery never ran or never finished (typically the user left before the
        # results page could fire it). Split so the dashboard shows them apart.
        genuine_miss=Count('id', filter=(
            ~Q(make='') & Q(paint_code='')
            & Q(recovery_attempted=True) & Q(recovery_duration_ms__isnull=False)
        )),
        incomplete=Count('id', filter=(
            ~Q(make='') & Q(paint_code='')
            & ~(Q(recovery_attempted=True) & Q(recovery_duration_ms__isnull=False))
        )),
        # Sub-count: vehicle found and a colour NAME recovered, but still no code.
        name_only_miss=Count('id', filter=Q(recovery_name_only=True) & Q(paint_code='')),
        # Email pipeline
        with_email=Count('id', filter=~Q(email='')),
        emails_sent_count=Count('id', filter=Q(email_sent=True)),
        # VDG cost flags (per-document returned counts)
        vdg_vehicle_returned_count=Count('id', filter=Q(vdg_vehicle_returned=True)),
        vdg_paint_returned_count=Count('id', filter=Q(vdg_paint_returned=True)),
        # Real billed spend: sum of the actual TransactionCost VDG charged, plus a
        # count of how many rows carry it (rows from before this field existed are
        # null and fall back to the per-document estimate below).
        real_cost_sum=Sum('vdg_transaction_cost'),
        real_cost_count=Count('id', filter=Q(vdg_transaction_cost__isnull=False)),
        # Average lookup duration (filtered nulls handled by Avg)
        avg_duration_ms=Avg('lookup_duration_ms'),
    )
    total_searches = top_metrics['total']
    today_searches = top_metrics['today']
    week_searches = top_metrics['week']
    month_searches = top_metrics['month']
    success_with_code = top_metrics['with_code']
    vehicle_found = top_metrics['vehicle_found']
    no_vehicle_count = top_metrics['no_vehicle']
    genuine_miss_count = top_metrics['genuine_miss']
    incomplete_count = top_metrics['incomplete']
    name_only_miss_count = top_metrics['name_only_miss']
    # Success rate counts only lookups that reached a DEFINITIVE outcome: either
    # a paint code was delivered (by any provider), or a vehicle was found and
    # the recovery pipeline ran to completion and still came up empty (a genuine
    # miss). Rows are excluded when no vehicle was identified (mistyped reg —
    # not a paint failure) or when the search never completed (the user left
    # before recovery could fire — which also covers all rows from before the
    # recovery leg existed, since they never had one to run). The rule is
    # self-scoping: no launch dates or era cutoffs needed.
    searched_to_completion = success_with_code + genuine_miss_count
    success_rate = (
        (success_with_code / searched_to_completion * 100)
        if searched_to_completion > 0 else 0
    )
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

    # Provider breakdown — where did the paint code come from? Single aggregate.
    provider_breakdown = Search.objects.aggregate(
        prov_vdg=Count('id', filter=Q(provider=Search.PROVIDER_VDG)),
        prov_vdg_retry=Count('id', filter=Q(provider=Search.PROVIDER_VDG_RETRY)),
        prov_pl24=Count('id', filter=Q(provider=Search.PROVIDER_PARTSLINK24)),
        prov_manual=Count('id', filter=Q(provider=Search.PROVIDER_MANUAL)),
        prov_none=Count('id', filter=Q(provider=Search.PROVIDER_NONE)),
        # Recovery funnel: of the lookups where the recovery ran, how did it do?
        recovery_runs=Count('id', filter=Q(recovery_attempted=True)),
        recovery_pl24_hits=Count('id', filter=Q(pl24_returned=True)),
        recovery_vdg_retry_hits=Count('id', filter=Q(vdg_retry_returned=True)),
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

    # VDG cost tracker.
    # Preferred source: the REAL amount VDG billed per lookup
    # (vdg_transaction_cost, captured from BillingInformation.TransactionCost) —
    # tier-correct and net of refunds, so no assumed per-document price. Rows from
    # before that field existed are null; for those we fall back to the old
    # per-document estimate so historical totals don't suddenly drop to zero.
    #
    # Fallback estimate uses the ORIGINAL Tier-1 document prices (£0.12 vehicle +
    # £0.33 paint are the current Tier-2 prices, but the legacy rows were billed
    # at the Tier-1 £0.15/£0.35 they were actually charged, so the estimate stays
    # honest for that era).
    real_cost_sum = float(top_metrics['real_cost_sum'] or 0)
    real_cost_count = top_metrics['real_cost_count'] or 0

    # Legacy rows (no recorded cost): those before this field. Estimate them with
    # the per-document method, scoped to ONLY the rows lacking a real cost.
    legacy = Search.objects.filter(vdg_transaction_cost__isnull=True).aggregate(
        n=Count('id'),
        veh=Count('id', filter=Q(vdg_vehicle_returned=True)),
        paint_ret=Count('id', filter=Q(vdg_paint_returned=True)),
    )
    legacy_n = legacy['n'] or 0
    legacy_vehicle = round((legacy['veh'] or 0) * 0.15, 2)
    legacy_paint_charged = round(legacy_n * 0.35, 2)
    legacy_paint_refunds = round((legacy_n - (legacy['paint_ret'] or 0)) * 0.35, 2)
    legacy_estimate = round(legacy_vehicle + legacy_paint_charged - legacy_paint_refunds, 2)

    estimated_cost = round(real_cost_sum + legacy_estimate, 2)

    # Kept for the admin template's existing labels.
    vdg_vehicle_calls = top_metrics['vdg_vehicle_returned_count']
    vdg_paint_calls = top_metrics['total']
    paint_calls_returned = top_metrics['vdg_paint_returned_count']
    paint_refunds_count = vdg_paint_calls - paint_calls_returned

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
        'vehicle_found': vehicle_found,
        'searched_to_completion': searched_to_completion,
        'no_vehicle_count': no_vehicle_count,
        'genuine_miss_count': genuine_miss_count,
        'incomplete_count': incomplete_count,
        'name_only_miss_count': name_only_miss_count,
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
        'estimated_cost': estimated_cost,
        'real_cost_sum': round(real_cost_sum, 2),
        'real_cost_count': real_cost_count,
        'legacy_estimate': legacy_estimate,
        'legacy_n': legacy_n,
        'vdg_balance': vdg_balance,
        'vdg_balance_at': vdg_balance_at,
        'provider_breakdown': provider_breakdown,
        'maintenance_mode': SiteConfig.get().maintenance_mode,
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
    # Optional free-text note from the admin, included in the email's "A note
    # from us" block. Transient — used only to compose the email, never stored.
    message = (request.POST.get('message') or '').strip()

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
    if len(message) > 1000:
        return JsonResponse({
            'success': False,
            'error': f'Note too long ({len(message)} chars, max 1000).',
        }, status=400)

    try:
        search = Search.objects.get(id=search_id)
    except Search.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'Search record not found.'}, status=404)

    if not search.email:
        return JsonResponse({'success': False, 'error': f'No email on file for {search.registration}.'}, status=400)

    # Atomically claim the row before sending. A single UPDATE filtered on
    # manual_lookup_completed=False means only ONE concurrent request (or a
    # double-click) wins; the rest see claimed==0 and 409. The claim is set
    # BEFORE the send, but the send happens OUTSIDE any DB lock (Resend is a slow
    # HTTP call), so we never hold a row lock across it. If the send fails we
    # release the claim below so it can be retried.
    claimed = Search.objects.filter(
        id=search_id, manual_lookup_completed=False
    ).update(manual_lookup_completed=True)
    if not claimed:
        return JsonResponse({'success': False, 'error': f'Already completed for {search.registration}.'}, status=409)

    # Title-case the description so '  glacier white-metallic ' becomes
    # 'Glacier White-Metallic' before saving and sending.
    paint_description_clean = smart_title(paint_description) if paint_description else ''

    # Look up swatch (hex) and canonical code so the email matches the website UI
    paint_hex, _paint_name, canonical_code = PaintLookup.lookup_with_canonical(
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
        message=message,
    )

    if not sent:
        # Release the claim so the request can be retried after a transient email
        # failure (we set manual_lookup_completed=True optimistically above).
        Search.objects.filter(id=search_id).update(manual_lookup_completed=False)
        return JsonResponse({
            'success': False,
            'error': f'Failed to send email to {search.email}. Please try again.',
        }, status=502)

    # Email is out — finalise the remaining fields (manual_lookup_completed was
    # already set by the claim above). recovery_name_only is cleared because a
    # real CODE has now been found manually: the OUTCOME column checks
    # recovery_name_only BEFORE success, so leaving it set would keep showing ◐
    # despite the code being filled. provider stays MANUAL — that's where the
    # CODE came from; partslink24 only supplied the name, which is preserved in
    # paint_description.
    Search.objects.filter(id=search_id).update(
        paint_code=paint_code,
        paint_description=paint_description_clean,
        provider=Search.PROVIDER_MANUAL,
        success=True,
        recovery_name_only=False,
        email_sent=True,
    )

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

    # Atomically claim the row (same pattern as submit_manual_lookup): a single
    # filtered UPDATE means a double-click or concurrent request can't dismiss
    # twice — the loser sees claimed==0 and 409. No send here, nothing to release.
    claimed = Search.objects.filter(
        id=search_id, manual_lookup_completed=False
    ).update(manual_lookup_completed=True)
    if not claimed:
        return JsonResponse({'success': False, 'error': f'Already actioned for {search.registration}.'}, status=409)

    return JsonResponse({
        'success': True,
        'message': f'Dismissed manual lookup request for {search.registration}.',
        'registration': search.registration,
    })