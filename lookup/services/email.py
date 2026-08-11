"""Email sending via Resend."""
import base64
import html as html_lib
import logging
import os
import resend
from django.conf import settings


logger = logging.getLogger(__name__)


def _esc(value):
    """HTML-escape a value for safe interpolation into email HTML bodies.

    Belt-and-braces against HTML/attribute injection from user-controlled values
    (registration, email address, contact message, VDG fields). Returns '' for
    falsy input so existing `{_esc(x) or '—'}` fallbacks still render the dash.
    Email subjects are plain text (not HTML) and reg/email are validated at the
    input boundary, so subjects are intentionally left un-escaped.
    """
    return html_lib.escape(str(value)) if value else ''


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


FOOTER = """
<div style="max-width: 560px; margin: 24px auto 0; text-align: center; color: #999; font-size: 12px;">
    Sent by <a href="https://coloureg.com" style="color: #0066cc; text-decoration: none;">coloureg.com</a>
</div>
"""


def _attachments(extra=None):
    """Build the Resend attachments list.

    Always includes the inline brand logo (referenced by content_id in the HTML).
    `extra` is an optional list of additional attachment dicts — used for photos
    a customer sends with a manual-lookup request, or that we send back with a
    reply. Each extra should look like:
        {"filename": "label.jpg", "content": <base64 str>, "content_type": "image/jpeg"}
    with NO content_id, so it arrives as a real attachment rather than an inline
    image. Kept backward compatible: every existing caller passes nothing.
    """
    atts = []
    if LOGO_BASE64:
        atts.append({
            "filename": "logo.png",
            "content": LOGO_BASE64,
            "content_id": "logo",
            "content_type": "image/png",
        })
    if extra:
        atts.extend(extra)
    return atts


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


def _make_images_responsive(html):
    """Inject responsive sizing into <img> tags emitted by Markdown.

    The Markdown ![]() syntax doesn't let the user add styles, so any image
    embedded in a compose email comes out at its natural pixel size and
    overflows narrow viewports (e.g. Gmail's mobile app on a 390px phone).

    Email clients are wildly inconsistent about CSS support, so we add the
    fix as INLINE attributes on every <img> tag:
      - max-width: 100%   — scale down to fit the container on any viewport
      - height: auto      — preserve aspect ratio when scaled
      - display: block    — avoid mystery whitespace under images
      - max-height: 600px — guard against absurdly tall portrait images
      - margin: 12px 0    — breathing room around images in text

    Single inline-style block; no JS, no media queries, no device detection.
    The image just fits whatever container it ends up in.
    """
    import re

    inline_style = (
        'max-width:100%;height:auto;display:block;'
        'max-height:600px;margin:12px 0;'
    )

    def _inject(match):
        attrs = match.group(1)
        # Strip any trailing whitespace + optional self-closing slash so we can
        # cleanly re-close the tag ourselves.
        attrs = re.sub(r'\s*/\s*$', '', attrs).rstrip()
        # If the img already has a style attribute, append our rules to it.
        # Otherwise, add a fresh style attribute.
        style_match = re.search(r'style\s*=\s*"([^"]*)"', attrs)
        if style_match:
            existing = style_match.group(1).rstrip(';')
            merged = f'{existing};{inline_style}' if existing else inline_style
            attrs = attrs[:style_match.start()] + f'style="{merged}"' + attrs[style_match.end():]
        else:
            attrs = f'{attrs} style="{inline_style}"'
        return f'<img {attrs.lstrip()}>'

    return re.sub(r'<img\b([^>]*?)\s*/?\s*>', _inject, html)


