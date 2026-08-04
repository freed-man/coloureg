import os
import re
import time
import logging
import requests
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from django.shortcuts import render, redirect
from django.conf import settings as dj_settings
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.core.exceptions import ValidationError
from django.core.validators import validate_email, validate_ipv46_address
from django.core.cache import caches
from django.db import connection
from django.db.models import Count, Avg, Q, Sum, Max, Case, When, IntegerField
from django.db.models.functions import TruncDate
from django.http import JsonResponse, HttpResponse
from django.utils import timezone
from django.views.decorators.http import require_POST, require_GET
from django.views.decorators.csrf import csrf_exempt
from django_ratelimit.core import is_ratelimited
from .models import Search, PaintLookup, SiteConfig, VrmCache
from .services.vdg import (
    get_combined_lookup,
    smart_title,
    normalize_fuel_type,
    fix_make_case,
    VdgError,
    VdgNotFoundError,
)
from .services.paint_resolver import (
    resolve_paint,
    _enrich_from_lookup,
    PL24_TIMEOUT,
    acquire_recovery_slot,
    release_recovery_slot,
)
from .services.uploads import process_image_upload
from .services.protection import (
    budget_exceeded,
    spend_today,
    get_cached_vrm_payload,
    store_vrm_payload,
    verify_turnstile,
    is_recent_miss,
    record_miss,
    clear_miss,
    sliding_rate_limited,
    credit_sliding_allowance,
    london_day_start,
)
from .services.payments import (
    payments_active,
    payments_configured,
    create_checkout_session,
    get_session,
    capture,
    construct_webhook_event,
)
from .services.email import (
    send_user_paint_code,
    send_admin_failure_notification,
    send_user_pending_notification,
    send_admin_contact_message,
    send_user_contact_confirmation,
    send_custom_message,
    send_admin_budget_alert,
    send_user_no_code_available,
)

logger = logging.getLogger(__name__)


def _valid_ip(value):
    """Return `value` if it is a real IPv4/IPv6 address, else None.

    Search.ip_address is a GenericIPAddressField, which Postgres implements as
    `inet`. Django does NOT validate on save() — GenericIPAddressField.
    get_prep_value passes any string straight through — so an unparseable value
    reaches the database and raises DataError. In index() that save happens
    AFTER the VDG call has been billed, so the lookup 500s, the row is lost and
    the money is spent.

    SQLite types the same column as char(39) and accepts anything, which is why
    this is invisible locally and in the battery: it only appears in production.

    Reachable because these values come from request headers. CF-Connecting-IP
    is unspoofable *through* Cloudflare (the edge overwrites it), but the origin
    also answers directly on its Railway hostname — which the Stripe webhook
    deliberately relies on — so a direct request can carry any value it likes.
    """
    if not value:
        return None
    try:
        validate_ipv46_address(value)
    except ValidationError:
        return None
    return value


def get_client_ip(request):
    # All traffic is proxied through Cloudflare (orange-cloud), which sets
    # CF-Connecting-IP to the single real client IP. Trust that first.
    # X-Forwarded-For is unreliable here: Cloudflare appends its edge IP and
    # Railway's proxy rewrites the chain, so the visitor isn't dependably the
    # first entry (that's why logs were showing 172.6x Cloudflare IPs).
    #
    # Every candidate is validated before it is returned (see _valid_ip). A
    # header that isn't an IP falls through to the next source rather than being
    # trusted, and if nothing valid is found we return None: the column is
    # nullable, and a null IP is a great deal better than a 500 on a lookup we
    # have already paid for. Rate limiting degrades safely too — junk-IP callers
    # all key to the same bucket, so they share one allowance between them
    # instead of getting a fresh one per forged header.
    cf_ip = request.META.get('HTTP_CF_CONNECTING_IP')
    if cf_ip:
        ip = _valid_ip(cf_ip.strip())
        if ip:
            return ip
    x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
    if x_forwarded_for:
        ip = _valid_ip(x_forwarded_for.split(',')[0].strip())
        if ip:
            return ip
    return _valid_ip(request.META.get('REMOTE_ADDR'))


def extract_mot_field(mot_data, field_name):
    """Pull a field from the DVLA MOT API response.

    DVLA's /v1/trade/vehicles/registration/{vrm} endpoint returns a single dict
    for one VRM (it never returns a list for the per-VRM endpoint).
    """
    if mot_data and isinstance(mot_data, dict):
        return mot_data.get(field_name)
    return None


# How long a resolved make is remembered, so index() can reuse what
# /vehicle-make/ already learned instead of asking DVLA a second time.
MAKE_CACHE_TTL_S = 600


def _make_cache_key(registration):
    return f'make:{registration}'


def _remember_make(registration, make):
    """Cache a resolved make so the LOOKUP can reuse it (paint23).

    /vehicle-make/ used to throw its answer away — every branch returned JSON
    and stored nothing — so when index() later wanted the make it had no choice
    but to ask DVLA again. That is the only reason two calls were needed. One
    line here removes the second call entirely.
    """
    if not (registration and make):
        return
    try:
        caches['default'].set(_make_cache_key(registration), make, MAKE_CACHE_TTL_S)
    except Exception:  # noqa: BLE001 — a cache miss just costs a lookup, not correctness
        pass


def _known_make(registration):
    """The make, if we can get it WITHOUT paying for it (paint23).

    Cheapest first, and never calls VDG:
      1. the cache /vehicle-make/ just populated
      2. VrmCache — we have served this reg before
      3. our own Search history

    Returns '' when only a paid call could tell us. Deliberately does NOT fall
    through to DVLA: index() decides whether that round-trip is worth it, and
    on the normal path /vehicle-make/ has already made it.
    """
    if not registration:
        return ''
    try:
        cached = caches['default'].get(_make_cache_key(registration))
        if cached:
            return cached
    except Exception:  # noqa: BLE001
        pass
    payload = get_cached_vrm_payload(registration, count_hit=False)
    if payload and payload.get('make'):
        return payload['make']
    prior = (
        Search.objects.filter(registration=registration)
        .exclude(make='')
        .order_by('-timestamp')
        .values_list('make', flat=True)
        .first()
    )
    return prior or ''


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


def _maybe_alert_budget(config):
    """Send the budget-tripped admin email at most once per London day.

    Called by the breaker each time a lookup is refused for budget reasons. The
    guard state lives on SiteConfig (budget_tripped + budget_tripped_date):
      - first refusal of the day  -> flag it, stamp today's date, send email
      - subsequent refusals       -> flag already set for today, do nothing
      - new day                   -> date no longer matches, so the first
                                     refusal of the new day re-arms and re-sends.
    The flag is also cleared implicitly when the budget stops being exceeded
    (spend resets at midnight), so no cron is needed. Email failures are
    swallowed — an alert must never take down the refusal path itself.
    """
    today = timezone.localdate()
    if config.budget_tripped and config.budget_tripped_date == today:
        return  # already alerted for today's trip
    config.budget_tripped = True
    config.budget_tripped_date = today
    config.save(update_fields=['budget_tripped', 'budget_tripped_date', 'updated_at'])
    try:
        send_admin_budget_alert(spend_today(), config.daily_budget_gbp)
    except Exception:
        # Never break the refusal path for a failed email — but do record it.
        # A silent failure here is the worst case: the breaker has tripped and
        # lookups are being refused, and the one person who needs to know has
        # not been told.
        logger.exception('Budget alert email failed to send')


