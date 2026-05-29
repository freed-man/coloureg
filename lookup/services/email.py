"""Email sending via Resend."""
import base64
import logging
import os
import resend
from django.conf import settings


logger = logging.getLogger(__name__)


def _client():
    """Initialize Resend client with API key."""
    resend.api_key = settings.RESEND_API_KEY
    return resend


def _load_logo():
    """Load the coloureg logo as base64 for inline email attachment."""
    logo_path = os.path.join(settings.BASE_DIR, 'static', 'images', 'logo.png')
    try:
        with open(logo_path, 'rb') as f:
            return base64.b64encode(f.read()).decode('utf-8')
    except FileNotFoundError:
        # Don't crash if the logo's missing — emails fall back to a text header
        # — but make it loud so a future move/rename doesn't silently strip the
        # branding from every email going out.
        logger.warning('Email logo not found at %s; using text fallback', logo_path)
        return None


LOGO_BASE64 = _load_logo()


def _brand_header():
    """Header with logo as inline attachment."""
    if LOGO_BASE64:
        return """
        <div style="background: #fff; padding: 28px 0; text-align: center; border-bottom: 1px solid #eee;">
            <img src="cid:logo" alt="coloureg" style="height: 64px; width: auto; display: inline-block;" />
        </div>
        """
    return """
    <div style="background: #fff; padding: 28px 0; text-align: center; border-bottom: 1px solid #eee;">
        <span style="font-size: 28px; font-weight: 500; letter-spacing: 3px; color: #666;">coloureg</span>
    </div>
    """


FONT_IMPORT = """
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600;700&display=swap');
</style>
"""


FOOTER = """
<div style="max-width: 560px; margin: 24px auto 0; text-align: center; color: #999; font-size: 12px;">
    Sent by <a href="https://coloureg.com" style="color: #0066cc; text-decoration: none;">coloureg.com</a>
</div>
"""


def _attachments():
    if LOGO_BASE64:
        return [{
            "filename": "logo.png",
            "content": LOGO_BASE64,
            "content_id": "logo",
            "content_type": "image/png",
        }]
    return []


def _safe_send(payload, context=''):
    """Wrap Resend's send call so a transport failure returns False (instead of
    raising) and is logged for visibility.

    Resend's send() can raise on auth errors, rate limits, network issues, or
    invalid payloads. Without this wrapper, every send_*() function would bury
    the actual exception in `except Exception: return False`, meaning failures
    silently disappear from Sentry and admin diagnostics. Logging here means
    the actual error is captured even though we still return False.

    `context` is a short label (e.g. 'paint_code', 'admin_failure') so the
    log line is greppable.
    """
    client = _client()
    try:
        client.Emails.send(payload)
        return True
    except Exception as exc:
        logger.warning('Resend send failed (%s): %s', context, exc)
        return False


def _brand_wrapper(body_html):
    """Wrap arbitrary body HTML in coloureg's standard branded shell.

    Used by send_custom_message for one-off admin compose emails. Existing
    send_*() functions still inline their own brand markup (deliberately
    untouched to avoid regressing working emails).

    `body_html` is dropped into the white content card with 32px padding,
    matching the visual style of the transactional emails.
    """
    return f"""
    {FONT_IMPORT}
    <div style="background: #f8f9fa; padding: 40px 20px; font-family: 'IBM Plex Sans', Arial, Helvetica, sans-serif;">
        <div style="max-width: 560px; margin: 0 auto; background: #fff; border-radius: 8px; overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,0.04);">
            {_brand_header()}
            <div style="padding: 32px; color: #1a1a1a; font-size: 15px; line-height: 1.6;">
                {body_html}
            </div>
        </div>
        {FOOTER}
    </div>
    """


def send_custom_message(to_email, subject, markdown_body):
    """Send a custom one-off email composed via the admin compose form.

    `markdown_body` is converted to HTML using the standard markdown library
    (no sanitisation — this endpoint is staff-only, so the input is trusted).
    The result is wrapped in coloureg's brand shell so the email looks like
    every other coloureg email.

    BCC: a copy goes to settings.DEFAULT_FROM_EMAIL (hello@coloureg.com) so
    you have a sent-folder record via your existing Dynadot forward to Gmail.
    """
    # Local import so the rest of email.py doesn't pull in markdown unless
    # this function is actually used.
    import markdown as _md

    body_html = _md.markdown(
        markdown_body or '',
        extensions=['nl2br', 'extra'],
    )
    html = _brand_wrapper(body_html)

    return _safe_send({
        "from": settings.DEFAULT_FROM_EMAIL,
        "to": [to_email],
        "bcc": [settings.DEFAULT_FROM_EMAIL],
        "subject": subject,
        "html": html,
        "attachments": _attachments(),
    }, context='custom_message')


