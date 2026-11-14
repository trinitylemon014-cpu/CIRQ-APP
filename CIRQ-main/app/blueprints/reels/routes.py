import math

import httpx
from flask import Blueprint, jsonify, make_response, render_template, request, redirect, url_for, flash, g, current_app, abort
from postgrest.exceptions import APIError
from app.decorators import login_required
from app.extensions import limiter, db
from app.services import supabase_service as sb, cloudinary_service as cs, social, boost_ad_service, likers as likers_service
from app.services import reel_shuffle as shuffle
from app.services.cloudinary_service import UploadError
from app.services.moderation_service import check_text
from app.utils import clean_text, iso, utcnow

bp = Blueprint("reels", __name__)
PER_PAGE = 5   # only a handful of videos are ever loaded at once
SCAN_SIZE = 12  # candidates checked (hydrated) at a time while filling a page
MAX_REEL_SECONDS = 90
VIEW_MIN_SECONDS = 3
SHUFFLE_MARKER = "shuffle"   # placeholder for data-newest: a shuffled feed has no "newer reels" cursor


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


def _candidate_pool(viewer, following, blocked):
    """(id, author_id) of the newest reels this viewer is allowed to see; the shuffle is done over this pool."""
    eligible_authors = sorted(following | {viewer["id"]})
    rows = (db().table("posts").select("id,author_id").eq("kind", "reel")
            .eq("moderation_status", "ok").is_("community_id", "null")
            .or_(f"visibility.eq.public,author_id.in.({','.join(eligible_authors)})")
            .order("created_at", desc=True).order("id", desc=True).limit(shuffle.POOL_SIZE).execute().data or [])
    return [r for r in rows if r["author_id"] not in blocked]


def _already_watched(viewer_id, reel_ids, before):
    """Reels in `reel_ids` this viewer had a counted view on before `before` (when this visit started).

    If the lookup fails the feed still works, it just can't push watched reels to the back."""
    watched = set()
    try:
        for i in range(0, len(reel_ids), shuffle.SEEN_CHUNK):
            chunk = reel_ids[i:i + shuffle.SEEN_CHUNK]
            rows = (db().table("reel_views").select("reel_id").eq("viewer_id", viewer_id)
                    .lt("created_at", before).in_("reel_id", chunk).execute().data or [])
            watched.update(r["reel_id"] for r in rows)
    except Exception:
        current_app.logger.exception("reel view history lookup failed; shuffling without it")
    return watched


def _empty_newer_response():
    """Old browser tabs still poll for 'newer' reels. A shuffled feed has none, so answer with an empty page."""
    resp = make_response("")
    resp.headers["X-Next-Cursor"] = ""
    resp.headers["X-After-Cursor"] = request.args.get("after", "")
    resp.headers["X-More-Newer"] = "0"
    return resp


@bp.get("/reels")
@login_required
def index():
    """A fresh shuffle on every visit. Reels you already watched (and your own) come after everything you haven't seen."""
    me = g.user
    if request.args.get("before") and request.args.get("after"):
        abort(400, description="Use either the older or newer reel cursor, not both.")
    if request.args.get("after"):
        return _empty_newer_response()

    cursor = None
    if request.args.get("before"):
        try:
            cursor = shuffle.decode_cursor(request.args["before"])
        except shuffle.BadCursor:
            abort(400, description="Invalid reel cursor.")
    seed = cursor["seed"] if cursor else shuffle.new_seed()
    t0 = cursor["t0"] if cursor else iso(utcnow())

    following = set(social.following_ids(me["id"]))
    hidden = social.blocked_between(me["id"])
    pool = _candidate_pool(me, following, hidden)
    pool_ids = [r["id"] for r in pool]
    # watched before this visit began + my own reels are served last; both sets are fixed for the whole visit,
    # so the order never changes while the user scrolls.
    back_of_queue = _already_watched(me["id"], pool_ids, t0) | {r["id"] for r in pool if r["author_id"] == me["id"]}
    queue = shuffle.remaining(shuffle.order(pool_ids, seed, back_of_queue), seed, back_of_queue,
                              cursor["position"] if cursor else None)

    visible, i = [], 0
    while len(visible) <= PER_PAGE and i < len(queue):
        batch, i = queue[i:i + SCAN_SIZE], i + SCAN_SIZE
        rows = {r["id"]: r for r in (db().table("posts").select("*").in_("id", batch).execute().data or [])}
        visible.extend(_visible_reels([rows[rid] for rid in batch if rid in rows], me, following, hidden))

    has_more = len(visible) > PER_PAGE
    page = visible[:PER_PAGE]
    next_cursor = shuffle.encode_cursor(seed, t0, page[-1]["id"], page[-1]["id"] in back_of_queue) if has_more and page else None
    sponsored_ad = boost_ad_service.for_viewer(me["id"], request.args.get("before", ""))
    if request.args.get("partial"):
        resp = make_response(render_template("reels/_items.html", reels=page, cursor=next_cursor, following=following,
                                             sponsored_ad=sponsored_ad, view_min_seconds=VIEW_MIN_SECONDS))
        resp.headers["X-Next-Cursor"] = next_cursor or ""
        return resp
    return render_template("reels/index.html", reels=page, cursor=next_cursor, newest=SHUFFLE_MARKER if page else "",
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


@bp.get("/reels/<reel_id>/viewers")
@login_required
@limiter.limit("120 per hour", key_func=lambda: g.user["id"])
def viewers(reel_id):
    """Owner-only list of the people who watched this reel (a view counts after VIEW_MIN_SECONDS, once per person)."""
    post = sb.get("posts", id=reel_id)
    if not post or post.get("kind") != "reel":
        abort(404)
    if post["author_id"] != g.user["id"]:
        abort(403)
    people, nxt = likers_service.viewers_page(reel_id, g.user, request.args.get("before"))
    return render_template("partials/viewers.html", people=people, next=nxt, first=not request.args.get("before"))


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
        newest = SHUFFLE_MARKER
        html = render_template("reels/_items.html", reels=rendered, cursor=None,
                               following=set(social.following_ids(g.user["id"])), view_min_seconds=VIEW_MIN_SECONDS)
        return jsonify(ok=True, redirect=url_for("reels.index"), html=html, newest=newest)
    flash("Reel posted!", "success")
    return redirect(url_for("reels.index"))
