import base64
import binascii
import math
import uuid

import httpx
from flask import Blueprint, jsonify, make_response, render_template, request, redirect, url_for, flash, g, current_app, abort
from postgrest.exceptions import APIError
from app.decorators import login_required
from app.extensions import limiter, db
from app.services import supabase_service as sb, cloudinary_service as cs, social, boost_ad_service
from app.services.cloudinary_service import UploadError
from app.services.moderation_service import check_text
from app.utils import clean_text, parse_dt

bp = Blueprint("reels", __name__)
PER_PAGE = 5   # only a handful of videos are ever loaded at once
SCAN_SIZE = 25
MAX_REEL_SECONDS = 90
VIEW_MIN_SECONDS = 3


def _cursor_values(cursor):
    if not cursor:
        return None
    try:
        decoded = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        created_at, post_id = decoded.split("|", 1)
        if not parse_dt(created_at):
            raise ValueError
        uuid.UUID(post_id)
        return created_at, post_id
    except (binascii.Error, UnicodeDecodeError, ValueError, TypeError):
        abort(400, description="Invalid reel cursor.")


def _cursor_for(post):
    return base64.urlsafe_b64encode(f"{post['created_at']}|{post['id']}".encode()).decode().rstrip("=")


def _cursor_filter(cursor, direction):
    created_at, post_id = cursor
    op = "lt" if direction == "before" else "gt"
    return f"and(created_at.{op}.{created_at}),and(created_at.eq.{created_at},id.{op}.{post_id})"


def _visible_reels(rows, viewer, following, blocked):
    hydrated = social.hydrate_posts(rows, viewer["id"])
    out = []
    for reel in hydrated:
        author = reel["author"]
        allowed_by_relationship = reel["author_id"] == viewer["id"] or reel["author_id"] in following
        if (author["account_status"] != "approved" or reel["author_id"] in blocked
                or not any(media.get("media_type") == "video" for media in reel["media"])):
            continue
        if reel["visibility"] != "public" and not allowed_by_relationship:
            continue
        if author.get("is_private") and not allowed_by_relationship:
            continue
        out.append(reel)
    return out


@bp.get("/reels")
@login_required
def index():
    me = g.user
    if request.args.get("before") and request.args.get("after"):
        abort(400, description="Use either the older or newer reel cursor, not both.")
    direction = "after" if request.args.get("after") else "before"
    initial_cursor = _cursor_values(request.args.get(direction))
    following = set(social.following_ids(me["id"]))
    hidden = social.blocked_between(me["id"])
    eligible_authors = sorted(following | {me["id"]})
    scan_cursor = initial_cursor
    scanned_ids = set()
    visible = []
    exhausted = False
    while len(visible) <= PER_PAGE and not exhausted:
        q = (db().table("posts").select("*").eq("kind", "reel")
             .eq("moderation_status", "ok").is_("community_id", "null")
             .or_(f"visibility.eq.public,author_id.in.({','.join(eligible_authors)})"))
        if scan_cursor:
            q = q.or_(_cursor_filter(scan_cursor, direction))
        rows = (q.order("created_at", desc=direction != "after")
                .order("id", desc=direction != "after").limit(SCAN_SIZE).execute().data or [])
        if not rows:
            break
        for reel in _visible_reels(rows, me, following, hidden):
            if reel["id"] not in scanned_ids:
                scanned_ids.add(reel["id"])
                visible.append(reel)
        exhausted = len(rows) < SCAN_SIZE
        scan_cursor = (rows[-1]["created_at"], rows[-1]["id"])

    has_more = len(visible) > PER_PAGE
    page = visible[:PER_PAGE]
    if direction == "after":
        page.reverse()
        cursor = _cursor_for(visible[PER_PAGE - 1]) if has_more else None
        after_cursor = (_cursor_for(visible[PER_PAGE - 1]) if has_more else
                        _cursor_for({"created_at": scan_cursor[0], "id": scan_cursor[1]}) if scan_cursor else None)
    else:
        cursor = _cursor_for(page[-1]) if has_more and page else None
        after_cursor = None
    sponsored_ad = boost_ad_service.for_viewer(me["id"], request.args.get(direction, ""))
    if request.args.get("partial"):
        rendered_page = page if direction == "before" else sorted(
            page, key=lambda r: (parse_dt(r["created_at"]), r["id"]), reverse=True)
        resp = make_response(render_template("reels/_items.html", reels=rendered_page, cursor=cursor, following=following,
                                             sponsored_ad=sponsored_ad, view_min_seconds=VIEW_MIN_SECONDS))
        resp.headers["X-Next-Cursor"] = cursor or ""
        if direction == "after":
            resp.headers["X-After-Cursor"] = after_cursor or ""
            resp.headers["X-More-Newer"] = "1" if has_more else "0"
        elif page:
            resp.headers["X-Newest-Cursor"] = _cursor_for(page[0])
        return resp
    return render_template("reels/index.html", reels=page, cursor=cursor, newest=(_cursor_for(page[0]) if page else ""),
                           following=following, sponsored_ad=sponsored_ad, view_min_seconds=VIEW_MIN_SECONDS)


