from collections import Counter
from flask import Blueprint, request, redirect, url_for, flash, g, jsonify, abort, current_app
from app.decorators import login_required
from app.extensions import limiter, db
from app.services import supabase_service as sb, cloudinary_service as cs, social, messaging
from app.services.cloudinary_service import UploadError
from app.services.moderation_service import check_text
from app.services.notification_service import notify
from app.utils import iso, utcnow, parse_dt, hours_from_now, clean_text, normalize_username, time_ago

bp = Blueprint("stories", __name__, url_prefix="/stories")


def active_stories(author_id):
    """Expiry is enforced in the query itself, never only in the browser."""
    return db().table("stories").select("*").eq("author_id", author_id).gt("expires_at", iso()) \
        .eq("moderation_status", "ok").order("created_at", desc=False).execute().data or []


def _live_story(story_id):
    """The story and its author, or 404. Same rules for viewing, liking and replying:
    unexpired, not removed by moderation, author approved, and the viewer allowed to see that author's content
    (blocks and private accounts are enforced here)."""
    s = sb.get("stories", id=story_id)
    exp = parse_dt(s["expires_at"]) if s else None
    if not s or not exp or exp < utcnow() or s["moderation_status"] != "ok":
        abort(404)
    author = sb.get("profiles", id=s["author_id"])
    if not author or author["account_status"] != "approved":
        abort(404)
    if s["author_id"] != g.user["id"] and not social.can_see_profile_content(g.user, author):
        abort(404)
    return s, author


@bp.post("")
@login_required
@limiter.limit("20 per hour")
def create():
    f = request.files.get("media")
    music = request.files.get("audio")
    has_music = bool(music and music.filename)
    caption = clean_text(request.form.get("caption"), 150)
    title = clean_text(request.form.get("audio_title"), 80)
    ok, why = check_text(caption)
    if ok and title:
        ok, why = check_text(title)
    if not ok:
        flash(why, "error")
        return redirect(url_for("feed.home"))
    try:
        if has_music:
            cs.validate_audio(music)          # reject a bad music file before anything is uploaded
        info = cs.upload(f, "stories", g.user["id"])
    except UploadError as e:
        flash(str(e), "error")
        return redirect(url_for("feed.home"))
    audio = None
    if has_music:
        try:
            audio = cs.upload_audio(music, "stories", g.user["id"])
        except UploadError as e:
            cs.destroy(info["public_id"], info["media_type"], info["delivery_type"])   # don't leave the photo/video orphaned
            flash(str(e), "error")
            return redirect(url_for("feed.home"))
    sb.insert("stories", {"author_id": g.user["id"], "media_type": info["media_type"], "url": info["url"],
                          "public_id": info["public_id"], "delivery_type": info["delivery_type"], "caption": caption or None,
                          "expires_at": iso(hours_from_now(current_app.config["STORY_TTL_HOURS"])),
                          **cs.audio_fields(audio, title)})
    social.grant_achievement(g.user["id"], "storyteller")
    flash("Story shared for 24 hours.", "success")
    return redirect(url_for("feed.home"))


@bp.get("/api/<username>")
@login_required
def api(username):
    prof = sb.get("profiles", username=normalize_username(username))
    if not prof or prof["account_status"] != "approved" or not social.can_see_profile_content(g.user, prof) and prof["id"] != g.user["id"]:
        abort(404)
    if social.is_blocked(g.user["id"], prof["id"]):
        abort(404)
    me = g.user["id"]
    mine = prof["id"] == me
    items = active_stories(prof["id"])
    ids = [s["id"] for s in items]
    # One query per table for the whole set (not one per story).
    views = sb.in_("story_views", "story_id", ids)
    likes = sb.in_("story_likes", "story_id", ids)
    hidden = social.blocked_between(me)
    comment_n = Counter(r["story_id"] for r in sb.in_("story_comments", "story_id", ids)
                        if r["moderation_status"] == "ok" and r["author_id"] not in hidden)
    seen = {r["story_id"] for r in views if r["viewer_id"] == me}
    liked = {r["story_id"] for r in likes if r["user_id"] == me}
    view_n, like_n = Counter(r["story_id"] for r in views), Counter(r["story_id"] for r in likes)
    return jsonify(user={"username": prof["username"], "name": prof["display_name"], "avatar": cs.avatar_src(prof, 80)},
                   me=mine,
                   can_reply=(not mine) and social.can_message(g.user, prof),
                   stories=[{"id": s["id"], "type": s["media_type"], "src": cs.media_src(s, 900), "caption": s["caption"],
                             "seen": s["id"] in seen, "liked": s["id"] in liked, "expires_at": s["expires_at"],
                             "audio": cs.audio_src(s) or None, "audio_title": s.get("audio_title"), "comments": comment_n[s["id"]],
                             # Only the owner ever sees the numbers.
                             "views": view_n[s["id"]] if mine else None, "likes": like_n[s["id"]] if mine else None} for s in items])


