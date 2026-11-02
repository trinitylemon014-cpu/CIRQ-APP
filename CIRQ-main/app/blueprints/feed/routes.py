from collections import Counter
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, g, abort, current_app
from app.decorators import login_required
from app.extensions import limiter
from app.services import supabase_service as sb, cloudinary_service as cs, social, recommender, boost_ad_service
from app.services.cloudinary_service import UploadError
from app.services.moderation_service import check_text
from app.services.notification_service import notify
from app.utils import iso, clean_text, utcnow, hours_from_now, parse_dt

bp = Blueprint("feed", __name__)


def _page_size():
    from flask import current_app
    return current_app.config["PAGE_SIZE"]


def feed_posts(uid, before=None):
    """Public posts + everything from people I follow (and me), newest first, cursor-paginated."""
    n = _page_size()
    following = social.following_ids(uid) + [uid]
    base = lambda q: (q.eq("kind", "post").eq("moderation_status", "ok").is_("community_id", "null")
                      .order("created_at", desc=True).limit(n + 1))
    from app.extensions import db
    q1 = base(db().table("posts").select("*").eq("visibility", "public"))
    q2 = base(db().table("posts").select("*").in_("author_id", following))
    if before:
        q1, q2 = q1.lt("created_at", before), q2.lt("created_at", before)
    merged = {p["id"]: p for p in (q1.execute().data or []) + (q2.execute().data or [])}
    rows = sorted(merged.values(), key=lambda p: p["created_at"], reverse=True)
    hidden = social.blocked_between(uid)
    has_more = len(rows) > n
    rows = rows[:n]
    cursor = rows[-1]["created_at"] if rows and has_more else None
    posts = [p for p in social.hydrate_posts(rows, uid) if p["author_id"] not in hidden
             and (p["author"]["account_status"] == "approved")
             and (p["visibility"] == "followers" and (p["author_id"] in following) or p["visibility"] == "public")]
    try:
        posts = recommender.drop_shown_suggestions(uid, posts, set(following))   # don't repeat what we showed as a suggestion
    except Exception:
        current_app.logger.exception("suggestion de-dup failed")
    return posts, cursor