@bp.post("/api/reels/<reel_id>/view")
@login_required
@limiter.limit("60 per minute", key_func=lambda: g.user["id"])
def record_view(reel_id):
    post = sb.get("posts", id=reel_id)
    if not post or post.get("kind") != "reel" or post.get("moderation_status") != "ok" or post.get("community_id"):
        abort(404)

    author = sb.get("profiles", id=post["author_id"])
    viewer_id = g.user["id"]
    if not author or social.is_blocked(viewer_id, author["id"]) or not social.can_view_post(g.user, post, author):
        abort(404)
    if not sb.get("post_media", post_id=reel_id, media_type="video"):
        abort(404)

    payload = request.get_json(silent=True) or {}
    try:
        watched_seconds = float(payload.get("watched_seconds", 0))
    except (TypeError, ValueError):
        return jsonify(error="Watch duration must be a number."), 400
    if not math.isfinite(watched_seconds) or watched_seconds < VIEW_MIN_SECONDS:
        return jsonify(error=f"Watch the reel for at least {VIEW_MIN_SECONDS} seconds to count a view."), 400

    if author["id"] == viewer_id:
        return jsonify(counted=False, views_count=post.get("views_count", 0))

    try:
        result = db().rpc("cirq_record_reel_view", {
            "p_reel_id": reel_id,
            "p_viewer_id": viewer_id,
        }).execute().data or []
    except (APIError, httpx.HTTPError):
        current_app.logger.exception("recording reel view failed")
        return jsonify(counted=False, views_count=post.get("views_count", 0)), 503

    row = result[0] if isinstance(result, list) and result else result if isinstance(result, dict) else None
    if not row:
        return jsonify(counted=False, views_count=post.get("views_count", 0)), 503
    return jsonify(counted=bool(row.get("counted")), views_count=row.get("views_count", post.get("views_count", 0)))


@bp.get("/reels/new")
@login_required
def new():
    return render_template("reels/new.html")


def _wants_json():
    return request.headers.get("X-Requested-With") == "fetch" or request.is_json


def _fail(message, status=400):
    """Errors go back as JSON to the progress uploader, or as a flash + redirect for a plain form post."""
    if _wants_json():
        return jsonify(error=message), status
    flash(message, "error")
    return redirect(url_for("reels.new"))


@bp.post("/reels")
@login_required
@limiter.limit("10 per hour")
def create():
    f = request.files.get("video")
    caption = clean_text(request.form.get("caption"), 300)
    ok, why = check_text(caption)
    if not ok:
        return _fail(why)
    try:
        info = cs.upload(f, "reels", g.user["id"], allow_image=False)   # validates (type, size, signature) first
    except UploadError as e:
        return _fail(str(e))
    duration = info.get("duration")
    if duration and float(duration) > MAX_REEL_SECONDS + 1:
        cs.destroy(info.get("public_id"), "video", info.get("delivery_type", "upload"))
        return _fail(f"Reels can be up to {MAX_REEL_SECONDS} seconds long.")
    vis = "followers" if g.user.get("is_private") else "public"
    post = None
    try:
        post = sb.insert("posts", {"author_id": g.user["id"], "kind": "reel", "caption": caption or None, "visibility": vis})
        sb.insert("post_media", {"post_id": post["id"], "owner_id": g.user["id"], **info})
    except Exception:
        current_app.logger.exception("saving reel failed")
        if post:
            sb.delete("posts", id=post["id"])
        cs.destroy(info.get("public_id"), "video", info.get("delivery_type", "upload"))   # don't leave an orphan file
        return _fail("We couldn't save your reel. Please try again.", 500)
    social.grant_achievement(g.user["id"], "reel_maker")
    if _wants_json():
        rendered = social.hydrate_posts([post], g.user["id"])
        rendered[0]["saved"] = False
        newest = _cursor_for(post)
        html = render_template("reels/_items.html", reels=rendered, cursor=None,
                               following=set(social.following_ids(g.user["id"])), view_min_seconds=VIEW_MIN_SECONDS)
        return jsonify(ok=True, redirect=url_for("reels.index"), html=html, newest=newest)
    flash("Reel posted!", "success")
    return redirect(url_for("reels.index"))
