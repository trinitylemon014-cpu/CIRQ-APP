import uuid

import httpx
from flask import Blueprint, current_app, render_template, request, redirect, url_for, flash, g, abort, jsonify
from postgrest.exceptions import APIError
from storage3.exceptions import StorageException
from app.decorators import login_required
from app.extensions import limiter, db
from app.services import supabase_service as sb, cloudinary_service as cs, social
from app.services.cloudinary_service import UploadError
from app.services import chat_attachments as ca
from app.services.chat_attachments import AttachmentError
from app.services import messaging
from app.services.messaging import dm_key, find_dm, get_or_create_dm   # dm_key re-exported for older imports
from app.services.moderation_service import check_text
from app.services.notification_service import notify
from app.utils import clean_text, iso, parse_dt, utcnow

bp = Blueprint("messages", __name__, url_prefix="/messages")
attachments_bp = Blueprint("attachments_api", __name__, url_prefix="/api/attachments")
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


def _tick_read_at(cid):
    """DMs only: when the other person last opened the chat. None for groups (no ticks there)."""
    conv = sb.get("conversations", id=cid)
    if not conv or conv["kind"] != "dm":
        return False
    for m in sb.select("conversation_members", conversation_id=cid):
        if m["user_id"] != g.user["id"]:
            if social.is_blocked(g.user["id"], m["user_id"]):
                return "blocked"                    # a blocked contact never sees 'seen', and you never see theirs
            return parse_dt(m["last_read_at"])
    return False


def _set_ticks(msgs, cid):
    read_at = _tick_read_at(cid)
    if read_at is False:
        return
    for m in msgs:
        if m["sender_id"] == g.user["id"]:
            m["tick"] = "sent" if read_at == "blocked" else messaging.tick_state(m, read_at)


def _drop_hidden(rows):
    """Messages that have already disappeared for me."""
    if not rows:
        return rows
    gone = messaging.hidden_ids(g.user["id"], [r["id"] for r in rows])
    return [r for r in rows if r["id"] not in gone]


