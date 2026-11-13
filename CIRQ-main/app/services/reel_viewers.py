"""Who viewed a reel. Only the reel's author ever sees this list (the route enforces that).

Rows come from `reel_views`, which only holds qualified views (a viewer who watched at least a few seconds, never the
author). People the author has blocked, or who have blocked the author, and accounts that aren't approved are left
out. Newest views first, 20 at a time.
"""
from app.services import social, supabase_service as sb
from app.utils import parse_dt, iso

PAGE = 20


def page(reel_id, author, before=None):
    """One page of viewers. Returns ([(profile, viewed_at), ...], cursor_for_the_next_page_or_None)."""
    q = sb.db().table("reel_views").select("viewer_id,created_at").eq("reel_id", reel_id)
    cutoff = parse_dt(before) if before else None
    if cutoff:
        q = q.lt("created_at", iso(cutoff))
    rows = q.order("created_at", desc=True).limit(PAGE + 1).execute().data or []
    more = len(rows) > PAGE
    rows = rows[:PAGE]
    people = sb.by_id("profiles", [r["viewer_id"] for r in rows])
    hidden = social.blocked_between(author["id"])
    out = [(people[r["viewer_id"]], r["created_at"]) for r in rows
           if r["viewer_id"] in people
           and people[r["viewer_id"]].get("account_status") == "approved"
           and r["viewer_id"] not in hidden]
    return out, (rows[-1]["created_at"] if more else None)
