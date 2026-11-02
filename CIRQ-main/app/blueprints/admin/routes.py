import csv
import io
import re
import zipfile
import xml.etree.ElementTree as ET

from flask import Blueprint, render_template, request, redirect, url_for, flash, g, abort, jsonify, current_app
from pypdf import PdfReader
from app.decorators import admin_required, moderator_or_admin
from app.extensions import db, csrf
from app.services import supabase_service as sb, auth_service, cloudinary_service as cs, coin_service, social
from app.services.account_service import approve_account, auto_approve_stale_accounts
from app.services.audit_service import audit
from app.services.mail_service import send_mail, mail_configured
from app.services.notification_service import notify
from app.utils import iso, clean_text, new_token, hours_from_now, utcnow, valid_email, normalize_school_name

bp = Blueprint("admin", __name__, url_prefix="/admin")


def _back(default="admin.dashboard"):
    return redirect(request.referrer or url_for(default))


@bp.get("")
@admin_required
def dashboard():
    c = lambda t, **k: sb.count(t, **k)
    stats = {
        "users_total": c("profiles"), "users_pending": c("profiles", account_status="pending"),
        "users_suspended": c("profiles", account_status="suspended"),
        "users_auto_approved": c("profiles", account_status="approved", approval_source="system"),
        "posts": c("posts", kind="post"), "reels": c("posts", kind="reel"),
        "stories_active": len(db().table("stories").select("id").gt("expires_at", iso()).execute().data or []),
        "events": c("events"), "reports_open": c("reports", status="open"),
        "resets_pending": c("password_reset_requests", status="pending"),
        "verifications_pending": c("school_verification_requests", status="pending"),
        "payers_pending": c("payer_numbers", status="pending"), "trivia_sets": c("trivia_sets"),
        "flip_rounds": c("game_rounds"),
    }
    return render_template("admin/dashboard.html", stats=stats, mail_ok=mail_configured())


@bp.route("/developer-card", methods=["GET", "POST"])
@admin_required
def developer_card():
    settings = sb.get("developer_card_settings", id="developer")
    if not settings:
        settings = sb.insert("developer_card_settings", {"id": "developer"})

    if request.method == "POST":
        name = clean_text(request.form.get("display_name"), 40)
        if len(name) < 2:
            flash("Developer card name must be at least 2 characters.", "error")
            return redirect(url_for("admin.developer_card"))

        values = {"display_name": name, "updated_by": g.user["id"], "updated_at": iso()}
        image = request.files.get("image")
        if image and image.filename:
            try:
                info = cs.upload(image, "game-cards", g.user["id"], allow_video=False)
            except cs.UploadError as e:
                flash(str(e), "error")
                return redirect(url_for("admin.developer_card"))
            values.update(image_url=info["url"], image_public_id=info["public_id"])
        elif request.form.get("remove"):
            values.update(image_url=None, image_public_id=None)

        sb.update("developer_card_settings", values, id="developer")
        if settings.get("image_public_id") and settings["image_public_id"] != values.get("image_public_id", settings["image_public_id"]):
            cs.destroy(settings["image_public_id"])
        audit(g.user["id"], "developer_card_updated", "developer_card", "developer")
        flash("Developer card saved.", "success")
        return redirect(url_for("admin.developer_card"))

    name = settings.get("display_name") or current_app.config["DEVELOPER_DISPLAY_NAME"]
    image = {"url": settings.get("image_url"), "public_id": settings.get("image_public_id"), "media_type": "image"}
    return render_template("admin/developer_card.html", name=name,
                           image=cs.media_src(image, 320) if image["url"] else "")


# ------------------------------------------------------------------ users
@bp.get("/users")
@admin_required
def users():
    status, q = request.args.get("status", ""), (request.args.get("q") or "").strip()[:40]
    query = db().table("profiles").select("*").order("created_at", desc=True).limit(80)
    if status:
        query = query.eq("account_status", status)
    if q:
        query = query.ilike("username", f"%{q.replace('%', '')}%")
    rows = query.execute().data or []
    schools = sb.by_id("schools", [r.get("school_id") for r in rows])
    for r in rows:
        r["school"] = schools.get(r.get("school_id"))
    return render_template("admin/users.html", users=rows, status=status, q=q)


@bp.post("/users/<uid>/<action>")
@admin_required
def user_action(uid, action):
    target = sb.get("profiles", id=uid)
    if not target:
        abort(404)
    note = clean_text(request.form.get("note"), 300)
    if target["id"] == g.user["id"] and action in ("suspend", "reject", "set_role", "remove"):
        flash("You can't do that to your own account.", "error")
        return _back("admin.users")
    if action == "approve":
        if not approve_account(uid, g.user["id"], expected_status=target["account_status"]):
            flash("This account changed while you were reviewing it. Refresh and try again.", "error")
            return _back("admin.users")
        return _back("admin.users")
    elif action == "reject":
        changed = sb.update("profiles", {
            "account_status": "rejected", "status_note": note or None,
            "approval_source": None, "approved_by": None, "approved_at": None,
        }, id=uid, account_status=target["account_status"])
        if not changed:
            flash("This account changed while you were reviewing it. Refresh and try again.", "error")
            return _back("admin.users")
    elif action == "suspend":
        sb.update("profiles", {"account_status": "suspended", "status_note": note or None,
                               "session_epoch": target.get("session_epoch", 0) + 1}, id=uid)   # kicks them out immediately
        notify(uid, "moderation", body="Your account was suspended.", dedupe=False)
    elif action == "unsuspend":
        sb.update("profiles", {"account_status": "approved", "status_note": None}, id=uid)
    elif action == "hold_auto_approval":
        if target["account_status"] != "pending":
            abort(400)
        blocked = not target.get("auto_approval_blocked", False)
        sb.update("profiles", {"auto_approval_blocked": blocked}, id=uid, account_status="pending")
        audit(g.user["id"], "user_auto_approval_hold_set", "profile", uid, blocked=blocked)
        flash("Automatic approval is on hold." if blocked else "Automatic approval is allowed.", "success")
        return _back("admin.users")
    elif action == "remove":
        if target.get("approval_source") != "system" or target["account_status"] != "approved":
            abort(400)
        audit(g.user["id"], "user_auto_approved_removed", "profile", uid, username=target["username"])
        auth_service.delete_auth_user(uid)
        flash("Auto-approved account removed.", "success")
        return _back("admin.auto_approved")
    elif action == "verify":
        sb.update("profiles", {"verified": not target["verified"]}, id=uid)
    elif action == "set_role":
        role = request.form.get("role")
        if role not in ("student", "moderator", "admin"):
            abort(400)
        sb.update("profiles", {"role": role}, id=uid)
    else:
        abort(404)
    audit(g.user["id"], f"user_{action}", "profile", uid, note=note)
    flash("Done.", "success")
    return _back("admin.users")