def _decorate(msgs):
    if msgs:
        _set_ticks(msgs, msgs[0]["conversation_id"])
    senders = sb.by_id("profiles", [m["sender_id"] for m in msgs])
    stories = sb.by_id("stories", [m.get("story_id") for m in msgs])
    attachments = {}
    for row in sb.in_("message_attachments", "message_id", [m["id"] for m in msgs]):
        attachments.setdefault(row["message_id"], []).append(row)
    now = utcnow()
    for m in msgs:
        m["sender"] = senders.get(m["sender_id"])
        # A story reply shows a small thumbnail while the story is still live; afterwards just the label.
        st = stories.get(m.get("story_id"))
        exp = parse_dt(st["expires_at"]) if st else None
        live = bool(st and exp and exp > now and st["moderation_status"] == "ok")
        if not live:
            m["story"] = None
        elif st["media_type"] == "text":
            m["story"] = {"src": None, "bg": st.get("bg_style") or "violet"}      # a small coloured "Aa" tile instead of a picture
        else:
            m["story"] = {"src": cs.media_src(st, 160, thumb=True)}
        if m.get("media_public_id"):
            m["media_row"] = {"url": m["media_url"], "public_id": m["media_public_id"], "media_type": m["media_type"], "delivery_type": "authenticated"}
        m["attachments"] = attachments.get(m["id"], [])
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
    gone = messaging.hidden_ids(me)
    for m in recent:
        if m["id"] in gone:
            continue
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
    i_blocked = {r["blocked_id"] for r in sb.select("blocks", blocker_id=me)}
    items = []
    q = (request.args.get("q") or "").strip().lower()
    for c in convs:
        other = people.get(others.get(c["id"]))
        if c["kind"] == "dm" and (not other or (other["id"] in hidden and other["id"] not in i_blocked)):
            continue
        title = other["display_name"] if other else (c["title"] or "Group")
        if q and q not in title.lower() and q not in (other["username"] if other else ""):
            continue
        items.append({"conv": c, "title": title, "other": other, "last": last.get(c["id"]), "unread": 0 if (other and other["id"] in i_blocked) else unread.get(c["id"], 0),
                      "blocked": bool(other and other["id"] in i_blocked), "snippet": _snippet(last.get(c["id"]), me)})
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
    try:
        messaging.purge_conversation(cid)          # delete what has disappeared for everyone in this chat
    except Exception:
        current_app.logger.exception("purging disappeared messages failed")
    q = db().table("messages").select("*").eq("conversation_id", cid).eq("moderation_status", "ok").order("created_at", desc=True).limit(PAGE + 1)
    if request.args.get("before"):
        q = q.lt("created_at", request.args["before"])
    rows = q.execute().data or []
    more = rows[PAGE - 1]["created_at"] if len(rows) > PAGE else None
    msgs = _decorate(_drop_hidden(list(reversed(rows[:PAGE]))))
    messaging.mark_delivered(me, [cid])
    sb.update("conversation_members", {"last_read_at": iso()}, conversation_id=cid, user_id=me)
    db().table("notifications").update({"read_at": iso()}).eq("user_id", me).eq("kind", "message").eq("ref_id", cid).is_("read_at", "null").execute()
    other, members, can_send, block_state = None, [], True, None
    if conv["kind"] == "dm":
        other = sb.get("profiles", id=[x for x in conv["dm_key"].split(":") if x != me][0])
        can_send = bool(other) and social.can_message(g.user, other)
        if other and sb.get("blocks", blocker_id=me, blocked_id=other["id"]):
            block_state = "i_blocked"
        elif other and sb.get("blocks", blocker_id=other["id"], blocked_id=me):
            block_state = "blocked_me"
    else:
        members = list(sb.by_id("profiles", [m["user_id"] for m in sb.select("conversation_members", conversation_id=cid)]).values())
    if request.args.get("partial"):
        return render_template("messages/_bubbles.html", msgs=msgs)
    mine = _membership(cid, me) or {}
    return render_template("messages/thread.html", conv=conv, msgs=msgs, other=other, members=members, more=more,
                           last_ts=msgs[-1]["created_at"] if msgs else "", can_send=can_send, block_state=block_state,
                           disappear_after=mine.get("disappear_after") or 0, disappear_options=messaging.DISAPPEAR_OPTIONS,
                           disappear_label=messaging.DISAPPEAR_LABELS.get(mine.get("disappear_after") or 0, "Off"))


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
            mine = sb.get("blocks", blocker_id=me, blocked_id=other["id"]) if other else None
            return jsonify(error="You blocked this contact. Unblock them to send messages." if mine else "You can't message this person."), 403
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
    messaging.after_send(msg, conv, me)
    for m in members:
        if m["user_id"] != me:
            notify(m["user_id"], "message", me, "conversation", cid, body=(conv.get("title") or ""))
    sb.update("conversation_members", {"last_read_at": iso()}, conversation_id=cid, user_id=me)
    msg["sender"] = g.user
    _set_ticks([msg], cid)
    if msg.get("media_public_id"):
        msg["media_row"] = {"url": msg["media_url"], "public_id": msg["media_public_id"], "media_type": msg["media_type"], "delivery_type": "authenticated"}
    return jsonify(html=render_template("messages/_bubbles.html", msgs=[msg]), ts=msg["created_at"])


def _attachment_conversation_allowed(conv, members, uid):
    others = [member["user_id"] for member in members if member["user_id"] != uid]
    blocked = social.blocked_between(uid)
    if any(other_id in blocked for other_id in others):
        return False
    if conv["kind"] == "dm":
        other = sb.get("profiles", id=others[0]) if others else None
        return bool(other) and social.can_message(g.user, other)
    return True


