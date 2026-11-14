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
