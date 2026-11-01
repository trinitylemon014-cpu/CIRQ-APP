import base64
import binascii
import uuid

from flask import Blueprint, jsonify, make_response, render_template, request, redirect, url_for, flash, g, current_app, abort
from app.decorators import login_required
from app.extensions import limiter, db
from app.services import supabase_service as sb, cloudinary_service as cs, social
from app.services.cloudinary_service import UploadError
from app.services.moderation_service import check_text
from app.utils import clean_text, parse_dt

bp = Blueprint("reels", __name__)
PER_PAGE = 5   # only a handful of videos are ever loaded at once
QUERY_SIZE = PER_PAGE + 1
MAX_REEL_SECONDS = 90


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


@bp.get("/reels")
@login_required
def index():
    me = g.user
    before = _cursor_values(request.args.get("before"))
    q = (db().table("posts").select("*").eq("kind", "reel")
         .eq("moderation_status", "ok").is_("community_id", "null")
         .order("created_at", desc=True).order("id", desc=True).limit(QUERY_SIZE))
    q = q.or_(f"visibility.eq.public,author_id.eq.{me['id']}")
    if before:
        created_at, post_id = before
        q = q.or_(f"created_at.lt.{created_at},and(created_at.eq.{created_at},id.lt.{post_id})")
    rows = q.execute().data or []
    has_more = len(rows) > PER_PAGE
    page = rows[:PER_PAGE]
    cursor = (base64.urlsafe_b64encode(f"{page[-1]['created_at']}|{page[-1]['id']}".encode())
              .decode().rstrip("=")) if has_more and page else None
    hidden = social.blocked_between(me["id"])
    reels = [r for r in social.hydrate_posts(page, me["id"])
             if r["author_id"] not in hidden and r["author"]["account_status"] == "approved"
             and (r["author_id"] == me["id"] or not r["author"].get("is_private"))]
    following = set(social.following_ids(me["id"]))
    if request.args.get("partial"):
        resp = make_response(render_template("reels/_items.html", reels=reels, cursor=cursor, following=following))
        resp.headers["X-Next-Cursor"] = cursor or ""
        return resp
    return render_template("reels/index.html", reels=reels, cursor=cursor, following=following)


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
        html = render_template("reels/_items.html", reels=rendered, cursor=None,
                               following=set(social.following_ids(g.user["id"])))
        return jsonify(ok=True, redirect=url_for("reels.index"), html=html)
    flash("Reel posted!", "success")
    return redirect(url_for("reels.index"))
