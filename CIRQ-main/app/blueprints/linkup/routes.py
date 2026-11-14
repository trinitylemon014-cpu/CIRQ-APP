from flask import Blueprint, render_template, request, redirect, url_for, flash, g, abort, jsonify
from app.decorators import login_required
from app.extensions import limiter, db
from app.services import supabase_service as sb, cloudinary_service as cs, social, event_chat
from app.services.cloudinary_service import UploadError
from app.services.moderation_service import check_text
from app.services.notification_service import notify
from app.utils import clean_text, iso, parse_local_eat, utcnow, parse_dt

bp = Blueprint("linkup", __name__, url_prefix="/linkup")


def _going_people(e, viewer):
    """Everyone who is going, host first, then in the order they joined. Blocked people are left out, and a private
    account is only listed for people who follow it (they are counted in `hidden`)."""
    rows = sb.select("event_rsvps", event_id=e["id"], status="going", order="created_at", desc=False, limit=500)
    people = sb.by_id("profiles", [r["user_id"] for r in rows])
    blocked = social.blocked_between(viewer["id"])
    following = {r["followee_id"] for r in sb.select("follows", follower_id=viewer["id"])}
    staff = viewer.get("role") in ("admin", "moderator")
    shown, hidden = [], 0
    for r in rows:
        p = people.get(r["user_id"])
        if not p or p.get("account_status") != "approved" or p["id"] in blocked:
            continue
        if p.get("is_private") and not (staff or p["id"] in (viewer["id"], e["host_id"]) or p["id"] in following):
            hidden += 1
            continue
        shown.append(p)
    shown.sort(key=lambda p: p["id"] != e["host_id"])             # host first (sort is stable)
    return shown, hidden


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
    event_chat.expire_due()
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
        event_chat.ensure_chat(ev)                     # every event gets its group chat straight away
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
    event_chat.expire_due()
    if e.get("conversation_id") and event_chat.is_over(e):         # the date has gone: the chat goes with it
        event_chat.close_event_chat(e)
    rsvp = sb.get("event_rsvps", event_id=event_id, user_id=me)
    rsvps = sb.select("event_rsvps", event_id=event_id, limit=500)
    going = [r for r in rsvps if r["status"] == "going"]
    people, hidden_going = _going_people(e, g.user)
    chat_members = sb.count("conversation_members", conversation_id=e["conversation_id"]) if e.get("conversation_id") else 0
    return render_template("linkup/detail.html", e=e, host=sb.get("profiles", id=e["host_id"]), school=sb.get("schools", id=e["school_id"]) if e.get("school_id") else None,
                           rsvp=rsvp, going_count=len(going), interested_count=len(rsvps) - len(going), people=people[:12],
                           more_going=max(0, len(people) + hidden_going - 12), chat_members=chat_members,
                           chat_closes=event_chat.expires_at_for(e),
                           is_host=e["host_id"] == me, share_url=request.url_root.rstrip("/") + url_for("linkup.detail", event_id=event_id))


@bp.get("/<event_id>/going")
@login_required
def going(event_id):
    """Full list of who is going. Anyone who can see the event can see this, so you know who you'd be joining."""
    e = sb.get("events", id=event_id)
    if not e or not _visible(e, g.user):
        abort(404)
    people, hidden = _going_people(e, g.user)
    rsvp = sb.get("event_rsvps", event_id=event_id, user_id=g.user["id"])
    school_ids = [p.get("school_id") for p in people]
    return render_template("linkup/going.html", e=e, people=people, hidden=hidden, rsvp=rsvp, schools=sb.by_id("schools", school_ids),
                           is_host=e["host_id"] == g.user["id"])


@bp.post("/<event_id>/rsvp")
@login_required
@limiter.limit("60 per hour")
def rsvp(event_id):
    e = sb.get("events", id=event_id)
    if not e or not _visible(e, g.user):
        abort(404)
    if e["status"] != "scheduled" or event_chat.is_over(e):
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
        if choice == "going":
            cid = event_chat.ensure_chat(e)                # older events without a chat get one now
            if cid and not sb.get("conversation_members", conversation_id=cid, user_id=me):
                sb.insert("conversation_members", {"conversation_id": cid, "user_id": me})
        if choice == "interested" and e.get("conversation_id") and e["host_id"] != me:
            sb.delete("conversation_members", conversation_id=e["conversation_id"], user_id=me)
        notify(e["host_id"], "rsvp", me, "event", event_id)
        social.grant_achievement(me, "joiner")
        flash("You're going! You've joined the event chat." if choice == "going" else "Marked as interested.", "success")
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
    event_chat.close_event_chat(e)                         # no event, no chat
    flash(f"Event {status}.", "info")
    return redirect(url_for("linkup.detail", event_id=event_id))
