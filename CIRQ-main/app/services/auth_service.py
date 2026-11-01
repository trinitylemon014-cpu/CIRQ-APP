"""Supabase Auth integration. Passwords are only ever handled by Supabase Auth (bcrypt, server-side).

Username login: the username is mapped to the profile row on the server, the account's email is read
from Supabase Auth with the service role, and the password is verified through Supabase Auth. Emails
are never sent to the browser.
"""
import logging
from app.extensions import db, new_anon_client
from app.services import supabase_service as sb

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


def verify_password(user_id, password):
    email = email_for(user_id)
    if not email:
        return False
    try:
        res = new_anon_client().auth.sign_in_with_password({"email": email, "password": password})
        return bool(res and res.user and res.user.id == user_id)
    except Exception:
        return False


def set_password(user_id, new_password):
    db().auth.admin.update_user_by_id(user_id, {"password": new_password})


def set_email(user_id, new_email):
    db().auth.admin.update_user_by_id(user_id, {"email": new_email, "email_confirm": True})


def delete_auth_user(user_id):
    db().auth.admin.delete_user(user_id)


def profile_by_username(username):
    return sb.get("profiles", username=username)