def with_suggestions(user, posts):
    """Weave 'Suggested for you' posts into the feed: one after every 4th post (max 3 per page). Never breaks the feed."""
    if len(posts) < 3:
        return posts
    try:
        sugg = recommender.suggest(user["id"], user, exclude_ids={p["id"] for p in posts}, n=min(3, max(1, len(posts) // 4)))
    except Exception:
        current_app.logger.exception("suggestions failed")
        return posts
    out, i = [], 0
    for n, p in enumerate(posts, 1):
        out.append(p)
        if n % 4 == 0 and i < len(sugg):
            out.append(sugg[i]); i += 1
    return out + sugg[i:]


def story_tray(uid):
    from app.extensions import db
    ids = social.following_ids(uid) + [uid]
    rows = db().table("stories").select("*").in_("author_id", ids).gt("expires_at", iso()).eq("moderation_status", "ok") \
        .order("created_at", desc=False).execute().data or []
    hidden = social.blocked_between(uid)
    seen = {r["story_id"] for r in sb.in_("story_views", "story_id", [r["id"] for r in rows]) if r["viewer_id"] == uid}
    authors = sb.by_id("profiles", [r["author_id"] for r in rows])
    tray = {}
    for r in rows:
        if r["author_id"] in hidden or r["author_id"] not in authors:
            continue
        t = tray.setdefault(r["author_id"], {"author": authors[r["author_id"]], "count": 0, "unseen": 0})
        t["count"] += 1
        t["unseen"] += 0 if r["id"] in seen else 1
    items = sorted(tray.values(), key=lambda t: (t["author"]["id"] != uid, t["unseen"] == 0))
    return items


@bp.get("/feed")
@login_required
def home():
    before = request.args.get("before")
    posts, cursor = feed_posts(g.user["id"], before)
    posts = with_suggestions(g.user, posts)
    sponsored_ad = boost_ad_service.for_viewer(g.user["id"], before or "")
    if request.args.get("partial"):
        return render_template("partials/post_list.html", posts=posts, cursor=cursor, sponsored_ad=sponsored_ad)
    tray = story_tray(g.user["id"])
    suggestions = [p for p in sb.select("profiles", account_status="approved", school_id=g.user.get("school_id"), limit=8)
                   if p["id"] != g.user["id"] and not social.is_following(g.user["id"], p["id"])
                   and not social.has_requested(g.user["id"], p["id"])][:4]
    return render_template("feed/home.html", posts=posts, cursor=cursor, tray=tray, suggestions=suggestions,
                           sponsored_ad=sponsored_ad)


def _save_media(file, kind_folder, post_id, owner_id, allow_video=True):
    info = cs.upload(file, kind_folder, owner_id, allow_video=allow_video)
    sb.insert("post_media", {"post_id": post_id, "owner_id": owner_id, **info})
    return info


@bp.post("/posts")
@login_required
@limiter.limit("20 per hour")
def create_post():
    caption = clean_text(request.form.get("caption"), 2000)
    file = request.files.get("media")
    has_file = bool(file and file.filename)
    music = request.files.get("audio")
    has_music = bool(music and music.filename)
    title = clean_text(request.form.get("audio_title"), 80)
    if not caption and not has_file and not has_music:
        flash("Write something or add a photo, video or music.", "error")
        return redirect(url_for("feed.home"))
    ok, why = check_text(caption)
    if ok and title:
        ok, why = check_text(title)
    if not ok:
        flash(why, "error")
        return redirect(url_for("feed.home"))
    vis = "followers" if g.user.get("is_private") or request.form.get("visibility") == "followers" else "public"
    if has_file or has_music:
        try:   # validate before creating the row so nothing half-exists
            if has_file:
                cs.validate(file)
            if has_music:
                cs.validate_audio(music)
        except UploadError as e:
            flash(str(e), "error")
            return redirect(url_for("feed.home"))
    post = sb.insert("posts", {"author_id": g.user["id"], "kind": "post", "caption": caption or None, "visibility": vis,
                               "moderation_status": "ok"})
    if has_file:
        try:
            _save_media(file, "posts", post["id"], g.user["id"])
        except UploadError as e:
            sb.delete("posts", id=post["id"])
            flash(str(e), "error")
            return redirect(url_for("feed.home"))
    if has_music:
        try:
            audio = cs.upload_audio(music, "posts", g.user["id"])
            sb.update("posts", cs.audio_fields(audio, title), id=post["id"])
        except UploadError as e:
            social.delete_post_media(post["id"])      # remove the photo/video that was already uploaded
            sb.delete("posts", id=post["id"])
            flash(str(e), "error")
            return redirect(url_for("feed.home"))
    social.grant_achievement(g.user["id"], "first_post")
    flash("Posted!", "success")
    return redirect(url_for("feed.home"))


def _own_post_or_404(post_id):
    post = sb.get("posts", id=post_id)
    if not post:
        abort(404)
    if post["author_id"] != g.user["id"]:
        abort(403)
    return post


@bp.post("/posts/<post_id>/edit")
@login_required
def edit_post(post_id):
    post = _own_post_or_404(post_id)
    caption = clean_text(request.form.get("caption"), 2000)
    ok, why = check_text(caption)
    if not ok:
        flash(why, "error")
    elif not caption and not sb.select("post_media", post_id=post_id):
        flash("A post needs a caption or media.", "error")
    else:
        sb.update("posts", {"caption": caption or None, "edited_at": iso()}, id=post_id)
        flash("Post updated.", "success")
    return redirect(request.referrer or url_for("feed.home"))


@bp.post("/posts/<post_id>/delete")
@login_required
def delete_post(post_id):
    post = _own_post_or_404(post_id)
    social.delete_post_media(post_id)
    sb.delete("posts", id=post_id)
    flash("Post deleted.", "info")
    return redirect(url_for("profiles.view", username=g.user["username"]) if post["kind"] == "post" else url_for("reels.index"))


@bp.post("/posts/<post_id>/like")
@login_required
@limiter.limit("240 per hour")
def like(post_id):
    post = sb.get("posts", id=post_id)
    author = sb.get("profiles", id=post["author_id"]) if post else None
    if not post or not social.can_view_post(g.user, post, author):
        abort(404)
    existing = sb.get("likes", post_id=post_id, user_id=g.user["id"])
    if existing:
        sb.delete("likes", post_id=post_id, user_id=g.user["id"])
        liked = False
    else:
        sb.insert("likes", {"post_id": post_id, "user_id": g.user["id"]})
        liked = True
        notify(post["author_id"], "like", g.user["id"], "post", post_id)
        recommender.record_safe(g.user, post, author, "like")
    fresh = sb.get("posts", "like_count", id=post_id)
    return jsonify(liked=liked, count=fresh["like_count"])


@bp.post("/posts/<post_id>/save")
@login_required
@limiter.limit("240 per hour")
def save_post(post_id):
    post = sb.get("posts", id=post_id)
    author = sb.get("profiles", id=post["author_id"]) if post else None
    if not post or not social.can_view_post(g.user, post, author):
        abort(404)
    saved = sb.get("saved_posts", post_id=post_id, user_id=g.user["id"])
    if saved:
        sb.delete("saved_posts", post_id=post_id, user_id=g.user["id"])
        state = False
    else:
        sb.insert("saved_posts", {"post_id": post_id, "user_id": g.user["id"]})
        state = True
    return jsonify(saved=state)


@bp.get("/post/<post_id>")
@login_required
def detail(post_id):
    post = sb.get("posts", id=post_id)
    author = sb.get("profiles", id=post["author_id"]) if post else None
    if not post or not social.can_view_post(g.user, post, author):
        abort(404)
    card = social.hydrate_posts([post], g.user["id"])[0]
    hidden = social.blocked_between(g.user["id"])
    comments = [c for c in sb.select("comments", post_id=post_id, moderation_status="ok", order="created_at", desc=False)
                if c["author_id"] not in hidden]
    authors = sb.by_id("profiles", [c["author_id"] for c in comments])
    likes = sb.in_("comment_likes", "comment_id", [c["id"] for c in comments])
    like_n = Counter(r["comment_id"] for r in likes)
    mine = {r["comment_id"] for r in likes if r["user_id"] == g.user["id"]}
    for c in comments:
        c["author"] = authors.get(c["author_id"])
        c["like_count"], c["liked"] = like_n[c["id"]], c["id"] in mine
    return render_template("feed/detail.html", post=card, comments=comments)


@bp.post("/posts/<post_id>/comments")
@login_required
@limiter.limit("60 per hour")
def add_comment(post_id):
    post = sb.get("posts", id=post_id)
    author = sb.get("profiles", id=post["author_id"]) if post else None
    if not post or not social.can_view_post(g.user, post, author):
        abort(404)
    body = clean_text(request.form.get("body") or (request.get_json(silent=True) or {}).get("body"), 500)
    ok, why = check_text(body)
    wants_json = request.headers.get("X-Requested-With") == "fetch"
    if not body or not ok:
        msg = why or "Write a comment first."
        if wants_json:
            return jsonify(error=msg), 400
        flash(msg, "error")
        return redirect(url_for("feed.detail", post_id=post_id))
    c = sb.insert("comments", {"post_id": post_id, "author_id": g.user["id"], "body": body})
    notify(post["author_id"], "comment", g.user["id"], "post", post_id, dedupe=False)
    recommender.record_safe(g.user, post, author, "comment")
    c["author"] = g.user
    c["like_count"], c["liked"] = 0, False
    if wants_json:
        return jsonify(html=render_template("partials/comment.html", c=c), count=sb.get("posts", "comment_count", id=post_id)["comment_count"])
    return redirect(url_for("feed.detail", post_id=post_id) + "#comments")


@bp.post("/comments/<comment_id>/like")
@login_required
@limiter.limit("240 per hour")
def like_comment(comment_id):
    """Toggle a like on a post comment. Same visibility rules as the post it belongs to."""
    c = sb.get("comments", id=comment_id)
    post = sb.get("posts", id=c["post_id"]) if c else None
    author = sb.get("profiles", id=post["author_id"]) if post else None
    if not c or c["moderation_status"] != "ok" or not post or not social.can_view_post(g.user, post, author) \
            or social.is_blocked(g.user["id"], c["author_id"]):
        abort(404)
    me = g.user["id"]
    if sb.get("comment_likes", comment_id=comment_id, user_id=me):
        sb.delete("comment_likes", comment_id=comment_id, user_id=me)
        liked = False
    else:
        sb.insert("comment_likes", {"comment_id": comment_id, "user_id": me})
        notify(c["author_id"], "comment_like", me, "post", post["id"])
        liked = True
    return jsonify(liked=liked, count=sb.count("comment_likes", comment_id=comment_id))


@bp.post("/comments/<comment_id>/delete")
@login_required
def delete_comment(comment_id):
    c = sb.get("comments", id=comment_id)
    if not c:
        abort(404)
    post = sb.get("posts", id=c["post_id"])
    if g.user["id"] not in (c["author_id"], post["author_id"]) and g.user.get("role") != "admin":
        abort(403)
    sb.delete("comments", id=comment_id)
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify(ok=True)
    flash("Comment deleted.", "info")
    return redirect(request.referrer or url_for("feed.detail", post_id=c["post_id"]))


@bp.post("/report")
@login_required
@limiter.limit("30 per hour")
def report():
    """Generic report endpoint for posts, comments, profiles, messages, events, stories and reels."""
    ttype = request.form.get("target_type", "")
    tid = request.form.get("target_id", "")
    reason = request.form.get("reason", "other")
    if ttype not in ("post", "comment", "profile", "message", "event", "story", "reel", "story_comment") or not tid:
        abort(400)
    if reason not in ("harassment", "spam", "inappropriate", "impersonation", "unsafe", "other"):
        reason = "other"
    table = {"post": "posts", "reel": "posts", "comment": "comments", "profile": "profiles", "message": "messages",
             "event": "events", "story": "stories", "story_comment": "story_comments"}[ttype]
    if not sb.get(table, id=tid):
        abort(404)
    if not sb.get("reports", reporter_id=g.user["id"], target_type=ttype, target_id=tid):
        sb.insert("reports", {"reporter_id": g.user["id"], "target_type": ttype, "target_id": tid, "reason": reason,
                              "details": clean_text(request.form.get("details"), 500) or None})
    msg = "Thanks. Our moderators will review this report."
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify(ok=True, message=msg)
    flash(msg, "success")
    return redirect(request.referrer or url_for("feed.home"))
