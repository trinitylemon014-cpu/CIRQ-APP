"""CIRQ LIVE business logic. Routes stay thin; every rule lives here so it can be tested without a browser.

Division of labour
  * LiveKit carries the audio/video (see livekit_service). Flask issues tokens and enforces host decisions.
  * PostgreSQL owns all money and counting rules: gifts, wallets, diamonds, battle scores and the guest limit are changed ONLY by the
    cirq_live_* functions in migrations/2026_11_14_live_streaming.sql (atomic, idempotent, row-locked). Python never edits a balance.
  * The realtime feed is the append-only live_events table. Browsers poll "events after id N" (see poll()). This works on the existing
    gunicorn/Supabase setup with no extra infrastructure; the scaling ceiling is documented in LIVE_STREAMING.md.
"""
import re
import uuid
from datetime import datetime, timedelta, timezone

from flask import current_app

from app.extensions import db
from app.services import supabase_service as sb, social, livekit_service as lk, cloudinary_service as cs, lending
from app.services.audit_service import audit
from app.services.moderation_service import check_text
from app.utils import clean_text, iso, parse_dt, utcnow

CATEGORIES = ["chat", "music", "gaming", "study", "sports", "art", "comedy", "education", "other"]
REACTIONS = ["❤️", "🔥", "👏", "😂", "😮", "🎉"]
REPORT_REASONS = ["harassment", "spam", "inappropriate", "unsafe", "impersonation", "other"]
BATTLE_MINUTES = (3, 5, 10, 15)
FOREVER = datetime(2999, 1, 1, tzinfo=timezone.utc)
_URL = re.compile(r"(https?://|www\.|\b[a-z0-9-]+\.(com|net|org|io|co|ug|me|ly|app|xyz)\b)", re.I)

# lifetime coins a viewer has gifted ONE host -> supporter badge shown beside their chat name in that host's rooms
SUPPORTER_TIERS = [(20000, "Diamond"), (5000, "Gold"), (1000, "Silver"), (100, "Bronze")]
# lifetime coins a host has earned from gifts -> creator level (Lv 1..10)
LEVEL_STEPS = [0, 500, 2000, 5000, 10000, 25000, 50000, 100000, 250000, 500000]


def cfg(key):
    return current_app.config[key]


# ------------------------------------------------------------------ small helpers
def supporter_tier(total_coins):
    for need, name in SUPPORTER_TIERS:
        if int(total_coins or 0) >= need:
            return name
    return None


def creator_level(earned_coins):
    earned = int(earned_coins or 0)
    level = max(i for i, need in enumerate(LEVEL_STEPS) if earned >= need)
    nxt = LEVEL_STEPS[level + 1] if level + 1 < len(LEVEL_STEPS) else None
    return {"level": level + 1, "earned": earned, "next_at": nxt, "to_next": (nxt - earned) if nxt else 0}


def week_start(now=None):
    now = now or utcnow()
    return (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)


def is_staff(user):
    return (user or {}).get("role") in ("admin", "moderator")


def _rpc(name, params):
    return db().rpc(name, params).execute().data


def _public_person(p, size=64):
    return {"username": p["username"], "display_name": p.get("display_name") or p["username"],
            "avatar": cs.avatar_src(p, size), "verified": bool(p.get("verified"))}


def _profile_by_username(username):
    name = (username or "").strip().lower().lstrip("@")
    return sb.get("profiles", username=name) if name else None


def feature_on():
    if not cfg("LIVE_ENABLED"):
        return False
    flag = sb.get("feature_flags", key="live_enabled")
    return True if flag is None else bool(flag.get("enabled"))


# ------------------------------------------------------------------ eligibility
def can_host(p):
    """(ok, message). LIVE_ELIGIBILITY=anyone (default): every approved account may go live. approved: also needs a verified email.
    verified: email verified plus a verified badge or an admin live-access grant."""
    if not feature_on():
        return False, "CIRQ LIVE is switched off right now."
    if p.get("account_status") != "approved":
        return False, "Your account must be approved before you can go live."
    mode = cfg("LIVE_ELIGIBILITY")
    if mode == "anyone":
        return True, ""
    if not p.get("email_verified_at"):
        return False, "Verify your email address before going live."
    if mode == "approved" or p.get("verified") or p.get("live_host_granted") or p.get("role") == "admin":
        return True, ""
    return False, "Going live is available to verified creators. Ask a CIRQ admin to enable live access for your account."


# ------------------------------------------------------------------ sessions: discovery, start, end
def get_session(sid):
    return sb.get("live_sessions", id=str(sid))


def reap_stale():
    """End lives whose host stopped sending heartbeats (closed the tab, lost signal). Cheap; called from discovery and start."""
    cutoff = iso(utcnow() - timedelta(seconds=cfg("LIVE_HOST_TIMEOUT_SECONDS")))
    rows = db().table("live_sessions").select("id,room_name").eq("status", "live").lt("last_heartbeat_at", cutoff).limit(25).execute().data or []
    for r in rows:
        end_session(r, None, "host_timeout")
    return len(rows)


def _viewer_counts(session_ids):
    if not session_ids:
        return {}
    cutoff = iso(utcnow() - timedelta(seconds=cfg("LIVE_VIEWER_TIMEOUT_SECONDS")))
    rows = db().table("live_viewers").select("session_id").in_("session_id", list(session_ids)).gte("last_seen_at", cutoff).is_("left_at", "null").execute().data or []
    out = {}
    for r in rows:
        out[r["session_id"]] = out.get(r["session_id"], 0) + 1
    return out


def viewer_count(sid):
    return _viewer_counts([sid]).get(sid, 0)


def active_sessions(viewer, category=None, limit=40):
    """Lives the viewer is allowed to see, followed creators first, then by audience size."""
    reap_stale()
    q = db().table("live_sessions").select("*").eq("status", "live").order("started_at", desc=True).limit(100)
    if category in CATEGORIES:
        q = q.eq("category", category)
    rows = q.execute().data or []
    hosts = sb.by_id("profiles", [r["host_id"] for r in rows])
    hidden = social.blocked_between(viewer["id"])
    following = set(social.following_ids(viewer["id"]))
    banned_by = {r["host_id"] for r in sb.select("live_host_blocks", user_id=viewer["id"])}
    counts = _viewer_counts([r["id"] for r in rows])
    out = []
    for r in rows:
        h = hosts.get(r["host_id"])
        if not h or h.get("account_status") != "approved":
            continue
        mine = r["host_id"] == viewer["id"]
        if not mine and not is_staff(viewer):
            if r["host_id"] in hidden or r["host_id"] in banned_by:
                continue
            if r["visibility"] == "followers" and r["host_id"] not in following:
                continue
        out.append({"id": r["id"], "title": r["title"], "category": r["category"], "mode": r["mode"], "visibility": r["visibility"],
                    "started_at": r["started_at"], "viewers": counts.get(r["id"], 0), "followed": r["host_id"] in following, "mine": mine,
                    "host": _public_person(h, 96)})
    out.sort(key=lambda s: (not s["followed"], -s["viewers"]))
    return out[:limit]


