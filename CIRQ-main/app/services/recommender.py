"""Personalised suggestions: learn what each student likes, then surface similar posts and reels.

How it works (all on-platform, explainable, no outside data):
  1. SIGNALS. Every like, comment, share, hide, dwell time on a post and watch-completion on a reel is stored in
     `post_signals`.
  2. INTERESTS. Each signal nudges a small per-student profile in `user_interests` along four dimensions:
     the author, the topics in the caption (#hashtags and keywords), the author's school, and the kind (post/reel).
     Weights fade with a 14-day half-life, so tastes can change.
  3. RANKING. Candidates (recent, public, approved posts) are scored = affinity + freshness + popularity
     + same-school + followed-author + a little daily variety, then diversified (one post per author).
  4. EXPLAINABLE. Every suggestion carries a short reason ("Because you like #football").

Safety: only public, approved, non-private, non-blocked content is ever suggested; hidden posts never return;
students can switch personalisation off or reset it (Settings). Nothing here is used for ads or leaves CIRQ.
"""
import hashlib
import logging
import math
import re
from datetime import timedelta

from app.extensions import db
from app.services import supabase_service as sb, social
from app.utils import iso, parse_dt, utcnow

log = logging.getLogger(__name__)

HALF_LIFE_DAYS = 14.0
MAX_W, MIN_W = 60.0, -30.0
PROFILE_ROWS = 400
CLIENT_EVENTS = {"view", "watch", "share", "hide"}        # what the browser may report; likes/comments are recorded server-side
ALL_EVENTS = CLIENT_EVENTS | {"like", "comment", "impression"}
DIMS = ("author", "topic", "school", "kind")

STOP = set("""about above after again also because been before being below between both came come could does done down each even
from have here into just like made make many more most much must only other over said same should since some such than that their
them then there these they this those through under very want well were what when where which while will with would your yours
class school students student today tomorrow yesterday thing things really people""".split())
TAG = re.compile(r"#(\w{2,30})", re.UNICODE)
WORD = re.compile(r"[^\W\d_]{4,20}", re.UNICODE)


# ---------------------------------------------------------------- topics & weights
def topics_of(text, limit=8):
    """{token: strength}. #hashtags are strong signals (1.0), plain keywords are weaker (0.5)."""
    if not text:
        return {}
    out = {"#" + t.lower(): 1.0 for t in TAG.findall(text)}
    counts = {}
    for w in WORD.findall(TAG.sub(" ", text).lower()):
        if w not in STOP:
            counts[w] = counts.get(w, 0) + 1
    for w, _ in sorted(counts.items(), key=lambda kv: -kv[1])[:limit]:
        out.setdefault(w, 0.5)
    return dict(list(out.items())[:limit])


def weight_for(event, value=0.0):
    """How strongly one signal says 'I like this' (positive) or 'show me less' (negative)."""
    value = float(value or 0)
    if event == "like":
        return 3.0
    if event == "comment":
        return 4.0
    if event == "share":
        return 4.0
    if event == "hide":
        return -8.0
    if event == "view":                       # dwell time in ms
        return 0.0 if value < 1500 else (0.3 if value < 4000 else 0.8)
    if event == "watch":                      # completion ratio; >1 means it was replayed
        if value < 0.2:
            return -0.6                       # swiped away quickly
        if value < 0.5:
            return 0.5
        if value < 0.9:
            return 2.0
        return 3.0 if value < 1.2 else 4.0
    return 0.0


def _features(post, author):
    yield "author", post["author_id"], 1.0
    yield "kind", post.get("kind") or "post", 0.3
    sid = (author or {}).get("school_id")
    if sid:
        yield "school", sid, 0.4
    for tok, strength in topics_of(post.get("caption")).items():
        yield "topic", tok, 0.6 * strength


# ---------------------------------------------------------------- profile (what this student likes)
def enabled(user):
    return (user or {}).get("personalized_recs", True) is not False


def empty_profile():
    return {d: {} for d in DIMS}


def load_profile(uid):
    now = utcnow()
    prof = empty_profile()
    rows = db().table("user_interests").select("*").eq("user_id", uid).order("updated_at", desc=True).limit(PROFILE_ROWS).execute().data or []
    for r in rows:
        if r["dim"] not in prof:
            continue
        age_days = max(0.0, (now - (parse_dt(r["updated_at"]) or now)).total_seconds() / 86400)
        w = float(r["weight"]) * 0.5 ** (age_days / HALF_LIFE_DAYS)
        if abs(w) >= 0.05:
            prof[r["dim"]][r["key"]] = w
    return prof


