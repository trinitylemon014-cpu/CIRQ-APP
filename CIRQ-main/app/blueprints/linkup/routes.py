from flask import Blueprint, render_template, request, redirect, url_for, flash, g, abort, jsonify
from app.decorators import login_required
from app.extensions import limiter, db
from app.services import supabase_service as sb, cloudinary_service as cs, social
from app.services.cloudinary_service import UploadError
from app.services.moderation_service import check_text
from app.services.notification_service import notify
from app.utils import clean_text, iso, parse_local_eat, utcnow, parse_dt

bp = Blueprint("linkup", __name__, url_prefix="/linkup")


def _visible(e, viewer):
    if viewer.get("role") in ("admin", "moderator") or e["host_id"] == viewer["id"]:
        return True
    if e["moderation_status"] != "ok" or social.is_blocked(viewer["id"], e["host_id"]):
        return False
    return e["visibility"] == "public" or (e.get("school_id") and e["school_id"] == viewer.get("school_id"))


@bp.get("")
@login_required
def index():
    tab = request.args.get("tab", "upcoming")
    me = g.user
    q = db().table("events").select("*").eq("moderation_status", "ok")
    if tab == "mine":
        going = [r["event_id"] for r in sb.select("event_rsvps", user_id=me["id"])]
        rows = {e["id"]: e for e in sb.in_("events", "id", going)}
        rows.update({e["id"]: e for e in sb.select("events", host_id=me["id"])})
        events = sorted(rows.values(), key=lambda e: e["starts_at"])
    else:
        q = q.eq("status", "scheduled").gt("starts_at", iso()).order("starts_at")
        if tab == "school" and me.get("school_id"):
            q = q.eq("school_id", me["school_id"])
        if request.args.get("q"):
            q = q.ilike("title", "%" + request.args["q"].replace("%", "")[:40] + "%")
        events = [e for e in (q.limit(40).execute().data or []) if _visible(e, me)]
    hosts = sb.by_id("profiles", [e["host_id"] for e in events])
    schools = sb.by_id("schools", [e.get("school_id") for e in events])
    for e in events:
        e["host"], e["school"] = hosts.get(e["host_id"]), schools.get(e.get("school_id"))
        e["going"] = sb.count("event_rsvps", event_id=e["id"], status="going")
    return render_template("linkup/index.html", events=events, tab=tab)


@bp.route("/new", methods=["GET", "POST"])
@login_required
@limiter.limit("10 per hour", methods=["POST"])
def new():
    if request.method == "POST":
        f = request.form
        title, desc, venue = clean_text(f.get("title"), 120), clean_text(f.get("description"), 2000), clean_text(f.get("venue"), 160)
        starts = parse_local_eat(f.get("starts_at"))
        errors = []
        if len(title) < 3:
            errors.append("Give your event a title (3+ characters).")
        if not venue:
            errors.append("Add a venue (a school or public place, not a home address).")
        if not starts or starts < utcnow():
            errors.append("Pick a start time in the future.")
        for text in (title, desc, venue):
            ok, why = check_text(text)
            if not ok:
                errors.append(why)
                break
        cap = f.get("capacity")
        capacity = int(cap) if cap and cap.isdigit() and 0 < int(cap) <= 5000 else None
        cover = request.files.get("cover")
        info = None
        if cover and cover.filename and not errors:
            try:
                info = cs.upload(cover, "events", g.user["id"], allow_video=False)
            except UploadError as e:
                errors.append(str(e))
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("linkup/new.html", form=f), 400
        ev = sb.insert("events", {"host_id": g.user["id"], "school_id": g.user.get("school_id"), "title": title, "description": desc or None,
                                  "venue": venue, "starts_at": iso(starts), "capacity": capacity,
                                  "visibility": "school" if f.get("visibility") == "school" else "public",
                                  "cover_url": info and info["url"], "cover_public_id": info and info["public_id"]})
        conv = sb.insert("conversations", {"kind": "group", "title": title[:60], "event_id": ev["id"], "created_by": g.user["id"]})
        sb.insert("conversation_members", {"conversation_id": conv["id"], "user_id": g.user["id"]})
        sb.update("events", {"conversation_id": conv["id"]}, id=ev["id"])
        sb.insert("event_rsvps", {"event_id": ev["id"], "user_id": g.user["id"], "status": "going"})
        social.grant_achievement(g.user["id"], "event_host")
        flash("Event created!", "success")
        return redirect(url_for("linkup.detail", event_id=ev["id"]))
    return render_template("linkup/new.html", form={})


