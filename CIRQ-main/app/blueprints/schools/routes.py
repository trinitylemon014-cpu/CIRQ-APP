from flask import Blueprint, render_template, request, redirect, url_for, flash, g, abort
from app.decorators import login_required
from app.extensions import limiter, db
from app.services import supabase_service as sb, social
from app.services.moderation_service import check_text
from app.services.notification_service import notify
from app.utils import clean_text, iso

bp = Blueprint("schools", __name__, url_prefix="/schools")


def community_for(school):
    c = sb.get("communities", school_id=school["id"])
    return c or sb.insert("communities", {"school_id": school["id"], "name": f"{school['name']} Community", "description": school.get("about")})


def _school_or_404(slug):
    s = sb.get("schools", slug=slug)
    if not s:
        abort(404)
    return s


@bp.get("")
@login_required
def index():
    return render_template("schools/index.html", schools=sb.select("schools", order="name", desc=False))


@bp.get("/<slug>")
@login_required
def view(slug):
    s = _school_or_404(slug)
    comm = community_for(s)
    me = g.user
    member = sb.get("community_members", community_id=comm["id"], user_id=me["id"])
    posts = []
    if member or me.get("role") == "admin":
        rows = db().table("posts").select("*").eq("community_id", comm["id"]).eq("moderation_status", "ok").order("created_at", desc=True).limit(30).execute().data or []
        hidden = social.blocked_between(me["id"])
        posts = [p for p in social.hydrate_posts(rows, me["id"]) if p["author_id"] not in hidden]
        posts.sort(key=lambda p: (not p["is_announcement"], ))   # announcements first (stable)
    events = [e for e in (db().table("events").select("*").eq("school_id", s["id"]).eq("status", "scheduled").eq("moderation_status", "ok")
                          .gt("starts_at", iso()).order("starts_at").limit(5).execute().data or [])]
    members = sb.count("community_members", community_id=comm["id"])
    pending = sb.get("school_verification_requests", requested_by=me["id"], school_id=s["id"], status="pending")
    return render_template("schools/view.html", s=s, comm=comm, member=member, posts=posts, events=events, members=members,
                           is_mod=bool(member and member["role"] == "moderator") or me.get("role") == "admin", pending=pending)


@bp.post("/<slug>/join")
@login_required
def join(slug):
    s = _school_or_404(slug)
    comm = community_for(s)
    if not sb.get("community_members", community_id=comm["id"], user_id=g.user["id"]):
        sb.insert("community_members", {"community_id": comm["id"], "user_id": g.user["id"], "role": "member"})
        social.grant_achievement(g.user["id"], "community")
        flash(f"You joined {s['name']}.", "success")
    return redirect(url_for("schools.view", slug=slug))


@bp.post("/<slug>/leave")
@login_required
def leave(slug):
    s = _school_or_404(slug)
    comm = community_for(s)
    sb.delete("community_members", community_id=comm["id"], user_id=g.user["id"])
    chat = sb.get("conversations", community_id=comm["id"])
    if chat:
        sb.delete("conversation_members", conversation_id=chat["id"], user_id=g.user["id"])
    flash("You left the community.", "info")
    return redirect(url_for("schools.view", slug=slug))


@bp.post("/<slug>/post")
@login_required
@limiter.limit("20 per hour")
def post(slug):
    s = _school_or_404(slug)
    comm = community_for(s)
    member = sb.get("community_members", community_id=comm["id"], user_id=g.user["id"])
    if not member:
        abort(403)
    text = clean_text(request.form.get("caption"), 1000)
    ok, why = check_text(text)
    if not text or not ok:
        flash(why or "Write something first.", "error")
        return redirect(url_for("schools.view", slug=slug))
    announce = bool(request.form.get("announcement")) and (member["role"] == "moderator" or g.user.get("role") == "admin")
    p = sb.insert("posts", {"author_id": g.user["id"], "kind": "post", "caption": text, "visibility": "public",
                            "community_id": comm["id"], "is_announcement": announce})
    if announce:
        for m in sb.select("community_members", community_id=comm["id"], limit=500):
            notify(m["user_id"], "community", g.user["id"], "post", p["id"], body=f"New announcement in {s['name']}", dedupe=False)
    return redirect(url_for("schools.view", slug=slug))


@bp.post("/<slug>/chat")
@login_required
def chat(slug):
    s = _school_or_404(slug)
    comm = community_for(s)
    if not sb.get("community_members", community_id=comm["id"], user_id=g.user["id"]):
        abort(403)
    conv = sb.get("conversations", community_id=comm["id"]) or sb.insert(
        "conversations", {"kind": "group", "title": f"{s['name']} chat", "community_id": comm["id"], "created_by": g.user["id"]})
    if not sb.get("conversation_members", conversation_id=conv["id"], user_id=g.user["id"]):
        sb.insert("conversation_members", {"conversation_id": conv["id"], "user_id": g.user["id"]})
    return redirect(url_for("messages.thread", cid=conv["id"]))


@bp.post("/<slug>/verify-request")
@login_required
@limiter.limit("3 per day")
def verify_request(slug):
    """Ask admins to verify the school. Verification is a manual review, never based on popularity."""
    s = _school_or_404(slug)
    if s["verified"]:
        flash("This school is already verified.", "info")
    elif sb.get("school_verification_requests", requested_by=g.user["id"], school_id=s["id"], status="pending"):
        flash("You already have a request waiting for review.", "info")
    else:
        note = clean_text(request.form.get("evidence_note"), 500)
        if len(note) < 20:
            flash("Explain how you can prove you represent the school (e.g. official school email, staff contact).", "error")
        else:
            sb.insert("school_verification_requests", {"school_id": s["id"], "requested_by": g.user["id"], "evidence_note": note})
            flash("Request sent. An administrator will review it.", "success")
    return redirect(url_for("schools.view", slug=slug))