def start_session(host, title, category, mode, visibility):
    """Returns (session, error_message, resumed). A host who still has a live (page reload, lost signal) simply resumes it."""
    ok, why = can_host(host)
    if not ok:
        return None, why, False
    if not lk.configured():
        return None, "Live streaming isn't connected yet: the server has no LiveKit credentials. See LIVE_STREAMING.md.", False
    reap_stale()
    existing = sb.get("live_sessions", host_id=host["id"], status="live")
    if existing:
        sb.update("live_sessions", {"last_heartbeat_at": iso()}, id=existing["id"])
        return existing, None, True
    title = clean_text(title, 80)
    if not title:
        return None, "Give your live a title.", False
    allowed, why = check_text(title)
    if not allowed:
        return None, why, False
    category = category if category in CATEGORIES else "chat"
    mode = mode if mode in ("video", "audio") else "video"
    visibility = visibility if visibility in ("public", "followers") else "public"
    if host.get("is_private"):
        visibility = "followers"                        # a private account is never shown to strangers
    since = iso(utcnow() - timedelta(hours=1))
    recent = db().table("live_sessions").select("id").eq("host_id", host["id"]).gte("started_at", since).limit(cfg("LIVE_STARTS_PER_HOUR") + 1).execute().data or []
    if len(recent) >= cfg("LIVE_STARTS_PER_HOUR"):
        return None, "You've started a lot of lives this hour. Please try again later.", False
    sid = str(uuid.uuid4())
    row = {"id": sid, "host_id": host["id"], "title": title, "category": category, "mode": mode, "visibility": visibility,
           "room_name": f"cirq-live-{sid}", "last_heartbeat_at": iso()}
    try:
        sess = sb.insert("live_sessions", row)
    except Exception:                                    # the one-live-per-host index fired: two tabs started at once
        existing = sb.get("live_sessions", host_id=host["id"], status="live")
        if existing:
            return existing, None, True
        raise
    audit(host["id"], "live_start", "live_session", sid, mode=mode, visibility=visibility)
    notify_followers(host, sess)
    return sess, None, False


def end_session(sess, actor, reason="host_ended"):
    """End once. Safe to call repeatedly (returns False when it was already over)."""
    ended = bool(_rpc("cirq_live_end_session", {"p_session": sess["id"], "p_reason": reason}))
    if ended:
        try:
            lk.delete_room(sess["room_name"])
        except lk.LiveKitError as exc:
            current_app.logger.warning("live: room close failed for %s: %s", sess["id"], exc)
        audit(actor["id"] if actor else None, "live_end", "live_session", sess["id"], reason=reason)
    return ended


def host_heartbeat(sess):
    count = viewer_count(sess["id"])
    values = {"last_heartbeat_at": iso()}
    if count > (sess.get("peak_viewers") or 0):
        values["peak_viewers"] = count
    sb.update("live_sessions", values, id=sess["id"])
    return count


def notify_followers(host, sess):
    """One in-app notification per follower who has not switched live alerts off. Capped so going live is never slow."""
    follower_ids = [r["follower_id"] for r in sb.select("follows", "follower_id", limit=cfg("LIVE_NOTIFY_MAX"), followee_id=host["id"])]
    if not follower_ids:
        return 0
    hidden = social.blocked_between(host["id"])
    rows = []
    for p in sb.in_("profiles", "id", follower_ids, "id,live_notify_enabled,account_status"):
        if p.get("account_status") == "approved" and p.get("live_notify_enabled", True) and p["id"] not in hidden:
            rows.append({"user_id": p["id"], "actor_id": host["id"], "kind": "live_started", "ref_type": "live", "ref_id": sess["id"],
                         "body": f"is live now: {sess['title']}"})
    for i in range(0, len(rows), 100):
        db().table("notifications").insert(rows[i:i + 100]).execute()
    return len(rows)


# ------------------------------------------------------------------ access, roles, viewers
def viewer_row(sid, uid):
    return sb.get("live_viewers", session_id=sid, user_id=uid)


def access_error(sess, user):
    """None if `user` may watch this live, otherwise (code, message)."""
    if not sess or sess["status"] != "live":
        return "ended", "This live has ended."
    uid, host = user["id"], sess["host_id"]
    if uid == host or is_staff(user):
        return None
    if social.is_blocked(uid, host):
        return "unavailable", "This live isn't available."
    if sb.get("live_host_blocks", host_id=host, user_id=uid):
        return "blocked", "You can't join this creator's lives."
    row = viewer_row(sess["id"], uid)
    if row and row.get("removed"):
        return "removed", "You were removed from this live."
    if sess["visibility"] == "followers" and not social.is_following(uid, host):
        return "followers_only", "This live is for followers only. Follow the creator to watch."
    return None


def authority(sess, user, row=None):
    """The moderation level of `user` in this room: host > staff > moderator > None."""
    if user["id"] == sess["host_id"]:
        return "host"
    if is_staff(user):
        return "staff"
    row = row if row is not None else viewer_row(sess["id"], user["id"])
    return "moderator" if row and row.get("role") == "moderator" else None


def join(sess, user):
    """Register presence. Returns None, or (code, message) when the person may not watch."""
    err = access_error(sess, user)
    if err:
        return err
    if user["id"] == sess["host_id"]:
        return None
    now = iso()
    row = viewer_row(sess["id"], user["id"])
    if row:
        sb.update("live_viewers", {"last_seen_at": now, "left_at": None}, session_id=sess["id"], user_id=user["id"])
    else:
        sb.insert("live_viewers", {"session_id": sess["id"], "user_id": user["id"], "joined_at": now, "last_seen_at": now})
    return None


def leave(sess, user):
    if viewer_row(sess["id"], user["id"]):
        sb.update("live_viewers", {"left_at": iso()}, session_id=sess["id"], user_id=user["id"])


def viewers_list(sess, limit=60):
    cutoff = iso(utcnow() - timedelta(seconds=cfg("LIVE_VIEWER_TIMEOUT_SECONDS")))
    rows = (db().table("live_viewers").select("*").eq("session_id", sess["id"]).gte("last_seen_at", cutoff).is_("left_at", "null")
            .order("joined_at", desc=True).limit(limit).execute().data or [])
    people = sb.by_id("profiles", [r["user_id"] for r in rows])
    supp = {r["user_id"]: r["total_coins"] for r in (db().table("live_supporters").select("user_id,total_coins").eq("host_id", sess["host_id"])
                                                     .in_("user_id", [r["user_id"] for r in rows] or ["-"]).execute().data or [])}
    out = []
    for r in rows:
        p = people.get(r["user_id"])
        if not p or not p.get("live_show_in_viewer_list", True):
            continue                                     # they chose to stay off the list (they still count in the total)
        out.append({**_public_person(p, 48), "role": r["role"], "supporter": supporter_tier(supp.get(r["user_id"]))})
    return out