def send_custom_message(to_email, subject, markdown_body, extra_attachments=None):
    """Send a custom one-off email composed via the admin compose form.

    `markdown_body` is converted to HTML using the standard markdown library
    (no sanitisation — this endpoint is staff-only, so the input is trusted).
    Images get responsive sizing injected via _make_images_responsive. The
    result is wrapped in coloureg's brand shell so the email looks like
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
    body_html = _make_images_responsive(body_html)
    html = _brand_wrapper(body_html)

    return _safe_send({
        "from": settings.DEFAULT_FROM_EMAIL,
        "to": [to_email],
        "bcc": [settings.DEFAULT_FROM_EMAIL],
        "subject": subject,
        "html": html,
        "attachments": _attachments(extra_attachments),
    }, context='custom_message')


def send_user_paint_code(to_email, registration, vehicle_title, vin_masked, colour, paint_code, paint_description, canonical_code=None, paint_hex=None, message='', extra_attachments=None, bcc_owner=False):
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
            f'<div style="height: 80px; background: {_esc(paint_hex)}; '
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
            f'also: {_esc(canonical_code)}'
            f'</div>'
        )
    else:
        canonical_html = ''

    # Optional 'A note from us' block — only rendered when the admin typed a note
    # on the manual-lookup form. Escaped (staff-entered, but still untrusted as
    # HTML) and newlines preserved. Blank message -> no block -> identical to the
    # standard email. Matches the results-page brand accent (#003399).
    if message and message.strip():
        safe_message = html_lib.escape(message.strip()).replace('\n', '<br>')
        note_html = (
            '<div style="border: 1px solid #e7e7e7; border-radius: 8px; '
            'padding: 15px 18px; margin-bottom: 24px; background: #fcfcfd;">'
            '<div style="font-size: 11px; font-weight: 600; letter-spacing: 0.8px; '
            'text-transform: uppercase; color: #003399; margin-bottom: 8px;">A note from us</div>'
            f'<div style="color: #444; font-size: 14px; line-height: 1.55;">{safe_message}</div>'
            '</div>'
        )
    else:
        note_html = ''

    html = f"""
    <div style="background: #f8f9fa; padding: 40px 20px; font-family: 'IBM Plex Sans', Arial, Helvetica, sans-serif;">
        <div style="max-width: 560px; margin: 0 auto; background: #fff; border-radius: 8px; overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,0.04);">
            {_brand_header()}
            <div style="padding: 40px 32px;">
                <h1 style="margin: 0 0 24px; font-size: 22px; color: #1a1a1a; font-weight: 600;">Your paint code:</h1>
                {swatch_html}
                <div style="background: #f8f9fa; padding: 32px; border-radius: {box_radius}; text-align: center; margin-bottom: 24px;">
                    <div style="font-family: 'IBM Plex Mono', 'Courier New', Courier, monospace; font-size: 42px; font-weight: 700; letter-spacing: 3px; color: #1a1a1a; font-feature-settings: 'zero' 0;">
                        {_esc(paint_code)}
                    </div>
                    {canonical_html}
                    {f'<div style="margin-top: 12px; color: #666; font-size: 14px; letter-spacing: 0.5px;">{_esc(paint_description)}</div>' if paint_description else ''}
                </div>
                {note_html}
                <table style="width: 100%; border-collapse: collapse;">
                    <tr>
                        <td style="padding: 12px 0; color: #666; font-size: 14px; width: 120px;">Vehicle</td>
                        <td style="padding: 12px 0; color: #1a1a1a; font-size: 14px;">{_esc(vehicle_title) or '—'}</td>
                    </tr>
                    <tr style="border-top: 1px solid #eee;">
                        <td style="padding: 12px 0; color: #666; font-size: 14px;">Registration</td>
                        <td style="padding: 12px 0; color: #1a1a1a; font-size: 14px;">{_esc(registration)}</td>
                    </tr>
                    <tr style="border-top: 1px solid #eee;">
                        <td style="padding: 12px 0; color: #666; font-size: 14px;">VIN</td>
                        <td style="padding: 12px 0; color: #1a1a1a; font-size: 14px; word-break: break-all; overflow-wrap: break-word;">{_esc(vin_masked) or '—'}</td>
                    </tr>
                    <tr style="border-top: 1px solid #eee;">
                        <td style="padding: 12px 0; color: #666; font-size: 14px;">Colour</td>
                        <td style="padding: 12px 0; color: #1a1a1a; font-size: 14px;">{_esc(colour) or '—'}</td>
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

    # BCC depends on WHO TRIGGERED the send, not on what the email contains —
    # this one function serves two callers with opposite needs (paint53).
    #
    #   submit_email()          bcc_owner=False (default). The automatic "email
    #     me this code" a customer triggers from a successful results page. Not
    #     correspondence: nothing to reply to, nothing to keep, and the Search
    #     row already records who asked (email, email_sent). paint41 removed the
    #     bcc for exactly this case and was right to.
    #
    #   submit_manual_lookup()  bcc_owner=True. A reply YOU wrote. The bcc is the
    #     only record of what actually went out — the code, your note, and any
    #     reply photo, which is deliberately never persisted to the row. If a
    #     customer later says "the code you sent was wrong", this is the evidence.
    #
    # paint41 reasoned about the automatic caller only and dropped the bcc from a
    # function BOTH use, silently taking it off manual replies too; the manual
    # justification was left sitting in this very comment block. Note the same
    # form's no-code branch (send_user_no_code_available) kept its bcc, so the
    # admin got a copy when no code was found and none when one was — which is
    # how the regression surfaced. Do not collapse this back to a bare flag on
    # the payload without keeping the two callers distinguishable.
    payload = {
        "from": settings.DEFAULT_FROM_EMAIL,
        "to": to_email,
        "subject": f"Paint code for {registration}: {subject_code}",
        "html": html,
        "attachments": _attachments(extra_attachments),
    }
    if bcc_owner:
        payload["bcc"] = [settings.DEFAULT_FROM_EMAIL]
    return _safe_send(payload, context='paint_code')