def send_user_paint_code(to_email, registration, vehicle_title, vin_masked, colour, paint_code, paint_description, canonical_code=None, paint_hex=None):
    """Email user the found paint code.

    If canonical_code is provided and differs from paint_code, the email displays
    the VDG-returned code prominently with an 'also: <canonical>' subline below,
    matching the results page UI. The subject line shows both codes
    (e.g. 'L8 / LZ9Y') so the inbox preview is unambiguous.

    If paint_hex is provided (a validated 6-digit hex colour), a swatch bar is
    rendered above the paint code box, matching the website's results page.
    """

    # Optional swatch bar — mirrors the results page UI
    if paint_hex:
        swatch_html = (
            f'<div style="height: 80px; background: {paint_hex}; '
            f'border-radius: 8px 8px 0 0;"></div>'
        )
        # Slightly adjust the box below so the rounded corners only show on bottom
        box_radius = '0 0 8px 8px'
    else:
        swatch_html = ''
        box_radius = '8px'

    # Build the optional 'also: LZ9Y' line that mirrors the results page
    if canonical_code and canonical_code.upper() != (paint_code or '').upper():
        canonical_html = (
            f'<div style="margin-top: 8px; color: #999; font-size: 13px; '
            f'letter-spacing: 0.3px; font-style: italic;">'
            f'also: {canonical_code}'
            f'</div>'
        )
    else:
        canonical_html = ''

    html = f"""
    {FONT_IMPORT}
    <div style="background: #f8f9fa; padding: 40px 20px; font-family: 'IBM Plex Sans', Arial, Helvetica, sans-serif;">
        <div style="max-width: 560px; margin: 0 auto; background: #fff; border-radius: 8px; overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,0.04);">
            {_brand_header()}
            <div style="padding: 40px 32px;">
                <h1 style="margin: 0 0 8px; font-size: 22px; color: #1a1a1a; font-weight: 600;">Your paint code</h1>
                <p style="margin: 0 0 24px; color: #666; font-size: 15px; line-height: 1.5;">
                    Here's the paint code for your vehicle.
                </p>
                {swatch_html}
                <div style="background: #f8f9fa; padding: 32px; border-radius: {box_radius}; text-align: center; margin-bottom: 24px;">
                    <div style="font-family: 'IBM Plex Mono', 'Courier New', Courier, monospace; font-size: 42px; font-weight: 700; letter-spacing: 3px; color: #1a1a1a; font-feature-settings: 'zero' 0;">
                        {paint_code}
                    </div>
                    {canonical_html}
                    {f'<div style="margin-top: 12px; color: #666; font-size: 14px; letter-spacing: 0.5px;">{paint_description}</div>' if paint_description else ''}
                </div>
                <table style="width: 100%; border-collapse: collapse;">
                    <tr>
                        <td style="padding: 12px 0; color: #666; font-size: 14px; width: 120px;">Vehicle</td>
                        <td style="padding: 12px 0; color: #1a1a1a; font-size: 14px;">{vehicle_title or '—'}</td>
                    </tr>
                    <tr style="border-top: 1px solid #eee;">
                        <td style="padding: 12px 0; color: #666; font-size: 14px;">Registration</td>
                        <td style="padding: 12px 0; color: #1a1a1a; font-size: 14px;">{registration}</td>
                    </tr>
                    <tr style="border-top: 1px solid #eee;">
                        <td style="padding: 12px 0; color: #666; font-size: 14px;">VIN</td>
                        <td style="padding: 12px 0; color: #1a1a1a; font-size: 14px;">{vin_masked or '—'}</td>
                    </tr>
                    <tr style="border-top: 1px solid #eee;">
                        <td style="padding: 12px 0; color: #666; font-size: 14px;">Colour</td>
                        <td style="padding: 12px 0; color: #1a1a1a; font-size: 14px;">{colour or '—'}</td>
                    </tr>
                </table>
            </div>
        </div>
        {FOOTER}
    </div>
    """

    # Subject: when both VDG's code and a canonical expansion exist, show both
    # so the inbox preview is unambiguous (e.g. 'L8 / LZ9Y'). Otherwise just
    # show the VDG code.
    if canonical_code and canonical_code.upper() != (paint_code or '').upper():
        subject_code = f"{paint_code} / {canonical_code}"
    else:
        subject_code = paint_code

    return _safe_send({
        "from": settings.DEFAULT_FROM_EMAIL,
        "to": to_email,
        "subject": f"Paint code for {registration}: {subject_code}",
        "html": html,
        "attachments": _attachments(),
    }, context='paint_code')