# ------------------------------------------------------------------ schools & verification
def _save_school_logo(school, file):
    """Upload a school logo (images only) and replace the old one. Returns (ok, message)."""
    try:
        info = cs.upload(file, "schools", g.user["id"], allow_video=False)
    except cs.UploadError as e:
        return False, str(e)
    old = school.get("logo_public_id")
    sb.update("schools", {"logo_url": info["url"], "logo_public_id": info["public_id"]}, id=school["id"])
    if old:
        cs.destroy(old, "image")   # don't leave the previous logo behind in Cloudinary
    audit(g.user["id"], "school_logo_set", "school", school["id"])
    return True, "Logo saved."


@bp.route("/schools", methods=["GET", "POST"])
@admin_required
def schools():
    if request.method == "POST":
        name = clean_text(request.form.get("name"), 100)
        if len(name) < 3:
            flash("Enter the school name.", "error")
        else:
            slug = "-".join("".join(ch if ch.isalnum() else " " for ch in name.lower()).split())
            if sb.get("schools", slug=slug):
                flash("That school already exists.", "error")
            else:
                s = sb.insert("schools", {"name": name, "slug": slug, "country": clean_text(request.form.get("country"), 40) or "Uganda",
                                          "city": clean_text(request.form.get("city"), 60) or None})
                sb.insert("communities", {"school_id": s["id"], "name": f"{name} Community"})
                audit(g.user["id"], "school_created", "school", s["id"], name=name)
                logo = request.files.get("logo")
                if logo and logo.filename:
                    ok, msg = _save_school_logo(s, logo)
                    flash("School added." if ok else f"School added, but the logo wasn't saved: {msg}", "success" if ok else "error")
                else:
                    flash("School added.", "success")
        return redirect(url_for("admin.schools"))
    reqs = sb.select("school_verification_requests", order="created_at", status="pending")
    users = sb.by_id("profiles", [r["requested_by"] for r in reqs])
    sch = sb.by_id("schools", [r["school_id"] for r in reqs])
    for r in reqs:
        r["user"], r["school"] = users.get(r["requested_by"]), sch.get(r["school_id"])
    submissions = sb.select("school_submissions", order="created_at", status="pending")
    submission_ids = [r["id"] for r in submissions]
    profile_rows = (db().table("profiles").select("school_submission_id")
                    .in_("school_submission_id", submission_ids).execute().data or []) if submission_ids else []
    counts = {}
    for row in profile_rows:
        counts[row["school_submission_id"]] = counts.get(row["school_submission_id"], 0) + 1
    submitters = sb.by_id("profiles", [r["submitted_by"] for r in submissions])
    for submission in submissions:
        submission["student_count"] = counts.get(submission["id"], 0)
        submission["submitter"] = submitters.get(submission.get("submitted_by"))
    all_schools = sb.select("schools", order="name", desc=False)
    return render_template("admin/schools.html", schools=all_schools, reqs=reqs, submissions=submissions)


@bp.post("/school-submissions/<sid>/<decision>")
@admin_required
def school_submission_action(sid, decision):
    submission = sb.get("school_submissions", id=sid, status="pending")
    if not submission or decision not in ("approve", "merge", "reject"):
        abort(404)

    note = clean_text(request.form.get("note"), 300)
    school = None
    if decision == "reject":
        status = "rejected"
    elif decision == "merge":
        school = sb.get("schools", id=request.form.get("school_id"))
        if not school:
            flash("Choose an existing school to merge into.", "error")
            return _back("admin.schools")
        status = "approved"
    else:
        normalized = normalize_school_name(submission["name"])
        school = next((row for row in sb.select("schools", order="name", desc=False)
                       if normalize_school_name(row["name"]) == normalized), None)
        if not school:
            slug = "-".join("".join(ch if ch.isalnum() else " " for ch in submission["name"].lower()).split())
            school = sb.insert("schools", {"name": submission["name"], "slug": slug, "verified": True})
            sb.insert("communities", {"school_id": school["id"], "name": f"{school['name']} Community"})
        status = "approved"

    changed = sb.update("school_submissions", {
        "status": status, "resolved_school_id": school["id"] if school else None,
        "reviewed_by": g.user["id"], "reviewed_at": iso(), "decision_note": note or None,
    }, id=sid, status="pending")
    if not changed:
        flash("This school suggestion was already reviewed. Refresh the page.", "error")
        return _back("admin.schools")

    affected = sb.select("profiles", school_submission_id=sid)
    if decision == "reject":
        for profile in affected:
            sb.update("profiles", {
                "school_id": None, "school_verified": False,
                "school_submission_id": None, "submitted_school_name": None,
            }, id=profile["id"], school_submission_id=sid)
            notify(profile["id"], "moderation",
                   body="Your school suggestion was declined. Choose a listed school in Settings after your account is approved.",
                   dedupe=False)
    else:
        if decision == "approve":
            sb.update("schools", {"verified": True}, id=school["id"])
        for profile in affected:
            sb.update("profiles", {"school_id": school["id"], "school_verified": True},
                      id=profile["id"], school_submission_id=sid)
            notify(profile["id"], "moderation", body=f"Your school has been matched to {school['name']}.", dedupe=False)

    audit(g.user["id"], f"school_submission_{decision}", "school_submission", sid,
          school_id=school["id"] if school else None, student_count=len(affected), note=note)
    flash("School suggestion reviewed.", "success")
    return redirect(url_for("admin.schools"))


@bp.get("/auto-approved")
@admin_required
def auto_approved():
    rows = sb.select("profiles", order="approved_at", account_status="approved", approval_source="system", limit=200)
    return render_template("admin/auto_approved.html", users=rows)


