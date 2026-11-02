from datetime import timedelta
from flask import current_app, url_for
from app.extensions import db
from app.services import supabase_service as sb, coin_service
from app.services.notification_service import notify
from app.services.audit_service import audit
from app.services.mail_service import send_mail
from app.utils import new_token, iso, hours_from_now, utcnow, parse_dt


def _finish_approval(user_id, username, source, actor_id=None):
    coins = current_app.config["WELCOME_COINS"]
    coin_service.award(user_id, coins, "welcome", "welcome",
                       message=f"Welcome to CIRQ! You've got {coins} app coins.")
    notify(user_id, "account", body="Your CIRQ account has been approved. Welcome in!", dedupe=False)
    if source == "system":
        admins = sb.select("profiles", "id", role="admin", account_status="approved")
        for admin in admins:
            notify(admin["id"], "account", ref_type="auto_approved_account", ref_id=user_id,
                   body=(f"@{username} was automatically approved after waiting "
                         f"{current_app.config['AUTO_APPROVE_AFTER_MINUTES']} minutes."))
        audit(None, "user_auto_approved", "profile", user_id, username=username,
              reason="pending_more_than_five_minutes")
    else:
        audit(actor_id, "user_approved", "profile", user_id)


def approve_account(user_id, actor_id=None, expected_status=None):
    """Approve an account only if its status has not changed since the caller read it."""
    profile = sb.get("profiles", id=user_id)
    if not profile:
        return False
    expected_status = expected_status or profile["account_status"]
    if expected_status not in ("pending", "rejected", "suspended"):
        return False
    values = {"account_status": "approved", "status_note": None, "approval_source": "admin",
              "approved_at": iso(), "approved_by": actor_id}
    if not sb.update("profiles", values, id=user_id, account_status=expected_status):
        return False
    _finish_approval(user_id, profile["username"], "admin", actor_id)
    return True


def auto_approve_if_due(profile):
    """Atomically approve one eligible account once its configured waiting period expires."""
    if (profile.get("account_status") != "pending" or not profile.get("email_verified_at")
            or profile.get("auto_approval_blocked")):
        return False
    registered_at = parse_dt(profile.get("created_at"))
    if not registered_at:
        return False
    cutoff = utcnow() - timedelta(minutes=current_app.config["AUTO_APPROVE_AFTER_MINUTES"])
    if registered_at > cutoff:
        return False
    values = {"account_status": "approved", "status_note": None, "approval_source": "system",
              "approved_at": iso(), "approved_by": None}
    claimed = sb.update("profiles", values, id=profile["id"], account_status="pending",
                        created_at=profile["created_at"], email_verified_at=profile["email_verified_at"],
                        auto_approval_blocked=False)
    if not claimed:
        return False
    _finish_approval(profile["id"], profile["username"], "system")
    return True


def auto_approve_stale_accounts():
    """Approve eligible accounts that remained pending beyond the configured delay."""
    cutoff = iso(utcnow() - timedelta(minutes=current_app.config["AUTO_APPROVE_AFTER_MINUTES"]))
    pending = (db().table("profiles").select("id,username,created_at,email_verified_at,auto_approval_blocked")
               .eq("account_status", "pending")
               .lt("created_at", cutoff).order("created_at").limit(500).execute().data or [])
    if not pending:
        return 0

    approved = 0
    for profile in pending:
        approved += bool(auto_approve_if_due(profile))
    return approved


def send_verification_email(user_id, email):
    token, digest = new_token()
    sb.insert("email_verifications", {"user_id": user_id, "token_hash": digest, "expires_at": iso(hours_from_now(48))})
    link = f"{current_app.config['APP_BASE_URL']}/verify-email/{token}"
    return send_mail(email, "Verify your CIRQ email",
                     f"Welcome to CIRQ!\n\nConfirm this email address to enable automatic account approval after the five-minute waiting period. You can also use it to recover your account if you forget your password:\n{link}\n\nThis link works once and expires in 48 hours. If you didn't sign up, ignore this email.")