def record(uid, items, learn=True):
    """items: [{'post': row, 'author': profile|None, 'event': str, 'value': number}]. Stores signals and updates interests."""
    if not items:
        return
    now = utcnow()
    prof, changed, rows = (load_profile(uid) if learn else empty_profile()), {}, []
    for it in items:
        post, event, value = it["post"], it["event"], float(it.get("value") or 0)
        if event not in ALL_EVENTS:
            continue
        rows.append({"user_id": uid, "post_id": post["id"], "author_id": post["author_id"], "event": event, "value": round(value, 3)})
        w = weight_for(event, value)
        if not learn or not w or post["author_id"] == uid:
            continue
        for dim, key, mult in _features(post, it.get("author")):
            new = max(MIN_W, min(MAX_W, prof[dim].get(key, 0.0) + w * mult))
            prof[dim][key] = new
            changed[(dim, key)] = new
    if rows:
        db().table("post_signals").insert(rows).execute()
    if changed:
        db().table("user_interests").upsert(
            [{"user_id": uid, "dim": d, "key": k, "weight": round(v, 4), "updated_at": iso(now)} for (d, k), v in changed.items()],
            on_conflict="user_id,dim,key").execute()


def record_safe(user, post, author, event, value=0.0):
    """Used from like/comment routes: recommendations must never be able to break the main action."""
    try:
        if enabled(user):
            record(user["id"], [{"post": post, "author": author, "event": event, "value": value}])
    except Exception:
        log.exception("recommender: could not record %s", event)


def reset(uid):
    sb.delete("user_interests", user_id=uid)
    sb.delete("post_signals", user_id=uid)


def cleanup_old_signals(days=30):
    cutoff = iso(utcnow() - timedelta(days=days))
    old = db().table("post_signals").select("id").lt("created_at", cutoff).limit(2000).execute().data or []
    for r in old:
        sb.delete("post_signals", id=r["id"])
    return len(old)


# ---------------------------------------------------------------- ranking
def _noise(uid, post_id):
    """Stable per student+post+day, so the feed varies day to day without reshuffling on every refresh."""
    h = hashlib.md5(f"{uid}:{post_id}:{utcnow().date()}".encode()).hexdigest()
    return int(h[:6], 16) / 0xFFFFFF


def affinity(post, author, prof):
    a = prof["author"].get(post["author_id"], 0.0)
    s = prof["school"].get((author or {}).get("school_id"), 0.0)
    k = prof["kind"].get(post.get("kind") or "post", 0.0)
    best = sorted(((prof["topic"].get(t, 0.0) * st, t) for t, st in topics_of(post.get("caption")).items()), reverse=True)[:3]
    t = sum(v for v, _ in best)
    parts = {"author": a, "topic": t, "school": s, "kind": k}
    return a + 0.8 * t + 0.4 * s + 0.3 * k, parts, (best[0][1] if best and best[0][0] > 0 else None)


def score_post(uid, post, author, prof, followed, my_school, has_media=True, now=None):
    now = now or utcnow()
    total, parts, tok = affinity(post, author, prof)
    age_h = max(0.0, (now - (parse_dt(post["created_at"]) or now)).total_seconds() / 3600)
    fresh = 0.5 ** (age_h / (72.0 if post.get("kind") == "reel" else 48.0))
    pop = min(1.0, math.log1p((post.get("like_count") or 0) + 2 * (post.get("comment_count") or 0)) / math.log(40))
    same_school = bool(my_school and (author or {}).get("school_id") == my_school)
    s = (0.5 * math.tanh(total / 8.0) + 0.22 * fresh + 0.15 * pop + (0.15 if same_school else 0.0)
         + (0.15 if post["author_id"] in followed else 0.0) + (0.1 if has_media else 0.0) + 0.08 * _noise(uid, post["id"]))
    return s, parts, tok, same_school


def reason_for(post, author, parts, tok, same_school):
    if parts["author"] >= 3:
        return f"Because you like @{author['username']}"
    if tok and parts["topic"] >= 1.5:
        return "Because you like " + (tok if tok.startswith("#") else f"“{tok}”")
    if same_school or parts["school"] >= 2:
        return "Popular at your school"
    return "Popular on CIRQ"


def _recent_ids(uid, events, days):
    since = iso(utcnow() - timedelta(days=days))
    rows = db().table("post_signals").select("post_id").eq("user_id", uid).in_("event", list(events)).gt("created_at", since).limit(1000).execute().data or []
    return {r["post_id"] for r in rows}


