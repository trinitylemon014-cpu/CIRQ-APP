"""Shared social helpers: visibility rules, hydration of posts, counters, achievements."""
from flask import current_app
from app.extensions import db
from app.services import supabase_service as sb
from app.services.notification_service import notify
from app.utils import tier_for, iso


def blocked_between(uid):
    """IDs the viewer blocked plus IDs that blocked the viewer."""
    mine = {r["blocked_id"] for r in sb.select("blocks", blocker_id=uid)}
    theirs = {r["blocker_id"] for r in sb.select("blocks", blocked_id=uid)}
    return mine | theirs


def is_blocked(a, b):
    return bool(sb.get("blocks", blocker_id=a, blocked_id=b) or sb.get("blocks", blocker_id=b, blocked_id=a))


def follower_count(uid):
    return sb.count("follows", followee_id=uid)


def following_count(uid):
    return sb.count("follows", follower_id=uid)


def is_following(a, b):
    return bool(sb.get("follows", follower_id=a, followee_id=b))


def following_ids(uid):
    return [r["followee_id"] for r in sb.select("follows", "followee_id", follower_id=uid)]


def has_requested(requester_id, target_id):
    return bool(sb.get("follow_requests", requester_id=requester_id, target_id=target_id))


def follow_state(viewer_id, target_id):
    """What the Follow button should say: 'following', 'requested' (pending on a private account) or 'none'."""
    if is_following(viewer_id, target_id):
        return "following"
    return "requested" if has_requested(viewer_id, target_id) else "none"


def requested_ids(uid):
    """IDs of private accounts this person has a pending follow request with."""
    return {r["target_id"] for r in sb.select("follow_requests", "target_id", requester_id=uid)}


def pending_request_count(uid):
    return sb.count("follow_requests", target_id=uid)


def remove_follow_request(requester_id, target_id):
    """Withdraw (by the requester) or decline (by the target). Also clears the target's unread request alert."""
    existed = has_requested(requester_id, target_id)
    sb.delete("follow_requests", requester_id=requester_id, target_id=target_id)
    sb.delete("notifications", user_id=target_id, kind="follow_request", actor_id=requester_id, read_at=None)
    return existed


def approve_follow_request(requester_id, target_id):
    """Turn a pending request into a real follow. Returns False when there was no request to approve."""
    if not has_requested(requester_id, target_id):
        return False
    sb.delete("follow_requests", requester_id=requester_id, target_id=target_id)
    sb.delete("notifications", user_id=target_id, kind="follow_request", actor_id=requester_id)
    if is_blocked(requester_id, target_id) or is_following(requester_id, target_id):
        return True
    sb.insert("follows", {"follower_id": requester_id, "followee_id": target_id})
    notify(requester_id, "follow_accepted", target_id, "profile", target_id)
    grant_achievement(requester_id, "first_follow")
    grant_achievement(target_id, "first_follower")
    return True


def can_message(sender, recipient):
    """May `sender` start or continue a direct chat with `recipient`?

    Public accounts can be messaged by anyone who isn't blocked. A PRIVATE account only chats with people it
    has approved as followers (or people it follows itself), so a pending request never opens a chat."""
    if sender["id"] == recipient["id"] or is_blocked(sender["id"], recipient["id"]):
        return False
    if not recipient.get("is_private"):
        return True
    return is_following(sender["id"], recipient["id"]) or is_following(recipient["id"], sender["id"])


def can_see_profile_content(viewer, target):
    if viewer["id"] == target["id"] or viewer.get("role") in ("admin", "moderator"):
        return True
    if is_blocked(viewer["id"], target["id"]):
        return False
    return (not target.get("is_private")) or is_following(viewer["id"], target["id"])


def can_view_post(viewer, post, author):
    if post["author_id"] == viewer["id"] or viewer.get("role") in ("admin", "moderator"):
        return True
    if post["moderation_status"] != "ok" or not author or author["account_status"] != "approved":
        return False
    if is_blocked(viewer["id"], author["id"]):
        return False
    if post.get("community_id"):
        return bool(sb.get("community_members", community_id=post["community_id"], user_id=viewer["id"]))
    if post["visibility"] == "public" and not author.get("is_private"):
        return True
    return is_following(viewer["id"], author["id"])


def hydrate_posts(posts, viewer_id):
    if not posts:
        return []
    ids = [p["id"] for p in posts]
    authors = sb.by_id("profiles", [p["author_id"] for p in posts])
    media = {}
    for m in sb.in_("post_media", "post_id", ids):
        media.setdefault(m["post_id"], []).append(m)
    liked = {r["post_id"] for r in sb.in_("likes", "post_id", ids) if r["user_id"] == viewer_id} if viewer_id else set()
    schools = sb.by_id("schools", [a.get("school_id") for a in authors.values()])
    out = []
    for p in posts:
        a = authors.get(p["author_id"])
        if not a:
            continue
        out.append({**p, "author": a, "school": schools.get(a.get("school_id")), "media": media.get(p["id"], []),
                    "liked": p["id"] in liked})
    return out


def profile_stats(profile):
    n = follower_count(profile["id"])
    return {"followers": n, "following": following_count(profile["id"]), "tier": tier_for(n)}


def grant_achievement(user_id, code):
    """Award an achievement once. Returns True when newly awarded."""
    if sb.get("user_achievements", user_id=user_id, code=code):
        return False
    ach = sb.get("achievements", code=code)
    if not ach:
        return False
    sb.insert("user_achievements", {"user_id": user_id, "code": code})
    notify(user_id, "account", body=f"Achievement unlocked: {ach['name']}", dedupe=False)
    return True


def user_achievements(user_id):
    mine = {r["code"]: r for r in sb.select("user_achievements", user_id=user_id)}
    allach = sb.select("achievements")
    return [{**a, "earned": a["code"] in mine, "awarded_at": mine.get(a["code"], {}).get("awarded_at")} for a in allach]


def delete_post_media(post_id):
    """Remove Cloudinary files for a post, then the rows (rows also cascade with the post)."""
    from app.services import cloudinary_service as cs
    row = sb.get("posts", "audio_public_id", id=post_id)
    if row and row.get("audio_public_id"):
        cs.destroy(row["audio_public_id"], "audio")
    for m in sb.select("post_media", post_id=post_id):
        cs.destroy(m["public_id"], m["media_type"], m.get("delivery_type", "upload"))