def send_user_no_code_available(to_email, registration, vehicle_title, colour, message='', extra_attachments=None, vin_masked='', colour_name=''):
    """Reply to a manual lookup that produced no orderable paint code (paint16).

    paint57 rewrote the copy. It used to ASSERT that no code exists for the
    vehicle ("there is no paint code published for this vehicle... never issued
    or recorded"), followed by a fixed "What you can do next" block. That claim
    is often FALSE. The common case is a PSA/Stellantis car where partslink24
    returns a colour NAME and no code: the code exists, we could not reach it.
    Telling a paying customer their car has no code, when a dealer can produce
    one in a minute, is the kind of wrong answer that earns a refund request.

    Both fixed paragraphs are therefore gone, and the explanation lives entirely
    in `message` — which submit_manual_lookup already REQUIRES on this path.
    Roland writes what is true for that car; the template no longer guesses.

    `colour_name` carries a manufacturer colour name when we have one but no
    code (e.g. "Black Pearl"). It is the useful half of the answer and lets the
    customer take a real name to a factor. In practice it is the same
    `paint_description` field the admin form already posts.

    Layout deliberately mirrors send_user_paint_code — same brand header, same
    optional payload box, same optional note block, same four-row table — so the
    two replies read as one product rather than two different emails.

    BCC'd to ourselves so there is a record of exactly what was sent.
    """

    name_block = ''
    if colour_name and colour_name.strip():
        name_block = f"""
                <div style="background: #f8f9fa; padding: 28px 32px; border-radius: 8px; text-align: center; margin-bottom: 24px;">
                    <div style="font-size: 11px; font-weight: 600; letter-spacing: 0.8px; text-transform: uppercase; color: #666; margin-bottom: 10px;">Colour name</div>
                    <div style="font-size: 26px; font-weight: 600; color: #1a1a1a; line-height: 1.3;">{_esc(colour_name.strip())}</div>
                </div>
        """

    note_block = ''
    if message and message.strip():
        safe_message = html_lib.escape(message.strip()).replace('\n', '<br>')
        note_block = f"""
                <div style="border: 1px solid #e7e7e7; border-radius: 8px; padding: 18px 20px; margin-bottom: 24px;">
                    <div style="font-size: 11px; font-weight: 600; letter-spacing: 0.8px; text-transform: uppercase; color: #003399; margin-bottom: 8px;">A note from us</div>
                    <div style="color: #444; font-size: 14px; line-height: 1.55;">{safe_message}</div>
                </div>
        """

    html = f"""
    <div style="background: #f8f9fa; padding: 40px 20px; font-family: 'IBM Plex Sans', Arial, Helvetica, sans-serif;">
        <div style="max-width: 560px; margin: 0 auto; background: #fff; border-radius: 8px; overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,0.04);">
            {_brand_header()}
            <div style="padding: 40px 32px;">
                <h1 style="margin: 0 0 24px; font-size: 22px; color: #1a1a1a; font-weight: 600;">We looked into {_esc(registration)}</h1>
                {name_block}
                {note_block}
                <table style="width: 100%; border-collapse: collapse; margin-top: 8px;">
                    <tr style="border-top: 1px solid #eee;">
                        <td style="padding: 12px 0; color: #666; font-size: 14px; width: 120px;">Vehicle</td>
                        <td style="padding: 12px 0; color: #1a1a1a; font-size: 14px;">{_esc(vehicle_title) or '&mdash;'}</td>
                    </tr>
                    <tr style="border-top: 1px solid #eee;">
                        <td style="padding: 12px 0; color: #666; font-size: 14px;">Registration</td>
                        <td style="padding: 12px 0; color: #1a1a1a; font-size: 14px;">{_esc(registration)}</td>
                    </tr>
                    <tr style="border-top: 1px solid #eee;">
                        <td style="padding: 12px 0; color: #666; font-size: 14px;">VIN</td>
                        <td style="padding: 12px 0; color: #1a1a1a; font-size: 14px; word-break: break-all; overflow-wrap: break-word;">{_esc(vin_masked) or '&mdash;'}</td>
                    </tr>
                    <tr style="border-top: 1px solid #eee;">
                        <td style="padding: 12px 0; color: #666; font-size: 14px;">Colour</td>
                        <td style="padding: 12px 0; color: #1a1a1a; font-size: 14px;">{_esc(colour) or '&mdash;'}</td>
                    </tr>
                </table>
            </div>
        </div>
        {FOOTER}
    </div>
    """

    return _safe_send({
        "from": settings.DEFAULT_FROM_EMAIL,
        "to": to_email,
        "bcc": [settings.DEFAULT_FROM_EMAIL],
        "subject": f"No paint code for {registration}",
        "html": html,
        "attachments": _attachments(extra_attachments),
    }, context='no_code_available')


