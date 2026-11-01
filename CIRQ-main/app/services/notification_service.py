from app.services import supabase_service as sb
from app.utils import iso

COPY = {
    "follow": "started following you",
    "follow_request": "requested to follow you",
    "follow_accepted": "accepted your follow request",
    "like": "liked your post",
    "story_like": "liked your story",
    "comment": "commented on your post",
    "story_comment": "commented on your story",
    "comment_like": "liked your comment",
    "message": "sent you a message",
    "rsvp": "responded to your event",
    "event_update": "updated an event you're attending",
    "community": "posted in your school community",
    "moderation": "",
    "account": "",
}


def notify(user_id, kind, actor_id=None, ref_type=None, ref_id=None, body=None, dedupe=True):
    """Create an in-app notification. Skips self-notifications and exact duplicates that are still unread."""
    if not user_id or (actor_id and user_id == actor_id):
        return None
    if dedupe:
        existing = [n for n in sb.select("notifications", user_id=user_id, kind=kind, ref_id=str(ref_id) if ref_id else None,
                                          actor_id=actor_id) if not n.get("read_at")] if ref_id else []
        if existing:
            return existing[0]
    return sb.insert("notifications", {"user_id": user_id, "actor_id": actor_id, "kind": kind, "ref_type": ref_type,
                                       "ref_id": str(ref_id) if ref_id else None, "body": body or COPY.get(kind, "")})


def unread_count(user_id):
    rows = sb.db().table("notifications").select("id", count="exact").eq("user_id", user_id).is_("read_at", "null").limit(1).execute()
    return rows.count or 0


def mark_all_read(user_id):
    sb.db().table("notifications").update({"read_at": iso()}).eq("user_id", user_id).is_("read_at", "null").execute()
