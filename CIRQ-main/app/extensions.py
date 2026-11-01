from flask import current_app
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_wtf.csrf import CSRFProtect

csrf = CSRFProtect()
limiter = Limiter(key_func=get_remote_address, default_limits=["300 per hour"])


def db():
    """Server-side Supabase client (service role). Never sent to the browser."""
    return current_app.extensions["cirq_db"]


def new_anon_client():
    """Fresh anon-key client, used only to verify passwords through Supabase Auth."""
    return current_app.extensions["cirq_anon_factory"]()