def _attachment_response(message):
    msg = _decorate([message])[0]
    return jsonify(html=render_template("messages/_bubbles.html", msgs=[msg]), ts=msg["created_at"])


@attachments_bp.post("/conversations/<cid>/messages")
@login_required
@limiter.limit("8 per hour")
def send_with_attachments(cid):
    conv = _conv_or_404(cid)
    me = g.user["id"]
    members = sb.select("conversation_members", conversation_id=cid)
    if not _attachment_conversation_allowed(conv, members, me):
        return jsonify(error="You can't send attachments in this chat."), 403

    raw_message_id = request.form.get("message_id", "")
    try:
        message_id = str(uuid.UUID(raw_message_id))
    except (ValueError, AttributeError):
        return jsonify(error="Invalid message request. Please try again."), 400

    existing = sb.get("messages", id=message_id)
    if existing:
        if existing["sender_id"] != me or existing["conversation_id"] != cid:
            return jsonify(error="That message request has already been used."), 409
        if existing.get("attachment_count", 0):
            saved_attachments = sb.select("message_attachments", message_id=message_id)
            if len(saved_attachments) < existing["attachment_count"]:
                return jsonify(error="That upload is still finishing. Retry in a moment."), 503
        return _attachment_response(existing)

    body = clean_text(request.form.get("body"), 2000)
    files = [file for file in request.files.getlist("attachments") if file and file.filename]
    if len(files) > current_app.config["CHAT_ATTACHMENT_MAX_FILES"]:
        return jsonify(error=f"You can attach up to {current_app.config['CHAT_ATTACHMENT_MAX_FILES']} files per message."), 400
    if not body and not files:
        return jsonify(error="Write a message or attach at least one file."), 400
    ok, why = check_text(body)
    if not ok:
        return jsonify(error=why), 400

    try:
        prepared = [ca.validate_upload(file) for file in files]
        batch_limit = current_app.config["CHAT_ATTACHMENT_BATCH_MAX_MB"] * 1024 * 1024
        if sum(item["size_bytes"] for item in prepared) > batch_limit:
            raise AttachmentError(
                f"Attachments in one message can total up to {current_app.config['CHAT_ATTACHMENT_BATCH_MAX_MB']} MB.",
                413,
            )
    except AttachmentError as error:
        return jsonify(error=str(error)), error.status_code

    message = sb.insert("messages", {
        "id": message_id,
        "conversation_id": cid,
        "sender_id": me,
        "body": body or None,
        "attachment_count": len(prepared),
    })
    uploaded_paths, saved = [], []
    try:
        for item in prepared:
            path = f"{cid}/{message_id}/{uuid.uuid4().hex}_{item['safe_filename']}"
            ca.upload_to_storage(path, item["data"], item["mime_type"])
            uploaded_paths.append(path)
            saved.append(sb.insert("message_attachments", {
                "message_id": message_id,
                "uploader_id": me,
                "storage_path": path,
                "original_filename": item["original_filename"],
                "mime_type": item["mime_type"],
                "size_bytes": item["size_bytes"],
                "kind": item["kind"],
                "width": item["width"],
                "height": item["height"],
                "duration": item["duration"],
            }))
    except (APIError, StorageException, httpx.HTTPError):
        current_app.logger.exception("chat attachment upload failed")
        try:
            ca.remove_from_storage(uploaded_paths)
        except (StorageException, httpx.HTTPError):
            current_app.logger.exception("chat attachment cleanup failed")
        sb.delete("messages", id=message_id)
        return jsonify(error="The upload failed. Your message wasn't sent; please retry."), 502

    messaging.after_send(message, conv, me)
    for member in members:
        if member["user_id"] != me:
            notify(member["user_id"], "message", me, "conversation", cid, body=(conv.get("title") or ""))
    sb.update("conversation_members", {"last_read_at": iso()}, conversation_id=cid, user_id=me)
    message["sender"] = g.user
    message["attachments"] = saved
    _set_ticks([message], cid)
    return jsonify(html=render_template("messages/_bubbles.html", msgs=[message]), ts=message["created_at"])


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
    rows = _decorate(_drop_hidden(q.limit(50).execute().data or []))
    if rows:
        sb.update("conversation_members", {"last_read_at": iso()}, conversation_id=cid, user_id=g.user["id"])
        messaging.mark_delivered(g.user["id"], [cid])
        db().table("notifications").update({"read_at": iso()}).eq("user_id", g.user["id"]).eq("kind", "message").eq("ref_id", cid).is_("read_at", "null").execute()
    # live tick updates for the messages I sent that aren't "seen" yet (the browser sends their ids)
    ticks = {}
    ids = [i for i in request.args.get("ids", "").split(",") if i][:60]
    read_at = _tick_read_at(cid) if ids else False
    if ids and read_at is not False:
        mine = db().table("messages").select("id,created_at,delivered_at").eq("conversation_id", cid) \
            .eq("sender_id", g.user["id"]).in_("id", ids).execute().data or []
        ticks = {m["id"]: "sent" if read_at == "blocked" else messaging.tick_state(m, read_at) for m in mine}
    return jsonify(html=render_template("messages/_bubbles.html", msgs=rows) if rows else "",
                   ts=rows[-1]["created_at"] if rows else after, ticks=ticks)


