"""Who liked a post, reel or comment.

The list follows the same rules as the thing being liked: a student only sees it if they can see the post (or comment) itself.
People the viewer has blocked, or who have blocked the viewer, and accounts that aren't approved are left out. Newest likes first, 20 at a time.
"""
from app.services import social, supabase_service as sb
from app.utils import parse_dt, iso

PAGE = 20


def page(table, key, key_id, viewer, before=None):
    """One page of likers from `likes` (key='post_id') or `comment_likes` (key='comment_id').
    Returns (profiles, cursor_for_the_next_page_or_None)."""
    q = sb.db().table(table).select("user_id,created_at").eq(key, key_id)
    cutoff = parse_dt(before) if before else None
    if cutoff:
        q = q.lt("created_at", iso(cutoff))
    rows = q.order("created_at", desc=True).limit(PAGE + 1).execute().data or []
    more = len(rows) > PAGE
    rows = rows[:PAGE]
    people = sb.by_id("profiles", [r["user_id"] for r in rows])
    hidden = social.blocked_between(viewer["id"])
    out = [people[r["user_id"]] for r in rows
           if r["user_id"] in people and people[r["user_id"]].get("account_status") == "approved" and r["user_id"] not in hidden]
    return out, (rows[-1]["created_at"] if more else None)
