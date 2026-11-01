"""Endpoints the browser uses to tell CIRQ what a student watches and skips (powers 'Suggested for you')."""
import uuid
from flask import Blueprint, abort, g, jsonify, request
from app.decorators import login_required
from app.extensions import limiter
from app.services import recommender as rec, social, supabase_service as sb

bp = Blueprint("recs", __name__)
MAX_BATCH = 30


def _is_uuid(v):
    try:
        uuid.UUID(str(v))
        return True
    except ValueError:
        return False


def _num(v, lo, hi):
    try:
        return max(lo, min(hi, float(v)))
    except (TypeError, ValueError):
        return 0.0


@bp.post("/api/signals")
@login_required
@limiter.limit("900 per hour")
def signals():
    """Batched viewing signals: {events:[{post_id, event: view|watch|share, value}]}. Validated and permission-checked."""
    data = request.get_json(silent=True) or {}
    events = data.get("events") if isinstance(data.get("events"), list) else []
    events = [e for e in events[:MAX_BATCH] if isinstance(e, dict) and e.get("event") in {"view", "watch", "share"} and _is_uuid(e.get("post_id"))]
    if not events or not rec.enabled(g.user):
        return jsonify(ok=True, stored=0)
    posts = sb.by_id("posts", [str(e.get("post_id")) for e in events])
    authors = sb.by_id("profiles", [p["author_id"] for p in posts.values()])
    allowed, items = {}, []
    for e in events:
        post = posts.get(str(e.get("post_id")))
        if not post:
            continue
        if post["id"] not in allowed:                      # one permission check per post
            allowed[post["id"]] = social.can_view_post(g.user, post, authors.get(post["author_id"]))
        if not allowed[post["id"]]:
            continue
        hi = 600000 if e["event"] == "view" else 5
        items.append({"post": post, "author": authors.get(post["author_id"]), "event": e["event"], "value": _num(e.get("value"), 0, hi)})
    rec.record(g.user["id"], items)
    return jsonify(ok=True, stored=len(items))


@bp.post("/posts/<post_id>/not-interested")
@login_required
@limiter.limit("120 per hour")
def not_interested(post_id):
    """'Not interested': never show this post again and show fewer like it."""
    post = sb.get("posts", id=post_id)
    author = sb.get("profiles", id=post["author_id"]) if post else None
    if not post or not social.can_view_post(g.user, post, author):
        abort(404)
    rec.record(g.user["id"], [{"post": post, "author": author, "event": "hide"}], learn=rec.enabled(g.user))
    return jsonify(ok=True, message="Got it. We'll show fewer posts like this.")
