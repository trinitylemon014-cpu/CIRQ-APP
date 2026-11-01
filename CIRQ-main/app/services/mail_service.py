import logging
import smtplib
from email.message import EmailMessage
from flask import current_app

log = logging.getLogger(__name__)


def mail_configured():
    c = current_app.config
    return bool(c.get("MAIL_SERVER") and c.get("MAIL_DEFAULT_SENDER"))


def send_mail(to, subject, body):
    """Send a plain-text email. Returns True on success. Never logs the body (it may hold a reset link)."""
    c = current_app.config
    if current_app.testing:
        current_app.extensions.setdefault("sent_mail", []).append({"to": to, "subject": subject, "body": body})
        return True
    if not mail_configured():
        log.warning("MAIL_SERVER is not configured; email '%s' was not sent", subject)
        return False
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = c["MAIL_DEFAULT_SENDER"], to, subject
    msg.set_content(body)
    try:
        with smtplib.SMTP(c["MAIL_SERVER"], c["MAIL_PORT"], timeout=15) as s:
            if c["MAIL_USE_TLS"]:
                s.starttls()
            if c["MAIL_USERNAME"]:
                s.login(c["MAIL_USERNAME"], c["MAIL_PASSWORD"])
            s.send_message(msg)
        return True
    except Exception as exc:
        log.error("email delivery failed: %s", type(exc).__name__)
        return False