@bp.post("/schools/requests/<rid>/<decision>")
@admin_required
def school_request(rid, decision):
    r = sb.get("school_verification_requests", id=rid)
    if not r or r["status"] != "pending" or decision not in ("approve", "reject"):
        abort(404)
    note = clean_text(request.form.get("note"), 300)
    sb.update("school_verification_requests", {"status": "approved" if decision == "approve" else "rejected", "reviewed_by": g.user["id"],
                                               "reviewed_at": iso(), "decision_note": note or None}, id=rid)
    if decision == "approve":
        sb.update("schools", {"verified": True}, id=r["school_id"])
        comm = sb.get("communities", school_id=r["school_id"])
        if comm:
            existing = sb.get("community_members", community_id=comm["id"], user_id=r["requested_by"])
            if existing:
                sb.update("community_members", {"role": "moderator"}, community_id=comm["id"], user_id=r["requested_by"])
            else:
                sb.insert("community_members", {"community_id": comm["id"], "user_id": r["requested_by"], "role": "moderator"})
    notify(r["requested_by"], "moderation", body=f"Your school verification request was {decision}d.", dedupe=False)
    audit(g.user["id"], f"school_verification_{decision}", "school", r["school_id"], note=note)
    flash("Request reviewed.", "success")
    return redirect(url_for("admin.schools"))


@bp.post("/schools/<sid>/logo")
@admin_required
def school_logo(sid):
    """Upload, replace or remove a school's logo. The logo is shown on the school's community page and in school lists."""
    s = sb.get("schools", id=sid)
    if not s:
        abort(404)
    if request.form.get("remove"):
        cs.destroy(s.get("logo_public_id"), "image")
        sb.update("schools", {"logo_url": None, "logo_public_id": None}, id=sid)
        audit(g.user["id"], "school_logo_removed", "school", sid)
        flash("Logo removed.", "info")
    else:
        f = request.files.get("logo")
        if not f or not f.filename:
            flash("Choose an image first.", "error")
        else:
            ok, msg = _save_school_logo(s, f)
            flash(msg, "success" if ok else "error")
    return redirect(url_for("admin.schools"))


@bp.post("/schools/<sid>/toggle-verified")
@admin_required
def school_toggle(sid):
    s = sb.get("schools", id=sid)
    if not s:
        abort(404)
    sb.update("schools", {"verified": not s["verified"]}, id=sid)
    audit(g.user["id"], "school_verified_toggled", "school", sid, verified=not s["verified"])
    return _back("admin.schools")


# ------------------------------------------------------------------ password resets
@bp.get("/password-resets")
@admin_required
def password_resets():
    status = request.args.get("status", "pending")
    rows = sb.select("password_reset_requests", order="created_at", status=status, limit=100) if status != "all" else sb.select("password_reset_requests", order="created_at", limit=100)
    users = sb.by_id("profiles", [r["user_id"] for r in rows])
    schools = sb.by_id("schools", [u.get("school_id") for u in users.values()])
    for r in rows:
        r["user"] = users.get(r["user_id"])
        r["school"] = schools.get((r["user"] or {}).get("school_id"))
        r["audit"] = [a for a in sb.select("audit_logs", order="created_at", target_type="password_reset_request", target_id=r["id"])]
    return render_template("admin/password_resets.html", rows=rows, status=status, mail_ok=mail_configured())


@bp.post("/password-resets/<rid>/<decision>")
@admin_required
def password_reset_decide(rid, decision):
    r = sb.get("password_reset_requests", id=rid)
    if not r or r["status"] != "pending" or decision not in ("approve", "reject"):
        abort(404)
    note = clean_text(request.form.get("note"), 300)
    user = sb.get("profiles", id=r["user_id"])
    if decision == "reject":
        sb.update("password_reset_requests", {"status": "rejected", "reviewed_by": g.user["id"], "reviewed_at": iso(), "decision_note": note or None}, id=rid)
        audit(g.user["id"], "password_reset_rejected", "password_reset_request", rid, note=note)
        flash("Request rejected.", "info")
        return redirect(url_for("admin.password_resets"))
    # Approval never changes the password. It only issues a single-use, time-limited link to a verified contact.
    replacement = (request.form.get("replacement_email") or "").strip().lower()
    if r["manual_review"]:
        if not valid_email(replacement):
            flash("Manual review: enter the replacement email you verified with the school coordinator.", "error")
            return _back("admin.password_resets")
        if len(note) < 10:
            flash("Manual review: record how you verified the student's identity.", "error")
            return _back("admin.password_resets")
        deliver_to = replacement
    else:
        deliver_to = auth_service.email_for(user["id"])
    token, digest = new_token()
    minutes = current_app.config["RESET_TOKEN_MINUTES"]
    link = f"{current_app.config['APP_BASE_URL']}/reset-password/{token}"
    sent = send_mail(deliver_to, "Reset your CIRQ password",
                     f"Hi {user['display_name']},\n\nAn administrator approved your password reset request. Choose a new password here:\n{link}\n\nThis link works once and expires in {minutes} minutes. If you didn't ask for this, ignore this email.")
    if not sent:
        flash("The email could not be sent, so the request was NOT approved. Check the MAIL_* settings and try again.", "error")
        return _back("admin.password_resets")
    details = {**(r.get("submitted_details") or {})}
    if r["manual_review"]:
        details["replacement_email"] = replacement
    sb.update("password_reset_requests", {"status": "approved", "reviewed_by": g.user["id"], "reviewed_at": iso(), "decision_note": note or None,
                                          "token_hash": digest, "token_expires_at": iso(hours_from_now(minutes / 60)),
                                          "submitted_details": details}, id=rid)
    audit(g.user["id"], "password_reset_approved", "password_reset_request", rid, manual=r["manual_review"], note=note)
    flash("Approved. A single-use reset link was emailed to the student.", "success")
    return redirect(url_for("admin.password_resets"))


# ------------------------------------------------------------------ reports & moderation
TABLES = {"post": "posts", "reel": "posts", "comment": "comments", "profile": "profiles", "message": "messages", "event": "events", "story": "stories",
          "story_comment": "story_comments"}


