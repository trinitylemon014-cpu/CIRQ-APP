from datetime import timedelta
from flask import current_app, url_for
from app.extensions import db
from app.services import supabase_service as sb, coin_service
from app.services.notification_service import notify
from app.services.audit_service import audit
from app.services.mail_service import send_mail
from app.utils import new_token, iso, hours_from_now, utcnow


def approve_account(user_id, actor_id=None):
    """Mark an account approved and give the one-time welcome coins (idempotent)."""
    sb.update("profiles", {"account_status": "approved", "status_note": None}, id=user_id)
    coins = current_app.config["WELCOME_COINS"]
    coin_service.award(user_id, coins, "welcome", "welcome",
                       message=f"Welcome to CIRQ! You've got {coins} app coins.")
    notify(user_id, "account", body="Your CIRQ account has been approved. Welcome in!", dedupe=False)


def auto_approve_stale_accounts():
    """Approve accounts still pending five minutes after signup and notify administrators."""
    cutoff = iso(utcnow() - timedelta(minutes=5))
    pending = (db().table("profiles").select("id,username").eq("account_status", "pending")
               .lt("created_at", cutoff).order("created_at").limit(500).execute().data or [])
    if not pending:
        return 0

    admins = sb.select("profiles", "id", role="admin", account_status="approved")
    approved = 0
    coins = current_app.config["WELCOME_COINS"]
    for profile in pending:
        claimed = sb.update("profiles", {"account_status": "approved", "status_note": None},
                            id=profile["id"], account_status="pending")
        if not claimed:
            continue
        coin_service.award(profile["id"], coins, "welcome", "welcome",
                           message=f"Welcome to CIRQ! You've got {coins} app coins.")
        for admin in admins:
            notify(admin["id"], "account", ref_type="auto_approved_account", ref_id=profile["id"],
                   body=f"@{profile['username']} was automatically approved after waiting five minutes.")
        audit(None, "user_auto_approved", "profile", profile["id"], username=profile["username"],
              reason="pending_more_than_five_minutes")
        approved += 1
    return approved


def send_verification_email(user_id, email):
    token, digest = new_token()
    sb.insert("email_verifications", {"user_id": user_id, "token_hash": digest, "expires_at": iso(hours_from_now(48))})
    link = f"{current_app.config['APP_BASE_URL']}/verify-email/{token}"
    return send_mail(email, "Verify your CIRQ email",
                     f"Welcome to CIRQ!\n\nConfirm this email address so you can recover your account if you ever forget your password:\n{link}\n\nThis link works once and expires in 48 hours. If you didn't sign up, ignore this email.")
