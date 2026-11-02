"""Supabase Auth integration. Passwords are only ever handled by Supabase Auth (bcrypt, server-side).

Username login: the username is mapped to the profile row on the server, the account's email is read
from Supabase Auth with the service role, and the password is verified through Supabase Auth. Emails
are never sent to the browser.
"""
import logging
from app.extensions import db, new_anon_client
from app.services import supabase_service as sb
from app.utils import normalize_username

log = logging.getLogger(__name__)


def create_auth_user(email, password):
    res = db().auth.admin.create_user({"email": email, "password": password, "email_confirm": True})
    return res.user.id


def email_for(user_id):
    try:
        return db().auth.admin.get_user_by_id(user_id).user.email
    except Exception:  # pragma: no cover - network errors
        log.warning("could not read auth email for a user")
        return None


def profile_by_email(email):
    """Resolve an email through a server-only RPC; profile rows never store or expose emails."""
    try:
        rows = db().rpc("cirq_profile_id_for_email", {"p_email": (email or "").strip().lower()}).execute().data or []
        if not rows:
            return None
        user_id = rows[0].get("user_id") if isinstance(rows[0], dict) else rows[0]
        return sb.get("profiles", id=user_id) if user_id else None
    except Exception:
        log.warning("could not resolve account by email", exc_info=True)
        return None


def profile_by_identifier(identifier):
    identifier = (identifier or "").strip()
    if not identifier:
        return None
    if "@" in identifier:
        return profile_by_email(identifier.lower())
    return sb.get("profiles", username=normalize_username(identifier))


def authenticate_email(email, password):
    if not email:
        email = "unknown@invalid.cirq"
    try:
        res = new_anon_client().auth.sign_in_with_password({"email": email, "password": password})
        return res.user.id if res and res.user else None
    except Exception:
        return None


def verify_password(user_id, password):
    email = email_for(user_id)
    return bool(email and authenticate_email(email, password) == user_id)


def set_password(user_id, new_password):
    db().auth.admin.update_user_by_id(user_id, {"password": new_password})


def set_email(user_id, new_email):
    db().auth.admin.update_user_by_id(user_id, {"email": new_email.strip().lower(), "email_confirm": True})


def delete_auth_user(user_id):
    db().auth.admin.delete_user(user_id)


def profile_by_username(username):
    return sb.get("profiles", username=username)
