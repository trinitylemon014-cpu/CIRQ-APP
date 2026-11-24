"""Direct-message helpers shared by the inbox and by story replies."""
from datetime import timedelta

from flask import current_app

from app.extensions import db
from app.services import supabase_service as sb
from app.utils import iso, parse_dt, utcnow


def dm_key(a, b):
    """Stable key for a 1:1 chat so the same two people never end up with two conversations."""
    lo, hi = sorted([a, b])
    return f"{lo}:{hi}"


def find_dm(a, b):
    return sb.get("conversations", dm_key=dm_key(a, b))


def get_or_create_dm(a, b):
    """Return the DM between two people, creating it (with both members) if it doesn't exist yet."""
    conv = find_dm(a, b)
    if conv:
        return conv
    conv = sb.insert("conversations", {"kind": "dm", "dm_key": dm_key(a, b), "created_by": a})
    sb.insert("conversation_members", {"conversation_id": conv["id"], "user_id": a})
    sb.insert("conversation_members", {"conversation_id": conv["id"], "user_id": b})
    return conv


# ---------------------------------------------------------------- ticks: sent / delivered / seen
def is_online(profile):
    """Active right now (and willing to show it). Same rule as the green presence dot."""
    if not profile or not profile.get("show_active_status", True):
        return False
    seen = parse_dt(profile.get("last_seen_at"))
    return bool(seen and seen.timestamp() >= utcnow().timestamp() - current_app.config["ACTIVE_WINDOW_SECONDS"])


def tick_state(msg, other_read_at):
    """'read' (blue double tick) once the other person opened the chat after it was sent,
    'delivered' (grey double tick) once their device has it, else 'sent' (single tick)."""
    created = parse_dt(msg.get("created_at"))
    if other_read_at and created and other_read_at >= created:
        return "read"
    return "delivered" if msg.get("delivered_at") else "sent"


def mark_delivered(uid, cids=None):
    """Everything addressed to `uid` has now reached them. Called whenever their browser checks in."""
    cids = cids or [m["conversation_id"] for m in sb.select("conversation_members", user_id=uid)]
    if not cids:
        return
    db().table("messages").update({"delivered_at": iso()}).in_("conversation_id", list(cids)) \
        .neq("sender_id", uid).is_("delivered_at", "null").execute()


# ---------------------------------------------------------------- disappearing messages (each person for themselves)
DISAPPEAR_OPTIONS = [(0, "Off"), (3600, "1 hour"), (86400, "24 hours"), (604800, "7 days"), (7776000, "90 days")]
DISAPPEAR_LABELS = dict(DISAPPEAR_OPTIONS)


def after_send(msg, conv, sender_id):
    """Run right after a message is stored: mark it delivered if the other person is online now,
    and start the disappearing timer for everyone in the chat who has one on."""
    members = sb.select("conversation_members", conversation_id=conv["id"])
    if conv["kind"] == "dm":
        other_id = next((m["user_id"] for m in members if m["user_id"] != sender_id), None)
        other = sb.get("profiles", id=other_id) if other_id else None
        if is_online(other):
            stamp = iso()
            sb.update("messages", {"delivered_at": stamp}, id=msg["id"])
            msg["delivered_at"] = stamp
    created = parse_dt(msg.get("created_at")) or utcnow()
    rows = [{"message_id": msg["id"], "user_id": m["user_id"], "conversation_id": conv["id"],
             "hide_at": iso(created + timedelta(seconds=m["disappear_after"]))}
            for m in members if m.get("disappear_after")]
    if rows:
        db().table("message_hides").insert(rows).execute()


def hidden_ids(uid, ids=None):
    """Messages that have already disappeared for this person."""
    q = db().table("message_hides").select("message_id").eq("user_id", uid).lte("hide_at", iso())
    if ids is not None:
        if not ids:
            return set()
        q = q.in_("message_id", list(ids))
    else:
        q = q.limit(5000)
    return {r["message_id"] for r in (q.execute().data or [])}


def purge_conversation(cid):
    """Really delete messages once they have disappeared for every member of the chat.
    A message someone has no timer for is never deleted, only hidden for the people who set one."""
    expired = db().table("message_hides").select("message_id,user_id").eq("conversation_id", cid) \
        .lte("hide_at", iso()).limit(1000).execute().data or []
    if not expired:
        return 0
    members = {m["user_id"] for m in sb.select("conversation_members", conversation_id=cid)}
    by_msg = {}
    for r in expired:
        by_msg.setdefault(r["message_id"], set()).add(r["user_id"])
    gone = [mid for mid, users in by_msg.items() if members <= users]
    if gone:                                       # never destroy a message someone has reported: moderators may still need it
        reported = {r["target_id"] for r in sb.in_("reports", "target_id", gone, cols="target_id,target_type") if r["target_type"] == "message"}
        gone = [mid for mid in gone if mid not in reported]
    if not gone:
        return 0
    try:
        from app.services import chat_attachments as ca
        ca.remove_from_storage([a["storage_path"] for a in sb.in_("message_attachments", "message_id", gone)])
    except Exception:
        current_app.logger.exception("could not remove attachment files for disappeared messages")
    db().table("message_hides").delete().in_("message_id", gone).execute()
    db().table("messages").delete().in_("id", gone).execute()
    return len(gone)