# ------------------------------------------------------------------ realtime feed
def add_event(sid, kind, actor_id=None, body=None, payload=None):
    return sb.insert("live_events", {"session_id": sid, "kind": kind, "actor_id": actor_id, "body": body, "payload": payload or {}})


def _serialize_events(sess, events):
    ids = {e["actor_id"] for e in events if e.get("actor_id")}
    people = sb.by_id("profiles", ids) if ids else {}
    supp, mods = {}, set()
    if ids:
        supp = {r["user_id"]: r["total_coins"] for r in (db().table("live_supporters").select("user_id,total_coins").eq("host_id", sess["host_id"])
                                                         .in_("user_id", list(ids)).execute().data or [])}
        mods = {r["user_id"] for r in (db().table("live_viewers").select("user_id").eq("session_id", sess["id"]).eq("role", "moderator")
                                       .in_("user_id", list(ids)).execute().data or [])}
    out = []
    for e in events:
        if e["kind"] == "chat" and e.get("deleted_at"):
            continue
        user = None
        p = people.get(e.get("actor_id"))
        if p:
            badges = []
            if p["id"] == sess["host_id"]:
                badges.append("host")
            if p["id"] in mods:
                badges.append("mod")
            tier = supporter_tier(supp.get(p["id"]))
            if tier:
                badges.append(tier.lower())
            if p.get("verified"):
                badges.append("verified")
            user = {**_public_person(p, 48), "badges": badges}
        out.append({"id": e["id"], "kind": e["kind"], "body": e.get("body"), "payload": e.get("payload") or {}, "ts": e["created_at"], "user": user})
    return out


def _recent_by(sid, uid, kind, n):
    return sb.select("live_events", order="id", limit=n, session_id=sid, actor_id=uid, kind=kind)


def _age(row):
    t = parse_dt(row["created_at"])
    return (utcnow() - t).total_seconds() if t else 9999


def _muted(row):
    until = parse_dt((row or {}).get("muted_until"))
    return bool(until and until > utcnow())


def post_chat(sess, user, body):
    """(event, None) or (None, message). Spam rules: length, no links/phone numbers, 1/sec, per-minute cap, no repeats, slow mode."""
    if sess["status"] != "live":
        return None, "This live has ended."
    if not sess["chat_enabled"] and authority(sess, user) not in ("host", "staff"):
        return None, "Chat is turned off for this live."
    row = viewer_row(sess["id"], user["id"]) if user["id"] != sess["host_id"] else None
    if row and row.get("removed"):
        return None, "You were removed from this live."
    if _muted(row):
        return None, "You're muted in this live."
    text = re.sub(r"\s+", " ", clean_text(body, cfg("LIVE_CHAT_MAX_CHARS") * 2)).strip()[:cfg("LIVE_CHAT_MAX_CHARS")]
    text = "".join(ch for ch in text if ch.isprintable())
    if not text:
        return None, "Type a message first."
    allowed, why = check_text(text)
    if not allowed:
        return None, why
    if _URL.search(text):
        return None, "Links aren't allowed in live chat."
    privileged = authority(sess, user, row) is not None
    recent = _recent_by(sess["id"], user["id"], "chat", max(cfg("LIVE_CHAT_PER_MINUTE"), 1))
    if recent:
        gap = _age(recent[0])
        if gap < 1:
            return None, "You're sending messages too fast."
        slow = sess.get("slow_mode_seconds") or 0
        if slow and not privileged and gap < slow:
            return None, f"Slow mode is on. Wait {int(slow - gap) + 1}s."
        if len([r for r in recent if _age(r) < 60]) >= cfg("LIVE_CHAT_PER_MINUTE") and not privileged:
            return None, "Too many messages. Take a breath and try again in a moment."
        if any(r.get("body") == text and _age(r) < 20 for r in recent):
            return None, "You just said that."
    return add_event(sess["id"], "chat", user["id"], text), None


def post_reaction(sess, user, emoji):
    if sess["status"] != "live" or emoji not in REACTIONS:
        return False, "Invalid reaction."
    row = viewer_row(sess["id"], user["id"]) if user["id"] != sess["host_id"] else None
    if (row and row.get("removed")) or _muted(row):
        return False, "You can't react right now."
    recent = _recent_by(sess["id"], user["id"], "reaction", cfg("LIVE_REACTIONS_PER_MINUTE"))
    if len([r for r in recent if _age(r) < 60]) >= cfg("LIVE_REACTIONS_PER_MINUTE"):
        return False, "Slow down a little."
    add_event(sess["id"], "reaction", user["id"], None, {"emoji": emoji})
    return True, ""


def poll(sess, user, since, heartbeat=False, battle_id=None):
    """Everything new since event `since`. Cheap path: one events query. Every ~4th call (heartbeat) also refreshes presence, the audience
    count and the caller's own moderation status."""
    out = {"status": sess["status"], "server_time": iso()}
    row = None
    if user["id"] != sess["host_id"]:
        row = viewer_row(sess["id"], user["id"])
        if not row and not is_staff(user):
            return {"status": "denied", "error": "Join the live first."}
        if row and row.get("removed"):
            return {"status": "removed", "error": "You were removed from this live."}
    events = (db().table("live_events").select("*").eq("session_id", sess["id"]).gt("id", int(since)).order("id").limit(100).execute().data or [])
    out["events"] = _serialize_events(sess, events)
    out["cursor"] = events[-1]["id"] if events else int(since)
    if heartbeat and sess["status"] == "live":
        if user["id"] == sess["host_id"]:
            out["viewer_count"] = host_heartbeat(sess)
        else:
            if row:
                sb.update("live_viewers", {"last_seen_at": iso(), "left_at": None}, session_id=sess["id"], user_id=user["id"])
            out["viewer_count"] = viewer_count(sess["id"])
    out["me"] = {"balance": user.get("coin_balance", 0), "muted": _muted(row), "role": authority(sess, user, row)}
    if battle_id:
        out["battle"] = battle_snapshot(battle_id, session_id=sess["id"], user=user)
    return out


# ------------------------------------------------------------------ moderation
_ROOM_MOD = {"mute", "unmute", "timeout", "remove", "delete_message", "pin", "unpin"}
_HOST_ONLY = {"block", "unblock", "mod_add", "mod_remove"}


def _log_mod(sess, actor, target_id, action, reason=None, expires_at=None):
    sb.insert("live_moderation_actions", {"session_id": sess["id"], "actor_id": actor["id"], "target_id": target_id, "action": action,
                                          "reason": clean_text(reason, 200) or None, "expires_at": expires_at})


