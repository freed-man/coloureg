"""Email sending via Resend."""
import base64
import os
import resend
from django.conf import settings


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


def send_user_paint_code(to_email, registration, make, model, year, paint_code, paint_description):
    client = _client()
    make_display = (make or '').title() if make else ''
    model_display = (model or '').title() if model else ''
    vehicle = f"{year or ''} {make_display} {model_display}".strip()

    html = f"""
    <div style="background: #f8f9fa; padding: 40px 20px; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif;">
        <div style="max-width: 560px; margin: 0 auto; background: #fff; border-radius: 8px; overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,0.04);">
            {_brand_header()}
            <div style="padding: 40px 32px;">
                <h1 style="margin: 0 0 8px; font-size: 22px; color: #1a1a1a; font-weight: 600;">Your paint code</h1>
                <p style="margin: 0 0 24px; color: #666; font-size: 15px; line-height: 1.5;">
                    Here's the paint code for your {make_display or 'vehicle'}.
                </p>
                <div style="background: #f8f9fa; padding: 32px; border-radius: 8px; text-align: center; margin-bottom: 24px;">
                    <div style="font-family: 'SF Mono', Monaco, Consolas, monospace; font-size: 42px; font-weight: 700; letter-spacing: 3px; color: #1a1a1a;">
                        {paint_code}
                    </div>
                    {f'<div style="margin-top: 12px; color: #666; font-size: 14px; letter-spacing: 0.5px;">{paint_description}</div>' if paint_description else ''}
                </div>
                <table style="width: 100%; border-collapse: collapse;">
                    <tr>
                        <td style="padding: 12px 0; color: #666; font-size: 14px; width: 120px;">Vehicle</td>
                        <td style="padding: 12px 0; color: #1a1a1a; font-size: 14px; font-weight: 500;">{vehicle}</td>
                    </tr>
                    <tr style="border-top: 1px solid #eee;">
                        <td style="padding: 12px 0; color: #666; font-size: 14px;">Registration</td>
                        <td style="padding: 12px 0; color: #1a1a1a; font-size: 16px; font-weight: 600;">{registration}</td>
                    </tr>
                </table>
            </div>
        </div>
        {FOOTER}
    </div>
    """

    try:
        client.Emails.send({
            "from": settings.DEFAULT_FROM_EMAIL,
            "to": to_email,
            "subject": f"Paint code for {registration}: {paint_code}",
            "html": html,
            "attachments": _attachments(),
        })
        return True
    except Exception:
        return False


def send_admin_failure_notification(registration, make, model, year, colour, vin, user_email):
    client = _client()
    make_display = (make or '').title() if make else ''
    model_display = (model or '').title() if model else ''
    colour_display = (colour or '').title() if colour else ''
    vehicle = f"{year or ''} {make_display} {model_display}".strip()

    html = f"""
    <div style="background: #f8f9fa; padding: 40px 20px; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif;">
        <div style="max-width: 560px; margin: 0 auto; background: #fff; border-radius: 8px; overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,0.04);">
            {_brand_header()}
            <div style="background: #C8102E; padding: 16px; text-align: center;">
                <span style="color: #fff; font-size: 14px; font-weight: 600; letter-spacing: 1px; text-transform: uppercase;">Manual lookup required</span>
            </div>
            <div style="padding: 32px;">
                <table style="width: 100%; border-collapse: collapse; margin-bottom: 24px;">
                    <tr style="border-bottom: 1px solid #eee;">
                        <td style="padding: 10px 0; color: #666; font-size: 13px; width: 100px;">Reg</td>
                        <td style="padding: 10px 0; color: #1a1a1a; font-size: 14px;">{registration}</td>
                    </tr>
                    <tr style="border-bottom: 1px solid #eee;">
                        <td style="padding: 10px 0; color: #666; font-size: 13px;">Vehicle</td>
                        <td style="padding: 10px 0; color: #1a1a1a; font-size: 14px;">{vehicle}</td>
                    </tr>
                    <tr style="border-bottom: 1px solid #eee;">
                        <td style="padding: 10px 0; color: #666; font-size: 13px;">Colour</td>
                        <td style="padding: 10px 0; color: #1a1a1a; font-size: 14px;">{colour_display or '—'}</td>
                    </tr>
                    <tr style="border-bottom: 1px solid #eee;">
                        <td style="padding: 10px 0; color: #666; font-size: 13px;">VIN</td>
                        <td style="padding: 10px 0; color: #1a1a1a; font-size: 14px;">{vin or '—'}</td>
                    </tr>
                    <tr>
                        <td style="padding: 10px 0; color: #666; font-size: 13px;">User</td>
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

    try:
        client.Emails.send({
            "from": settings.DEFAULT_FROM_EMAIL,
            "to": settings.ADMIN_EMAIL,
            "reply_to": user_email,
            "subject": f"Pending Request - {registration}",
            "html": html,
            "attachments": _attachments(),
        })
        return True
    except Exception:
        return False


def send_user_pending_notification(to_email, registration, make):
    client = _client()
    make_display = (make or '').title() if make else ''

    html = f"""
    <div style="background: #f8f9fa; padding: 40px 20px; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif;">
        <div style="max-width: 560px; margin: 0 auto; background: #fff; border-radius: 8px; overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,0.04);">
            {_brand_header()}
            <div style="padding: 40px 32px;">
                <h1 style="margin: 0 0 12px; font-size: 22px; color: #1a1a1a; font-weight: 600;">Thanks — we're on it</h1>
                <p style="margin: 0 0 16px; color: #4a4a4a; font-size: 15px; line-height: 1.6;">
                    {make_display or "The manufacturer"}'s servers didn't respond in time for your lookup:
                </p>
                <div style="background: #f8f9fa; padding: 20px; border-radius: 8px; text-align: center; margin-bottom: 24px;">
                    <div style="font-size: 24px; font-weight: 700; color: #1a1a1a; letter-spacing: 2px;">
                        {registration}
                    </div>
                </div>
                <p style="margin: 0 0 24px; color: #4a4a4a; font-size: 15px; line-height: 1.6;">
                    We'll retrieve the paint code directly from the manufacturer database and email it to you within 12 hours.
                </p>
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

    try:
        client.Emails.send({
            "from": settings.DEFAULT_FROM_EMAIL,
            "to": to_email,
            "subject": f"We've received your paint code request for {registration}",
            "html": html,
            "attachments": _attachments(),
        })
        return True
    except Exception:
        return False