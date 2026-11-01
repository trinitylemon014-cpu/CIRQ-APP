import hashlib
from flask import Blueprint, render_template, request, redirect, url_for, flash, session, g, current_app, abort
from app.extensions import limiter, csrf
from app.services import supabase_service as sb, auth_service
from app.services.account_service import approve_account, send_verification_email
from app.services.audit_service import audit
from app.services.mail_service import send_mail
from app.services.notification_service import notify
from app.utils import (normalize_username, valid_username, valid_email, password_problems, new_token, hash_token,
                       iso, utcnow, parse_dt, hours_from_now, clean_text)

bp = Blueprint("auth", __name__)

GENERIC_LOGIN_ERROR = "That username or password isn't right."


def _safe_next(target):
    return target if target and target.startswith("/") and not target.startswith("//") else None


def _start_session(profile, remember):
    session.clear()
    session["uid"] = profile["id"]
    session["epoch"] = profile.get("session_epoch", 0)
    session.permanent = bool(remember)
    current_app.permanent_session_lifetime = __import__("datetime").timedelta(days=current_app.config["REMEMBER_DAYS"])


@bp.get("/")
def landing():
    if g.user:
        return redirect(url_for("feed.home"))
    return render_template("auth/landing.html")


@bp.route("/register", methods=["GET", "POST"])
@limiter.limit("8 per hour", methods=["POST"])
def register():
    if g.user:
        return redirect(url_for("feed.home"))
    flag = sb.get("feature_flags", key="registrations_open")
    if flag and not flag["enabled"]:
        return render_template("auth/closed.html")
    schools = sb.select("schools", order="name", desc=False)
    form = {}
    if request.method == "POST":
        f = request.form
        form = {k: f.get(k, "") for k in ("username", "display_name", "email", "school_id")}
        username = normalize_username(f.get("username"))
        email = (f.get("email") or "").strip().lower()
        display = clean_text(f.get("display_name"), 40)
        school_id = f.get("school_id")
        pw, pw2 = f.get("password", ""), f.get("password2", "")
        errors = []
        if not valid_username(username):
            errors.append("Username: 3-24 characters, letters, numbers, dots or underscores.")
        elif sb.get("profiles", "id", username=username):
            errors.append("That username is taken.")
        if len(display) < 2:
            errors.append("Enter your name (at least 2 characters).")
        if not valid_email(email):
            errors.append("Enter a valid email address.")
        if not any(s["id"] == school_id for s in schools):
            errors.append("Choose your school.")
        errors += password_problems(pw, username)
        if pw != pw2:
            errors.append("Passwords don't match.")
        if not f.get("terms"):
            errors.append("Please accept the Community Guidelines and Privacy Notice.")
        if not f.get("age_ok"):
            errors.append("CIRQ is for students aged 13 and over.")
        if not errors:
            try:
                uid = auth_service.create_auth_user(email, pw)
            except Exception:
                errors.append("We couldn't create an account with those details. If you already have one, log in instead.")
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("auth/register.html", schools=schools, form=form), 400
        sb.insert("profiles", {"id": uid, "username": username, "display_name": display, "school_id": school_id,
                               "account_status": "pending", "terms_accepted_at": iso()})
        school_comm = sb.get("communities", school_id=school_id)
        if current_app.config["AUTO_APPROVE_NEW_ACCOUNTS"]:
            approve_account(uid)
        send_verification_email(uid, email)
        profile = sb.get("profiles", id=uid)
        _start_session(profile, False)
        flash("Account created! " + ("Welcome to CIRQ." if profile["account_status"] == "approved" else "An administrator will review it shortly."), "success")
        return redirect(url_for("auth.pending") if profile["account_status"] != "approved" else url_for("feed.home"))
    return render_template("auth/register.html", schools=schools, form=form)


@bp.route("/login", methods=["GET", "POST"])
@limiter.limit("10 per minute; 40 per hour", methods=["POST"])
def login():
    if g.user:
        return redirect(url_for("feed.home"))
    if request.method == "POST":
        username = normalize_username(request.form.get("username"))
        password = request.form.get("password", "")
        profile = sb.get("profiles", username=username) if username else None
        if not profile or not auth_service.verify_password(profile["id"], password):
            flash(GENERIC_LOGIN_ERROR, "error")
            return render_template("auth/login.html", username=username), 401
        if profile["account_status"] == "suspended":
            flash("This account is suspended. Contact your school coordinator or CIRQ support.", "error")
            return render_template("auth/login.html", username=username), 403
        if profile["account_status"] == "rejected":
            flash("This account wasn't approved. Contact your school coordinator or CIRQ support.", "error")
            return render_template("auth/login.html", username=username), 403
        _start_session(profile, request.form.get("remember"))
        dest = _safe_next(request.args.get("next"))
        return redirect(dest or (url_for("feed.home") if profile["account_status"] == "approved" else url_for("auth.pending")))
    return render_template("auth/login.html", username="")