def _ensure_row(sess, target_id):
    row = viewer_row(sess["id"], target_id)
    if not row:
        row = sb.insert("live_viewers", {"session_id": sess["id"], "user_id": target_id, "left_at": iso()})
    return row


def moderate(sess, actor, action, username, minutes=None, reason=None):
    """Mute / unmute / timeout / remove / block / unblock / add or drop a moderator. Returns (ok, message)."""
    if sess["status"] != "live":
        return False, "This live has ended."
    level = authority(sess, actor)
    allowed = _ROOM_MOD if level in ("moderator", "staff") else (_ROOM_MOD | _HOST_ONLY if level == "host" else set())
    if action not in allowed:
        return False, "You can't do that in this live."
    target = _profile_by_username(username)
    if not target or target["id"] == actor["id"]:
        return False, "Choose someone else."
    if target["id"] == sess["host_id"] or is_staff(target):
        return False, "You can't moderate the host or CIRQ staff."
    trow = viewer_row(sess["id"], target["id"])
    if trow and trow.get("role") == "moderator" and level != "host" and action not in ("unmute",):
        return False, "Only the host can moderate a moderator."
    now = utcnow()
    expires = None
    if action == "mute":
        _ensure_row(sess, target["id"])
        sb.update("live_viewers", {"muted_until": iso(FOREVER)}, session_id=sess["id"], user_id=target["id"])
    elif action == "unmute":
        sb.update("live_viewers", {"muted_until": None}, session_id=sess["id"], user_id=target["id"])
    elif action == "timeout":
        mins = min(max(int(minutes or 5), 1), 1440)
        expires = now + timedelta(minutes=mins)
        _ensure_row(sess, target["id"])
        sb.update("live_viewers", {"muted_until": iso(expires)}, session_id=sess["id"], user_id=target["id"])
    elif action in ("remove", "block"):
        _ensure_row(sess, target["id"])
        sb.update("live_viewers", {"removed": True, "left_at": iso(), "role": "viewer"}, session_id=sess["id"], user_id=target["id"])
        _drop_guest(sess, target["id"])
        try:
            lk.remove_participant(sess["room_name"], lk.identity_for(target["id"]))
        except lk.LiveKitError as exc:
            current_app.logger.warning("live: remove participant failed: %s", exc)
        if action == "block":
            try:
                sb.insert("live_host_blocks", {"host_id": sess["host_id"], "user_id": target["id"]})
            except Exception:
                pass                                       # already blocked
    elif action == "unblock":
        sb.delete("live_host_blocks", host_id=sess["host_id"], user_id=target["id"])
    elif action == "mod_add":
        row = _ensure_row(sess, target["id"])
        if row.get("removed"):
            return False, "That person was removed from this live."
        sb.update("live_viewers", {"role": "moderator"}, session_id=sess["id"], user_id=target["id"])
    elif action == "mod_remove":
        sb.update("live_viewers", {"role": "viewer"}, session_id=sess["id"], user_id=target["id"])
    else:
        return False, "Unknown action."
    _log_mod(sess, actor, target["id"], action, reason, iso(expires) if expires else None)
    if action in ("remove", "block", "mod_add", "mod_remove", "unblock"):
        audit(actor["id"], f"live_{action}", "live_session", sess["id"], target=target["username"])
    add_event(sess["id"], "mod", actor["id"], None, {"action": action, "target": target["username"], "minutes": minutes if action == "timeout" else None})
    return True, ""


def delete_message(sess, actor, event_id):
    ev = sb.get("live_events", id=int(event_id), session_id=sess["id"], kind="chat")
    if not ev or ev.get("deleted_at"):
        return False, "Message not found."
    if ev["actor_id"] != actor["id"] and authority(sess, actor) is None:
        return False, "You can't delete that message."
    sb.update("live_events", {"deleted_at": iso()}, id=ev["id"])
    if sess.get("pinned_event_id") == ev["id"]:
        sb.update("live_sessions", {"pinned_event_id": None}, id=sess["id"])
    _log_mod(sess, actor, ev["actor_id"], "delete_message")
    add_event(sess["id"], "delete_msg", actor["id"], None, {"event_id": ev["id"]})
    return True, ""


def pin_message(sess, actor, event_id):
    if authority(sess, actor) is None:
        return False, "Only the host or a moderator can pin."
    ev = sb.get("live_events", id=int(event_id), session_id=sess["id"], kind="chat")
    if not ev or ev.get("deleted_at"):
        return False, "Message not found."
    sb.update("live_sessions", {"pinned_event_id": ev["id"]}, id=sess["id"])
    _log_mod(sess, actor, ev["actor_id"], "pin")
    author = sb.get("profiles", "username,display_name", id=ev["actor_id"]) or {}
    add_event(sess["id"], "pin", actor["id"], ev["body"], {"event_id": ev["id"], "sender": author.get("username"), "sender_name": author.get("display_name")})
    return True, ""


def unpin_message(sess, actor):
    if authority(sess, actor) is None:
        return False, "Only the host or a moderator can unpin."
    sb.update("live_sessions", {"pinned_event_id": None}, id=sess["id"])
    _log_mod(sess, actor, None, "unpin")
    add_event(sess["id"], "unpin", actor["id"])
    return True, ""


def update_settings(sess, host, chat_enabled=None, slow_mode_seconds=None, guests_enabled=None):
    if host["id"] != sess["host_id"]:
        return False, "Only the host can change this."
    values = {}
    if chat_enabled is not None:
        values["chat_enabled"] = bool(chat_enabled)
    if guests_enabled is not None:
        values["guests_enabled"] = bool(guests_enabled)
    if slow_mode_seconds is not None:
        values["slow_mode_seconds"] = min(max(int(slow_mode_seconds), 0), 120)
        _log_mod(sess, host, None, "slow_mode", reason=str(values["slow_mode_seconds"]))
    if values:
        sb.update("live_sessions", values, id=sess["id"])
        add_event(sess["id"], "state", host["id"], None, {"what": "settings"})
    return True, ""


def report(sess, reporter, reason, username=None, event_id=None, details=None):
    if reason not in REPORT_REASONS:
        return False, "Choose a reason."
    target = _profile_by_username(username) if username else None
    if username and not target:
        return False, "Person not found."
    if target and target["id"] == reporter["id"]:
        return False, "You can't report yourself."
    if event_id:
        ev = sb.get("live_events", id=int(event_id), session_id=sess["id"])
        if not ev:
            return False, "Message not found."
        target = target or (sb.get("profiles", id=ev["actor_id"]) if ev.get("actor_id") else None)
    row = {"session_id": sess["id"], "reporter_id": reporter["id"], "target_user_id": target["id"] if target else None,
           "event_id": int(event_id) if event_id else None, "reason": reason, "details": clean_text(details, 500) or None}
    try:
        sb.insert("live_reports", row)
    except Exception:
        return True, "You've already reported this. Our team will review it."
    return True, "Thanks. Our team will review your report."