# ---------------------------------------------------------------- reply / edit / delete (WhatsApp / Instagram style)
def _age_seconds(msg):
    created = parse_dt(msg.get("created_at"))
    return (utcnow() - created).total_seconds() if created else 0


def has_report(message_id):
    return any(r["target_type"] == "message" for r in sb.select("reports", target_id=message_id))


def edit_problem(msg, uid):
    """Why this message can't be edited right now (a sentence for the user), or None if it can. Only the sender, only plain text
    (no photo/file/story reply), only for a short while after sending."""
    if msg["sender_id"] != uid:
        return "You can only edit your own messages."
    if msg.get("deleted_at"):
        return "This message was deleted."
    if not (msg.get("body") or "").strip() or msg.get("media_public_id") or msg.get("attachment_count") or msg.get("story_reply") or msg.get("story_id"):
        return "Only text messages can be edited."
    minutes = current_app.config.get("MESSAGE_EDIT_MINUTES", 15)
    if minutes and _age_seconds(msg) > minutes * 60:
        return f"You can edit a message for {minutes} minutes after sending it."
    return None


def unsend_problem(msg, uid):
    """Why 'delete for everyone' isn't allowed for this message, or None. Only the sender, only within the configured window."""
    if msg["sender_id"] != uid:
        return "Only the person who sent a message can delete it for everyone."
    hours = current_app.config.get("MESSAGE_UNSEND_HOURS", 48)
    if not msg.get("deleted_at") and hours and _age_seconds(msg) > hours * 3600:
        return f"You can delete a message for everyone only within {hours} hours of sending it. You can still delete it for yourself."
    return None


def can_edit(msg, uid):
    return edit_problem(msg, uid) is None


def can_unsend(msg, uid):
    return not msg.get("deleted_at") and unsend_problem(msg, uid) is None


def preview_text(msg, limit=140):
    """One line describing a message, for reply quotes and the reply bar."""
    if msg.get("deleted_at"):
        return "This message was deleted"
    body = (msg.get("body") or "").strip()
    if body:
        return body[:limit] + ("…" if len(body) > limit else "")
    if msg.get("media_type") == "video":
        return "🎥 Video"
    if msg.get("media_public_id"):
        return "📷 Photo"
    return "📎 Attachment" if msg.get("attachment_count") else "Message"


def record_edit(msg, new_body, editor_id):
    """Change the text of a sent message. The earlier version is kept (moderators can see it), so editing can't be used to
    hide something that was reported."""
    sb.insert("message_edits", {"message_id": msg["id"], "editor_id": editor_id, "old_body": msg["body"]})
    stamp = iso()
    sb.update("messages", {"body": new_body, "edited_at": stamp}, id=msg["id"])
    return {**msg, "body": new_body, "edited_at": stamp}


def hide_for(uid, msg):
    """'Delete for me': the message disappears for this person only. Reuses the disappearing-message table (hide_at = now)."""
    row = sb.get("message_hides", message_id=msg["id"], user_id=uid)
    if row:
        sb.update("message_hides", {"hide_at": iso()}, id=row["id"])
    else:
        sb.insert("message_hides", {"message_id": msg["id"], "user_id": uid, "conversation_id": msg["conversation_id"], "hide_at": iso()})


def unsend(msg, by_id):
    """'Delete for everyone'. The row stays as a tombstone so replies and reports still make sense. The content and any files are
    wiped, unless the message has been reported: then moderators keep it, and the sender is never told why."""
    if msg.get("deleted_at"):
        return msg
    patch = {"deleted_at": iso(), "deleted_by": by_id}
    if not has_report(msg["id"]):
        files = sb.select("message_attachments", message_id=msg["id"])
        try:
            if files:
                from app.services import chat_attachments as ca
                ca.remove_from_storage([f["storage_path"] for f in files])
            if msg.get("media_public_id"):
                from app.services import cloudinary_service as cs
                cs.destroy(msg["media_public_id"], msg.get("media_type") or "image", "authenticated")
        except Exception:
            current_app.logger.exception("could not remove files for a message deleted for everyone")
        if files:
            db().table("message_attachments").delete().eq("message_id", msg["id"]).execute()
        patch.update(body="", media_url=None, media_public_id=None, media_type=None, attachment_count=0)
    sb.update("messages", patch, id=msg["id"])
    return {**msg, **patch}


def clear_chat(cid, uid):
    """'Delete chat' for one person: their history up to now is hidden from them only; the other people keep theirs.
    New messages bring the chat back, starting from an empty page."""
    stamp = iso()
    sb.update("conversation_members", {"cleared_at": stamp, "last_read_at": stamp}, conversation_id=cid, user_id=uid)
    db().table("notifications").update({"read_at": stamp}).eq("user_id", uid).eq("kind", "message").eq("ref_id", cid).is_("read_at", "null").execute()
