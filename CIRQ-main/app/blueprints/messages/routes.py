from flask import Blueprint, render_template, request, redirect, url_for, flash, g, abort, jsonify
from app.decorators import login_required
from app.extensions import limiter, db
from app.services import supabase_service as sb, cloudinary_service as cs, social
from app.services.cloudinary_service import UploadError
from app.services.messaging import dm_key, find_dm, get_or_create_dm   # dm_key re-exported for older imports
from app.services.moderation_service import check_text
from app.services.notification_service import notify
from app.utils import clean_text, iso, parse_dt, utcnow

bp = Blueprint("messages", __name__, url_prefix="/messages")
PAGE = 30


def unread_total(uid):
    """Nav badge: one unread 'message' notification exists per conversation with new messages."""
    r = db().table("notifications").select("id", count="exact").eq("user_id", uid).eq("kind", "message").is_("read_at", "null").limit(1).execute()
    return r.count or 0


def _membership(cid, uid):
    return sb.get("conversation_members", conversation_id=cid, user_id=uid)


def _conv_or_404(cid):
    """Every read/write goes through this: only members may touch a conversation."""
    conv = sb.get("conversations", id=cid)
    if not conv or not _membership(cid, g.user["id"]):
        abort(404)
    return conv


def _decorate(msgs):
    senders = sb.by_id("profiles", [m["sender_id"] for m in msgs])
    stories = sb.by_id("stories", [m.get("story_id") for m in msgs])
    now = utcnow()
    for m in msgs:
        m["sender"] = senders.get(m["sender_id"])
        # A story reply shows a small thumbnail while the story is still live; afterwards just the label.
        st = stories.get(m.get("story_id"))
        exp = parse_dt(st["expires_at"]) if st else None
        m["story"] = {"src": cs.media_src(st, 160, thumb=True)} if st and exp and exp > now and st["moderation_status"] == "ok" else None
        if m.get("media_public_id"):
            m["media_row"] = {"url": m["media_url"], "public_id": m["media_public_id"], "media_type": m["media_type"], "delivery_type": "authenticated"}
    return msgs


def _snippet(m, me):
    """One-line preview for the inbox list."""
    if not m:
        return "Say hi 👋"
    if m.get("story_reply"):
        label = "You replied to their story" if m["sender_id"] == me else "Replied to your story"
        return f"{label}: {m['body']}" if m.get("body") else label
    return m.get("body") or "📎 Attachment"


@bp.get("")
@login_required
def inbox():
    me = g.user["id"]
    mem = {m["conversation_id"]: m for m in sb.select("conversation_members", user_id=me)}
    convs = sb.in_("conversations", "id", list(mem))
    recent = db().table("messages").select("*").in_("conversation_id", list(mem) or ["00000000-0000-0000-0000-000000000000"]) \
        .eq("moderation_status", "ok").order("created_at", desc=True).limit(400).execute().data or []
    last, unread = {}, {}
    for m in recent:
        last.setdefault(m["conversation_id"], m)
        if m["sender_id"] != me and m["created_at"] > mem[m["conversation_id"]]["last_read_at"]:
            unread[m["conversation_id"]] = unread.get(m["conversation_id"], 0) + 1
    others = {}
    for c in convs:
        if c["kind"] == "dm":
            other_id = [x for x in c["dm_key"].split(":") if x != me][0]
            others[c["id"]] = other_id
    people = sb.by_id("profiles", list(others.values()))
    hidden = social.blocked_between(me)
    items = []
    q = (request.args.get("q") or "").strip().lower()
    for c in convs:
        other = people.get(others.get(c["id"]))
        if c["kind"] == "dm" and (not other or other["id"] in hidden):
            continue
        title = other["display_name"] if other else (c["title"] or "Group")
        if q and q not in title.lower() and q not in (other["username"] if other else ""):
            continue
        items.append({"conv": c, "title": title, "other": other, "last": last.get(c["id"]), "unread": unread.get(c["id"], 0),
                      "snippet": _snippet(last.get(c["id"]), me)})
    items.sort(key=lambda i: i["last"]["created_at"] if i["last"] else i["conv"]["created_at"], reverse=True)
    return render_template("messages/inbox.html", items=items, q=q)


@bp.post("/start/<username>")
@login_required
@limiter.limit("30 per hour")
def start(username):
    other = sb.get("profiles", username=username.lower())
    me = g.user["id"]
    if not other or other["id"] == me or other["account_status"] != "approved" or social.is_blocked(me, other["id"]):
        abort(404)
    conv = find_dm(me, other["id"])
    if not conv:
        if not social.can_message(g.user, other):
            # Private account: chat opens only after the owner approves the follow request.
            flash(f"@{other['username']} has a private account. You can message them once they approve your follow request.", "info")
            return redirect(url_for("profiles.view", username=other["username"]))
        conv = get_or_create_dm(me, other["id"])
    return redirect(url_for("messages.thread", cid=conv["id"]))