def send_admin_failure_notification(registration, vehicle_title, vin_full, colour, user_email):
    """Email admin when paint code wasn't found and user requested manual lookup."""

    html = f"""
    {FONT_IMPORT}
    <div style="background: #f8f9fa; padding: 40px 20px; font-family: 'IBM Plex Sans', Arial, Helvetica, sans-serif;">
        <div style="max-width: 560px; margin: 0 auto; background: #fff; border-radius: 8px; overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,0.04);">
            {_brand_header()}
            <div style="background: #C8102E; padding: 16px; text-align: center;">
                <span style="color: #fff; font-size: 14px; font-weight: 600; letter-spacing: 1px; text-transform: uppercase;">Manual lookup required</span>
            </div>
            <div style="padding: 32px;">
                <table style="width: 100%; border-collapse: collapse; margin-bottom: 24px;">
                    <tr style="border-bottom: 1px solid #eee;">
                        <td style="padding: 10px 16px 10px 0; color: #666; font-size: 13px; width: 100px;">Vehicle</td>
                        <td style="padding: 10px 0; color: #1a1a1a; font-size: 14px;">{vehicle_title or '—'}</td>
                    </tr>
                    <tr style="border-bottom: 1px solid #eee;">
                        <td style="padding: 10px 16px 10px 0; color: #666; font-size: 13px;">Registration</td>
                        <td style="padding: 10px 0; color: #1a1a1a; font-size: 14px;">{registration}</td>
                    </tr>
                    <tr style="border-bottom: 1px solid #eee;">
                        <td style="padding: 10px 16px 10px 0; color: #666; font-size: 13px;">VIN</td>
                        <td style="padding: 10px 0; color: #1a1a1a; font-size: 14px; font-family: 'IBM Plex Sans', Arial, Helvetica, sans-serif;">{vin_full or '—'}</td>
                    </tr>
                    <tr style="border-bottom: 1px solid #eee;">
                        <td style="padding: 10px 16px 10px 0; color: #666; font-size: 13px;">Colour</td>
                        <td style="padding: 10px 0; color: #1a1a1a; font-size: 14px;">{colour or '—'}</td>
                    </tr>
                    <tr>
                        <td style="padding: 10px 16px 10px 0; color: #666; font-size: 13px;">User</td>
                        <td style="padding: 10px 0;"><a href="mailto:{user_email}" style="color: #003399; font-size: 14px;">{user_email}</a></td>
                    </tr>
                </table>

                <p style="margin: 0; color: #999; font-size: 12px;">
                    Reply to this email to respond directly to the user.
                </p>
            </div>
        </div>
        {FOOTER}
    </div>
    """

    return _safe_send({
        "from": settings.DEFAULT_FROM_EMAIL,
        "to": settings.ADMIN_EMAIL,
        "reply_to": user_email,
        "subject": f"Pending Request - {registration}",
        "html": html,
        "attachments": _attachments(),
    }, context='admin_failure')


def send_user_pending_notification(to_email, registration, vehicle_title, vin_masked, colour):
    """Email user confirming we'll do manual lookup."""

    html = f"""
    {FONT_IMPORT}
    <div style="background: #f8f9fa; padding: 40px 20px; font-family: 'IBM Plex Sans', Arial, Helvetica, sans-serif;">
        <div style="max-width: 560px; margin: 0 auto; background: #fff; border-radius: 8px; overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,0.04);">
            {_brand_header()}
            <div style="padding: 40px 32px;">
                <h1 style="margin: 0 0 12px; font-size: 22px; color: #1a1a1a; font-weight: 600;">Thanks — we're on it</h1>
                <p style="margin: 0 0 24px; color: #4a4a4a; font-size: 15px; line-height: 1.6;">
                    The manufacturer's servers didn't respond in time. We'll retrieve the paint code directly from the manufacturer database and email it to you within 12 hours.
                </p>
                <table style="width: 100%; border-collapse: collapse; margin-bottom: 24px;">
                    <tr style="border-bottom: 1px solid #eee;">
                        <td style="padding: 10px 16px 10px 0; color: #666; font-size: 13px; width: 100px;">Vehicle</td>
                        <td style="padding: 10px 0; color: #1a1a1a; font-size: 14px;">{vehicle_title or '—'}</td>
                    </tr>
                    <tr style="border-bottom: 1px solid #eee;">
                        <td style="padding: 10px 16px 10px 0; color: #666; font-size: 13px;">Registration</td>
                        <td style="padding: 10px 0; color: #1a1a1a; font-size: 14px;">{registration}</td>
                    </tr>
                    <tr style="border-bottom: 1px solid #eee;">
                        <td style="padding: 10px 16px 10px 0; color: #666; font-size: 13px;">VIN</td>
                        <td style="padding: 10px 0; color: #1a1a1a; font-size: 14px;">{vin_masked or '—'}</td>
                    </tr>
                    <tr>
                        <td style="padding: 10px 16px 10px 0; color: #666; font-size: 13px;">Colour</td>
                        <td style="padding: 10px 0; color: #1a1a1a; font-size: 14px;">{colour or '—'}</td>
                    </tr>
                </table>
                <div style="background: #f0f4ff; border-left: 3px solid #003399; padding: 16px 20px; border-radius: 4px;">
                    <p style="margin: 0; color: #003399; font-size: 13px;">
                        Any questions? Just reply to this email.
                    </p>
                </div>
            </div>
        </div>
        {FOOTER}
    </div>
    """

    return _safe_send({
        "from": settings.DEFAULT_FROM_EMAIL,
        "to": to_email,
        "subject": f"We've received your paint code request for {registration}",
        "html": html,
        "attachments": _attachments(),
    }, context='user_pending')


