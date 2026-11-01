"""Direct-message helpers shared by the inbox and by story replies."""
from app.services import supabase_service as sb


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