@bp.get("/reports")
@moderator_or_admin
def reports():
    status = request.args.get("status", "open")
    rows = sb.select("reports", order="created_at", status=status, limit=100)
    reporters = sb.by_id("profiles", [r["reporter_id"] for r in rows])
    for r in rows:
        r["reporter"] = reporters.get(r["reporter_id"])
        t = sb.get(TABLES[r["target_type"]], id=r["target_id"])
        r["target"] = t
        r["preview"] = (t or {}).get("caption") or (t or {}).get("body") or (t or {}).get("title") or (t or {}).get("username") or "(content removed)"
    return render_template("admin/reports.html", rows=rows, status=status)


@bp.post("/reports/<rid>/<decision>")
@moderator_or_admin
def report_decide(rid, decision):
    r = sb.get("reports", id=rid)
    if not r or decision not in ("dismiss", "remove", "suspend"):
        abort(404)
    note = clean_text(request.form.get("note"), 300)
    table = TABLES[r["target_type"]]
    if decision == "remove" and r["target_type"] != "profile":
        sb.update(table, {"moderation_status": "removed"}, id=r["target_id"])
        target = sb.get(table, id=r["target_id"])
        owner = (target or {}).get("author_id") or (target or {}).get("sender_id") or (target or {}).get("host_id")
        notify(owner, "moderation", body="One of your items was removed for breaking community guidelines.", dedupe=False)
    elif decision == "suspend":
        if g.user.get("role") != "admin":
            abort(403)
        uid = r["target_id"] if r["target_type"] == "profile" else (sb.get(table, id=r["target_id"]) or {}).get("author_id")
        t = sb.get("profiles", id=uid) if uid else None
        if t and t["id"] != g.user["id"]:
            sb.update("profiles", {"account_status": "suspended", "status_note": note or None, "session_epoch": t.get("session_epoch", 0) + 1}, id=uid)
    sb.update("reports", {"status": "dismissed" if decision == "dismiss" else "actioned", "resolved_by": g.user["id"],
                          "resolution_note": note or None, "resolved_at": iso()}, id=rid)
    audit(g.user["id"], f"report_{decision}", r["target_type"], r["target_id"], report=rid, note=note)
    flash("Report resolved.", "success")
    return redirect(url_for("admin.reports"))


@bp.get("/media")
@moderator_or_admin
def media():
    rows = db().table("posts").select("*").order("created_at", desc=True).limit(40).execute().data or []
    items = [p for p in social.hydrate_posts(rows, g.user["id"]) if p["media"]]
    return render_template("admin/media.html", items=items)


@bp.post("/posts/<pid>/moderate")
@moderator_or_admin
def moderate_post(pid):
    status = request.form.get("status")
    if status not in ("ok", "flagged", "removed") or not sb.get("posts", id=pid):
        abort(400)
    sb.update("posts", {"moderation_status": status}, id=pid)
    audit(g.user["id"], f"post_{status}", "post", pid, note=clean_text(request.form.get("note"), 200))
    return _back("admin.media")


# ------------------------------------------------------------------ payments (manual mobile money)
@bp.get("/payments")
@admin_required
def payments():
    pending = sb.select("payer_numbers", order="created_at", status="pending")
    approved = sb.select("payer_numbers", order="created_at", status="approved", limit=100)
    users = sb.by_id("profiles", [p["user_id"] for p in pending + approved])
    for p in pending + approved:
        p["user"] = users.get(p["user_id"])
    recent = sb.select("payments", order="created_at", limit=30)
    ru = sb.by_id("profiles", [p["user_id"] for p in recent])
    for p in recent:
        p["user"] = ru.get(p["user_id"])
    cfg = current_app.config
    return render_template("admin/payments.html", pending=pending, approved=approved, recent=recent, enabled=cfg["PAID_COMPETITIONS_ENABLED"],
                           plans=cfg["BOOST_PLANS"], fee=cfg["TRIVIA_ENTRY_FEE_UGX"])


@bp.post("/payments/numbers/<nid>/<decision>")
@admin_required
def payer_decide(nid, decision):
    n = sb.get("payer_numbers", id=nid)
    if not n or decision not in ("approve", "reject", "revoke"):
        abort(404)
    status = {"approve": "approved", "reject": "rejected", "revoke": "revoked"}[decision]
    sb.update("payer_numbers", {"status": status, "reviewed_by": g.user["id"], "reviewed_at": iso(), "note": clean_text(request.form.get("note"), 200) or None}, id=nid)
    notify(n["user_id"], "moderation", body=f"Your payment number was {status}.", dedupe=False)
    audit(g.user["id"], f"payer_number_{status}", "payer_number", nid)
    flash(f"Number {status}.", "success")
    return redirect(url_for("admin.payments"))


