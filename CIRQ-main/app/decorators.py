from functools import wraps
from flask import g, redirect, url_for, request, abort, jsonify, flash


def _wants_json():
    return request.is_json or request.headers.get("X-Requested-With") == "fetch" or request.path.startswith(("/api/", "/live/api/"))


def login_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if not g.user:
            if _wants_json():
                return jsonify(error="Please log in to continue."), 401
            flash("Log in to continue.", "info")
            return redirect(url_for("auth.login", next=request.full_path.rstrip("?")))
        if g.user["account_status"] != "approved":
            if _wants_json():
                return jsonify(error="Your account is not active yet."), 403
            return redirect(url_for("auth.pending"))
        return f(*a, **kw)
    return wrapper


def admin_required(f):
    """Role is re-read from the database on every request (see app.load_user)."""
    @wraps(f)
    @login_required
    def wrapper(*a, **kw):
        if g.user.get("role") != "admin":
            abort(403)
        return f(*a, **kw)
    return wrapper


def moderator_or_admin(f):
    @wraps(f)
    @login_required
    def wrapper(*a, **kw):
        if g.user.get("role") not in ("admin", "moderator"):
            abort(403)
        return f(*a, **kw)
    return wrapper
