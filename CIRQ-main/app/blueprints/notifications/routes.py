from flask import Blueprint, render_template, redirect, url_for, g, jsonify, abort
from app.decorators import login_required
from app.services import supabase_service as sb, social
from app.services.notification_service import unread_count, mark_all_read
from app.utils import iso

bp = Blueprint("notifications", __name__)


def target_url(n):
    k, ref = n["kind"], n.get("ref_id")
    if k == "account" and n.get("ref_type") == "auto_approved_account":
        profile = sb.get("profiles", "username", id=ref) if ref else None
        return url_for("profiles.view", username=profile["username"]) if profile else url_for("admin.users", status="approved")
    if k == "follow_request":
        return url_for("profiles.follow_requests")
    if k in ("follow", "follow_accepted", "story_like") and n.get("actor_id"):
        a = sb.get("profiles", "username", id=n["actor_id"])
        return url_for("profiles.view", username=a["username"]) if a else url_for("notifications.index")
    if k in ("like", "comment") and ref:
        return url_for("feed.detail", post_id=ref)
    if k == "comment_like" and ref and n.get("ref_type") == "post":
        return url_for("feed.detail", post_id=ref)
    if k in ("story_comment", "comment_like"):      # stories live in the tray on the home page
        return url_for("feed.home")
    if k == "message" and ref:
        return url_for("messages.thread", cid=ref)
    if k in ("rsvp", "event_update") and ref:
        return url_for("linkup.detail", event_id=ref)
    return url_for("notifications.index")


@bp.get("/notifications")
@login_required
def index():
    rows = sb.select("notifications", order="created_at", limit=60, user_id=g.user["id"])
    actors = sb.by_id("profiles", [r.get("actor_id") for r in rows])
    for r in rows:
        r["actor"] = actors.get(r.get("actor_id"))
    # Instagram-style "Follow requests" entry at the top of Activity (only people the viewer hasn't hidden)
    hidden = social.blocked_between(g.user["id"])
    pending = [r for r in sb.select("follow_requests", order="created_at", target_id=g.user["id"]) if r["requester_id"] not in hidden]
    preview = list(sb.by_id("profiles", [r["requester_id"] for r in pending[:3]]).values())
    return render_template("notifications/index.html", items=rows, req_count=len(pending), req_preview=preview)


@bp.post("/notifications/read-all")
@login_required
def read_all():
    mark_all_read(g.user["id"])
    return redirect(url_for("notifications.index"))


@bp.get("/notifications/<nid>/open")
@login_required
def open_one(nid):
    n = sb.get("notifications", id=nid)
    if not n or n["user_id"] != g.user["id"]:
        abort(404)
    if not n.get("read_at"):
        sb.update("notifications", {"read_at": iso()}, id=nid)
    return redirect(target_url(n))


@bp.get("/api/notifications/count")
@login_required
def count():
    return jsonify(count=unread_count(g.user["id"]))