@bp.post("/<story_id>/view")
@login_required
def view(story_id):
    s, _ = _live_story(story_id)
    if s["author_id"] != g.user["id"] and not sb.get("story_views", story_id=story_id, viewer_id=g.user["id"]):
        sb.insert("story_views", {"story_id": story_id, "viewer_id": g.user["id"]})
    return jsonify(ok=True)


@bp.post("/<story_id>/like")
@login_required
@limiter.limit("240 per hour")
def like(story_id):
    """Toggle a like. The owner gets one 'liked your story' alert; unliking withdraws a still-unread alert."""
    s, _ = _live_story(story_id)
    me = g.user["id"]
    if s["author_id"] == me:
        abort(400)
    if sb.get("story_likes", story_id=story_id, user_id=me):
        sb.delete("story_likes", story_id=story_id, user_id=me)
        sb.delete("notifications", user_id=s["author_id"], kind="story_like", actor_id=me, ref_id=story_id, read_at=None)
        liked = False
    else:
        sb.insert("story_likes", {"story_id": story_id, "user_id": me})
        notify(s["author_id"], "story_like", me, "story", story_id)
        liked = True
    return jsonify(liked=liked)


@bp.get("/<story_id>/insights")
@login_required
def insights(story_id):
    """Owner-only: how many people viewed / liked a story and who they are."""
    s = sb.get("stories", id=story_id)
    if not s:
        abort(404)
    if s["author_id"] != g.user["id"]:
        abort(403)
    views = sb.select("story_views", story_id=story_id, order="created_at")
    likes = sb.select("story_likes", story_id=story_id, order="created_at")
    hidden = social.blocked_between(g.user["id"])
    people = sb.by_id("profiles", [r["viewer_id"] for r in views] + [r["user_id"] for r in likes])
    liked_ids = {r["user_id"] for r in likes}

    def person(uid, at):
        p = people.get(uid)
        if not p or uid in hidden or p["account_status"] != "approved":
            return None
        return {"username": p["username"], "name": p["display_name"], "avatar": cs.avatar_src(p, 80),
                "liked": uid in liked_ids, "ago": time_ago(at)}

    likers = [x for x in (person(r["user_id"], r["created_at"]) for r in likes) if x]
    viewers = [x for x in (person(r["viewer_id"], r["created_at"]) for r in views) if x]
    return jsonify(views=len(viewers), likes=len(likers), likers=likers, viewers=viewers)


@bp.post("/<story_id>/reply")
@login_required
@limiter.limit("60 per hour")
def reply(story_id):
    """Reply to a story. It is delivered as a direct message, so it lands in the owner's inbox
    (and unread badge) exactly like a normal chat message, labelled 'Replied to your story'."""
    s, author = _live_story(story_id)
    me = g.user
    if author["id"] == me["id"]:
        abort(400)
    body = clean_text(request.form.get("body") or (request.get_json(silent=True) or {}).get("body"), 500)
    if not body:
        return jsonify(error="Write a reply first."), 400
    ok, why = check_text(body)
    if not ok:
        return jsonify(error=why), 400
    if not social.can_message(me, author):
        return jsonify(error=f"@{author['username']} has a private account. You can reply once they approve your follow request."), 403
    conv = messaging.get_or_create_dm(me["id"], author["id"])
    sb.insert("messages", {"conversation_id": conv["id"], "sender_id": me["id"], "body": body,
                           "story_id": s["id"], "story_reply": True})
    notify(author["id"], "message", me["id"], "conversation", conv["id"], body="replied to your story")
    sb.update("conversation_members", {"last_read_at": iso()}, conversation_id=conv["id"], user_id=me["id"])
    return jsonify(ok=True, conversation=url_for("messages.thread", cid=conv["id"]))


