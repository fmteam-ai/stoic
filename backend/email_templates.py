"""Admin → Integrations → E-mail templates: catalog, sample-data preview, test send.

Each entry renders with the SAME builder the production code path uses, so the
preview is what customers receive (sample data only — no real tokens/codes).
"""
from datetime import datetime, timezone


def _sample_name() -> str:
    return "Ada"


def _activation():
    from activation import _email_html, _frontend_base
    return "Activate your STOIC account", _email_html(name=_sample_name(), link=f"{_frontend_base()}/verify-email?token=SAMPLE-TOKEN")


def _existing_account():
    from activation import _frontend_base
    text = ("Someone tried to create a STOIC account with this email address, but an account already exists.\n\n"
            f"If that was you, sign in here: {_frontend_base()}/login\n"
            "If you forgot your password, use the reset link on the sign-in page.\n\n"
            "If this wasn't you, no action is needed — nothing has changed.\n\n— STOIC · Disciplined AI Trading")
    return "You already have a STOIC account", f"<pre style='font-family:sans-serif;white-space:pre-wrap'>{text}</pre>"


def _password_reset():
    from password_reset import _email_html, _frontend_base
    return "Reset your STOIC password", _email_html(name=_sample_name(), link=f"{_frontend_base()}/reset-password?token=SAMPLE-TOKEN")


def _login_otp():
    from login_otp import _otp_email_html
    return "123456 is your STOIC sign-in code", _otp_email_html(_sample_name(), "123456")


def _new_login_alert():
    from login_alerts import _alert_html
    return "STOIC — new sign-in to your account", _alert_html(_sample_name(), "203.0.113.42", "Chrome 126 · Windows 11",
                                                               datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"))


def _renewal_7d():
    from background_loops import _renewal_email_html
    return "Your STOIC plan ends in 7 days — renew to keep trading", _renewal_email_html(_sample_name(), "Trader Annual", "2026-07-15", 7)


def _renewal_1d():
    from background_loops import _renewal_email_html
    return "Your STOIC plan ends in 1 day — renew to keep trading", _renewal_email_html(_sample_name(), "Trader Annual", "2026-07-15", 1)


def _support_new_ticket():
    from routes.support_routes import _email_html
    return "[STOIC support] New billing ticket: Invoice question", _email_html(
        "New support ticket", ["From: ada@example.com", "Category: billing", "Subject: Invoice question",
                               "Could you send me a VAT invoice for my annual plan?"], "/admin/support")


def _support_admin_reply():
    from routes.support_routes import _email_html
    return "[STOIC support] Reply to: Invoice question", _email_html(
        "Support replied to your ticket", ["Subject: Invoice question", "Your VAT invoice is attached to the ticket thread."], "/support")


def _test_alert():
    from routes.notification_routes import _test_email_html
    return "[TEST] STOIC · Test alert — no action required", _test_email_html(_sample_name(), datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"))


REGISTRY = {
    "activation": ("Account activation", "Sent at sign-up with the e-mail verification link", "auth", _activation),
    "existing_account": ("Existing-account notice", "Sent when someone registers with an already-used e-mail", "auth", _existing_account),
    "password_reset": ("Password reset", "Sent from the 'forgot password' flow", "auth", _password_reset),
    "login_otp": ("Sign-in code (e-mail OTP)", "Sent at login when the e-mail OTP gate is enabled", "auth", _login_otp),
    "new_login_alert": ("New sign-in alert", "Sent on a sign-in from an unrecognised IP", "security", _new_login_alert),
    "renewal_7d": ("Renewal reminder · 7 days", "Sent 7 days before a plan expires", "billing", _renewal_7d),
    "renewal_1d": ("Renewal reminder · 1 day", "Sent the day before a plan expires", "billing", _renewal_1d),
    "support_new_ticket": ("Support · new ticket (to admins)", "Sent to SUPPORT_NOTIFY_EMAIL when a user opens a ticket", "support", _support_new_ticket),
    "support_admin_reply": ("Support · reply (to user)", "Sent to the ticket owner when an admin replies", "support", _support_admin_reply),
    "test_alert": ("Notification test e-mail", "Sent from Notifications → 'send test e-mail'", "alerts", _test_alert),
}


def catalog() -> list[dict]:
    return [{"id": k, "label": v[0], "description": v[1], "category": v[2]} for k, v in REGISTRY.items()]


def render(template_id: str) -> dict:
    if template_id not in REGISTRY:
        raise KeyError(template_id)
    subject, html = REGISTRY[template_id][3]()
    return {"id": template_id, "label": REGISTRY[template_id][0], "subject": subject, "html": html}


async def send_test(template_id: str, recipient: str) -> dict:
    from email_sender import send_email, is_configured
    if not is_configured():
        return {"ok": False, "error": "RESEND_API_KEY not configured"}
    r = render(template_id)
    res = await send_email(recipient, f"[PREVIEW] {r['subject']}", r["html"])
    return {"ok": bool(res.get("ok")), "id": res.get("id"), "error": res.get("error"), "recipient": recipient,
            "subject": f"[PREVIEW] {r['subject']}"}