@bp.get("/<cid>")
@login_required
def thread(cid):
    conv = _conv_or_404(cid)
    me = g.user["id"]
    q = db().table("messages").select("*").eq("conversation_id", cid).eq("moderation_status", "ok").order("created_at", desc=True).limit(PAGE + 1)
    if request.args.get("before"):
        q = q.lt("created_at", request.args["before"])
    rows = q.execute().data or []
    more = rows[PAGE - 1]["created_at"] if len(rows) > PAGE else None
    msgs = _decorate(list(reversed(rows[:PAGE])))
    sb.update("conversation_members", {"last_read_at": iso()}, conversation_id=cid, user_id=me)
    db().table("notifications").update({"read_at": iso()}).eq("user_id", me).eq("kind", "message").eq("ref_id", cid).is_("read_at", "null").execute()
    other, members, can_send = None, [], True
    if conv["kind"] == "dm":
        other = sb.get("profiles", id=[x for x in conv["dm_key"].split(":") if x != me][0])
        can_send = bool(other) and social.can_message(g.user, other)
    else:
        members = list(sb.by_id("profiles", [m["user_id"] for m in sb.select("conversation_members", conversation_id=cid)]).values())
    if request.args.get("partial"):
        return render_template("messages/_bubbles.html", msgs=msgs)
    return render_template("messages/thread.html", conv=conv, msgs=msgs, other=other, members=members, more=more,
                           last_ts=msgs[-1]["created_at"] if msgs else "", can_send=can_send)


@bp.post("/<cid>/send")
@login_required
@limiter.limit("60 per minute")
def send(cid):
    conv = _conv_or_404(cid)
    me = g.user["id"]
    members = sb.select("conversation_members", conversation_id=cid)
    if conv["kind"] == "dm":
        other_id = [m["user_id"] for m in members if m["user_id"] != me]
        other = sb.get("profiles", id=other_id[0]) if other_id else None
        if not other or social.is_blocked(me, other["id"]):
            return jsonify(error="You can't message this person."), 403
        if not social.can_message(g.user, other):
            return jsonify(error=f"@{other['username']} has a private account. Wait for them to approve your follow request."), 403
    body = clean_text(request.form.get("body"), 2000)
    file = request.files.get("attachment")
    if not body and not (file and file.filename):
        return jsonify(error="Type a message first."), 400
    ok, why = check_text(body)
    if not ok:
        return jsonify(error=why), 400
    row = {"conversation_id": cid, "sender_id": me, "body": body or None}
    if file and file.filename:
        try:
            info = cs.upload(file, "chat", me, private=True)   # private "authenticated" delivery; signed URLs on read
        except UploadError as e:
            return jsonify(error=str(e)), 400
        row.update(media_type=info["media_type"], media_url=info["url"], media_public_id=info["public_id"])
    msg = sb.insert("messages", row)
    for m in members:
        if m["user_id"] != me:
            notify(m["user_id"], "message", me, "conversation", cid, body=(conv.get("title") or ""))
    sb.update("conversation_members", {"last_read_at": iso()}, conversation_id=cid, user_id=me)
    msg["sender"] = g.user
    if msg.get("media_public_id"):
        msg["media_row"] = {"url": msg["media_url"], "public_id": msg["media_public_id"], "media_type": msg["media_type"], "delivery_type": "authenticated"}
    return jsonify(html=render_template("messages/_bubbles.html", msgs=[msg]), ts=msg["created_at"])


@bp.get("/<cid>/poll")
@login_required
@limiter.limit("90 per minute")
def poll(cid):
    """Polling fallback (every few seconds from the browser). Returns only messages newer than `after`."""
    _conv_or_404(cid)
    after = request.args.get("after", "").replace(" ", "+")   # a raw "+" in a URL arrives as a space
    q = db().table("messages").select("*").eq("conversation_id", cid).eq("moderation_status", "ok").neq("sender_id", g.user["id"]).order("created_at")
    if after:
        q = q.gt("created_at", after)
    rows = _decorate(q.limit(50).execute().data or [])
    if rows:
        sb.update("conversation_members", {"last_read_at": iso()}, conversation_id=cid, user_id=g.user["id"])
        db().table("notifications").update({"read_at": iso()}).eq("user_id", g.user["id"]).eq("kind", "message").eq("ref_id", cid).is_("read_at", "null").execute()
    return jsonify(html=render_template("messages/_bubbles.html", msgs=rows) if rows else "", ts=rows[-1]["created_at"] if rows else after)