# ---------------------------------------------------------------- comments on stories (and likes on those comments)
def _comment_json(c, author, me_id, owner_id, is_admin, likes=0, liked=False):
    return {"id": c["id"], "body": c["body"], "ago": time_ago(c["created_at"]), "mine": c["author_id"] == me_id,
            "can_delete": c["author_id"] == me_id or owner_id == me_id or is_admin, "likes": likes, "liked": liked,
            "user": {"username": author["username"], "name": author["display_name"], "avatar": cs.avatar_src(author, 64)}}


@bp.get("/<story_id>/comments")
@login_required
def comments(story_id):
    """Comments on a story, visible to everyone who may see the story. Blocked people are hidden both ways."""
    s, _ = _live_story(story_id)
    me = g.user["id"]
    hidden = social.blocked_between(me)
    rows = [c for c in sb.select("story_comments", story_id=story_id, moderation_status="ok", order="created_at", desc=False, limit=200)
            if c["author_id"] not in hidden]
    people = sb.by_id("profiles", [c["author_id"] for c in rows])
    likes = sb.in_("story_comment_likes", "comment_id", [c["id"] for c in rows])
    like_n = Counter(r["comment_id"] for r in likes)
    mine = {r["comment_id"] for r in likes if r["user_id"] == me}
    admin = g.user.get("role") == "admin"
    out = [_comment_json(c, people[c["author_id"]], me, s["author_id"], admin, like_n[c["id"]], c["id"] in mine)
           for c in rows if people.get(c["author_id"]) and people[c["author_id"]]["account_status"] == "approved"]
    return jsonify(comments=out, count=len(out))


@bp.post("/<story_id>/comments")
@login_required
@limiter.limit("60 per hour")
def add_comment(story_id):
    s, _ = _live_story(story_id)
    me = g.user
    body = clean_text(request.form.get("body") or (request.get_json(silent=True) or {}).get("body"), 500)
    if not body:
        return jsonify(error="Write a comment first."), 400
    ok, why = check_text(body)
    if not ok:
        return jsonify(error=why), 400
    c = sb.insert("story_comments", {"story_id": story_id, "author_id": me["id"], "body": body})
    notify(s["author_id"], "story_comment", me["id"], "story", story_id, dedupe=False)
    return jsonify(comment=_comment_json(c, me, me["id"], s["author_id"], me.get("role") == "admin"))


def _live_comment(cid):
    """A visible story comment and its (still live) story. Same visibility rules as the story itself."""
    c = sb.get("story_comments", id=cid)
    if not c or c["moderation_status"] != "ok":
        abort(404)
    s, _ = _live_story(c["story_id"])
    if social.is_blocked(g.user["id"], c["author_id"]):
        abort(404)
    return c, s


@bp.post("/comments/<cid>/like")
@login_required
@limiter.limit("240 per hour")
def like_comment(cid):
    c, s = _live_comment(cid)
    me = g.user["id"]
    if sb.get("story_comment_likes", comment_id=cid, user_id=me):
        sb.delete("story_comment_likes", comment_id=cid, user_id=me)
        liked = False
    else:
        sb.insert("story_comment_likes", {"comment_id": cid, "user_id": me})
        notify(c["author_id"], "comment_like", me, "story", s["id"])
        liked = True
    return jsonify(liked=liked, count=sb.count("story_comment_likes", comment_id=cid))


@bp.post("/comments/<cid>/delete")
@login_required
def delete_comment(cid):
    """The commenter, the story's owner and admins can remove a comment."""
    c = sb.get("story_comments", id=cid)
    if not c:
        abort(404)
    s = sb.get("stories", id=c["story_id"])
    if g.user["id"] not in (c["author_id"], (s or {}).get("author_id")) and g.user.get("role") != "admin":
        abort(403)
    sb.delete("story_comments", id=cid)
    return jsonify(ok=True)


@bp.post("/<story_id>/delete")
@login_required
def delete(story_id):
    s = sb.get("stories", id=story_id)
    if not s:
        abort(404)
    if s["author_id"] != g.user["id"]:
        abort(403)
    cs.destroy(s["public_id"], s["media_type"], s.get("delivery_type", "upload"))
    cs.destroy(s.get("audio_public_id"), "audio")
    sb.delete("stories", id=story_id)
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify(ok=True)
    flash("Story deleted.", "info")
    return redirect(url_for("feed.home"))