def send_admin_failure_notification(registration, vehicle_title, vin_full, colour, user_email, customer_message='', extra_attachments=None):
    """Email admin when paint code wasn't found and user requested manual lookup.

    `customer_message` is optional free text the customer added to the request
    ("it's an import", "resprayed by previous owner", "estate not saloon"). It is
    frequently the detail that turns a dead end into a found code, so it is shown
    prominently rather than buried. `extra_attachments` carries any photo they
    uploaded (e.g. of the paint label), which arrives attached to this email —
    nothing is stored server-side.
    """

    message_block = ''
    if customer_message and customer_message.strip():
        message_block = f"""
                <div style="background: #f0f4ff; border-left: 3px solid #003399; padding: 16px 20px; border-radius: 4px; margin-bottom: 24px;">
                    <p style="margin: 0 0 6px; color: #003399; font-size: 11px; font-weight: 600; letter-spacing: 0.8px; text-transform: uppercase;">Customer said</p>
                    <p style="margin: 0; color: #1a1a1a; font-size: 14px; line-height: 1.6; white-space: pre-wrap;">{_esc(customer_message.strip())}</p>
                </div>
        """

    html = f"""
    <div style="background: #f8f9fa; padding: 40px 20px; font-family: 'IBM Plex Sans', Arial, Helvetica, sans-serif;">
        <div style="max-width: 560px; margin: 0 auto; background: #fff; border-radius: 8px; overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,0.04);">
            {_brand_header()}
            <div style="background: #C8102E; padding: 16px; text-align: center;">
                <span style="color: #fff; font-size: 14px; font-weight: 600; letter-spacing: 1px; text-transform: uppercase;">Manual lookup required</span>
            </div>
            <div style="padding: 32px;">
                <table style="width: 100%; border-collapse: collapse; margin-bottom: 24px;">
                    <tr style="border-bottom: 1px solid #eee;">
                        <td style="padding: 10px 16px 10px 0; color: #666; font-size: 13px; width: 64px;">Vehicle</td>
                        <td style="padding: 10px 0; color: #1a1a1a; font-size: 14px;">{_esc(vehicle_title) or '—'}</td>
                    </tr>
                    <tr style="border-bottom: 1px solid #eee;">
                        <td style="padding: 10px 16px 10px 0; color: #666; font-size: 13px;">Registration</td>
                        <td style="padding: 10px 0; color: #1a1a1a; font-size: 14px;">{_esc(registration)}</td>
                    </tr>
                    <tr style="border-bottom: 1px solid #eee;">
                        <td style="padding: 10px 16px 10px 0; color: #666; font-size: 13px;">VIN</td>
                        <td style="padding: 10px 0; color: #1a1a1a; font-size: 14px; font-family: 'IBM Plex Sans', Arial, Helvetica, sans-serif; overflow-wrap: break-word;">{_esc(vin_full) or '—'}</td>
                    </tr>
                    <tr style="border-bottom: 1px solid #eee;">
                        <td style="padding: 10px 16px 10px 0; color: #666; font-size: 13px;">Colour</td>
                        <td style="padding: 10px 0; color: #1a1a1a; font-size: 14px;">{_esc(colour) or '—'}</td>
                    </tr>
                    <tr>
                        <td style="padding: 10px 16px 10px 0; color: #666; font-size: 13px; vertical-align: top;">User</td>
                        <td style="padding: 10px 0; word-break: break-all; overflow-wrap: break-word;"><a href="mailto:{_esc(user_email)}" style="color: #003399; font-size: 14px; word-break: break-all; overflow-wrap: break-word;">{_esc(user_email)}</a></td>
                    </tr>
                </table>

                {message_block}
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
        "attachments": _attachments(extra_attachments),
    }, context='admin_failure')


def send_user_pending_notification(to_email, registration, vehicle_title, vin_masked, colour):
    """Email user confirming we'll do manual lookup."""

    html = f"""
    <div style="background: #f8f9fa; padding: 40px 20px; font-family: 'IBM Plex Sans', Arial, Helvetica, sans-serif;">
        <div style="max-width: 560px; margin: 0 auto; background: #fff; border-radius: 8px; overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,0.04);">
            {_brand_header()}
            <div style="padding: 40px 32px;">
                <h1 style="margin: 0 0 12px; font-size: 22px; color: #1a1a1a; font-weight: 600;">Thanks — we're on it</h1>
                <p style="margin: 0 0 24px; color: #4a4a4a; font-size: 15px; line-height: 1.6;">
                    The manufacturer's servers didn't respond in time. We'll retrieve the paint code directly from the manufacturer database and email it to you, usually within 1 hour.
                </p>
                <table style="width: 100%; border-collapse: collapse; margin-bottom: 24px;">
                    <tr style="border-bottom: 1px solid #eee;">
                        <td style="padding: 10px 16px 10px 0; color: #666; font-size: 13px; width: 100px;">Vehicle</td>
                        <td style="padding: 10px 0; color: #1a1a1a; font-size: 14px;">{_esc(vehicle_title) or '—'}</td>
                    </tr>
                    <tr style="border-bottom: 1px solid #eee;">
                        <td style="padding: 10px 16px 10px 0; color: #666; font-size: 13px;">Registration</td>
                        <td style="padding: 10px 0; color: #1a1a1a; font-size: 14px;">{_esc(registration)}</td>
                    </tr>
                    <tr style="border-bottom: 1px solid #eee;">
                        <td style="padding: 10px 16px 10px 0; color: #666; font-size: 13px;">VIN</td>
                        <td style="padding: 10px 0; color: #1a1a1a; font-size: 14px; word-break: break-all; overflow-wrap: break-word;">{_esc(vin_masked) or '—'}</td>
                    </tr>
                    <tr>
                        <td style="padding: 10px 16px 10px 0; color: #666; font-size: 13px;">Colour</td>
                        <td style="padding: 10px 0; color: #1a1a1a; font-size: 14px;">{_esc(colour) or '—'}</td>
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
        "subject": f"We're looking into {registration}",
        "html": html,
        "attachments": _attachments(),
    }, context='user_pending')


def send_admin_contact_message(contact_type, user_email, message):
    """Send contact form message to admin."""
    type_label = contact_type.title()

    html = f"""
    <div style="background: #f8f9fa; padding: 40px 20px; font-family: 'IBM Plex Sans', Arial, Helvetica, sans-serif;">
        <div style="max-width: 560px; margin: 0 auto; background: #fff; border-radius: 8px; overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,0.04);">
            {_brand_header()}
            <div style="background: #003399; padding: 16px; text-align: center;">
                <span style="color: #fff; font-size: 14px; font-weight: 600; letter-spacing: 1px; text-transform: uppercase;">New {_esc(type_label)} Message</span>
            </div>
            <div style="padding: 32px;">
                <table style="width: 100%; border-collapse: collapse; margin-bottom: 24px;">
                    <tr style="border-bottom: 1px solid #eee;">
                        <td style="padding: 10px 16px 10px 0; color: #666; font-size: 13px; width: 80px;">Type</td>
                        <td style="padding: 10px 0; color: #1a1a1a; font-size: 14px;">{_esc(type_label)}</td>
                    </tr>
                    <tr style="border-bottom: 1px solid #eee;">
                        <td style="padding: 10px 16px 10px 0; color: #666; font-size: 13px; vertical-align: top;">From</td>
                        <td style="padding: 10px 0; word-break: break-all; overflow-wrap: break-word;"><a href="mailto:{_esc(user_email)}" style="color: #003399; font-size: 14px; word-break: break-all; overflow-wrap: break-word;">{_esc(user_email)}</a></td>
                    </tr>
                </table>

                <div style="background: #f8f9fa; padding: 20px; border-radius: 8px; margin-bottom: 16px;">
                    <p style="margin: 0; color: #1a1a1a; font-size: 14px; line-height: 1.6; white-space: pre-wrap;">{_esc(message)}</p>
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
    <div style="background: #f8f9fa; padding: 40px 20px; font-family: 'IBM Plex Sans', Arial, Helvetica, sans-serif;">
        <div style="max-width: 560px; margin: 0 auto; background: #fff; border-radius: 8px; overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,0.04);">
            {_brand_header()}
            <div style="padding: 40px 32px;">
                <h1 style="margin: 0 0 12px; font-size: 22px; color: #1a1a1a; font-weight: 600;">Thanks for getting in touch</h1>
                <p style="margin: 0 0 16px; color: #4a4a4a; font-size: 15px; line-height: 1.6;">
                    We've received your message and will get back to you, usually within 1 hour.
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