@bp.post("/logout")
def logout():
    session.clear()
    flash("You've been logged out.", "info")
    return redirect(url_for("auth.landing"))


@bp.get("/pending")
def pending():
    if not g.user:
        return redirect(url_for("auth.login"))
    if g.user["account_status"] == "approved":
        return redirect(url_for("feed.home"))
    return render_template("auth/pending.html")


@bp.get("/verify-email/<token>")
def verify_email(token):
    row = sb.get("email_verifications", token_hash=hash_token(token))
    exp = parse_dt(row["expires_at"]) if row else None
    if not row or row.get("used_at") or not exp or exp < utcnow():
        flash("That verification link is invalid or has expired.", "error")
        return redirect(url_for("auth.landing"))
    sb.update("email_verifications", {"used_at": iso()}, id=row["id"])
    sb.update("profiles", {"email_verified_at": iso()}, id=row["user_id"])
    flash("Email verified. Thank you!", "success")
    return redirect(url_for("auth.login"))


# ---------------------------------------------------------------- password recovery (admin-approved)
@bp.route("/forgot-password", methods=["GET", "POST"])
@limiter.limit("5 per hour", methods=["POST"])
def forgot_password():
    schools = sb.select("schools", order="name", desc=False)
    if request.method == "POST":
        f = request.form
        username = normalize_username(f.get("username"))
        school_id = f.get("school_id")
        email = (f.get("email") or "").strip().lower()
        display = clean_text(f.get("display_name"), 40).lower()
        no_access = bool(f.get("no_email_access"))
        note = clean_text(f.get("note"), 300)
        profile = sb.get("profiles", username=username) if username else None
        if profile and profile["account_status"] == "approved" and profile.get("school_id") == school_id:
            matches = (display and display == (profile["display_name"] or "").lower()) if no_access else \
                      (email and email == (auth_service.email_for(profile["id"]) or "").lower())
            if matches:
                open_reqs = [r for r in sb.select("password_reset_requests", user_id=profile["id"]) if r["status"] in ("pending", "approved")]
                recent = [r for r in sb.select("password_reset_requests", user_id=profile["id"])
                          if (parse_dt(r["created_at"]) or utcnow()) > utcnow() - __import__("datetime").timedelta(days=1)]
                if not open_reqs and len(recent) < 3:
                    ip_hash = hashlib.sha256((request.remote_addr or "").encode()).hexdigest()[:32]
                    sb.insert("password_reset_requests", {
                        "user_id": profile["id"], "status": "pending", "request_ip_hash": ip_hash,
                        "manual_review": no_access or not profile.get("email_verified_at"),
                        "submitted_details": {"no_email_access": no_access, "note": note}})
        # Same response whatever happened, so usernames can't be enumerated.
        return render_template("auth/forgot_submitted.html")
    return render_template("auth/forgot.html", schools=schools)


@bp.route("/reset-password/<token>", methods=["GET", "POST"])
@limiter.limit("10 per hour", methods=["POST"])
def reset_password(token):
    req = sb.get("password_reset_requests", token_hash=hash_token(token))
    exp = parse_dt(req["token_expires_at"]) if req and req.get("token_expires_at") else None
    valid = bool(req and req["status"] == "approved" and not req.get("token_used_at") and exp and exp > utcnow())
    if not valid:
        return render_template("auth/reset_invalid.html"), 400
    profile = sb.get("profiles", id=req["user_id"])
    if request.method == "POST":
        pw, pw2 = request.form.get("password", ""), request.form.get("password2", "")
        errors = password_problems(pw, profile["username"]) + (["Passwords don't match."] if pw != pw2 else [])
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("auth/reset.html", token=token), 400
        # Single-use: burn the token first, then change the password.
        claimed = sb.update("password_reset_requests", {"token_used_at": iso(), "status": "completed", "completed_at": iso()},
                            id=req["id"], token_used_at=None)
        if not claimed:
            return render_template("auth/reset_invalid.html"), 400
        old_email = auth_service.email_for(profile["id"])
        auth_service.set_password(profile["id"], pw)
        new_email = (req.get("submitted_details") or {}).get("replacement_email")
        if req.get("manual_review") and new_email:   # contact verified out-of-band by an admin
            auth_service.set_email(profile["id"], new_email)
        sb.update("profiles", {"session_epoch": profile.get("session_epoch", 0) + 1}, id=profile["id"])  # signs out every device
        audit(profile["id"], "password_reset_completed", "password_reset_request", req["id"])
        notify(profile["id"], "account", body="Your password was changed. If this wasn't you, contact CIRQ support.", dedupe=False)
        for email in {e for e in (old_email, new_email) if e}:
            send_mail(email, "Your CIRQ password was changed",
                      "Your CIRQ password was just changed using an approved reset link. If this wasn't you, contact your school coordinator or CIRQ support right away.")
        session.clear()
        flash("Password updated. Log in with your new password.", "success")
        return redirect(url_for("auth.login"))
    return render_template("auth/reset.html", token=token)