def _dm_other(conv):
    """The other person in a one-to-one chat (404 for group chats: block/report here are for contacts)."""
    if conv["kind"] != "dm":
        abort(404)
    other_id = next((x for x in conv["dm_key"].split(":") if x != g.user["id"]), None)
    other = sb.get("profiles", id=other_id) if other_id else None
    if not other:
        abort(404)
    return other


@bp.post("/<cid>/block")
@login_required
@limiter.limit("60 per hour", key_func=lambda: g.user["id"])
def block_contact(cid):
    """Block or unblock the person in this chat (WhatsApp style). History stays here; sending stops both ways."""
    other = _dm_other(_conv_or_404(cid))
    if request.form.get("action") == "unblock":
        social.unblock_user(g.user["id"], other["id"])
        flash(f"Unblocked @{other['username']}.", "info")
    else:
        social.block_user(g.user["id"], other["id"])
        flash(f"Blocked @{other['username']}. They can't message you or see when you're online.", "success")
    return redirect(url_for("messages.thread", cid=cid))


@bp.post("/<cid>/report")
@login_required
@limiter.limit("20 per hour", key_func=lambda: g.user["id"])
def report_contact(cid):
    """Report the person in this chat. Moderators also get their last 5 messages to you as evidence, and you
    can block them in the same step. The reported person is never told."""
    other = _dm_other(_conv_or_404(cid))
    me = g.user["id"]
    reason = request.form.get("reason", "other")
    if reason not in ("harassment", "spam", "inappropriate", "impersonation", "unsafe", "other"):
        reason = "other"
    details = clean_text(request.form.get("details"), 440)

    def file(target_type, target_id, note=None):
        if not sb.get("reports", reporter_id=me, target_type=target_type, target_id=target_id):
            sb.insert("reports", {"reporter_id": me, "target_type": target_type, "target_id": target_id,
                                  "reason": reason, "details": note})

    file("profile", other["id"], ("Reported from chat. " + (details or "")).strip())
    recent = db().table("messages").select("id").eq("conversation_id", cid).eq("sender_id", other["id"]) \
        .order("created_at", desc=True).limit(5).execute().data or []
    for row in recent:
        file("message", row["id"], "Included with a chat report.")
    blocked = False
    if request.form.get("also_block"):
        social.block_user(me, other["id"])
        blocked = True
    msg = "Thanks. Our moderators will review this report." + (f" @{other['username']} is blocked." if blocked else "")
    return jsonify(ok=True, message=msg, blocked=blocked)