def index(request):
    # Single cached SiteConfig read for the whole request (maintenance flag,
    # blocklists, budget, payments switch). Cached in-process, so this is a
    # memory read on a warm worker — no DB query on the homepage GET.
    config = SiteConfig.get()
    maintenance = config.maintenance_mode

    if request.method == 'POST':
        if maintenance:
            # Refuse silently-but-clearly: no VDG call, no Search row, just
            # re-render the maintenance page. (Covers direct/scripted POSTs.)
            return render(request, 'lookup/index.html', {
                'maintenance_mode': True,
                'turnstile_site_key': dj_settings.TURNSTILE_SITE_KEY,
            })

        # --- Blocklist guard (A) -------------------------------------------
        # Refuse blocked registrations / IPs / user-agents before anything
        # costs money or touches VDG. Reg is the strong signal (abusers reuse
        # regs); IP and UA are scalpels for the lazy case. We compute the client
        # IP + reg once here and reuse them below. The response is a generic
        # error — we don't tell a blocked client why.
        client_ip = get_client_ip(request)
        posted_reg = request.POST.get('registration', '').strip().upper().replace(' ', '')
        if (config.is_ip_blocked(client_ip)
                or (posted_reg and config.is_reg_blocked(posted_reg))):
            messages.error(
                request,
                'Sorry, we could not process that request. Please try again '
                'later.'
            )
            return render(request, 'lookup/index.html', {
                'turnstile_site_key': dj_settings.TURNSTILE_SITE_KEY,
            })

        # --- Turnstile verification (E) -------------------------------------
        # When configured (both keys in env), every lookup POST must carry a
        # valid token from the invisible widget in the form. Scripts that POST
        # directly never have one, so they're rejected here — before the budget
        # query, before ratelimit, before Stripe or VDG. Costs a real user
        # nothing: the widget solves in the background while they type.
        # Unconfigured -> verify_turnstile returns True (feature off).
        if not verify_turnstile(
            request.POST.get('cf-turnstile-response', ''), client_ip
        ):
            # Record the refusal. Without this a Turnstile rejection is a pure
            # ABSENCE — the user sees an error and nothing anywhere shows a
            # lookup was refused, so a widget silently failing for some browser
            # would look identical to a quiet day. Cloudflare's own analytics
            # report challenge outcomes, but they can't tell us which of OUR
            # lookups were turned away. Logged as a Search row (no VDG call, no
            # cost) so the count sits alongside real volume on one dashboard.
            _record_turnstile_block(request, client_ip)
            messages.error(
                request,
                'We could not verify your browser. Please reload the page '
                'and try again.'
            )
            return render(request, 'lookup/index.html', {
                'turnstile_site_key': dj_settings.TURNSTILE_SITE_KEY,
                'payments_on': payments_active(config),
                'payments_configured': payments_configured(),
                'lookup_price': dj_settings.LOOKUP_PRICE_PENCE / 100.0,
            })

        # --- Daily budget breaker (A) --------------------------------------
        # Hard ceiling on VDG spend per London day. If today's real (refund-net)
        # spend has reached the configured budget, refuse new lookups until the
        # day rolls over — a bounded worst case even if every other layer is
        # bypassed. Email the admin ONCE per trip. Budget 0 disables this.
        if budget_exceeded(config):
            _maybe_alert_budget(config)
            messages.error(
                request,
                'Lookups are temporarily paused. Please try again later.'
            )
            return render(request, 'lookup/index.html', {
                'turnstile_site_key': dj_settings.TURNSTILE_SITE_KEY,
            })

        # Sliding window (paint16), replacing django-ratelimit's fixed hourly
        # bucket. The fixed window let one IP make 4 lookups in 13 minutes by
        # straddling the top of the hour (observed 31 Jul), and up to 6 in the
        # worst case. This counts requests in the trailing 60 minutes, so the
        # limit holds regardless of where the clock is.
        was_limited = sliding_rate_limited('lookup', client_ip)
        if was_limited:
            messages.error(
                request,
                'Too many searches. Please wait an hour before trying again.'
            )
            return render(request, 'lookup/index.html', {
                'turnstile_site_key': dj_settings.TURNSTILE_SITE_KEY,
            })

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

        # --- Unsupported make (paint23) ------------------------------------
        # Some manufacturers we simply cannot resolve a paint code for. Running
        # the full pipeline on them costs a paid VDG call, then a second one on
        # the retry, and ends in a failure the visitor waited up to a minute
        # for. Refusing here costs nothing and answers instantly.
        #
        # _known_make never calls VDG. On the normal path /vehicle-make/ has
        # just resolved and cached the make for the spinner text, so this is a
        # free cache read — the same DVLA answer, used twice instead of fetched
        # twice. Repeat registrations skip DVLA entirely (VrmCache or our own
        # history answer them).
        #
        # It returns '' when only a paid call could tell us the make. That is
        # deliberately allowed through: refusing on a guess would block real
        # vehicles, and an unknown make is exactly the case where we have no
        # grounds to refuse. This gate only ever fires on a make we KNOW.
        _known = _known_make(registration)
        if _known and config.is_make_unsupported(_known):
            messages.error(
                request,
                f'We cannot currently find paint codes for {_known} vehicles. '
                f'No charge has been made — send us a message and we will look '
                f'into it by hand.'
            )
            return redirect('index')

        # --- VRM result cache (A) ------------------------------------------
        # If we've recently returned a successful result for this exact reg,
        # serve it from storage with NO VDG call — repeated lookups of the same
        # reg (the observed abuse pattern) cost £0. Paint codes are immutable, so
        # a stored result is correct; the TTL only bounds the rare cherished-
        # transfer case. We still log a lightweight Search row (provider=cache)
        # so the dashboard sees the request, but it carries no cost and makes no
        # external call. Cache is bypassed entirely while payments are enabled
        # (a paying customer's flow is handled separately) — belt and braces,
        # since payments_enabled is False today.
        if not config.payments_enabled:
            cached_payload = get_cached_vrm_payload(registration)
            if cached_payload:
                cache_search = Search(
                    registration=registration,
                    ip_address=get_client_ip(request),
                    user_agent=request.META.get('HTTP_USER_AGENT', ''),
                    device=parse_device(request.META.get('HTTP_USER_AGENT', '')),
                    make=cached_payload.get('make', ''),
                    model=cached_payload.get('model', ''),
                    year=cached_payload.get('year'),
                    colour=cached_payload.get('colour', ''),
                    vehicle_title=cached_payload.get('vehicle_title', ''),
                    category=cached_payload.get('category', ''),
                    vin=cached_payload.get('vin', '') or '',
                    paint_code=cached_payload.get('paint_code', ''),
                    paint_description=cached_payload.get('paint_description', ''),
                    provider=Search.PROVIDER_CACHE,
                    success=bool(cached_payload.get('paint_code')),
                    lookup_duration_ms=int((time.time() - start_time) * 1000),
                )
                cache_search.save()
                payload = dict(cached_payload)
                payload['search_id'] = cache_search.id
                payload['paint_pending'] = False  # cached results are complete
                request.session['vehicle_data'] = payload
                return redirect('results')

            # --- Negative cache (A): recently-failed reg ---------------------
            # If this exact reg failed a lookup within the last hour, don't spend
            # on VDG again — a proxy pool hammering one unanswerable reg (the
            # observed abuse) is served an instant failure instead of a full-price
            # miss each time. Short TTL so a reg that only failed because VDG
            # flaked gets a genuine fresh attempt soon after. Logs a lightweight
            # Search row (provider=cache) so the failure is still visible.
            if is_recent_miss(registration):
                miss_search = Search(
                    registration=registration,
                    ip_address=get_client_ip(request),
                    user_agent=request.META.get('HTTP_USER_AGENT', ''),
                    device=parse_device(request.META.get('HTTP_USER_AGENT', '')),
                    provider=Search.PROVIDER_CACHE,
                    success=False,
                    error_message='negative-cache hit (recent miss, VDG skipped)',
                    lookup_duration_ms=int((time.time() - start_time) * 1000),
                )
                miss_search.save()
                # Point them at the contact form, which is on /help/ — the
                # manual-lookup capture lives on the results page, and we are
                # deliberately not sending them there (that is the page this
                # short-circuit exists to avoid re-running). Saying "below"
                # would be wrong: the homepage has no such form.
                messages.error(
                    request,
                    'We checked that registration very recently and could not '
                    'find a paint code for it. Send us a message and we will '
                    'look into it by hand.'
                )
                return render(request, 'lookup/index.html', {
                    'turnstile_site_key': dj_settings.TURNSTILE_SITE_KEY,
                    'payments_on': payments_active(config),
                    'payments_configured': payments_configured(),
                    'lookup_price': dj_settings.LOOKUP_PRICE_PENCE / 100.0,
                })

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
        # billing_sink captures cost/balance even when the call below raises or
        # reports not-found (paint18). VDG bills for those too, and discarding
        # the response used to discard the receipt with it — spend the daily
        # budget breaker could never see.
        vdg_billing = {}
        try:
            vdg_data = get_combined_lookup(registration, billing_sink=vdg_billing)
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
        except Exception as e:  # noqa: BLE001 — trust boundary
            # VDG is an external system whose response shape we don't control.
            # A structurally unexpected payload (a null inside PaintCodeList, a
            # field that becomes a string, an API revision) would otherwise raise
            # an AttributeError straight out of the parser and 500 the page —
            # after VDG has already charged us. Treat anything unexpected as
            # "provider failed" so we degrade to the DVLA/MOT fallback instead,
            # and log it so a genuine API change is visible rather than silent.
            logger.exception('VDG returned an unparseable payload for %s', registration)
            search.error_message = f'VDG: unparseable response ({type(e).__name__})'

        # Whatever happened above, if VDG billed us and nothing was recorded on
        # the row, record it now. The sink is the only source on the failure
        # paths, and an unrecorded charge is invisible to the budget breaker.
        if search.vdg_transaction_cost is None and vdg_billing.get('transaction_cost') is not None:
            search.vdg_transaction_cost = vdg_billing['transaction_cost']
        if latest_balance is None and vdg_billing.get('balance') is not None:
            latest_balance = vdg_billing['balance']

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
            #
            # We land here when VDG's VehicleDetails document did NOT succeed
            # (vehicle_returned is False). Historically that meant "VDG knows
            # nothing", so we discarded the whole VDG response and rebuilt from
            # DVLA + MOT. But there is a third case the boolean hides:
            # VehicleDetails can come back StatusCode 25
            # ("NoDvlaRegistrationDataAvailable") — VDG DOES know the vehicle and
            # populated the identification block (VIN, and often make/model via
            # ModelDetails + a VIN echo in VehicleCodes) — its DVLA MIRROR is
            # simply stale, which happens for vehicles registered in the last few
            # weeks. In that case the parser still returns a VIN (and category),
            # but the old all-or-nothing gate threw them away, so:
            #   - brand-new vehicles showed no VIN even though VDG returned one, and
            #   - paint recovery never fired, because paint_pending needs a VIN.
            # Salvage whatever identification VDG did provide BEFORE falling back,
            # so the DVLA/MOT lookup only fills the gaps that remain. The VIN and
            # category are the load-bearing fields here: VIN gates the /lookup-
            # status pl24 recovery, and category routes it (commercial vs car).
            if vdg_data:
                vin = vdg_data.get('vin', '') or vin
                category = vdg_data.get('category', '') or category
                # Take VDG's make/model/etc as a base too when present; DVLA+MOT
                # below overwrite any that come back non-empty, so a stale-mirror
                # response that still carried ModelDetails isn't wasted, and a
                # truly empty VDG response changes nothing.
                make = vdg_data.get('make', '') or make
                model = vdg_data.get('model', '') or model
                year = vdg_data.get('year') if vdg_data.get('year') is not None else year
                colour = vdg_data.get('colour', '') or colour
                fuel_type = vdg_data.get('fuel_type', '') or fuel_type
                transmission = vdg_data.get('transmission', '') or transmission
                engine_description = (
                    vdg_data.get('engine_description', '') or engine_description
                )

            dvla = get_dvla_data(registration)
            if not dvla:
                # DVLA has nothing. Normally that's a genuine "not found" — but
                # if VDG already identified the vehicle (we salvaged a VIN just
                # above, the stale-mirror / status-25 case), it IS a real vehicle
                # and we should proceed with the VDG data rather than dead-end the
                # user. Only bail when NEITHER source knows the vehicle.
                if not vin:
                    search.success = False
                    search.lookup_duration_ms = int((time.time() - start_time) * 1000)
                    if not search.error_message:
                        search.error_message = 'DVLA + VDG: vehicle not found'
                    else:
                        search.error_message += ' | DVLA: not found'
                    search.save()
                    # Remember this dud reg so a repeat within the hour is served
                    # instantly (this is the common not-found exit — an invalid or
                    # unrecognised reg — and the main thing a proxy pool hammers).
                    record_miss(registration)
                    messages.error(
                        request,
                        'Vehicle not found. Please check the registration '
                        'number is correct and try again.'
                    )
                    return redirect('index')
                # else: fall through with the salvaged VDG fields (make/model/etc
                # already set above); MOT can still add a model below.
            else:
                # DVLA responded — use its fields, but never let an EMPTY DVLA
                # value overwrite something VDG already gave us (guard each with
                # `or <existing>`), so the salvage above is preserved when DVLA is
                # sparse.
                make = fix_make_case((dvla.get('make', '') or '').title()) or make
                if dvla.get('yearOfManufacture') is not None:
                    year = dvla.get('yearOfManufacture')
                colour = (dvla.get('colour', '') or '').title() or colour
                fuel_type = normalize_fuel_type(dvla.get('fuelType', '')) or fuel_type

            mot = get_mot_data(registration)
            mot_model = extract_mot_field(mot, 'model') or ''
            model = mot_model.title() or model

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

        # Gate BEFORE the session is written and the redirect happens (paint22).
        # results() reads this off the row, so it has to be decided here rather
        # than inferred later from the session copy.
        _apply_paywall(search, config)

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
            # Recovery gate. Previously `bool(vin) and not paint_code`, which
            # coupled the whole recovery leg to having a VIN — but only the pl24
            # scrape needs one. The VDG retry works from the registration alone.
            # So when VDG fails outright (no VIN, DVLA supplies the details) we
            # used to skip BOTH legs when we could still have run the retry.
            # _pl24_lookup already returns None without a VIN, so the pl24 leg
            # simply no-ops and the retry still gets its chance.
            'paint_pending': (not paint_code) and bool(make),
        }

        # --- VRM cache write (A) -------------------------------------------
        # A successful lookup (paint code delivered) is stored so the next
        # request for this reg is served from cache with zero VDG spend. Only
        # complete successes are cached — a miss must stay live so recovery /
        # retry always gets a fresh shot. Payload excludes the request-specific
        # keys (search_id, paint_pending); store_vrm_payload strips them anyway.
        if paint_code:
            store_vrm_payload(registration, request.session['vehicle_data'])
            clear_miss(registration)  # a prior miss is now stale — forget it
        elif bool(vin):
            # Vehicle found but no paint yet. The pl24/VDG-retry recovery may
            # still succeed via /lookup-status, so DON'T record a miss here —
            # that's done only if recovery also comes back empty (see
            # lookup_status). Recording now would wrongly short-circuit the very
            # next lookup while recovery could still find the code.
            pass
        else:
            # No vehicle at all (or a genuine dead end) -> remember the miss so a
            # repeat within the hour is served instantly without VDG spend.
            record_miss(registration)

        return redirect('results')

    return render(request, 'lookup/index.html', {
        'maintenance_mode': maintenance,
        'turnstile_site_key': dj_settings.TURNSTILE_SITE_KEY,
        # Paid mode (F): when active, the form posts to /paid/start/ instead of
        # here, and the button/label reflect the price. Dormant by default.
        'payments_on': payments_active(config),
        'payments_configured': payments_configured(),
        'lookup_price': dj_settings.LOOKUP_PRICE_PENCE / 100.0,
    })