def send_admin_contact_message(contact_type, user_email, message):
    """Send contact form message to admin."""
    type_label = contact_type.title()

    html = f"""
    {FONT_IMPORT}
    <div style="background: #f8f9fa; padding: 40px 20px; font-family: 'IBM Plex Sans', Arial, Helvetica, sans-serif;">
        <div style="max-width: 560px; margin: 0 auto; background: #fff; border-radius: 8px; overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,0.04);">
            {_brand_header()}
            <div style="background: #003399; padding: 16px; text-align: center;">
                <span style="color: #fff; font-size: 14px; font-weight: 600; letter-spacing: 1px; text-transform: uppercase;">New {type_label} Message</span>
            </div>
            <div style="padding: 32px;">
                <table style="width: 100%; border-collapse: collapse; margin-bottom: 24px;">
                    <tr style="border-bottom: 1px solid #eee;">
                        <td style="padding: 10px 16px 10px 0; color: #666; font-size: 13px; width: 80px;">Type</td>
                        <td style="padding: 10px 0; color: #1a1a1a; font-size: 14px;">{type_label}</td>
                    </tr>
                    <tr style="border-bottom: 1px solid #eee;">
                        <td style="padding: 10px 16px 10px 0; color: #666; font-size: 13px;">From</td>
                        <td style="padding: 10px 0;"><a href="mailto:{user_email}" style="color: #003399; font-size: 14px;">{user_email}</a></td>
                    </tr>
                </table>

                <div style="background: #f8f9fa; padding: 20px; border-radius: 8px; margin-bottom: 16px;">
                    <p style="margin: 0; color: #1a1a1a; font-size: 14px; line-height: 1.6; white-space: pre-wrap;">{message}</p>
                </div>

                <p style="margin: 0; color: #999; font-size: 12px;">
                    Reply to this email to respond directly.
                </p>
            </div>
        </div>
        {FOOTER}
    </div>
    """

    return _safe_send({
        "from": settings.DEFAULT_FROM_EMAIL,
        "to": settings.ADMIN_EMAIL,
        "reply_to": user_email,
        "subject": f"[{type_label}] Message from {user_email}",
        "html": html,
        "attachments": _attachments(),
    }, context='admin_contact')


def send_user_contact_confirmation(to_email):
    """Confirm to user that their message was received."""

    html = f"""
    {FONT_IMPORT}
    <div style="background: #f8f9fa; padding: 40px 20px; font-family: 'IBM Plex Sans', Arial, Helvetica, sans-serif;">
        <div style="max-width: 560px; margin: 0 auto; background: #fff; border-radius: 8px; overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,0.04);">
            {_brand_header()}
            <div style="padding: 40px 32px;">
                <h1 style="margin: 0 0 12px; font-size: 22px; color: #1a1a1a; font-weight: 600;">Thanks for getting in touch</h1>
                <p style="margin: 0 0 16px; color: #4a4a4a; font-size: 15px; line-height: 1.6;">
                    We've received your message and will get back to you within 12 hours.
                </p>
                <div style="background: #f0f4ff; border-left: 3px solid #003399; padding: 16px 20px; border-radius: 4px;">
                    <p style="margin: 0; color: #003399; font-size: 13px;">
                        Need to add something? Just reply to this email.
                    </p>
                </div>
            </div>
        </div>
        {FOOTER}
    </div>
    """

    return _safe_send({
        "from": settings.DEFAULT_FROM_EMAIL,
        "to": to_email,
        "subject": "We've received your message",
        "html": html,
        "attachments": _attachments(),
    }, context='user_contact_confirmation')