# ------------------------------------------------------------------ guests
def _guest_sources(inv):
    out = []
    if inv.get("can_publish_audio"):
        out.append("microphone")
    if inv.get("can_publish_video") and inv.get("media") != "audio":
        out.append("camera")
    return out


def _drop_guest(sess, guest_id, status="removed"):
    inv = sb.get("live_guest_invites", session_id=sess["id"], guest_id=guest_id)
    if inv and inv["status"] in ("invited", "requested", "accepted"):
        sb.update("live_guest_invites", {"status": status, "ended_at": iso()}, id=inv["id"])
        if inv["status"] == "accepted":
            try:
                lk.remove_participant(sess["room_name"], lk.identity_for(guest_id))
            except lk.LiveKitError as exc:
                current_app.logger.warning("live: guest removal failed: %s", exc)
        return inv
    return None


def _in_room(sess, uid):
    row = viewer_row(sess["id"], uid)
    seen = parse_dt((row or {}).get("last_seen_at"))
    return bool(row and not row.get("removed") and not row.get("left_at") and seen and (utcnow() - seen).total_seconds() < cfg("LIVE_VIEWER_TIMEOUT_SECONDS") * 2)


def invite_guest(sess, host, username, media="video"):
    if host["id"] != sess["host_id"] or sess["status"] != "live":
        return False, "Only the host of a live can invite guests."
    if not sess["guests_enabled"]:
        return False, "Guests are switched off for this live."
    target = _profile_by_username(username)
    if not target or target["id"] == host["id"] or target.get("account_status") != "approved":
        return False, "Choose a viewer who is in your live."
    if social.is_blocked(host["id"], target["id"]) or not _in_room(sess, target["id"]):
        return False, "That person needs to be watching your live to be invited."
    media = media if media in ("video", "audio") and sess["mode"] == "video" else "audio"
    inv = sb.get("live_guest_invites", session_id=sess["id"], guest_id=target["id"])
    values = {"status": "invited", "initiated_by": "host", "media": media, "can_publish_audio": True, "can_publish_video": media == "video",
              "consent_at": None, "accepted_at": None, "ended_at": None}
    if inv:
        if inv["status"] in ("invited", "requested", "accepted"):
            return False, "They already have an invitation or are a guest."
        sb.update("live_guest_invites", values, id=inv["id"])
    else:
        sb.insert("live_guest_invites", {"session_id": sess["id"], "guest_id": target["id"], **values})
    add_event(sess["id"], "guest", host["id"], None, {"state": "invited", "username": target["username"]})
    return True, ""


def request_guest(sess, user, media="video"):
    if sess["status"] != "live" or user["id"] == sess["host_id"] or not sess["guests_enabled"]:
        return False, "Guests can't join this live."
    if access_error(sess, user) or not _in_room(sess, user["id"]):
        return False, "Join the live first."
    media = media if media in ("video", "audio") and sess["mode"] == "video" else "audio"
    inv = sb.get("live_guest_invites", session_id=sess["id"], guest_id=user["id"])
    values = {"status": "requested", "initiated_by": "guest", "media": media, "can_publish_audio": True, "can_publish_video": media == "video",
              "consent_at": iso(), "accepted_at": None, "ended_at": None}
    if inv:
        if inv["status"] in ("invited", "requested", "accepted"):
            return False, "You already have a request or invitation here."
        sb.update("live_guest_invites", values, id=inv["id"])
    else:
        sb.insert("live_guest_invites", {"session_id": sess["id"], "guest_id": user["id"], **values})
    add_event(sess["id"], "guest", user["id"], None, {"state": "requested", "username": user["username"]})
    return True, ""


def respond_guest(sess, actor, invite_id, accept):
    inv = sb.get("live_guest_invites", id=invite_id, session_id=sess["id"])
    if not inv:
        return False, "Invitation not found."
    guest = sb.get("profiles", "username", id=inv["guest_id"]) or {}
    if accept:
        res = _rpc("cirq_live_guest_accept", {"p_invite": invite_id, "p_actor": actor["id"], "p_max": cfg("LIVE_MAX_GUESTS")})
        msg = {"ok": "", "full": f"All {cfg('LIVE_MAX_GUESTS')} guest spots are taken.", "forbidden": "That's not yours to answer.",
               "bad_state": "That invitation is no longer open.", "ended": "This live has ended.", "missing": "Invitation not found."}.get(res, "Couldn't accept.")
        if res != "ok":
            return False, msg
        add_event(sess["id"], "guest", actor["id"], None, {"state": "accepted", "username": guest.get("username")})
        add_event(sess["id"], "state", actor["id"], None, {"what": "guests"})
        return True, ""
    mine = (inv["status"] == "invited" and actor["id"] == inv["guest_id"]) or (inv["status"] == "requested" and actor["id"] == sess["host_id"])
    if not mine:
        return False, "That invitation is no longer open."
    sb.update("live_guest_invites", {"status": "rejected", "ended_at": iso()}, id=inv["id"], status=inv["status"])
    add_event(sess["id"], "guest", actor["id"], None, {"state": "rejected", "username": guest.get("username")})
    return True, ""


def remove_guest(sess, host, invite_id):
    inv = sb.get("live_guest_invites", id=invite_id, session_id=sess["id"])
    if host["id"] != sess["host_id"] or not inv:
        return False, "Not allowed."
    _drop_guest(sess, inv["guest_id"], "removed")
    _log_mod(sess, host, inv["guest_id"], "guest_remove")
    add_event(sess["id"], "state", host["id"], None, {"what": "guests"})
    return True, ""


def leave_guest(sess, user):
    inv = _drop_guest(sess, user["id"], "left")
    if inv:
        add_event(sess["id"], "state", user["id"], None, {"what": "guests"})
    return bool(inv)


def set_guest_permissions(sess, host, invite_id, audio=None, video=None):
    if host["id"] != sess["host_id"]:
        return False, "Not allowed."
    inv = sb.get("live_guest_invites", id=invite_id, session_id=sess["id"], status="accepted")
    if not inv:
        return False, "Guest not found."
    values = {}
    if audio is not None:
        values["can_publish_audio"] = bool(audio)
    if video is not None:
        values["can_publish_video"] = bool(video) and inv["media"] == "video"
    sb.update("live_guest_invites", values, id=inv["id"])
    merged = {**inv, **values}
    try:
        sources = _guest_sources(merged)
        lk.set_publish_permissions(sess["room_name"], lk.identity_for(inv["guest_id"]), can_publish=bool(sources), sources=sources)
    except lk.LiveKitError as exc:
        current_app.logger.warning("live: permission update failed: %s", exc)
    if audio is False:
        _log_mod(sess, host, inv["guest_id"], "guest_mute")
    add_event(sess["id"], "state", host["id"], None, {"what": "guests"})
    return True, ""