@bp.post("/payments/record")
@admin_required
def record_payment():
    """Admin saw money arrive from a verified number: record it once (by mobile-money reference) and grant what was paid for."""
    cfg = current_app.config
    if not cfg["PAID_COMPETITIONS_ENABLED"]:
        abort(403)
    number = sb.get("payer_numbers", id=request.form.get("payer_number_id", ""))
    purpose = request.form.get("purpose")
    ref = clean_text(request.form.get("provider_reference"), 60)
    try:
        amount = int(request.form.get("amount_ugx", "0"))
    except ValueError:
        amount = 0
    if not number or number["status"] != "approved":
        flash("Choose an approved number.", "error")
    elif purpose not in ("trivia_entry", "boost_3d", "boost_5d") or amount <= 0 or len(ref) < 4:
        flash("Enter the purpose, the amount and the mobile-money transaction ID.", "error")
    elif sb.get("payments", provider_reference=ref):
        flash("That transaction ID was already recorded. Nothing was credited twice.", "error")
    else:
        expected = cfg["TRIVIA_ENTRY_FEE_UGX"] if purpose == "trivia_entry" else cfg["BOOST_PLANS"][purpose][0]
        if amount < expected or (purpose != "trivia_entry" and amount != expected):
            flash(f"Amount doesn't match: expected UGX {expected:,}" + ("+ (multiples of the entry fee)" if purpose == "trivia_entry" else "") + ".", "error")
        else:
            pay = sb.insert("payments", {"user_id": number["user_id"], "payer_number_id": number["id"], "amount_ugx": amount, "purpose": purpose,
                                         "provider_reference": ref, "recorded_by": g.user["id"], "note": clean_text(request.form.get("note"), 200) or None})
            if purpose == "trivia_entry":
                for _ in range(max(1, amount // cfg["TRIVIA_ENTRY_FEE_UGX"])):
                    sb.insert("trivia_entry_credits", {"user_id": number["user_id"], "payment_id": pay["id"]})
                msg = "Your trivia entry payment was received. You can play now!"
            else:
                days = cfg["BOOST_PLANS"][purpose][1]
                end = coin_service.grant_boost(number["user_id"], days, "payment", pay["id"])
                social.grant_achievement(number["user_id"], "booster")
                msg = f"Payment received. Your account is boosted for {days} days."
            notify(number["user_id"], "account", body=msg, dedupe=False)
            audit(g.user["id"], "payment_recorded", "payment", pay["id"], purpose=purpose, amount=amount)
            flash("Payment recorded and credited.", "success")
    return redirect(url_for("admin.payments"))


# ------------------------------------------------------------------ trivia authoring
TRIVIA_CSV_REQUIRED = {"question", "option_a", "option_b", "option_c", "option_d", "correct_answer"}
TRIVIA_CSV_OPTIONAL = {"time_limit_seconds"}
TRIVIA_CSV_MAX_BYTES = 2 * 1024 * 1024
TRIVIA_CSV_MAX_QUESTIONS = 500
TRIVIA_PDF_MAX_PAGES = 100
TRIVIA_PDF_MAX_TEXT = 2 * 1024 * 1024
TRIVIA_PDF_QUESTION = re.compile(
    r"^(?:question\s*(?:#\s*)?(?P<question_number>\d+)?\s*[:.)\-]?\s*"
    r"|q(?:uestion)?\s*#?\s*(?P<q_number>\d+)\s*[:.)\-]\s*"
    r"|(?P<number>\d{1,3})\s*[.)\-:]\s+)(?P<prompt>.*)$",
    re.IGNORECASE,
)
TRIVIA_PDF_OPTION = re.compile(r"^\(?([A-D])\)?\s*[.)\-:]\s+(.+)$", re.IGNORECASE)
TRIVIA_PDF_INLINE_OPTION = re.compile(r"(?<!\S)\(?([A-D])\)?\s*[.)\-:]\s+", re.IGNORECASE)
TRIVIA_PDF_ANSWER = re.compile(r"^(?:(?:correct\s+)?answer|correct)\s*[:\-]\s*(.+)$", re.IGNORECASE)
TRIVIA_PDF_ANSWER_KEY = re.compile(r"^(?:answer\s*key|answers|solutions)\s*:?\s*(.*)$", re.IGNORECASE)
TRIVIA_PDF_ANSWER_KEY_ROW = re.compile(r"(?<!\w)(?:question\s*|q\s*)?#?\s*(\d{1,3})\s*[.)\-:]?\s*([A-D])\b", re.IGNORECASE)
TRIVIA_PDF_TIME = re.compile(r"^time(?:\s+limit)?(?:\s*\(s\))?\s*[:\-]\s*(\d+)(?:\s*(?:s|sec|seconds))?$", re.IGNORECASE)


def _validate_trivia_rows(rows, issues=None):
    issues = issues or []
    questions = []
    for row_number, row in rows:
        prompt = clean_text(row.get("question"), TRIVIA_CSV_MAX_BYTES)
        options = [clean_text(row.get(f"option_{letter}"), TRIVIA_CSV_MAX_BYTES) for letter in "abcd"]
        answer = clean_text(row.get("correct_answer"), TRIVIA_CSV_MAX_BYTES)
        limit_value = clean_text(row.get("time_limit_seconds"), TRIVIA_CSV_MAX_BYTES)
        try:
            limit = int(limit_value or "20")
        except ValueError:
            limit = 0

        problems = []
        if not 3 <= len(prompt) <= 300:
            problems.append("question must be 3-300 characters")
        if any(not option or len(option) > 120 for option in options):
            problems.append("all four options are required and must be 120 characters or fewer")
        if len({option.casefold() for option in options}) != 4:
            problems.append("the four options must be different")
        if not 5 <= limit <= 120:
            problems.append("time_limit_seconds must be 5-120")

        answer_letter = answer.upper()
        if len(answer_letter) == 1 and answer_letter in "ABCD":
            correct_index = ord(answer_letter) - ord("A")
        else:
            matches = [i for i, option in enumerate(options) if option.casefold() == answer.casefold()]
            correct_index = matches[0] if len(matches) == 1 else -1
        if not 0 <= correct_index <= 3:
            problems.append("correct_answer must be A, B, C, D, or match one option exactly")

        if problems:
            issues.append(f"{row_number}: " + ", ".join(problems) + ".")
        else:
            questions.append({"prompt": prompt, "options": options, "correct_index": correct_index, "time_limit_seconds": limit})

    if issues:
        return None, "Nothing was imported. " + " ".join(issues[:5])
    if not questions:
        return None, "The file has no question rows to import."
    if len(questions) > TRIVIA_CSV_MAX_QUESTIONS:
        return None, f"A set can contain at most {TRIVIA_CSV_MAX_QUESTIONS} questions."
    return questions, None


def _parse_trivia_csv(raw):
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None, "Save the CSV as UTF-8 and try again."

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return None, "The CSV is empty or missing its header row."
    headers = [header.strip().lower() for header in reader.fieldnames]
    if len(headers) != len(set(headers)):
        return None, "The CSV contains duplicate column names."
    missing = TRIVIA_CSV_REQUIRED - set(headers)
    extra = set(headers) - TRIVIA_CSV_REQUIRED - TRIVIA_CSV_OPTIONAL
    if missing or extra:
        details = []
        if missing:
            details.append("missing " + ", ".join(sorted(missing)))
        if extra:
            details.append("unknown " + ", ".join(sorted(extra)))
        return None, "Check the CSV headers: " + "; ".join(details) + "."
    reader.fieldnames = headers

    rows, issues, rows_seen = [], [], 0
    try:
        for row in reader:
            if None in row:
                issues.append(f"Row {reader.line_num}: has more values than headers.")
                continue
            if not any(clean_text(value, 500) for value in row.values()):
                continue
            rows_seen += 1
            if rows_seen > TRIVIA_CSV_MAX_QUESTIONS:
                return None, f"A set can contain at most {TRIVIA_CSV_MAX_QUESTIONS} questions."
            rows.append((f"Row {reader.line_num}", row))
    except csv.Error:
        return None, "The CSV formatting is invalid. Check quotes and commas, then try again."
    return _validate_trivia_rows(rows, issues)


def _parse_trivia_pdf(raw):
    try:
        reader = PdfReader(io.BytesIO(raw), strict=False)
        if reader.is_encrypted:
            return None, "Encrypted PDFs aren't supported. Upload an unlocked PDF."
        if len(reader.pages) > TRIVIA_PDF_MAX_PAGES:
            return None, f"The PDF can contain at most {TRIVIA_PDF_MAX_PAGES} pages."
        text_parts, text_size = [], 0
        for page in reader.pages:
            page_text = page.extract_text() or ""
            text_size += len(page_text)
            if text_size > TRIVIA_PDF_MAX_TEXT:
                return None, "The extracted PDF text is too large to import."
            text_parts.append(page_text)
    except Exception:
        return None, "Couldn't read this PDF. Upload a valid, unlocked PDF and try again."

    text = "\n".join(text_parts)
    if not text.strip():
        return None, ("This PDF has no selectable text. Scanned PDFs are not supported yet; "
                      "run OCR to make the PDF text selectable, then upload it again.")

    return _parse_trivia_text(text, "PDF")


def _parse_trivia_docx(raw):
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            document = archive.getinfo("word/document.xml")
            if document.file_size > TRIVIA_PDF_MAX_TEXT:
                return None, "The extracted Word document text is too large to import."
            with archive.open(document) as xml_file:
                xml = xml_file.read(TRIVIA_PDF_MAX_TEXT + 1)
            if len(xml) > TRIVIA_PDF_MAX_TEXT:
                return None, "The extracted Word document text is too large to import."
            try:
                numbering = archive.getinfo("word/numbering.xml")
            except KeyError:
                numbering_xml = None
            else:
                if numbering.file_size > TRIVIA_PDF_MAX_TEXT:
                    return None, "The extracted Word document text is too large to import."
                with archive.open(numbering) as xml_file:
                    numbering_xml = xml_file.read(TRIVIA_PDF_MAX_TEXT + 1)
                if len(numbering_xml) > TRIVIA_PDF_MAX_TEXT:
                    return None, "The extracted Word document text is too large to import."
    except (zipfile.BadZipFile, KeyError, RuntimeError, OSError):
        return None, "Couldn't read this Word document. Upload a valid .docx file and try again."

    if re.search(rb"<!DOCTYPE|<!ENTITY", xml, re.IGNORECASE) or (
        numbering_xml and re.search(rb"<!DOCTYPE|<!ENTITY", numbering_xml, re.IGNORECASE)
    ):
        return None, "This Word document contains unsupported XML declarations."
    try:
        root = ET.fromstring(xml)
        numbering_root = ET.fromstring(numbering_xml) if numbering_xml else None
    except ET.ParseError:
        return None, "Couldn't read this Word document. Upload a valid .docx file and try again."

    namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

    def word_int(element, attribute, default):
        try:
            return int(element.get(f"{namespace}{attribute}", str(default)))
        except (AttributeError, TypeError, ValueError):
            return default

    numbering_formats = {}
    num_to_abstract = {}
    if numbering_root is not None:
        for abstract in numbering_root.findall(f"{namespace}abstractNum"):
            abstract_id = abstract.get(f"{namespace}abstractNumId")
            for level in abstract.findall(f"{namespace}lvl"):
                level_id = word_int(level, "ilvl", 0)
                number_format = level.find(f"{namespace}numFmt")
                level_text = level.find(f"{namespace}lvlText")
                start = level.find(f"{namespace}start")
                if number_format is not None and level_text is not None:
                    numbering_formats[(abstract_id, level_id)] = (
                        number_format.get(f"{namespace}val", ""),
                        level_text.get(f"{namespace}val", ""),
                        word_int(start, "val", 1),
                    )
        for num in numbering_root.findall(f"{namespace}num"):
            num_id = num.get(f"{namespace}numId")
            abstract_id = num.find(f"{namespace}abstractNumId")
            if abstract_id is not None:
                num_to_abstract[num_id] = abstract_id.get(f"{namespace}val")

    def format_number(value, number_format):
        if number_format in ("upperLetter", "lowerLetter"):
            letter = ""
            while value:
                value, remainder = divmod(value - 1, 26)
                letter = chr(ord("A") + remainder) + letter
            return letter.upper() if number_format == "upperLetter" else letter.lower()
        if number_format in ("upperRoman", "lowerRoman"):
            numerals = ((1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"),
                        (90, "XC"), (50, "L"), (40, "XL"), (10, "X"), (9, "IX"),
                        (5, "V"), (4, "IV"), (1, "I"))
            numeral = ""
            for amount, symbol in numerals:
                count, value = divmod(value, amount)
                numeral += symbol * count
            return numeral.upper() if number_format == "upperRoman" else numeral.lower()
        return str(value)

    counters = {}
    paragraphs = []
    for paragraph in root.iter(f"{namespace}p"):
        parts = []
        for node in paragraph.iter():
            if node.tag == f"{namespace}t" and node.text:
                parts.append(node.text)
            elif node.tag == f"{namespace}tab":
                parts.append(" ")
            elif node.tag == f"{namespace}br":
                parts.append("\n")
        paragraph_text = "".join(parts)
        num_pr = paragraph.find(f"{namespace}pPr/{namespace}numPr")
        if num_pr is not None:
            num_id = num_pr.find(f"{namespace}numId")
            level_id = num_pr.find(f"{namespace}ilvl")
            num_id = num_id.get(f"{namespace}val") if num_id is not None else None
            level_id = word_int(level_id, "val", 0)
            abstract_id = num_to_abstract.get(num_id)
            numbering = numbering_formats.get((abstract_id, level_id))
            if numbering and not TRIVIA_PDF_QUESTION.match(paragraph_text.strip()):
                number_format, level_text, start = numbering
                if number_format != "bullet":
                    key = (num_id, level_id)
                    counters[key] = counters.get(key, start - 1) + 1
                    for deeper_level in range(level_id + 1, 9):
                        counters.pop((num_id, deeper_level), None)
                    prefix = level_text
                    for index in range(level_id + 1):
                        value = counters.get((num_id, index), 1)
                        prefix = prefix.replace(f"%{index + 1}", format_number(value, numbering_formats.get((abstract_id, index), ("decimal", "", 1))[0]))
                    paragraph_text = f"{prefix} {paragraph_text}".strip()
        paragraphs.append(paragraph_text)
    text = "\n".join(paragraphs)
    if len(text) > TRIVIA_PDF_MAX_TEXT:
        return None, "The extracted Word document text is too large to import."
    if not text.strip():
        return None, "This Word document contains no readable text to import."
    return _parse_trivia_text(text, "Word document")


def _parse_trivia_text(text, source):
    rows, issues, question = [], [], None
    answer_key = {}
    in_answer_key = False

    def save_question():
        if question:
            question_number = question["number"]
            answer = question["answer"] or answer_key.get(question_number, "")
            rows.append((f"Question on line {question['line']}", {
                "question": question["prompt"],
                **{f"option_{letter.lower()}": question["options"].get(letter, "") for letter in "ABCD"},
                "correct_answer": answer,
                "time_limit_seconds": question["time_limit"],
                "_question_number": question_number,
            }))

    def add_options(line, line_number):
        markers = list(TRIVIA_PDF_INLINE_OPTION.finditer(line))
        if not markers or (len(markers) == 1 and markers[0].start() != 0):
            return False
        if len(markers) == 1:
            match = TRIVIA_PDF_OPTION.match(line)
            if not match:
                return False
            markers = [match]
            values = [(match.group(1).upper(), match.group(2).strip())]
        else:
            values = []
            for index, marker in enumerate(markers):
                start = marker.end()
                end = markers[index + 1].start() if index + 1 < len(markers) else len(line)
                values.append((marker.group(1).upper(), line[start:end].strip()))
        for letter, option in values:
            if letter in question["options"]:
                issues.append(f"{source} line {line_number}: duplicate option {letter}.")
            elif option:
                question["options"][letter] = option
        return True

    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip().replace("\u00a0", " ")
        if not line or re.fullmatch(r"(?:page\s+)?\d+(?:\s+of\s+\d+)?", line, re.IGNORECASE):
            continue
        key_heading = TRIVIA_PDF_ANSWER_KEY.match(line)
        if key_heading:
            save_question()
            question = None
            in_answer_key = True
            for match in TRIVIA_PDF_ANSWER_KEY_ROW.finditer(key_heading.group(1)):
                answer_key[int(match.group(1))] = match.group(2).upper()
            continue
        if in_answer_key:
            for match in TRIVIA_PDF_ANSWER_KEY_ROW.finditer(line):
                answer_key[int(match.group(1))] = match.group(2).upper()
            continue
        match = TRIVIA_PDF_QUESTION.match(line)
        if match:
            save_question()
            number = next((match.group(key) for key in ("question_number", "q_number", "number") if match.group(key)), None)
            question = {"line": line_number, "number": int(number) if number else len(rows) + 1,
                        "prompt": match.group("prompt").strip(), "options": {}, "answer": "", "time_limit": "20"}
            if question["prompt"]:
                add_options(question["prompt"], line_number)
                if question["options"]:
                    first_option = TRIVIA_PDF_INLINE_OPTION.search(question["prompt"])
                    if first_option:
                        question["prompt"] = question["prompt"][:first_option.start()].strip()
            continue
        if not question:
            continue

        if add_options(line, line_number):
            continue
        match = TRIVIA_PDF_ANSWER.match(line)
        if match:
            if question["answer"]:
                issues.append(f"{source} line {line_number}: duplicate answer.")
            else:
                question["answer"] = match.group(1).strip()
            continue
        match = TRIVIA_PDF_TIME.match(line)
        if match:
            question["time_limit"] = match.group(1)
            continue

        if not question["options"] and not question["answer"]:
            question["prompt"] = f"{question['prompt']} {line}".strip()
        elif question["options"] and not question["answer"]:
            last_option = next(reversed(question["options"]))
            question["options"][last_option] += f" {line}"
        elif question["answer"]:
            question["answer"] += f" {line}"

    save_question()
    if issues:
        return None, "Nothing was imported. " + " ".join(issues[:5])
    if not rows:
        return None, ("No questions could be identified. Use numbered questions (for example, "
                      "'1. Question text') or 'Question 1:', followed by A-D options. "
                      "Include an 'Answer: B' or an 'Answer key' section.")
    for _, row in rows:
        question_number = row.pop("_question_number")
        if not row["correct_answer"]:
            row["correct_answer"] = answer_key.get(question_number, "")
    return _validate_trivia_rows(rows)


def _parse_trivia_upload(file):
    if not file or not file.filename:
        return None, "Choose a CSV, PDF, or Word document."
    extension = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if extension not in ("csv", "pdf", "docx"):
        return None, "Upload a .csv, .pdf, or .docx file."
    raw = file.stream.read(TRIVIA_CSV_MAX_BYTES + 1)
    if len(raw) > TRIVIA_CSV_MAX_BYTES:
        return None, "The file must be 2 MB or smaller."
    if extension == "pdf":
        return _parse_trivia_pdf(raw)
    if extension == "docx":
        return _parse_trivia_docx(raw)
    return _parse_trivia_csv(raw)


@bp.route("/trivia", methods=["GET", "POST"])
@admin_required
def trivia():
    if request.method == "POST":
        title = clean_text(request.form.get("title"), 100)
        if len(title) < 3:
            flash("Give the set a title.", "error")
        else:
            s = sb.insert("trivia_sets", {"title": title, "description": clean_text(request.form.get("description"), 500) or None, "created_by": g.user["id"]})
            audit(g.user["id"], "trivia_set_created", "trivia_set", s["id"])
            return redirect(url_for("admin.trivia_edit", set_id=s["id"]))
    sets = sb.select("trivia_sets", order="created_at")
    for s in sets:
        s["q_count"] = sb.count("trivia_questions", set_id=s["id"])
        s["attempts"] = sb.count("trivia_attempts", set_id=s["id"])
    return render_template("admin/trivia.html", sets=sets)


@bp.get("/trivia/<set_id>")
@admin_required
def trivia_edit(set_id):
    s = sb.get("trivia_sets", id=set_id)
    if not s:
        abort(404)
    return render_template("admin/trivia_edit.html", s=s, questions=sb.select("trivia_questions", order="position", desc=False, set_id=set_id))


@bp.post("/trivia/<set_id>/questions")
@admin_required
def trivia_add_question(set_id):
    s = sb.get("trivia_sets", id=set_id)
    if not s:
        abort(404)
    if s["status"] != "draft":
        flash("Unpublish (set back to draft) before editing questions.", "error")
        return redirect(url_for("admin.trivia_edit", set_id=set_id))
    f = request.form
    options = [clean_text(f.get(f"option{i}"), 120) for i in range(4)]
    prompt = clean_text(f.get("prompt"), 300)
    try:
        correct = int(f.get("correct", "-1"))
        limit = int(f.get("time_limit_seconds", "20"))
    except ValueError:
        correct, limit = -1, 20
    if len(prompt) < 3 or not all(options) or not 0 <= correct <= 3 or not 5 <= limit <= 120 or len(set(o.lower() for o in options)) < 4:
        flash("Enter the question, four different options, the correct answer and a time limit (5-120s).", "error")
    else:
        pos = sb.count("trivia_questions", set_id=set_id) + 1
        sb.insert("trivia_questions", {"set_id": set_id, "position": pos, "prompt": prompt, "options": options, "correct_index": correct, "time_limit_seconds": limit})
        flash("Question added.", "success")
    return redirect(url_for("admin.trivia_edit", set_id=set_id))


@bp.post("/trivia/<set_id>/questions/import")
@admin_required
def trivia_import_questions(set_id):
    s = sb.get("trivia_sets", id=set_id)
    if not s:
        abort(404)
    if s["status"] != "draft":
        flash("Only draft sets can receive imported questions.", "error")
        return redirect(url_for("admin.trivia_edit", set_id=set_id))

    questions, error = _parse_trivia_upload(request.files.get("questions_file"))
    if error:
        flash(error, "error")
        return redirect(url_for("admin.trivia_edit", set_id=set_id))

    first_position = sb.count("trivia_questions", set_id=set_id) + 1
    rows = [{"set_id": set_id, "position": first_position + i, **question} for i, question in enumerate(questions)]
    try:
        db().table("trivia_questions").insert(rows).execute()
    except Exception:
        current_app.logger.exception("trivia import failed")
        flash("The questions could not be saved. Nothing was imported; please try again.", "error")
        return redirect(url_for("admin.trivia_edit", set_id=set_id))

    audit(g.user["id"], "trivia_questions_imported", "trivia_set", set_id, count=len(rows))
    flash(f"Imported {len(rows)} questions. Review them before publishing.", "success")
    return redirect(url_for("admin.trivia_edit", set_id=set_id))


@bp.post("/trivia/<set_id>/questions/<qid>/delete")
@admin_required
def trivia_delete_question(set_id, qid):
    s = sb.get("trivia_sets", id=set_id)
    if not s or s["status"] != "draft":
        flash("Only draft sets can be edited.", "error")
        return redirect(url_for("admin.trivia_edit", set_id=set_id))
    sb.delete("trivia_questions", id=qid, set_id=set_id)
    for i, q in enumerate(sb.select("trivia_questions", order="position", desc=False, set_id=set_id), start=1):
        if q["position"] != i:
            sb.update("trivia_questions", {"position": i + 1000}, id=q["id"])   # avoid unique clashes while renumbering
    for i, q in enumerate(sb.select("trivia_questions", order="position", desc=False, set_id=set_id), start=1):
        sb.update("trivia_questions", {"position": i}, id=q["id"])
    return redirect(url_for("admin.trivia_edit", set_id=set_id))


@bp.post("/trivia/<set_id>/status/<status>")
@admin_required
def trivia_status(set_id, status):
    s = sb.get("trivia_sets", id=set_id)
    if not s or status not in ("draft", "published", "closed"):
        abort(404)
    if status == "published" and sb.count("trivia_questions", set_id=set_id) < 1:
        flash("Add at least one question before publishing.", "error")
    elif status == "draft" and sb.count("trivia_attempts", set_id=set_id) > 0:
        flash("This set already has attempts, so it can't go back to draft. Close it instead.", "error")
    else:
        sb.update("trivia_sets", {"status": status, "published_at": iso() if status == "published" else s.get("published_at")}, id=set_id)
        audit(g.user["id"], f"trivia_{status}", "trivia_set", set_id)
        flash(f"Set is now {status}.", "success")
    return redirect(url_for("admin.trivia_edit", set_id=set_id))


# ------------------------------------------------------------------ flags, audit, cron
@bp.route("/flags", methods=["GET", "POST"])
@admin_required
def flags():
    if request.method == "POST":
        key = request.form.get("key")
        f = sb.get("feature_flags", key=key)
        if not f:
            abort(404)
        sb.update("feature_flags", {"enabled": not f["enabled"], "updated_by": g.user["id"], "updated_at": iso()}, key=key)
        audit(g.user["id"], "feature_flag_toggled", "feature_flag", key, enabled=not f["enabled"])
        return redirect(url_for("admin.flags"))
    return render_template("admin/flags.html", flags=sb.select("feature_flags", order="key", desc=False),
                           paid=current_app.config["PAID_COMPETITIONS_ENABLED"])


@bp.get("/audit")
@admin_required
def audit_log():
    rows = sb.select("audit_logs", order="created_at", limit=150)
    actors = sb.by_id("profiles", [r.get("actor_id") for r in rows])
    for r in rows:
        r["actor"] = actors.get(r.get("actor_id"))
    return render_template("admin/audit.html", rows=rows)


@bp.post("/internal/cleanup")
@csrf.exempt
def cleanup():
    """Called by a Render cron job: `curl -X POST -H "Authorization: Bearer $CRON_SECRET" .../admin/internal/cleanup`"""
    secret = current_app.config["CRON_SECRET"]
    import hmac
    if not secret or not hmac.compare_digest(request.headers.get("Authorization", ""), f"Bearer {secret}"):
        abort(403)
    from app import run_cleanup
    return jsonify(result=run_cleanup())


@bp.post("/internal/auto-approve-pending")
@csrf.exempt
def auto_approve_pending():
    secret = current_app.config["CRON_SECRET"]
    import hmac
    if not secret or not hmac.compare_digest(request.headers.get("Authorization", ""), f"Bearer {secret}"):
        abort(403)
    return jsonify(approved=auto_approve_stale_accounts())