def _candidates(kind, days, cap):
    since = iso(utcnow() - timedelta(days=days))
    return (db().table("posts").select("*").eq("kind", kind).eq("visibility", "public").eq("moderation_status", "ok")
            .is_("community_id", "null").gt("created_at", since).order("created_at", desc=True).limit(cap).execute().data or [])


def _eligible(uid, rows):
    """Drop anything that must never be recommended: blocked people, hidden posts, unapproved or private accounts, no media row."""
    if not rows:
        return []
    blocked = social.blocked_between(uid)
    hidden = _recent_ids(uid, ("hide",), 3650)
    authors = sb.by_id("profiles", [r["author_id"] for r in rows])
    media = {m["post_id"] for m in sb.in_("post_media", "post_id", [r["id"] for r in rows], "post_id")}
    out = []
    for r in rows:
        a = authors.get(r["author_id"])
        if (not a or a["account_status"] != "approved" or a.get("is_private") or r["author_id"] in blocked or r["id"] in hidden):
            continue
        out.append((r, a, r["id"] in media))
    return out


def _diversify(ranked, n, per_author=1, consecutive_only=False):
    """per_author: cap per author (suggestions). consecutive_only: just avoid back-to-back posts by one author (reels),
    but still fill the page when one author has most of the content."""
    pool, out, count, last = list(ranked), [], {}, None
    while pool and len(out) < n:
        if consecutive_only:
            item = next((i for i in pool if i[0]["author_id"] != last), pool[0])
        else:
            item = next((i for i in pool if count.get(i[0]["author_id"], 0) < per_author), None)
            if item is None:
                break
        pool.remove(item)
        aid = item[0]["author_id"]
        out.append(item)
        count[aid] = count.get(aid, 0) + 1
        last = aid
    return out


def suggest(uid, user, exclude_ids=(), n=3):
    """'Suggested for you' posts from people the student doesn't follow yet. Returns hydrated post dicts with .suggested/.reason."""
    prof = load_profile(uid) if enabled(user) else empty_profile()
    followed = set(social.following_ids(uid))
    skip = set(exclude_ids) | _recent_ids(uid, ("impression",), 2)
    rows = [r for r in _candidates("post", 14, 80) if r["id"] not in skip and r["author_id"] != uid and r["author_id"] not in followed]
    scored = []
    for r, a, has_media in _eligible(uid, rows):
        s, parts, tok, same = score_post(uid, r, a, prof, followed, user.get("school_id"), has_media)
        scored.append((r, s, reason_for(r, a, parts, tok, same)))
    scored.sort(key=lambda x: x[1], reverse=True)
    picked = _diversify(scored, n)
    if not picked:
        return []
    reasons = {r["id"]: why for r, _, why in picked}
    cards = social.hydrate_posts([r for r, _, _ in picked], uid)
    for c in cards:
        c["suggested"], c["reason"] = True, reasons.get(c["id"], "Suggested for you")
    try:
        record(uid, [{"post": c, "author": c["author"], "event": "impression"} for c in cards], learn=False)
    except Exception:
        log.exception("recommender: impressions not stored")
    return cards


def drop_shown_suggestions(uid, posts, followed):
    """The normal feed lists public posts too; don't repeat a post we already showed as a suggestion."""
    shown = _recent_ids(uid, ("impression",), 2)
    return [p for p in posts if not (p["id"] in shown and p["author_id"] not in followed and p["author_id"] != uid)]


def rank_reels(uid, user, seen_ids, n):
    """Personalised reel order. Returns (rows, has_more). `seen_ids` are reels already served in this session."""
    prof = load_profile(uid) if enabled(user) else empty_profile()
    followed = set(social.following_ids(uid))
    seen = set(seen_ids)
    watched = _recent_ids(uid, ("watch", "view"), 3)
    rows = [r for r in _candidates("reel", 30, 100) if r["id"] not in seen]
    scored = []
    for r, a, has_media in _eligible(uid, rows):
        if not has_media:
            continue
        s, *_ = score_post(uid, r, a, prof, followed, user.get("school_id"), True)
        if r["id"] in watched:
            s -= 0.5                           # already watched recently: show fresh ones first
        scored.append((r, s))
    scored.sort(key=lambda x: x[1], reverse=True)
    picked = _diversify(scored, n, consecutive_only=True)
    return [r for r, _ in picked], len(scored) > n