@require_POST
def vehicle_make(request):
    """Return just the manufacturer for a registration, as fast as possible.

    Exists purely for the loading experience: the homepage fires this ALONGSIDE
    the main lookup POST (two independent requests, in parallel), so the spinner
    can say "Requesting Ford build sheet..." instead of something generic. It is
    strictly cosmetic — the real lookup neither waits for it nor depends on it,
    and if this fails the message simply stays as it was.

    Because it is off the critical path, it is ordered cheapest-first:
      1. VrmCache        — if we have served this reg before, zero API calls
      2. Recent Search   — likewise, from our own history
      3. DVLA            — only if we genuinely have never seen the reg
    Most repeat traffic never touches DVLA at all.

    Protected but deliberately more generously than a lookup: it costs no VDG
    money and returns only a manufacturer name (low value to an abuser), but it
    does consume a third-party API call, so it gets its own rate-limit bucket.
    """
    registration = (request.POST.get('registration') or '').strip().upper().replace(' ', '')
    if not re.fullmatch(r'[A-Z0-9]{1,8}', registration or ''):
        return JsonResponse({})

    config = SiteConfig.get()
    client_ip = get_client_ip(request)
    if config.is_ip_blocked(client_ip) or config.is_reg_blocked(registration):
        return JsonResponse({})

    # Separate, roomier bucket than the 3/h lookup limit — a typo shouldn't cost
    # someone their allowance, but this can't be hammered for free either.
    if sliding_rate_limited('make', client_ip, limit=15):
        return JsonResponse({})

    # 1. Anything we already hold?
    # count_hit=False: this is a name lookup for the loading message, not a
    # lookup being served from cache. Counting it would double the dashboard's
    # "free repeats served" figure.
    cached = get_cached_vrm_payload(registration, count_hit=False)
    if cached and cached.get('make'):
        _remember_make(registration, cached['make'])
        return JsonResponse({'make': cached['make'],
                             'supported': not config.is_make_unsupported(cached['make'])})

    prior = (
        Search.objects.filter(registration=registration)
        .exclude(make='')
        .order_by('-timestamp')
        .values_list('make', flat=True)
        .first()
    )
    if prior:
        _remember_make(registration, prior)
        return JsonResponse({'make': prior,
                             'supported': not config.is_make_unsupported(prior)})

    # 2. Ask DVLA. Short timeout: this is decoration, and a slow answer is worse
    # than no answer — the message would land after the results page already had.
    try:
        dvla = get_dvla_data(registration)
    except Exception:
        dvla = None
    if dvla and dvla.get('make'):
        _resolved = fix_make_case((dvla.get('make') or '').title())
        # Remembered so index() can gate on it without a second DVLA call.
        _remember_make(registration, _resolved)
        return JsonResponse({'make': _resolved,
                             'supported': not config.is_make_unsupported(_resolved)})
    return JsonResponse({})


