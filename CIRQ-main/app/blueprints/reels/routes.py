from flask import Blueprint, jsonify, make_response, render_template, request, redirect, url_for, flash, g, current_app, abort
from app.decorators import login_required
from app.extensions import limiter, db
from app.services import supabase_service as sb, cloudinary_service as cs, social, recommender as rec
from app.services.cloudinary_service import UploadError
from app.services.moderation_service import check_text
from app.utils import clean_text

bp = Blueprint("reels", __name__)
PER_PAGE = 5   # only a handful of videos are ever loaded at once
MAX_REEL_SECONDS = 90


def _seen_from(cursor):
    """The cursor is the list of reel ids already served (newest 60), so personalised ranking can page without repeats."""
    return [x for x in (cursor or "").split(",") if x and len(x) <= 40 and x.replace("-", "").isalnum()][-60:]


@bp.get("/reels")
@login_required
def index():
    me = g.user
    seen = _seen_from(request.args.get("before"))
    try:
        rows, has_more = rec.rank_reels(me["id"], me, seen, PER_PAGE)
    except Exception:                       # recommendations must never take Reels down: fall back to newest first
        current_app.logger.exception("reel ranking failed")
        q = db().table("posts").select("*").eq("kind", "reel").eq("visibility", "public").eq("moderation_status", "ok") \
            .is_("community_id", "null").order("created_at", desc=True).limit(PER_PAGE + 1 + len(seen))
        rows = [r for r in (q.execute().data or []) if r["id"] not in seen][:PER_PAGE + 1]
        has_more, rows = len(rows) > PER_PAGE, rows[:PER_PAGE]
    cursor = ",".join((seen + [r["id"] for r in rows])[-60:]) if has_more else None
    hidden = social.blocked_between(me["id"])
    reels = [r for r in social.hydrate_posts(rows, me["id"])
             if r["author_id"] not in hidden and r["author"]["account_status"] == "approved" and not r["author"].get("is_private")]
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
        return jsonify(ok=True, redirect=url_for("reels.index"))
    flash("Reel posted!", "success")
    return redirect(url_for("reels.index"))