@bp.get("/<event_id>")
@login_required
def detail(event_id):
    e = sb.get("events", id=event_id)
    if not e or not _visible(e, g.user):
        abort(404)
    me = g.user["id"]
    rsvp = sb.get("event_rsvps", event_id=event_id, user_id=me)
    rsvps = sb.select("event_rsvps", event_id=event_id, limit=200)
    going = [r for r in rsvps if r["status"] == "going"]
    attendees = list(sb.by_id("profiles", [r["user_id"] for r in going][:12]).values())
    return render_template("linkup/detail.html", e=e, host=sb.get("profiles", id=e["host_id"]), school=sb.get("schools", id=e["school_id"]) if e.get("school_id") else None,
                           rsvp=rsvp, going_count=len(going), interested_count=len(rsvps) - len(going), attendees=attendees,
                           is_host=e["host_id"] == me, share_url=request.url_root.rstrip("/") + url_for("linkup.detail", event_id=event_id))


@bp.post("/<event_id>/rsvp")
@login_required
@limiter.limit("60 per hour")
def rsvp(event_id):
    e = sb.get("events", id=event_id)
    if not e or not _visible(e, g.user):
        abort(404)
    if e["status"] != "scheduled":
        flash("This event is no longer open for RSVPs.", "error")
        return redirect(url_for("linkup.detail", event_id=event_id))
    choice = request.form.get("status")
    me = g.user["id"]
    if choice == "none":
        sb.delete("event_rsvps", event_id=event_id, user_id=me)
        if e.get("conversation_id") and e["host_id"] != me:
            sb.delete("conversation_members", conversation_id=e["conversation_id"], user_id=me)
        flash("RSVP removed.", "info")
    elif choice in ("going", "interested"):
        existing = sb.get("event_rsvps", event_id=event_id, user_id=me)
        if choice == "going" and e.get("capacity") and not (existing and existing["status"] == "going") \
                and sb.count("event_rsvps", event_id=event_id, status="going") >= e["capacity"]:
            flash("This event is full.", "error")
            return redirect(url_for("linkup.detail", event_id=event_id))
        sb.upsert("event_rsvps", {"event_id": event_id, "user_id": me, "status": choice}, on_conflict="event_id,user_id")
        if choice == "going" and e.get("conversation_id") and not sb.get("conversation_members", conversation_id=e["conversation_id"], user_id=me):
            sb.insert("conversation_members", {"conversation_id": e["conversation_id"], "user_id": me})
        if choice == "interested" and e.get("conversation_id") and e["host_id"] != me:
            sb.delete("conversation_members", conversation_id=e["conversation_id"], user_id=me)
        notify(e["host_id"], "rsvp", me, "event", event_id)
        social.grant_achievement(me, "joiner")
        flash("You're going!" if choice == "going" else "Marked as interested.", "success")
    return redirect(url_for("linkup.detail", event_id=event_id))


@bp.post("/<event_id>/<action>")
@login_required
def host_action(event_id, action):
    if action not in ("cancel", "complete"):
        abort(404)
    e = sb.get("events", id=event_id)
    if not e:
        abort(404)
    if e["host_id"] != g.user["id"] and g.user.get("role") != "admin":
        abort(403)
    status = "cancelled" if action == "cancel" else "completed"
    sb.update("events", {"status": status}, id=event_id)
    for r in sb.select("event_rsvps", event_id=event_id):
        notify(r["user_id"], "event_update", g.user["id"], "event", event_id,
               body=f"“{e['title']}” was {status}.", dedupe=False)
    flash(f"Event {status}.", "info")
    return redirect(url_for("linkup.detail", event_id=event_id))