def send_admin_budget_alert(spend_today, budget):
    """Email admin ONCE when the daily VDG budget breaker trips (paint15).

    Sent by the breaker in views.index the first time a lookup is refused
    because today's real (refund-net) VDG spend reached the configured daily
    budget. The caller is responsible for the once-per-day guard
    (SiteConfig.budget_tripped / budget_tripped_date) — this function just
    sends.
    """

    html = f"""
    <div style="background: #f8f9fa; padding: 40px 20px; font-family: 'IBM Plex Sans', Arial, Helvetica, sans-serif;">
        <div style="max-width: 560px; margin: 0 auto; background: #fff; border-radius: 8px; overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,0.04);">
            {_brand_header()}
            <div style="background: #C8102E; padding: 16px; text-align: center;">
                <span style="color: #fff; font-size: 14px; font-weight: 600; letter-spacing: 1px; text-transform: uppercase;">Daily budget reached — lookups paused</span>
            </div>
            <div style="padding: 32px;">
                <p style="margin: 0 0 20px; color: #4a4a4a; font-size: 15px; line-height: 1.6;">
                    Today's VDG spend has reached the daily budget, so new lookups
                    are being refused until midnight (London time). Existing pages
                    and the admin dashboard keep working.
                </p>
                <table style="width: 100%; border-collapse: collapse; margin-bottom: 24px;">
                    <tr style="border-bottom: 1px solid #eee;">
                        <td style="padding: 10px 16px 10px 0; color: #666; font-size: 13px; width: 140px;">Spend today (net)</td>
                        <td style="padding: 10px 0; color: #1a1a1a; font-size: 14px;">£{_esc(f'{spend_today:.2f}')}</td>
                    </tr>
                    <tr style="border-bottom: 1px solid #eee;">
                        <td style="padding: 10px 16px 10px 0; color: #666; font-size: 13px;">Daily budget</td>
                        <td style="padding: 10px 0; color: #1a1a1a; font-size: 14px;">£{_esc(f'{budget:.2f}')}</td>
                    </tr>
                </table>
                <p style="margin: 0; color: #999; font-size: 12px;">
                    To resume lookups today, raise or zero the budget in /admin-stats/.
                    If this spend wasn't expected, check the recent lookups table for
                    abuse before raising it.
                </p>
            </div>
        </div>
        {FOOTER}
    </div>
    """

    return _safe_send({
        "from": settings.DEFAULT_FROM_EMAIL,
        "to": settings.ADMIN_EMAIL,
        "subject": "coloureg: daily VDG budget reached — lookups paused",
        "html": html,
        "attachments": _attachments(),
    }, context='admin_budget_alert')