def guests_state(sess, user):
    rows = sb.select("live_guest_invites", session_id=sess["id"])
    people = sb.by_id("profiles", [r["guest_id"] for r in rows])
    is_host = user["id"] == sess["host_id"]
    accepted, pending, mine = [], [], None
    for r in rows:
        p = people.get(r["guest_id"])
        if not p:
            continue
        item = {**_public_person(p, 64), "media": r["media"], "audio": r["can_publish_audio"], "video": r["can_publish_video"] and r["media"] == "video",
                "identity": lk.identity_for(p["id"]) if lk.configured() else ""}
        if r["status"] == "accepted":
            accepted.append({**item, **({"invite_id": r["id"]} if is_host else {})})
        elif r["status"] in ("invited", "requested") and is_host:
            pending.append({**item, "invite_id": r["id"], "status": r["status"]})
        if r["guest_id"] == user["id"] and r["status"] in ("invited", "requested", "accepted"):
            mine = {"invite_id": r["id"], "status": r["status"], "media": r["media"]}
    return {"accepted": accepted, "pending": pending, "mine": mine, "max": cfg("LIVE_MAX_GUESTS")}


# ------------------------------------------------------------------ tokens
def issue_token(sess, user, target="self"):
    """(payload, error). Hosts publish; accepted guests publish only what the host allowed and only after consenting; everyone else
    can only watch. `target="opponent"` returns a watch-only token for the other host's room during a PK battle."""
    if not lk.configured():
        return None, "Live streaming isn't connected on this server."
    err = join(sess, user) if user["id"] != sess["host_id"] else access_error(sess, user)
    if err:
        return None, err[1]
    identity = lk.identity_for(user["id"])
    meta = '{"u":"%s"}' % user["username"]
    name = user.get("display_name") or user["username"]
    if target == "opponent":
        b = current_battle(sess["id"])
        if not b or b["status"] not in ("countdown", "active"):
            return None, "There's no battle running."
        other = b["sides"]["b"]["session_id"] if b["sides"]["a"]["session_id"] == sess["id"] else b["sides"]["a"]["session_id"]
        other_sess = get_session(other)
        if not other_sess or other_sess["status"] != "live":
            return None, "The other creator is no longer live."
        token = lk.mint_token(identity, other_sess["room_name"], name=name, metadata=meta, can_publish=False, ttl=3600)
        return {"url": lk.ws_url(), "token": token, "role": "viewer", "can_publish": False, "sources": [], "room": "opponent"}, None
    role, sources, ttl = "viewer", [], 3600
    if user["id"] == sess["host_id"]:
        role, sources, ttl = "host", (lk.SOURCES_VIDEO_HOST if sess["mode"] == "video" else lk.SOURCES_AUDIO_HOST), 4 * 3600
    else:
        inv = sb.get("live_guest_invites", session_id=sess["id"], guest_id=user["id"], status="accepted")
        if inv and inv.get("consent_at"):
            role, sources, ttl = "guest", _guest_sources(inv), 3600
    token = lk.mint_token(identity, sess["room_name"], name=name, metadata=meta, can_publish=bool(sources), sources=sources, ttl=ttl)
    return {"url": lk.ws_url(), "token": token, "role": role, "can_publish": bool(sources), "sources": sources, "room": "self"}, None


# ------------------------------------------------------------------ gifts
def gifts_catalog():
    out = []
    for g in sb.select("live_gifts", order="sort_order", desc=False, active=True):
        host = g["price"] * g["host_share_pct"] // 100
        out.append({"code": g["code"], "name": g["name"], "price": g["price"], "host_coins": host, "tier": g["tier"], "icon": g["icon"],
                    "color": g.get("color"), "diamonds": g["display_diamonds"], "diamond_value": g["diamond_display_value"]})
    return out


_GIFT_ERRORS = {
    "session_ended": (410, "This live has ended."),
    "self_gift": (400, "You can't send gifts to yourself."),
    "gift_unavailable": (404, "That gift isn't available."),
    "blocked": (403, "You can't send gifts in this live."),
    "bad_request": (400, "Something was wrong with that request. Try again."),
    "request_reuse": (409, "That request was already used for a different gift."),
}


def send_gift(user, sess, code, request_id):
    """(payload, http_status). The browser supplies only WHICH gift and a per-tap request id; price, split and host come from the database."""
    err = access_error(sess, user) if user["id"] != sess["host_id"] else None
    if err:
        return {"ok": False, "error": err[1], "code": err[0]}, 403 if err[0] != "ended" else 410
    res = _rpc("cirq_live_send_gift", {"p_sender": user["id"], "p_session": sess["id"], "p_gift": code, "p_request": request_id,
                                       "p_daily_cap": cfg("LIVE_GIFT_DAILY_COINS")})
    status = res.get("status")
    if status == "insufficient":
        return {"ok": False, "code": status, "error": f"You need {res['price']} coins for that gift and have {res['balance']}.", "balance": res["balance"]}, 402
    if status == "cap_reached":
        return {"ok": False, "code": status, "error": "You've reached today's live gift limit. Try again tomorrow."}, 429
    if status in _GIFT_ERRORS:
        http, msg = _GIFT_ERRORS[status]
        return {"ok": False, "code": status, "error": msg}, http
    if status not in ("ok", "duplicate"):
        return {"ok": False, "code": "error", "error": "Couldn't send the gift."}, 500
    # The host just EARNED coins: honour any overdue-loan collection exactly like every other earning (idempotent on the transaction id).
    try:
        lending.garnish(res["host_id"], res["host_coins"], "live_gift_in", res["txn_id"])
    except Exception:
        current_app.logger.exception("live: loan garnish failed for gift %s", res.get("txn_id"))
    cat = {g["code"]: g for g in gifts_catalog()}.get(res["gift_code"], {})
    host = sb.get("profiles", "username,display_name", id=res["host_id"]) or {}
    return {"ok": True, "duplicate": status == "duplicate", "txn_id": res["txn_id"],
            "gift": {"code": res["gift_code"], "name": cat.get("name"), "icon": cat.get("icon"), "tier": cat.get("tier")},
            "gross": res["gross"], "host_coins": res["host_coins"], "platform_coins": res["platform_coins"], "diamonds": res["diamonds"],
            "diamond_value": res["diamond_value"], "reward_coins": res["host_coins"], "balance": res["balance"], "battle_id": res.get("battle_id"),
            "scores": res.get("scores"),
            "message": _gift_message(res, cat.get("name") or "gift", host)}, 200


