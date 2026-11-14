"""Event group chats: one is made with every event, everyone who is 'going' joins it, and it is deleted once the
event date has gone (or the host cancels / completes the event)."""
import time
from datetime import timedelta, timezone

from flask import current_app

from app.extensions import db
from app.services import supabase_service as sb
from app.utils import EAT, iso, parse_dt, utcnow

_last_sweep = 0.0
SWEEP_EVERY = 60            # seconds between opportunistic sweeps in one worker


def expires_at_for(event):
    """The chat closes at the end of the event's day (East Africa Time), or when the event ends if that is later."""
    start = parse_dt(event["starts_at"])
    day_end = (start.astimezone(EAT).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)).astimezone(timezone.utc)
    end = parse_dt(event.get("ends_at"))
    return max(day_end, end) if end else day_end


def is_over(event):
    return expires_at_for(event) <= utcnow()


def ensure_chat(event):
    """Return the event's chat id, creating it (host as first member) if it doesn't exist yet. None once the event is over."""
    if event.get("conversation_id"):
        return event["conversation_id"]
    if is_over(event) or event.get("status") != "scheduled":
        return None
    conv = sb.insert("conversations", {"kind": "group", "title": event["title"][:60], "event_id": event["id"],
                                       "created_by": event["host_id"], "expires_at": iso(expires_at_for(event))})
    sb.insert("conversation_members", {"conversation_id": conv["id"], "user_id": event["host_id"]})
    sb.update("events", {"conversation_id": conv["id"]}, id=event["id"])
    event["conversation_id"] = conv["id"]
    return conv["id"]


def is_expired(conv):
    stamp = parse_dt(conv.get("expires_at")) if conv else None
    return bool(stamp and stamp <= utcnow())


def close_conversation(conv):
    """Delete the chat for good: messages, attachment files, members, unread alerts."""
    cid = conv["id"]
    msg_ids = [m["id"] for m in db().table("messages").select("id").eq("conversation_id", cid).limit(5000).execute().data or []]
    if msg_ids:
        try:
            from app.services import chat_attachments as ca
            ca.remove_from_storage([a["storage_path"] for a in sb.in_("message_attachments", "message_id", msg_ids)])
        except Exception:
            current_app.logger.exception("could not remove attachment files for closed event chat")
        for i in range(0, len(msg_ids), 80):
            db().table("message_attachments").delete().in_("message_id", msg_ids[i:i + 80]).execute()
    db().table("notifications").delete().eq("kind", "message").eq("ref_id", cid).execute()
    db().table("message_hides").delete().eq("conversation_id", cid).execute()
    db().table("messages").delete().eq("conversation_id", cid).execute()
    sb.delete("conversation_members", conversation_id=cid)
    sb.delete("conversations", id=cid)
    if conv.get("event_id"):
        sb.update("events", {"conversation_id": None}, id=conv["event_id"])


def close_event_chat(event):
    """Used when the host cancels or completes the event, or a moderator removes it."""
    if event.get("conversation_id"):
        conv = sb.get("conversations", id=event["conversation_id"])
        if conv:
            close_conversation(conv)
        else:
            sb.update("events", {"conversation_id": None}, id=event["id"])
        event["conversation_id"] = None


def expire_due(force=False):
    """Delete every event chat whose time is up. Cheap and rate-limited, so pages can call it freely."""
    global _last_sweep
    if not force and time.monotonic() - _last_sweep < SWEEP_EVERY:
        return 0
    _last_sweep = time.monotonic()
    due = db().table("conversations").select("*").lte("expires_at", iso()).limit(50).execute().data or []
    for conv in due:
        close_conversation(conv)
    return len(due)
