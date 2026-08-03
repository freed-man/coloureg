"""Stripe payment integration for the paid-lookup flow (F, paint15).

The economic problem this solves: a lookup costs money to fulfil (a VDG call),
and ~27% of lookups return no paint code. So the flow must charge the customer
only when a result is actually delivered. Stripe's manual-capture (auth-then-
capture) does exactly this:

    1. AUTHORISE the fee (place a hold) when the customer pays at Checkout.
    2. Run the lookup.
    3. CAPTURE the fee if a paint code was found.
    4. CANCEL the authorisation (free) if not — the customer is never charged.

The amount is settings.LOOKUP_PRICE_PENCE (currently 200 = £2.00), not a
literal anywhere in this module — earlier revisions of this docstring said £1
throughout, which is the sort of drift that gets believed on the money path.

Two independent gates keep this dormant until deliberately switched on:
  * env keys (STRIPE_SECRET_KEY etc.) must be present, AND
  * SiteConfig.payments_enabled must be True (defaults False).
With either missing, `payments_active()` is False and the site serves free
lookups exactly as before. This lets the whole module ship and be tested
against Stripe TEST keys with zero effect on production behaviour.

Fraud posture (built in, see the flow in views): Checkout is Stripe-hosted so
Stripe owns card-testing detection and Radar; wallets (Apple/Google Pay) are
enabled for tokenised, biometric payment; and Turnstile guards the page before
a PaymentIntent is ever created. The auth is cancelled immediately (an
authorisation reversal, not an expiry) to stay clear of card-network limits on
uncaptured low-value auths.
"""

import logging

from django.conf import settings

logger = logging.getLogger(__name__)


def _stripe():
    """Return the configured stripe module, or None if keys are absent.

    Imported lazily so the dependency is only touched when payments are used —
    the rest of the site never imports stripe at startup.
    """
    if not settings.STRIPE_SECRET_KEY:
        return None
    import stripe
    stripe.api_key = settings.STRIPE_SECRET_KEY
    return stripe


def payments_configured():
    """True when the Stripe secret key is present (env side of the gate)."""
    return bool(settings.STRIPE_SECRET_KEY)


def payments_active(config=None):
    """True only when BOTH gates are open: env keys present AND the
    SiteConfig.payments_enabled switch is on.

    `config` may be passed in to avoid a second SiteConfig fetch; otherwise it's
    loaded here.
    """
    if not payments_configured():
        return False
    if config is None:
        from lookup.models import SiteConfig
        config = SiteConfig.get()
    return bool(config.payments_enabled)