@bp.post("/delivered")
@login_required
@limiter.limit("30 per minute", key_func=lambda: g.user["id"])
def delivered():
    """The browser checks in (every ~30s while the app is open): whatever was sent to me has now reached me,
    so the sender's single tick becomes a double tick. Works even if I hide my active status."""
    try:
        messaging.mark_delivered(g.user["id"])
    except (APIError, httpx.HTTPError):
        current_app.logger.exception("marking messages delivered failed")
        return jsonify(ok=False), 503
    return jsonify(ok=True)


@bp.post("/<cid>/disappearing")
@login_required
@limiter.limit("30 per hour", key_func=lambda: g.user["id"])
def set_disappearing(cid):
    """My own timer for this chat. It only applies to new messages and only hides them for me."""
    _conv_or_404(cid)
    try:
        seconds = int(request.form.get("seconds") or (request.get_json(silent=True) or {}).get("seconds") or 0)
    except (TypeError, ValueError):
        return jsonify(error="Pick one of the options."), 400
    if seconds not in messaging.DISAPPEAR_LABELS:
        return jsonify(error="Pick one of the options."), 400
    sb.update("conversation_members", {"disappear_after": seconds or None}, conversation_id=cid, user_id=g.user["id"])
    return jsonify(ok=True, seconds=seconds, label=messaging.DISAPPEAR_LABELS[seconds])


def _attachment_or_404(attachment_id):
    attachment = sb.get("message_attachments", id=attachment_id)
    if not attachment:
        abort(404)
    message = sb.get("messages", id=attachment["message_id"])
    if not message or message["moderation_status"] != "ok" or attachment["uploader_id"] != message["sender_id"]:
        abort(404)
    conv = _conv_or_404(message["conversation_id"])
    members = sb.select("conversation_members", conversation_id=conv["id"])
    if not _attachment_conversation_allowed(conv, members, g.user["id"]):
        abort(404)
    return attachment


@attachments_bp.get("/<attachment_id>/view")
@login_required
@limiter.limit("180 per minute")
def view_attachment(attachment_id):
    attachment = _attachment_or_404(attachment_id)
    try:
        url = ca.signed_url(attachment["storage_path"])
    except (StorageException, httpx.HTTPError):
        current_app.logger.exception("chat attachment signed URL creation failed")
        return jsonify(error="This attachment is temporarily unavailable."), 502
    if not url:
        return jsonify(error="This attachment is temporarily unavailable."), 502
    response = redirect(url, code=302)
    response.headers["Cache-Control"] = "private, no-store"
    return response


@attachments_bp.get("/<attachment_id>/preview-url")
@login_required
@limiter.limit("60 per minute")
def attachment_preview_url(attachment_id):
    attachment = _attachment_or_404(attachment_id)
    if attachment["mime_type"] != "application/pdf":
        abort(404)
    try:
        url = ca.signed_url(attachment["storage_path"])
    except (StorageException, httpx.HTTPError):
        current_app.logger.exception("chat attachment preview URL creation failed")
        return jsonify(error="This attachment is temporarily unavailable."), 502
    if not url:
        return jsonify(error="This attachment is temporarily unavailable."), 502
    response = jsonify(url=url)
    response.headers["Cache-Control"] = "private, no-store"
    return response


@attachments_bp.get("/<attachment_id>/download")
@login_required
@limiter.limit("60 per minute")
def download_attachment(attachment_id):
    attachment = _attachment_or_404(attachment_id)
    try:
        url = ca.signed_url(attachment["storage_path"], attachment["original_filename"])
    except (StorageException, httpx.HTTPError):
        current_app.logger.exception("chat attachment download URL creation failed")
        return jsonify(error="This attachment is temporarily unavailable."), 502
    if not url:
        return jsonify(error="This attachment is temporarily unavailable."), 502
    response = redirect(url, code=302)
    response.headers["Cache-Control"] = "private, no-store"
    return response