def _gift_message(res, name, host):
    to = host.get("display_name") or host.get("username") or "the host"
    if res["gift_code"] == "mystery_box":
        return f"Your Mystery Box opened! {to} received {res['host_coins']} coins."
    if res["diamonds"]:
        return (f"{res['diamonds']} diamonds shown ({res['diamonds']} × {res['diamond_value']} = {res['diamonds'] * res['diamond_value']} displayed value). "
                f"{to} earned {res['host_coins']} coins.")
    return f"You sent {name}. {to} earned {res['host_coins']} coins."


# ------------------------------------------------------------------ PK battles
def _side_info(part, people, sessions):
    p = people.get(part["host_id"], {})
    return {**(_public_person(p, 64) if p else {}), "session_id": part["session_id"], "score": part["score"]}


def battle_snapshot(battle_id, session_id=None, user=None, tick=True):
    """Advance the battle clock (idempotent) and describe it. None if it doesn't exist or doesn't involve this session."""
    if tick:
        _rpc("cirq_live_battle_tick", {"p_battle": battle_id, "p_force": False})
    b = sb.get("live_battles", id=battle_id)
    if not b or (session_id and session_id not in (b["session_a"], b["session_b"])):
        return None
    parts = {p["side"]: p for p in sb.select("live_battle_participants", battle_id=b["id"])}
    people = sb.by_id("profiles", [p["host_id"] for p in parts.values()])
    sides = {s: _side_info(parts[s], people, None) for s in ("a", "b") if s in parts}
    winner = sb.get("profiles", "username", id=b["winner_host_id"]) if b.get("winner_host_id") else None
    you = None
    if user:
        you = next((s for s, pt in parts.items() if pt["host_id"] == user["id"]), None)
    if you is None and session_id:
        you = next((s for s, pt in parts.items() if pt["session_id"] == session_id), None)
    return {"id": b["id"], "status": b["status"], "duration_seconds": b["duration_seconds"], "countdown_seconds": b["countdown_seconds"],
            "starts_at": b.get("starts_at"), "ends_at": b.get("ends_at"), "outcome": b.get("outcome"), "winner": winner["username"] if winner else None,
            "scores": {s: sides[s]["score"] for s in sides}, "sides": sides, "you_side": you, "server_time": iso()}


def current_battle(session_id):
    """The latest battle of this session that is running, counting down, or ended in the last 90 seconds."""
    parts = sb.select("live_battle_participants", session_id=session_id)
    if not parts:
        return None
    battles = sb.in_("live_battles", "id", [p["battle_id"] for p in parts])
    now = utcnow()
    keep = []
    for b in battles:
        if b["status"] in ("countdown", "active"):
            keep.append(b)
        elif b["status"] == "ended" and b.get("ended_at") and (now - parse_dt(b["ended_at"])).total_seconds() < 90:
            keep.append(b)
    if not keep:
        return None
    keep.sort(key=lambda b: b["invited_at"], reverse=True)
    return battle_snapshot(keep[0]["id"], session_id=session_id, tick=keep[0]["status"] in ("countdown", "active"))