def create_checkout_session(registration, success_url, cancel_url, client_ip=None):
    """Create a manual-capture Checkout Session for one lookup.

    Returns the Stripe Checkout Session (caller redirects to session.url), or
    None if Stripe isn't configured. The fee is only AUTHORISED here (capture
    happens later, on a paint hit) via payment_intent_data.capture_method=manual.
    The registration is stashed in metadata so the success handler and the
    webhook can both recover which lookup this payment is for.
    """
    stripe = _stripe()
    if stripe is None:
        return None

    price_pence = settings.LOOKUP_PRICE_PENCE
    session = stripe.checkout.Session.create(
        mode='payment',
        # Wallets appear automatically when enabled on the account; 'card'
        # covers the rest. Stripe-hosted page => Stripe owns fraud/SCA.
        payment_method_types=['card'],
        line_items=[{
            'price_data': {
                'currency': 'gbp',
                'product_data': {
                    'name': f'Paint code lookup — {registration}',
                    'description': 'Manufacturer paint colour code for your vehicle.',
                },
                'unit_amount': price_pence,
            },
            'quantity': 1,
        }],
        payment_intent_data={
            # THE key setting: authorise now, capture later (or cancel).
            'capture_method': 'manual',
            'metadata': {'registration': registration},
            'description': f'Paint code lookup — {registration}',
        },
        # client_ip / user_agent ride in metadata rather than being read off the
        # fulfilling request (paint18). Fulfilment happens either on the
        # customer's return to /paid/success/ OR on the Stripe webhook — and on
        # the webhook route the request is STRIPE, so reading it there would
        # record Stripe's IP as the customer's. Metadata is captured here, at
        # the one moment we are definitely talking to the customer.
        #
        # Stripe caps metadata values at 500 characters and user agents can
        # exceed that, so truncate. The IP is stored raw and validated on the
        # way back out (views._valid_ip) — it lands in an `inet` column, which
        # rejects anything that is not an address.
        metadata={
            'registration': registration,
            'client_ip': (client_ip or '')[:64],
            'user_agent': (user_agent or '')[:400],
        },
        # --- CCR 2013 reg 37 consent (paint15) ---------------------------------
        # Digital content sold at a distance carries a 14-day cancellation right.
        # A trader must NOT begin supply inside that window unless the consumer
        # (a) gives express consent to immediate performance and (b) acknowledges
        # that doing so loses the right to cancel. Without BOTH, reg 37(4) says
        # the consumer bears no cost — i.e. they could take the paint code and
        # reclaim the fee for 14 days (or up to 12 months if the pre-contract
        # information was never given). Since this service delivers instantly,
        # that would be every single customer.
        #
        # The ticked terms box is the express consent (an active tick — pre-ticked
        # boxes are expressly disallowed); the submit message carries the
        # acknowledgement, shown before the pay button is pressed, i.e. before
        # supply begins. Requires a Terms of Service URL set in the Stripe
        # dashboard (Settings -> Public details) or Checkout will reject this.
        consent_collection={'terms_of_service': 'required'},
        custom_text={
            'terms_of_service_acceptance': {
                'message': (
                    'I ask coloureg to run my lookup immediately and I accept '
                    'that I lose my 14-day right to cancel once the paint code '
                    'is delivered. If no paint code is found, I am not charged.'
                ),
            },
            'submit': {
                'message': (
                    'Your card is only charged if we find your paint code. '
                    'If we find nothing, the payment is cancelled automatically.'
                ),
            },
        },
        success_url=success_url,
        cancel_url=cancel_url,
        # NOTE (paint17): `expires_at=None` used to be passed here, under a
        # comment about not leaving holds dangling. Removed, for two reasons.
        #
        # First it did nothing: stripe-python's encoder drops None parameters
        # outright (_encode.py, `if value is None: continue`), so it was never
        # sent and Checkout's 24-hour default applied regardless.
        #
        # Second — and this is the part worth remembering — the premise was
        # wrong. expires_at governs how long the SESSION stays usable: status
        # goes open -> expired if the customer never pays. It has nothing to do
        # with the authorisation hold, because a hold only exists once payment
        # succeeds, at which point status is `complete` and expiry no longer
        # applies. Hold duration is the card network's (~7 days) and our
        # protection against dangling ones is calling cancel() immediately on a
        # miss, which _fulfil_paid_session does.
        #
        # So shortening it would buy nothing and cost something real: a customer
        # who opens Checkout on a phone, gets interrupted, and comes back 35
        # minutes later would find a dead page. Left at the default.
    )
    return session


def get_session(session_id):
    """Retrieve a Checkout Session (expanded to its PaymentIntent), or None."""
    stripe = _stripe()
    if stripe is None:
        return None
    try:
        return stripe.checkout.Session.retrieve(
            session_id, expand=['payment_intent']
        )
    except Exception as e:
        logger.warning('Stripe get_session failed: %s', e)
        return None


def capture(payment_intent_id):
    """Capture a previously-authorised PaymentIntent (charge the customer).

    Called when a paint code WAS delivered. Idempotent-ish: capturing an
    already-captured intent raises, which we swallow to True since the desired
    state (captured) already holds. Returns True on success.
    """
    stripe = _stripe()
    if stripe is None:
        return False
    try:
        stripe.PaymentIntent.capture(payment_intent_id)
        return True
    except Exception as e:
        # Already captured is a success from our POV; anything else is logged.
        msg = str(e).lower()
        if 'already been captured' in msg or 'already captured' in msg:
            return True
        logger.warning('Stripe capture failed for %s: %s', payment_intent_id, e)
        return False


def cancel(payment_intent_id):
    """Cancel (reverse) an authorisation — the customer is NOT charged.

    Called when no paint code was found. This is an immediate authorisation
    reversal, not letting the hold expire, which keeps us clear of card-network
    limits on uncaptured low-value auths. Cancelling an already-cancelled or
    captured intent raises; we log and move on. Returns True on success.
    """
    stripe = _stripe()
    if stripe is None:
        return False
    try:
        stripe.PaymentIntent.cancel(payment_intent_id)
        return True
    except Exception as e:
        logger.warning('Stripe cancel failed for %s: %s', payment_intent_id, e)
        return False


def construct_webhook_event(payload, sig_header):
    """Verify a webhook payload against STRIPE_WEBHOOK_SECRET and return the
    Event, or None if verification fails (bad signature / not configured).

    The signature check is the ONLY thing securing the webhook endpoint — which
    is why the endpoint must be reachable by Stripe without Cloudflare bot
    protection challenging it (register it against the Railway hostname). A
    failed verification returns None and the view responds 400.
    """
    stripe = _stripe()
    if stripe is None or not settings.STRIPE_WEBHOOK_SECRET:
        return None
    try:
        return stripe.Webhook.construct_event(
            payload, sig_header, settings.STRIPE_WEBHOOK_SECRET
        )
    except Exception as e:
        logger.warning('Stripe webhook verification failed: %s', e)
        return None