def _record_turnstile_block(request, client_ip):
    """Log a lookup refused by Turnstile, so refusals are countable.

    Deliberately a Search row rather than only a log line: it puts the count on
    the same dashboard as real lookup volume, which is what makes the signal
    readable. A rise in blocks alongside a fall in lookups is unambiguous; two
    separate dashboards correlated by eye is not.

    Marked provider='none' with success=False and no make, so it lands in the
    EXCLUDED bucket of the success rate — a refused request never reached the
    pipeline, so counting it as a failure would understate how well the pipeline
    is doing. Best-effort: a logging failure must never break the refusal.
    """
    try:
        Search.objects.create(
            registration=(request.POST.get('registration') or '')
                .strip().upper().replace(' ', '')[:10],
            ip_address=client_ip,
            user_agent=request.META.get('HTTP_USER_AGENT', ''),
            device=parse_device(request.META.get('HTTP_USER_AGENT', '')),
            provider=Search.PROVIDER_NONE,
            success=False,
            error_message='turnstile_blocked',
            # No duration: nothing was looked up, so a timing figure here would
            # be noise in the avg-lookup-time metric rather than information.
            lookup_duration_ms=None,
        )
    except Exception:
        logger.exception('Could not record a Turnstile block')


@require_GET
def security_txt(request):
    """Serve /.well-known/security.txt (RFC 9116).

    Tells security researchers how to report a vulnerability responsibly,
    instead of guessing at an address or posting it publicly.

    Generated rather than served as a static file for one reason: RFC 9116 makes
    `Expires` mandatory, and a file with a past expiry is treated as invalid.
    A hardcoded date would quietly rot the moment it passed. Computing it a year
    ahead on each request means it is always valid without anyone remembering to
    update it.
    """
    expires = (timezone.now() + timedelta(days=365)).strftime('%Y-%m-%dT%H:%M:%S.000Z')
    body = (
        f"Contact: mailto:{dj_settings.DEFAULT_FROM_EMAIL}\n"
        f"Expires: {expires}\n"
        "Preferred-Languages: en\n"
        "Canonical: https://coloureg.com/.well-known/security.txt\n"
    )
    return HttpResponse(body, content_type='text/plain; charset=utf-8')


@require_GET
def warm(request):
    """Pre-warm the database connection so a lookup doesn't pay a cold start.

    The homepage fires this (once) when the user focuses the registration field
    — typically a few seconds to tens of seconds before they submit. By then
    Neon's compute (which suspends after 5 idle minutes) has already resumed, so
    the subsequent lookup POST finds a warm DB and skips the ~0.5–2s wake-up. It
    does the minimum work needed to open a real connection and returns 204 with
    no body.

    Deliberately cheap and side-effect-free: one `SELECT 1`, no Search row, no
    session write, no VDG call. Bots that POST straight to `/` never focus a
    form field, so they don't trigger this — only real browsers do. Failures are
    swallowed (still 204): warming is best-effort, and a failed warm just means
    the lookup pays the cold start it would have paid anyway.
    """
    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT 1')
            cursor.fetchone()
    except Exception:
        pass
    return HttpResponse(status=204)


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

    # --- Locked result (paint22) ------------------------------------------
    # Read the gate off the ROW, not the session copy: the session was written
    # when the lookup ran, but a background recovery can have completed (and
    # gated the row) since. The row is the only current truth.
    #
    # The locked context is BUILT SEPARATELY rather than assembled and then
    # stripped. Redacting a full context is one forgotten key away from leaking
    # the answer, and the answer is the entire product. Nothing below this
    # branch runs while locked, so paint_code, paint_description,
    # all_paint_codes and the swatch lookups are never even computed.
    _locked_search = None
    _sid = vehicle_data.get('search_id')
    if _sid:
        try:
            _locked_search = Search.objects.get(id=_sid)
        except (Search.DoesNotExist, ValueError, TypeError):
            _locked_search = None
    if _locked_search is not None and _locked_search.is_locked():
        ctx = {
            'registration': _locked_search.registration,
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
            'search_id': _locked_search.id,
            'email_submitted': email_submitted,
            'paint_pending': False,
            'paint_name_only': False,
            # The unlock form carries its own Turnstile widget, so the key has
            # to be in this context too — the locked context is built from
            # scratch and inherits nothing.
            'turnstile_site_key': dj_settings.TURNSTILE_SITE_KEY,
        }
        ctx.update(_locked_payload(_locked_search))
        return render(request, 'lookup/results.html', ctx)

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


# How long the page is told to wait before retrying a 'busy' response.
RECOVERY_BUSY_RETRY_S = 4


@require_GET
def lookup_status(request, search_id):
    """Concurrency gate in front of the real handler (paint19).

    _lookup_status runs the recovery race, which parks this thread for up to
    ~65s. Gunicorn serves 16 concurrent requests, so 16 simultaneous paint
    misses park every thread — the healthcheck included, and a failed
    healthcheck gets the container restarted with lookups in flight.

    So take a slot or decline immediately. Declining returns the thread at once
    instead of queueing on a lock (a thread blocked on a semaphore is just as
    unavailable), and 'busy' costs nothing: no claim is made, no VDG call
    happens, and the page retries. The row is untouched, so the retry runs the
    recovery properly rather than inheriting a half-started one.
    """
    if not acquire_recovery_slot():
        return JsonResponse({
            'status': 'busy',
            'retry_after': RECOVERY_BUSY_RETRY_S,
        })
    try:
        return _lookup_status(request, search_id)
    finally:
        release_recovery_slot()


def _lookup_status(request, search_id):
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
        result = resolve_paint(
            registration, vin, make, category, telemetry=telemetry,
            model=vehicle_data.get('model', ''), search_id=search_id,
        )
    except Exception:  # noqa: BLE001 — never let a fallback failure 500 the poll
        # Log it. Sentry only reports UNHANDLED exceptions, so without this a
        # bug anywhere in the recovery race (two external services, threads,
        # timeouts — the most intricate code here) would fail completely
        # silently: the user sees a generic error, the row is stamped as
        # attempted, and nothing indicates why. Swallowing is right; swallowing
        # quietly is not.
        logger.exception('Recovery failed for search_id=%s', search_id)
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
        # Recovery is now exhausted for this reg (VDG first pass + VDG retry +
        # pl24 all came back without a code) — remember the miss so a repeat
        # within the hour is served instantly without re-running the whole chain.
        reg_missed = vehicle_data.get('registration')
        if reg_missed:
            record_miss(reg_missed)
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

    # VRM cache write (A): a recovery hit is a full success too — cache it so a
    # repeat of this reg is served for £0. Same rule as the first-pass write:
    # only cache when a real paint code was delivered (which is precisely this
    # branch). Name-only recoveries never reach here, so they're never cached.
    reg_for_cache = vehicle_data.get('registration')
    if reg_for_cache and paint_code:
        store_vrm_payload(reg_for_cache, vehicle_data)
        clear_miss(reg_for_cache)  # recovery found it — drop any stale miss

    # The recovery finished while the page was polling. If that result is
    # chargeable it must NOT be pushed down as JSON (paint22) — the poll is the
    # same door as the page, and handing the code to the browser here would
    # bypass the gate entirely. Tell the page to reload instead; results() then
    # renders the locked view.
    _poll_locked = _locked_status_response(search_id)
    if _poll_locked is not None:
        return _poll_locked

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


# Poll interval for the loser-waits path. Starts short (most recoveries that are
# already running finish quickly) and backs off, because every tick is a
# Search.objects.get() — a Neon round-trip that also holds its compute awake.
# A flat 2s over the 75s ceiling was up to 37 queries per waiting request; this
# is roughly a third of that with no user-visible change (paint19).
RECOVERY_WAIT_POLL_S = 1.0
RECOVERY_WAIT_POLL_MAX_S = 6.0
RECOVERY_WAIT_POLL_GROWTH = 1.5


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
    poll = RECOVERY_WAIT_POLL_S
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
        time.sleep(poll)
        poll = min(poll * RECOVERY_WAIT_POLL_GROWTH, RECOVERY_WAIT_POLL_MAX_S)
    return JsonResponse({'status': 'error'}, status=200)


