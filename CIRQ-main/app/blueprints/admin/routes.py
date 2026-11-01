import csv
import io

from flask import Blueprint, render_template, request, redirect, url_for, flash, g, abort, jsonify, current_app
from app.decorators import admin_required, moderator_or_admin
from app.extensions import db, csrf
from app.services import supabase_service as sb, auth_service, cloudinary_service as cs, coin_service, social
from app.services.account_service import approve_account
from app.services.audit_service import audit
from app.services.mail_service import send_mail, mail_configured
from app.services.notification_service import notify
from app.utils import iso, clean_text, new_token, hours_from_now, utcnow, valid_email

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
        "posts": c("posts", kind="post"), "reels": c("posts", kind="reel"),
        "stories_active": len(db().table("stories").select("id").gt("expires_at", iso()).execute().data or []),
        "events": c("events"), "reports_open": c("reports", status="open"),
        "resets_pending": c("password_reset_requests", status="pending"),
        "verifications_pending": c("school_verification_requests", status="pending"),
        "payers_pending": c("payer_numbers", status="pending"), "trivia_sets": c("trivia_sets"),
        "flip_rounds": c("game_rounds"),
    }
    return render_template("admin/dashboard.html", stats=stats, mail_ok=mail_configured())


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
    if target["id"] == g.user["id"] and action in ("suspend", "reject", "set_role"):
        flash("You can't do that to your own account.", "error")
        return _back("admin.users")
    if action == "approve":
        approve_account(uid, g.user["id"])
    elif action == "reject":
        sb.update("profiles", {"account_status": "rejected", "status_note": note or None}, id=uid)
    elif action == "suspend":
        sb.update("profiles", {"account_status": "suspended", "status_note": note or None,
                               "session_epoch": target.get("session_epoch", 0) + 1}, id=uid)   # kicks them out immediately
        notify(uid, "moderation", body="Your account was suspended.", dedupe=False)
    elif action == "unsuspend":
        sb.update("profiles", {"account_status": "approved", "status_note": None}, id=uid)
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
    return render_template("admin/schools.html", schools=sb.select("schools", order="name", desc=False), reqs=reqs)


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


def _parse_trivia_csv(file):
    if not file or not file.filename:
        return None, "Choose a CSV file."
    if not file.filename.lower().endswith(".csv"):
        return None, "Upload a .csv file."
    raw = file.stream.read(TRIVIA_CSV_MAX_BYTES + 1)
    if len(raw) > TRIVIA_CSV_MAX_BYTES:
        return None, "The CSV file must be 2 MB or smaller."
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

    questions, issues, rows_seen = [], [], 0
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

            prompt = clean_text(row.get("question"), 300)
            options = [clean_text(row.get(f"option_{letter}"), 120) for letter in "abcd"]
            answer = clean_text(row.get("correct_answer"), 120)
            try:
                limit = int(clean_text(row.get("time_limit_seconds"), 3) or "20")
            except ValueError:
                limit = 0

            problems = []
            if len(prompt) < 3:
                problems.append("question must be 3-300 characters")
            if not all(options):
                problems.append("all four options are required")
            if len({option.casefold() for option in options}) != 4:
                problems.append("the four options must be different")
            if not 5 <= limit <= 120:
                problems.append("time_limit_seconds must be 5-120")

            answer_letter = answer.upper()
            if answer_letter in "ABCD" and len(answer_letter) == 1:
                correct_index = ord(answer_letter) - ord("A")
            else:
                matches = [i for i, option in enumerate(options) if option.casefold() == answer.casefold()]
                correct_index = matches[0] if len(matches) == 1 else -1
            if not 0 <= correct_index <= 3:
                problems.append("correct_answer must be A, B, C, D, or match one option exactly")

            if problems:
                issues.append(f"Row {reader.line_num}: " + ", ".join(problems) + ".")
            else:
                questions.append({"prompt": prompt, "options": options, "correct_index": correct_index, "time_limit_seconds": limit})
    except csv.Error:
        return None, "The CSV formatting is invalid. Check quotes and commas, then try again."

    if issues:
        return None, "Nothing was imported. " + " ".join(issues[:5])
    if not questions:
        return None, "The CSV has no question rows to import."
    return questions, None


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

    questions, error = _parse_trivia_csv(request.files.get("questions_csv"))
    if error:
        flash(error, "error")
        return redirect(url_for("admin.trivia_edit", set_id=set_id))

    first_position = sb.count("trivia_questions", set_id=set_id) + 1
    rows = [{"set_id": set_id, "position": first_position + i, **question} for i, question in enumerate(questions)]
    try:
        db().table("trivia_questions").insert(rows).execute()
    except Exception:
        current_app.logger.exception("trivia CSV import failed")
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