def battle_invites_for(host):
    now = iso()
    rows = [b for b in sb.select("live_battles", host_b=host["id"], status="invited") if b["expires_at"] > now]
    people = sb.by_id("profiles", [b["host_a"] for b in rows])
    return [{"id": b["id"], "from": _public_person(people[b["host_a"]], 64), "duration_minutes": b["duration_seconds"] // 60, "expires_at": b["expires_at"]}
            for b in rows if b["host_a"] in people]


def battle_invite(sess, host, username, minutes):
    if host["id"] != sess["host_id"] or sess["status"] != "live":
        return False, "Only a live host can start a PK battle.", None
    minutes = int(minutes or 5)
    if minutes not in BATTLE_MINUTES:
        return False, "Choose a battle length of " + ", ".join(str(m) for m in BATTLE_MINUTES) + " minutes.", None
    target = _profile_by_username(username)
    if not target or target["id"] == host["id"]:
        return False, "Choose another creator.", None
    other = sb.get("live_sessions", host_id=target["id"], status="live")
    if not other:
        return False, f"@{target['username']} isn't live right now.", None
    if social.is_blocked(host["id"], target["id"]):
        return False, "You can't battle that creator.", None
    for sid in (sess["id"], other["id"]):
        if current_battle(sid):
            return False, "A PK battle is already in progress.", None
    now = iso()
    if [b for b in sb.select("live_battles", host_a=host["id"], status="invited") if b["expires_at"] > now]:
        return False, "You already have a pending PK invite.", None
    expires = iso(utcnow() + timedelta(seconds=cfg("LIVE_BATTLE_INVITE_SECONDS")))
    b = sb.insert("live_battles", {"session_a": sess["id"], "session_b": other["id"], "host_a": host["id"], "host_b": target["id"],
                                   "duration_seconds": minutes * 60, "countdown_seconds": cfg("LIVE_BATTLE_COUNTDOWN_SECONDS"), "expires_at": expires})
    sb.insert("live_battle_participants", {"battle_id": b["id"], "host_id": host["id"], "session_id": sess["id"], "side": "a"})
    sb.insert("live_battle_participants", {"battle_id": b["id"], "host_id": target["id"], "session_id": other["id"], "side": "b"})
    add_event(other["id"], "state", host["id"], None, {"what": "battle_invite", "from": host["username"], "minutes": minutes})
    return True, "", b["id"]


_BATTLE_MSG = {"forbidden": "That invite isn't yours to answer.", "bad_state": "That invite is no longer open.", "expired": "That invite expired.",
               "not_live": "One of the creators is no longer live.", "busy": "One of the creators is already in a battle.", "missing": "Invite not found."}


def battle_respond(battle_id, actor, accept):
    b = sb.get("live_battles", id=battle_id)
    if not b:
        return False, "Invite not found."
    if accept:
        res = _rpc("cirq_live_battle_accept", {"p_battle": battle_id, "p_actor": actor["id"], "p_countdown": cfg("LIVE_BATTLE_COUNTDOWN_SECONDS")})
        return (True, "") if res == "ok" else (False, _BATTLE_MSG.get(res, "Couldn't start the battle."))
    if b["host_b"] != actor["id"] or b["status"] != "invited":
        return False, "That invite is no longer open."
    sb.update("live_battles", {"status": "declined", "ended_at": iso()}, id=battle_id, status="invited")
    return True, ""


def battle_end(battle_id, actor):
    b = sb.get("live_battles", id=battle_id)
    if not b or actor["id"] not in (b["host_a"], b["host_b"]):
        return False, "Not allowed."
    _rpc("cirq_live_battle_tick", {"p_battle": battle_id, "p_force": True})
    return True, ""


# ------------------------------------------------------------------ the full room state (one call when a page opens or the host changes something)
def room_state(sess, user):
    host = sb.get("profiles", id=sess["host_id"])
    me_row = viewer_row(sess["id"], user["id"]) if user["id"] != sess["host_id"] else None
    level = authority(sess, user, me_row)
    last = db().table("live_events").select("*").eq("session_id", sess["id"]).order("id", desc=True).limit(60).execute().data or []
    history = _serialize_events(sess, [e for e in reversed(last) if e["kind"] in ("chat", "gift", "pin")])
    pinned = None
    if sess.get("pinned_event_id"):
        ev = sb.get("live_events", id=sess["pinned_event_id"])
        if ev and not ev.get("deleted_at"):
            pinned = _serialize_events(sess, [ev])[0]
    battle = current_battle(sess["id"])
    fan = bool(sb.get("live_fan_club_members", host_id=host["id"], user_id=user["id"])) if user["id"] != host["id"] else False
    supp = sb.get("live_supporters", host_id=host["id"], user_id=user["id"]) if user["id"] != host["id"] else None
    lifetime = (_rpc("cirq_live_creator_summary", {"p_host": host["id"]}) or {}).get("host_earnings", 0)
    state = {
        "session": {"id": sess["id"], "title": sess["title"], "category": sess["category"], "mode": sess["mode"], "visibility": sess["visibility"],
                    "status": sess["status"], "started_at": sess["started_at"], "chat_enabled": sess["chat_enabled"], "guests_enabled": sess["guests_enabled"],
                    "slow_mode_seconds": sess["slow_mode_seconds"]},
        "host": {**_public_person(host, 128), "level": creator_level(lifetime)["level"], "followers": social.follower_count(host["id"]),
                 "follow_state": social.follow_state(user["id"], host["id"]) if user["id"] != host["id"] else "self", "private": bool(host.get("is_private"))},
        "me": {"username": user["username"], "role": level, "is_host": user["id"] == host["id"], "can_moderate": level is not None, "balance": user.get("coin_balance", 0),
               "muted": _muted(me_row), "fan_club": fan, "supporter": supporter_tier((supp or {}).get("total_coins")), "can_request_guest": bool(sess["guests_enabled"]) and user["id"] != host["id"]},
        "viewer_count": viewer_count(sess["id"]), "viewers": viewers_list(sess, 40), "history": history, "pinned": pinned,
        "cursor": last[0]["id"] if last else 0, "guests": guests_state(sess, user), "battle": battle, "gifts": gifts_catalog(),
        "reactions": REACTIONS, "report_reasons": REPORT_REASONS, "battle_minutes": list(BATTLE_MINUTES),
        "poll_seconds": cfg("LIVE_POLL_SECONDS"), "max_chat": cfg("LIVE_CHAT_MAX_CHARS"), "provider_ready": lk.configured(),
    }
    state["battle_invites"] = battle_invites_for(user) if level == "host" else []
    if level == "host":
        mods = sb.by_id("profiles", [r["user_id"] for r in sb.select("live_viewers", session_id=sess["id"], role="moderator")], "id,username,display_name")
        state["moderators"] = [{"username": m["username"], "display_name": m["display_name"]} for m in mods.values()]
    return state


# ------------------------------------------------------------------ creator wallet, rankings, privacy
def creator_dashboard(host):
    summary = _rpc("cirq_live_creator_summary", {"p_host": host["id"]}) or {}
    tx = sb.select("live_gift_transactions", order="created_at", limit=30, host_id=host["id"])
    senders = sb.by_id("profiles", [t["sender_id"] for t in tx])
    names = {g["code"]: g["name"] for g in gifts_catalog()}
    history = [{"id": t["id"], "when": t["created_at"], "gift": names.get(t["gift_code"], t["gift_code"]), "code": t["gift_code"],
                "from": senders.get(t["sender_id"], {}).get("username", "unknown"), "gross": t["gross_coins"], "earned": t["host_coins"],
                "platform": t["platform_coins"], "diamonds": t["displayed_diamonds"], "diamond_value": t["displayed_diamond_value"]} for t in tx]
    supporters = sb.select("live_supporters", order="total_coins", limit=10, host_id=host["id"])
    sp_people = sb.by_id("profiles", [s["user_id"] for s in supporters])
    top = [{"username": sp_people[s["user_id"]]["username"], "display_name": sp_people[s["user_id"]]["display_name"], "total": s["total_coins"],
            "gifts": s["gifts_count"], "tier": supporter_tier(s["total_coins"])} for s in supporters if s["user_id"] in sp_people]
    sessions = sb.select("live_sessions", order="started_at", limit=15, host_id=host["id"])
    wallet = sb.get("profiles", "coin_balance", id=host["id"]) or {}
    club = sb.count("live_fan_club_members", host_id=host["id"])
    return {"summary": summary, "history": history, "supporters": top, "sessions": sessions, "wallet_balance": wallet.get("coin_balance", 0),
            "level": creator_level(summary.get("host_earnings", 0)), "fan_club_members": club}


def rankings(limit=20):
    since = iso(week_start())
    hosts_w = _rpc("cirq_live_host_ranking", {"p_since": since, "p_limit": limit}) or []
    hosts_all = _rpc("cirq_live_host_ranking", {"p_since": None, "p_limit": limit}) or []
    gifters_w = _rpc("cirq_live_top_gifters", {"p_host": None, "p_since": since, "p_limit": limit}) or []
    people = sb.by_id("profiles", [r["host_id"] for r in hosts_w + hosts_all] + [r["user_id"] for r in gifters_w])

    def host_rows(rows):
        return [{**_public_person(people[r["host_id"]], 64), "coins": r["host_coins"], "gifts": r["gifts"], "level": creator_level(r["host_coins"])["level"]}
                for r in rows if r["host_id"] in people]
    return {"week_start": since, "hosts_week": host_rows(hosts_w), "hosts_all": host_rows(hosts_all),
            "gifters_week": [{**_public_person(people[r["user_id"]], 64), "coins": r["total_coins"], "gifts": r["gifts"]} for r in gifters_w if r["user_id"] in people]}


def update_privacy(user, notify_enabled, show_in_list):
    sb.update("profiles", {"live_notify_enabled": bool(notify_enabled), "live_show_in_viewer_list": bool(show_in_list)}, id=user["id"])


def my_fan_clubs(user):
    rows = sb.select("live_fan_club_members", order="joined_at", user_id=user["id"])
    hosts = sb.by_id("profiles", [r["host_id"] for r in rows])
    return [{**_public_person(hosts[r["host_id"]], 64), "joined_at": r["joined_at"]} for r in rows if r["host_id"] in hosts]


def leave_fan_club(user, username):
    host = _profile_by_username(username)
    if host:
        sb.delete("live_fan_club_members", host_id=host["id"], user_id=user["id"])
    return True