def _locked_status_response(search_id):
    """JSON for a poll whose result turned out to be chargeable (paint22).

    Returns None when the row is not locked, so the caller carries on as normal.
    """
    try:
        row = Search.objects.get(id=search_id)
    except (Search.DoesNotExist, ValueError, TypeError):
        return None
    if not row.is_locked():
        return None
    payload = {'status': 'locked'}
    payload.update(_locked_payload(row))
    return JsonResponse(payload)


def _apply_paywall(search, config=None):
    """Gate a completed result behind payment, if it is one we charge for.

    Called wherever a lookup reaches its FINAL state — the first VDG call in
    index(), and again in _record_paint_hit when the background recovery
    supplies a code later. Both are needed: a lookup that starts as "no paint
    yet" and recovers a code minutes later is just as chargeable as one that
    resolves immediately.

    Policy, deliberately strict: we charge only when BOTH a code and a colour
    name are present. A code with no name is a partial answer, and selling
    partial answers is how you end up arguing with customers. It is 2 lookups in
    867, so the generosity costs almost nothing.

    Returns True if the row is now withheld pending payment.
    """
    if search is None:
        return False
    if not payments_active(config):
        return False
    if not (search.paint_code and search.paint_description):
        return False
    if search.paid_unlocked:
        return False          # already bought — never re-gate it
    if not search.paywalled:
        search.paywalled = True
        try:
            search.save(update_fields=['paywalled'])
        except Exception:  # noqa: BLE001 — gating must not break the lookup
            logger.warning('could not mark search=%s paywalled', search.pk,
                           exc_info=True)
    return True


def _locked_payload(search, config=None):
    """What a locked result is allowed to tell the browser (paint22).

    Availability ONLY. The code and the colour name never leave the server
    while a result is locked — not blurred, not hidden with CSS, not present in
    the DOM at all — because anything delivered to the page can be read out of
    it. The customer sees that we have the answer, not what it is.
    """
    return {
        'locked': True,
        'code_available': bool(search.paint_code),
        'name_available': bool(search.paint_description),
        'lookup_price': _price_display(config),
    }


def _price_display(config=None):
    pence = getattr(dj_settings, 'LOOKUP_PRICE_PENCE', 200)
    return f'£{pence / 100:.2f}'


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
    fields = ['recovery_attempted', 'vdg_retry_returned', 'pl24_attempted',
              'pl24_returned', 'recovery_name_only', 'recovery_duration_ms']

    # --- Retry spend (paint15) ------------------------------------------------
    # The recovery makes a SECOND VDG call, which VDG bills whether or not it
    # returns paint (~£0.45 on a hit, ~£0.12 refund-net on a miss). Add it to the
    # row so vdg_transaction_cost is the TOTAL real spend for this lookup, not
    # just the first call. This is what makes the daily budget breaker accurate:
    # ~58% of lookups retry, so without this it would see roughly 60% of true
    # spend and a £30 budget would silently run to ~£50.
    # Safe from double-counting: the caller claims the recovery atomically
    # (filter recovery_attempted=False -> update), so this runs exactly once per
    # row; concurrent pollers wait on the result instead of re-running it.
    # NOTE (paint21): the retry's cost and balance are NO LONGER applied here.
    # They are written by the retry worker itself, atomically, the moment it
    # finishes — see paint_resolver._record_retry_billing.
    #
    # They had to move. This function runs after resolve_paint returns, and when
    # pl24 wins the race resolve_paint returns while the VDG retry is still in
    # flight. The retry cannot be cancelled (it has already started), so it
    # completes, VDG bills us, and its cost arrived in the telemetry dict after
    # this had already read it. On real traffic that lost about GBP1/day, always
    # under, never over.
    #
    # Writing them here now would be actively harmful: a bare assignment plus
    # save() would OVERWRITE the atomic F() update the worker performs, so
    # vdg_transaction_cost and vdg_balance_after_call must stay out of `fields`.
    return fields


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

    # A lookup that started as "no paint yet" and recovered a code minutes later
    # is worth exactly as much as one that resolved immediately, so it gets
    # gated too (paint22). Runs after the save above so the row it inspects
    # already carries the recovered code and name.
    _apply_paywall(search)


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
    # Optional context from the customer (paint16). Free text plus an optional
    # photo — typically of the paint-code label or the V5C. Both are genuinely
    # useful: a note like "it's a Japanese import" or a legible label photo is
    # often the difference between a dead end and a found code. Capped and
    # validated; neither is required, and a bad upload is silently ignored
    # rather than failing the request.
    customer_message = request.POST.get('customer_message', '').strip()[:2000]
    photo = process_image_upload(
        request.FILES.get('photo'), filename_prefix='customer-photo'
    )

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

    # update_fields throughout submit_email (paint20). A bare save() writes EVERY
    # column from an in-memory copy of the row, and this view holds that copy
    # across up to two Resend calls (30s timeout each). The background recovery
    # writes paint_code/success/provider to the SAME row over the same window,
    # and it writes them narrowly — so a full save here silently reverts them,
    # blanking a paint code that was genuinely found.
    #
    # The visitor is then told no code exists, the row records a failure, and
    # once payments are live the authorisation would be cancelled on a lookup
    # that actually succeeded. Naming the columns each save owns removes the
    # whole class: two writers touching different columns can no longer collide.
    email_fields = ['email']
    if customer_message:
        search.customer_message = customer_message
        email_fields.append('customer_message')
    search.email = email
    search.save(update_fields=email_fields)

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
            search.save(update_fields=['email_sent'])
    else:
        admin_sent = send_admin_failure_notification(
            registration=search.registration,
            vehicle_title=search.vehicle_title,
            vin_full=search.vin,
            colour=search.colour,
            user_email=email,
            customer_message=customer_message,
            extra_attachments=[photo] if photo else None,
        )
        user_sent = send_user_pending_notification(
            to_email=email,
            registration=search.registration,
            vehicle_title=search.vehicle_title,
            vin_masked=vin_masked,
            colour=search.colour,
        )
        if admin_sent and user_sent:
            # The riskiest of the three: this runs after BOTH sends above, so
            # the in-memory row can be a minute stale by now.
            search.email_sent = True
            search.save(update_fields=['email_sent'])

    request.session['email_submitted'] = email
    return redirect('results')


def about(request):
    return render(request, 'lookup/about.html', {
        'payments_on': payments_active(),
    })


def privacy(request):
    return render(request, 'lookup/privacy.html', {
        'payments_on': payments_active(),
    })


def disclaimer(request):
    # payments_on drives the free-vs-paid variants of this page: the liability
    # section, the refund/cancellation terms, and the trading disclosure all
    # switch automatically when payments are enabled, so there's no manual edit
    # to forget on launch day.
    return render(request, 'lookup/disclaimer.html', {
        'payments_on': payments_active(),
    })


