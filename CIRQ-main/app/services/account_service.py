from flask import current_app, url_for
from app.services import supabase_service as sb, coin_service
from app.services.notification_service import notify
from app.services.mail_service import send_mail
from app.utils import new_token, iso, hours_from_now


def approve_account(user_id, actor_id=None):
    """Mark an account approved and give the one-time welcome coins (idempotent)."""
    sb.update("profiles", {"account_status": "approved", "status_note": None}, id=user_id)
    coins = current_app.config["WELCOME_COINS"]
    coin_service.award(user_id, coins, "welcome", "welcome",
                       message=f"Welcome to CIRQ! You've got {coins} app coins.")
    notify(user_id, "account", body="Your CIRQ account has been approved. Welcome in!", dedupe=False)


def send_verification_email(user_id, email):
    token, digest = new_token()
    sb.insert("email_verifications", {"user_id": user_id, "token_hash": digest, "expires_at": iso(hours_from_now(48))})
    link = f"{current_app.config['APP_BASE_URL']}/verify-email/{token}"
    return send_mail(email, "Verify your CIRQ email",
                     f"Welcome to CIRQ!\n\nConfirm this email address so you can recover your account if you ever forget your password:\n{link}\n\nThis link works once and expires in 48 hours. If you didn't sign up, ignore this email.")