def help_page(request):
    contact_submitted = request.session.pop('contact_submitted', None)
    return render(request, 'lookup/help.html', {
        'contact_submitted': contact_submitted,
        'payments_on': payments_active(),
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

    # Save the daily VDG budget (A). Same redirect-after-POST pattern as the
    # maintenance toggle. Accepts a decimal amount; 0 disables the breaker.
    # Also clears the tripped flag so a raised budget takes effect immediately
    # (otherwise today's earlier trip would keep the alert suppressed even
    # though lookups have resumed).
    if request.method == 'POST' and request.POST.get('action') == 'save_budget':
        cfg = SiteConfig.get()
        raw = (request.POST.get('daily_budget_gbp') or '').strip()
        try:
            value = Decimal(raw)
            if value < 0:
                raise InvalidOperation
        except (InvalidOperation, ValueError):
            messages.error(request, 'Budget must be a number (0 disables the limit).')
            return redirect('admin_stats')
        # Upper bound. The column is numeric(8,2), so anything at or above
        # 1,000,000 is unstorable: Postgres raises DataError and 500s this save,
        # while SQLite accepts it and then fails on every subsequent read — which
        # would break SiteConfig.get(), and that runs on every request.
        # 10,000 is far above any plausible daily VDG budget (a heavy day is
        # ~£20 against a balance in the low hundreds), so a larger number is a
        # mistyped zero rather than an intention.
        if value > Decimal('10000'):
            messages.error(
                request,
                'That budget looks like a typo — the daily limit is capped at '
                '£10,000. Enter a smaller amount, or 0 to disable the limit.'
            )
            return redirect('admin_stats')
        cfg.daily_budget_gbp = value
        cfg.budget_tripped = False
        cfg.budget_tripped_date = None
        cfg.save(update_fields=[
            'daily_budget_gbp', 'budget_tripped', 'budget_tripped_date', 'updated_at',
        ])
        messages.success(
            request,
            f'Daily budget set to £{value:.2f}.' if value > 0
            else 'Daily budget disabled (no spend limit).'
        )
        return redirect('admin_stats')

    # Save the blocklists (A). Three textareas, newline/comma separated; stored
    # verbatim (parsing happens at match time via SiteConfig helpers).
    if request.method == 'POST' and request.POST.get('action') == 'save_blocklists':
        cfg = SiteConfig.get()
        cfg.blocked_regs = (request.POST.get('blocked_regs') or '').strip()
        cfg.blocked_ips = (request.POST.get('blocked_ips') or '').strip()
        # paint23: makes we cannot resolve. Editable here rather than in code so
        # a manufacturer can be switched off the moment the data says so, and
        # switched back on just as fast if a curated override later fixes it.
        cfg.unsupported_makes = (request.POST.get('unsupported_makes') or '').strip()
        cfg.save(update_fields=['blocked_regs', 'blocked_ips',
                                'unsupported_makes', 'updated_at'])
        n = len(cfg.blocked_reg_set()) + len(cfg.blocked_ip_set())
        messages.success(request, f'Blocklists saved ({n} entries active).')
        return redirect('admin_stats')

    # Payments master switch (F). Only meaningful once the Stripe env keys are
    # set; the view guards on payments_active() which checks both.
    if request.method == 'POST' and request.POST.get('action') == 'toggle_payments':
        cfg = SiteConfig.get()
        cfg.payments_enabled = not cfg.payments_enabled
        cfg.save(update_fields=['payments_enabled', 'updated_at'])
        if cfg.payments_enabled and not payments_configured():
            messages.warning(
                request,
                'Payments switched ON but Stripe keys are missing — lookups '
                'stay free until STRIPE_SECRET_KEY is set in the environment.'
            )
        else:
            messages.success(
                request,
                'Payments ON — lookups now require payment.' if cfg.payments_enabled
                else 'Payments OFF — lookups are free again.'
            )
        return redirect('admin_stats')

    now = timezone.now()
    # London-local midnight, NOT UTC midnight (paint17). This used to be
    # now.replace(hour=0, ...) on a UTC-aware datetime, which under BST starts
    # the dashboard's day at 01:00 London while the budget breaker's day (see
    # protection.london_day_start) starts at 00:00. Lookups in that hour counted
    # toward the breaker but were missing from the "today" card — two panels
    # side by side quietly disagreeing, every day from late March to late
    # October. Both now read the day the same way.
    today_start = london_day_start(now)
    week_ago = now - timedelta(days=7)
    month_ago = now - timedelta(days=30)

    # Top metrics + email + cost — collapsed into a single aggregate so the
    # dashboard does ONE round-trip to Postgres instead of ~10. Each metric is
    # a conditional Count over the same Search table, so they fit naturally
    # into one SELECT with FILTER clauses.
    # Requests refused by Turnstile are logged as Search rows (so the IP, UA and
    # device are available when investigating), but they are NOT lookups: nothing
    # was searched and nothing was spent. Excluding them here keeps every volume
    # and outcome metric measuring the pipeline rather than the front door —
    # otherwise a run of blocks would inflate daily totals and, because the rows
    # carry no make, pile into the funnel's "no vehicle identified" bucket as
    # though people had mistyped their registration. The blocks have their own
    # counter instead.
    lookups = Search.objects.exclude(error_message__contains='turnstile_blocked')

    top_metrics = lookups.aggregate(
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
    #
    # paint16: rows flagged no_code_available are a THIRD outcome — we searched
    # every source and the manufacturer has no published code. They are excluded
    # from BOTH sides: counting them as failures punishes us for a gap in the
    # manufacturer's data, counting them as successes claims a code we never
    # delivered. They are reported separately instead.
    no_code_available_count = lookups.filter(no_code_available=True).count()
    searched_to_completion = success_with_code + genuine_miss_count
    success_rate = (
        (success_with_code / searched_to_completion * 100)
        if searched_to_completion > 0 else 0
    )

    total_emails = top_metrics['with_email']
    emails_sent = top_metrics['emails_sent_count']
    conversion_rate = (total_emails / total_searches * 100) if total_searches > 0 else 0
    avg_duration_s = round((top_metrics['avg_duration_ms'] or 0) / 1000, 2)

    # --- Daily chart data: ONE pass over the 30-day window --------------
    # Volume, outcome split and resolution source were originally three separate
    # queries, each grouping the same rows by the same truncated date — three
    # scans and three timezone-aware date casts over identical data. They are
    # merged here into a single grouped query with conditional aggregates, so
    # the database walks the window once. At 50k rows the three-query version
    # was the slowest thing on the dashboard by a wide margin.
    #
    # The source counts carry `paint_code__gt=''` inside each filter rather than
    # on the queryset, which is what the separate source query used to do at the
    # queryset level — same rows, same numbers, one fewer scan.
    daily_rows = (
        lookups.filter(timestamp__gte=month_ago)
        .annotate(date=TruncDate('timestamp'))
        .values('date')
        .annotate(
            delivered=Count('id', filter=Q(paint_code__gt='')),
            no_code=Count('id', filter=Q(no_code_available=True)),
            failed=Count('id', filter=Q(
                paint_code='', no_code_available=False,
                recovery_attempted=True, make__gt='')),
            excluded=Count('id', filter=Q(paint_code='', no_code_available=False) &
                           (Q(make='') | Q(recovery_attempted=False))),
            s_vdg=Count('id', filter=Q(paint_code__gt='', provider=Search.PROVIDER_VDG)),
            s_retry=Count('id', filter=Q(paint_code__gt='', provider=Search.PROVIDER_VDG_RETRY)),
            s_pl24=Count('id', filter=Q(paint_code__gt='', provider=Search.PROVIDER_PARTSLINK24)),
            s_manual=Count('id', filter=Q(paint_code__gt='', provider=Search.PROVIDER_MANUAL)),
            s_cache=Count('id', filter=Q(paint_code__gt='', provider=Search.PROVIDER_CACHE)),
        )
        .order_by('date')
    )
    daily_map = {r['date']: r for r in daily_rows}

    chart_labels = []
    chart_delivered, chart_failed, chart_nocode, chart_excluded = [], [], [], []
    src_vdg, src_retry, src_pl24, src_manual, src_cache = [], [], [], [], []
    for i in range(30, -1, -1):
        d = (now - timedelta(days=i)).date()
        row = daily_map.get(d, {})
        chart_labels.append(d.strftime('%b %d'))
        chart_delivered.append(row.get('delivered', 0))
        chart_failed.append(row.get('failed', 0))
        chart_nocode.append(row.get('no_code', 0))
        chart_excluded.append(row.get('excluded', 0))
        src_vdg.append(row.get('s_vdg', 0))
        src_retry.append(row.get('s_retry', 0))
        src_pl24.append(row.get('s_pl24', 0))
        src_manual.append(row.get('s_manual', 0))
        src_cache.append(row.get('s_cache', 0))

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
        .order_by('-timestamp')[:50]
    )

    # (total_emails, emails_sent, conversion_rate computed in top_metrics above)

    # Device breakdown (mobile / tablet / desktop)
    # Stored at write time on Search.device, so this is one fast aggregate
    # instead of iterating every Search row in Python on every render.
    device_counts = {'mobile': 0, 'tablet': 0, 'desktop': 0, 'unknown': 0}
    for row in (
        lookups.exclude(device='')
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

    _cache_hits = VrmCache.objects.aggregate(h=Sum('hit_count'))['h'] or 0
    _avg_real_cost = float(real_cost_sum / real_cost_count) if real_cost_count else 0.0

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
        'chart_delivered': chart_delivered,
        'chart_failed': chart_failed,
        'chart_nocode': chart_nocode,
        'chart_excluded': chart_excluded,
        'src_vdg': src_vdg,
        'src_retry': src_retry,
        'src_pl24': src_pl24,
        'src_manual': src_manual,
        'src_cache': src_cache,
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
        # --- paint16 metrics ---
        'no_code_available_count': no_code_available_count,
        # Lookups refused by Turnstile. Expected to be near-zero and made up of
        # bots; a rise here alongside a fall in real volume is the signature of
        # the widget failing for genuine users.
        'turnstile_blocks_today': Search.objects.filter(
            timestamp__gte=today_start, error_message__contains='turnstile_blocked'
        ).count(),
        'turnstile_blocks_week': Search.objects.filter(
            timestamp__gte=week_ago, error_message__contains='turnstile_blocked'
        ).count(),
        # Payments that were authorised but never captured — the customer got
        # their code and we didn't get paid. Should always be zero; if it isn't,
        # those rows need chasing in Stripe.
        'capture_failures': Search.objects.filter(
            error_message__contains='stripe_capture_failed').count(),
        'maintenance_mode': SiteConfig.get().maintenance_mode,
        # --- Protection panel (A) ---
        'site_config': SiteConfig.get(),
        'spend_today': spend_today(),
        'vrm_cache_count': VrmCache.objects.count(),
        'vrm_cache_hits': _cache_hits,
        # What those hits saved: each was a repeat lookup answered from storage,
        # so it cost nothing at VDG. Valued at the average real cost of the
        # lookups we DID pay for, rather than a hardcoded tier price — that way
        # it stays accurate if the tier changes.
        'cache_saved': round(_cache_hits * _avg_real_cost, 2),
        'payments_enabled': SiteConfig.get().payments_enabled,
        'payments_configured': payments_configured(),
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
    # Optional attachment (paint16) — same handling as the manual-lookup reply:
    # validated by magic bytes, capped, never stored. The BCC on
    # send_custom_message means a copy (with the attachment) lands in
    # hello@coloureg.com, so there is a sent-folder record.
    attachment = process_image_upload(
        request.FILES.get('photo'), filename_prefix='attachment'
    )

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

    sent = send_custom_message(to_email, subject, body, extra_attachments=[attachment] if attachment else None)
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
    # Free-text note from the admin, shown in the email's "A note from us"
    # block. Now also PERSISTED (paint16) — previously it went into the email
    # and was discarded, leaving no record of what the customer was actually
    # told, including any caveat ("closest match for the year", "verify against
    # the label"). Stored so the row is a full account of the exchange.
    message = (request.POST.get('message') or '').strip()
    # Optional photo to send with the reply — e.g. a shot of the manufacturer
    # record or the paint label. Never stored: it rides on the email, and the
    # BCC copy in send_user_paint_code gives us our own record of it.
    reply_photo = process_image_upload(
        request.FILES.get('photo'), filename_prefix='paint-reference'
    )

    # THIRD OUTCOME (paint16): the admin can now submit with NO paint code to
    # record "we searched every source and this vehicle has no published code".
    # Previously the code field was mandatory, forcing 'N/A' into a column that
    # then counted as a successful lookup — inflating the success rate with
    # non-results. An empty code now means: send the no-code-available email,
    # flag the row as no_code_available, and leave success=False so it is
    # excluded from the success-rate denominator rather than counted either way.
    # A note is REQUIRED in that case, since the customer needs an explanation.
    no_code = not paint_code

    if not search_id:
        return JsonResponse({'success': False, 'error': 'Search ID is required.'}, status=400)
    if no_code and not message:
        return JsonResponse({
            'success': False,
            'error': 'Add a note explaining why no code is available — the customer sees it.',
        }, status=400)

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
    except (Search.DoesNotExist, ValueError, TypeError):
        # ValueError/TypeError because a non-numeric search_id raises on the
        # PK coercion, NOT DoesNotExist — so a malformed value used to 500
        # instead of returning this clean 404 (paint19). The same triple is
        # already caught in _wait_for_recovery_result; this just applies the
        # existing pattern consistently.
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

    if no_code:
        # Third outcome: tell them plainly that no code exists, with the
        # explanation the admin wrote. Different email entirely — showing a
        # blank code box would look broken.
        sent = send_user_no_code_available(
            to_email=search.email,
            registration=search.registration,
            vehicle_title=search.vehicle_title,
            colour=search.colour,
            message=message,
            extra_attachments=[reply_photo] if reply_photo else None,
        )
    else:
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
            extra_attachments=[reply_photo] if reply_photo else None,
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
        # success stays False for the no-code outcome: nothing was delivered, so
        # counting it as a success would overstate the hit rate. It is NOT a
        # failure either — no_code_available marks it as the third state, which
        # the stats exclude from the denominator entirely.
        success=not no_code,
        no_code_available=no_code,
        recovery_name_only=False,
        email_sent=True,
        manual_note=message,
    )

    # Cache a manually-found code (paint16b). The manual route is the most
    # expensive result we produce — it costs Roland's time, not an API call — so
    # not storing it meant doing the same hand-search again the next time anyone
    # asked for that registration. Caching turns that one-off effort into a
    # permanent asset: the next request for this reg is answered instantly from
    # storage with no VDG spend and no inbox round-trip. Only real codes are
    # cached; the no-code outcome is never stored, so a vehicle whose code is
    # later published still gets a fresh attempt.
    if not no_code and paint_code:
        store_vrm_payload(search.registration, {
            'registration': search.registration,
            'make': search.make,
            'model': search.model,
            'year': search.year,
            'colour': search.colour,
            'vin': search.vin,
            'vin_masked': mask_vin(search.vin),
            'vehicle_title': search.vehicle_title,
            'category': search.category,
            'paint_code': paint_code,
            'paint_description': paint_description_clean,
            'all_paint_codes': [],
            'make_logo': make_to_logo(search.make),
            'fuel_type': '',
            'transmission': '',
            'engine_description': '',
        })
        clear_miss(search.registration)

    if no_code:
        return JsonResponse({
            'success': True,
            'no_code': True,
            'message': f'Told {search.email} no code is available for {search.registration}.',
            'registration': search.registration,
            'email': search.email,
        })

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
    except (Search.DoesNotExist, ValueError, TypeError):
        # ValueError/TypeError because a non-numeric search_id raises on the
        # PK coercion, NOT DoesNotExist — so a malformed value used to 500
        # instead of returning this clean 404 (paint19). The same triple is
        # already caught in _wait_for_recovery_result; this just applies the
        # existing pattern consistently.
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
# NOTE (paint22): perform_lookup_core was removed here. It existed solely so the
# paid flow could run a lookup DURING fulfilment, and that flow no longer exists
# — the lookup happens first, free, in index(), and payment gates only the
# reveal. What it left behind was a second copy of index()'s resolution logic
# that nothing called, which is the kind of thing that quietly drifts out of
# step with the original and then gets resurrected by someone who assumes it
# still works.


@require_POST
def start_paid_lookup(request):
    """Send a customer to Checkout to UNLOCK a result we have already found.

    Reversed in paint22. This used to take payment first and look up second, so
    a quarter of paying customers were told afterwards that nothing was found.
    The lookup now happens on the free path in index(); by the time anyone
    reaches here the answer exists, is complete (code AND name), and is sitting
    on the Search row untouched by the browser. Payment gates the reveal only.

    Consequences worth stating:
      - No VDG call happens here or in fulfilment, so there is nothing to refund
        and nothing to reverse. Capture is unconditional on payment success.
      - The registration is NOT taken from the POST body. It comes from the
        session's search_id, so a caller cannot pay for one reg and unlock
        another, and cannot mint a charge for a lookup that never ran.
      - A cache hit costs us nothing in VDG and is still chargeable. The
        customer is buying the answer, not the act of querying a provider.
    """
    config = SiteConfig.get()
    if not payments_active(config):
        return redirect('index')

    client_ip = get_client_ip(request)

    # The ONLY source of what is being bought. Anything from the request body
    # would let a caller nominate a different row.
    vehicle_data = request.session.get('vehicle_data') or {}
    search_id = vehicle_data.get('search_id')
    if not search_id:
        messages.error(request, 'Please look up a vehicle first.')
        return redirect('index')

    try:
        search = Search.objects.get(id=search_id)
    except (Search.DoesNotExist, ValueError, TypeError):
        messages.error(request, 'Please look up a vehicle first.')
        return redirect('index')

    registration = search.registration

    if (config.is_ip_blocked(client_ip)
            or (registration and config.is_reg_blocked(registration))):
        messages.error(request, 'Sorry, we could not process that request.')
        return redirect('index')

    # Already bought — send them to the answer rather than charging twice.
    if search.paid_unlocked:
        return redirect('results')

    # Nothing to sell. Either the lookup found no complete result, or payments
    # were off when it ran. Either way this must not become a charge.
    if not search.is_locked():
        return redirect('results')

    # NOTE: no budget-breaker check and no VDG spend guard here, unlike the old
    # flow. Neither applies any more — this path makes no provider call. The
    # breaker still guards index(), which is where spending now happens.

    if not verify_turnstile(request.POST.get('cf-turnstile-response', ''), client_ip):
        _record_turnstile_block(request, client_ip)
        messages.error(request, 'We could not verify your browser. Please reload and try again.')
        return redirect('index')

    success_url = request.build_absolute_uri('/paid/success/') + '?session_id={CHECKOUT_SESSION_ID}'
    cancel_url = request.build_absolute_uri('/results/')
    session = create_checkout_session(
        registration, success_url, cancel_url, client_ip,
        user_agent=request.META.get('HTTP_USER_AGENT', ''),
        search_id=search.id,
    )
    if session is None or not getattr(session, 'url', None):
        messages.error(request, 'Payment is temporarily unavailable. Please try again later.')
        return redirect('results')
    return redirect(session.url)


def _sget(obj, key, default=None):
    """Read a key from a Stripe response object OR a plain dict.

    stripe-python 15.x StripeObject is NOT a dict subclass and has no .get():
    it supports obj['key'] and obj.key only. Calling .get() on one raises
    AttributeError, and every object in this flow — Session, its nested
    metadata, the expanded PaymentIntent, and the webhook Event — is one.

    That mattered: the paid flow was written entirely against .get() and tested
    against plain dict fixtures, so it passed the battery while failing on the
    first real Stripe response. Routing every read through here keeps both
    shapes working, so the dict-based tests stay valid AND production works.

    Missing keys and explicit nulls both return `default`, matching dict.get()
    semantics closely enough that call sites read the same either way.
    """
    if obj is None:
        return default
    try:
        value = obj[key]
    except (KeyError, IndexError, TypeError, AttributeError):
        return default
    return default if value is None else value


def _append_search_note(search, note):
    """Append a marker to Search.error_message without destroying what's there.

    error_message doubles as the paid-flow idempotency key (it carries
    `stripe_session=<id>`), so this MUST append rather than overwrite — clobbering
    it would let the webhook and the success redirect both fulfil the same
    session again.
    """
    if search is None:
        return
    try:
        existing = search.error_message or ''
        if note in existing:
            return
        search.error_message = (existing + ' | ' + note).strip(' |')[:500]
        search.save(update_fields=['error_message'])
    except Exception:
        logger.exception('Could not append note %r to search %s', note, getattr(search, 'id', None))


def _record_capture_outcome(search, captured, session_id):
    """Record whether the money was actually taken.

    capture() returns a bool, and it was previously discarded. That meant a
    declined or errored capture was invisible: the customer received their paint
    code, the row said success, and no payment had been taken — a silent revenue
    leak with nothing to reconcile against.

    We still DELIVER the code when capture fails. The lookup has already been
    performed and paid for at VDG, and withholding the result would leave the
    customer with neither a charge nor an answer, which is a worse outcome for
    them and for us. Instead the failure is stamped on the row so it can be
    chased, and logged so it surfaces in Sentry.
    """
    if captured:
        return
    logger.error(
        'Stripe capture FAILED but a paint code was delivered '
        '(session=%s, search=%s) — payment not taken',
        session_id, getattr(search, 'id', None),
    )
    _append_search_note(search, 'stripe_capture_failed')


def _fulfil_paid_session(session):
    """Release a result the customer has paid to see (paint22).

    This used to RUN the lookup after payment. It no longer does anything of the
    sort: the answer already exists on the row, so fulfilment is unlocking it.
    That removes the failure mode the old design was built around — there is no
    lookup left to fail, so nothing to cancel and nothing to reverse.

    Returns the Search row on success, or None if the session must not be
    honoured.
    """
    if session is None:
        return None

    meta = _sget(session, 'metadata') or {}
    reg = _sget(meta, 'registration')
    raw_search_id = _sget(meta, 'search_id')
    pi = _sget(session, 'payment_intent')
    payment_intent_id = pi if isinstance(pi, str) else _sget(pi, 'id')
    if not reg or not payment_intent_id or not raw_search_id:
        return None

    # --- The customer must actually have paid (paint17, retained) ----------
    # Stripe creates the PaymentIntent when the SESSION is created, not when the
    # card is authorised, so an ABANDONED session still carries a reg and a PI
    # id. Without this someone could start a payment, copy the cs_... id out of
    # the Checkout URL, close the tab, and unlock the result for nothing.
    #
    # NOT payment_status == 'paid': under capture_method='manual' a correctly
    # authorised session reports 'unpaid' right up until we capture it.
    if _sget(session, 'status') != 'complete':
        return None
    pi_status = None if isinstance(pi, str) else _sget(pi, 'status')
    if pi_status is not None and pi_status not in ('requires_capture', 'succeeded'):
        return None

    try:
        search = Search.objects.get(id=raw_search_id)
    except (Search.DoesNotExist, ValueError, TypeError):
        return None

    # The row must match the session — belt and braces against a mangled or
    # replayed metadata payload naming somebody else's lookup.
    if search.registration != reg:
        return None

    session_id = _sget(session, 'id')

    # Idempotent: /paid/success/ and the webhook both arrive here, often at once.
    if search.paid_unlocked:
        return search

    # One winner only. cache.add is atomic — the loser hits the cache table's
    # primary key and gets False — so a simultaneous success-page hit and
    # webhook cannot both capture the same payment.
    lock_key = f'fulfil-lock:{session_id}'
    if not caches['default'].add(lock_key, 1, 300):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            time.sleep(0.5)
            search.refresh_from_db()
            if search.paid_unlocked:
                return search
        return None

    try:
        # Unlock FIRST, capture second. If the capture then fails we have given
        # away one result — recoverable, and we know exactly which row to chase.
        # The reverse order risks taking money and failing to deliver, which is
        # the outcome this whole redesign exists to remove.
        search.paid_unlocked = True
        search.save(update_fields=['paid_unlocked'])

        captured = capture(payment_intent_id)
        _record_capture_outcome(search, captured, session_id)

        # A paid lookup should not count against the free allowance (paint22).
        # Credited on CAPTURE, not on unlock: this rewards money actually taken,
        # not merely authorised.
        #
        # Keyed to the IP that did the SEARCH, not the one paying. Those can
        # legitimately differ — search on mobile data, pay on wifi — and the
        # searcher's is the one that will run the next lookup. It is already on
        # the row. If it is NULL (paint19 stores unvalidated IPs that way) the
        # credit is skipped and the payment stands regardless.
        if captured:
            credit_sliding_allowance('lookup', search.ip_address)
        return search
    finally:
        caches['default'].delete(lock_key)


def paid_success(request):
    """Stripe success redirect: unlock the result and show it (paint22).

    The session already holds this lookup's vehicle_data — the customer has been
    looking at the locked version of this very page. So there is nothing to
    rebuild here: unlock the row and send them back, where results() now renders
    the full answer because is_locked() has become False.
    """
    if not payments_active():
        return redirect('index')
    session_id = request.GET.get('session_id')
    if not session_id:
        return redirect('index')
    search = _fulfil_paid_session(get_session(session_id))
    if search is None:
        messages.error(
            request,
            'We could not confirm that payment. If you have been charged, '
            'please get in touch and we will sort it out straight away.'
        )
        return redirect('index')

    # Rebuild the session pointer if it was lost (different tab, or the webhook
    # fulfilled first). Without this the customer pays and lands on "no vehicle
    # data found", which would be the worst possible moment for that message.
    vehicle_data = request.session.get('vehicle_data') or {}
    if vehicle_data.get('search_id') != search.id:
        cached = get_cached_vrm_payload(search.registration, count_hit=False)
        if cached:
            cached['search_id'] = search.id
            request.session['vehicle_data'] = cached
        else:
            request.session['vehicle_data'] = {
                'registration': search.registration,
                'make': search.make,
                'search_id': search.id,
                'paint_code': search.paint_code,
                'paint_description': search.paint_description,
                'all_paint_codes': [],
            }
    return redirect('results')


def paid_cancel(request):
    """Customer abandoned Checkout — nothing charged, back to the homepage."""
    return redirect('index')


@csrf_exempt
@require_POST
def stripe_webhook(request):
    """Backstop fulfilment for closed-tab-after-payment.

    Verified by signature (the only thing securing this endpoint — register it
    against the Railway hostname so Cloudflare bot protection can't challenge
    Stripe's POSTs). Handles checkout.session.completed by fulfilling the same
    idempotent path as the success redirect. Always 200 on a verified event so
    Stripe doesn't retry a handled one; 400 on a bad signature.
    """
    event = construct_webhook_event(request.body, request.META.get('HTTP_STRIPE_SIGNATURE', ''))
    if event is None:
        return HttpResponse(status=400)
    if _sget(event, 'type') == 'checkout.session.completed':
        session = _sget(_sget(event, 'data'), 'object')
        # Re-retrieve expanded so payment_intent carries a status we can check.
        full = get_session(_sget(session, 'id'))
        try:
            _fulfil_paid_session(full or session)
        except Exception:
            logger.exception('Webhook fulfilment failed')
            # Still 200: we don't want infinite Stripe retries on our bug; the
            # success redirect path is the primary and this is the backstop.
    return HttpResponse(status=200)
