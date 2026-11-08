"""Account discovery for the Discover page: popular, trending and personalised account suggestions.

Everything is built from data the app already has: the follow graph, blocks, school, verification, boosts, follower tiers
and the recommender's learned interests. Nothing here invents numbers: a figure that can't be known is simply left out.

Follower counts only exist as rows in `follows`, so ranking uses one cheap aggregate (the optional SQL functions in
migrations/2026_11_08_discover_accounts.sql) that is cached for a minute per worker. Without that migration it falls back to a
bounded, slower strategy, so the page always works.
"""
import hashlib
import math
import time
import uuid
from collections import Counter, defaultdict
from datetime import timedelta

from flask import current_app

from app.extensions import db
from app.services import supabase_service as sb, social, recommender
from app.services.coin_service import is_boosted
from app.utils import iso, parse_dt, utcnow, tier_for

TRENDING_DAYS = 7
POOL_SIZE = 200          # how many top accounts the aggregate returns
RESULT_CAP = 120         # most candidates a search / category listing ever ranks
PER_PAGE = 24            # cards per page in results mode
HOME_POPULAR, HOME_RECOMMENDED, HOME_TRENDING = 12, 8, 10
CATEGORIES = ("all", "school", "verified", "creators")
VIEWS = ("popular", "recommended", "trending")

_cache = {"at": 0.0, "data": None}


# ------------------------------------------------------------------ follower aggregates
def _build_pool():
    since = iso(utcnow() - timedelta(days=TRENDING_DAYS))
    try:
        rows = db().rpc("cirq_discover_pool", {"p_since": since, "p_limit": POOL_SIZE}).execute().data or []
        return {"followers": {r["profile_id"]: int(r["followers"]) for r in rows},
                "recent": {r["profile_id"]: int(r["recent"]) for r in rows if r.get("recent")}}
    except Exception:       # migration not applied (or the call failed): bounded fallback instead of an error page
        current_app.logger.warning("cirq_discover_pool unavailable; Discover is using its slower fallback")
    # Fallback: sample the newest follow rows. Popular accounts keep gaining followers, so they show up here often; counts are then exact.
    rows = db().table("follows").select("followee_id,created_at").order("created_at", desc=True).limit(1000).execute().data or []
    cutoff = utcnow() - timedelta(days=TRENDING_DAYS)
    freq = Counter(r["followee_id"] for r in rows)
    recent = Counter(r["followee_id"] for r in rows if (parse_dt(r["created_at"]) or cutoff) >= cutoff)
    newest = db().table("profiles").select("id").eq("account_status", "approved").order("created_at", desc=True).limit(10).execute().data or []
    ids = list(dict.fromkeys([i for i, _ in freq.most_common(30)] + [i for i, _ in recent.most_common(10)] + [r["id"] for r in newest]))
    return {"followers": {i: sb.count("follows", followee_id=i) for i in ids}, "recent": dict(recent)}


def pool():
    """{'followers': {id: n}, 'recent': {id: new followers this week}} for the most-followed / fastest-growing accounts."""
    ttl = 0 if current_app.testing else int(current_app.config.get("DISCOVER_CACHE_SECONDS", 60))
    now = time.monotonic()
    if ttl and _cache["data"] is not None and now - _cache["at"] < ttl:
        return _cache["data"]
    data = _build_pool()
    if ttl:
        _cache.update(at=now, data=data)
    return data


def follower_counts(ids, budget=40):
    """Exact follower counts for these accounts (one SQL call). Fallback counts at most `budget` accounts one by one."""
    ids = list(dict.fromkeys(i for i in ids if i))
    if not ids:
        return {}
    try:
        rows = db().rpc("cirq_follower_counts", {"p_ids": ids}).execute().data or []
        out = {i: 0 for i in ids}
        out.update({r["profile_id"]: int(r["followers"]) for r in rows})
        return out
    except Exception:
        known, out, spent = pool()["followers"], {}, 0
        for i in ids:
            if i in known:
                out[i] = known[i]
            elif spent < budget:
                out[i], spent = sb.count("follows", followee_id=i), spent + 1
            else:
                out[i] = 0
        return out


# ------------------------------------------------------------------ viewer context
class Viewer:
    """Who is looking, and which accounts they can't or needn't be shown."""

    def __init__(self, me):
        self.me = me
        self.hidden = social.blocked_between(me["id"])
        self.following = set(social.following_ids(me["id"]))
        self.asked = social.requested_ids(me["id"])

    def ok(self, p):
        return bool(p) and p.get("account_status") == "approved" and p["id"] != self.me["id"] and p["id"] not in self.hidden

    def state(self, pid):
        return "following" if pid in self.following else ("requested" if pid in self.asked else "none")


def _profiles(ids):
    return sb.by_id("profiles", list(dict.fromkeys(i for i in ids if i)))


def _score(p, followers, v):
    """Popularity on a log scale (so one celebrity doesn't bury everyone) plus a nudge for the viewer's own school."""
    same = bool(v.me.get("school_id")) and p.get("school_id") == v.me.get("school_id")
    return math.log1p(followers) + (0.8 if same else 0.0)


def _by_newest(rows):
    return sorted(rows, key=lambda p: p.get("created_at") or "", reverse=True)


# ------------------------------------------------------------------ the three discovery lists
def popular(v, limit):
    pl = pool()
    top = [i for i, _ in sorted(pl["followers"].items(), key=lambda kv: -kv[1])][:max(60, limit * 2)]
    rows = _profiles(top)
    for p in db().table("profiles").select("*").eq("account_status", "approved").order("created_at", desc=True).limit(limit).execute().data or []:
        rows.setdefault(p["id"], p)       # young accounts with no followers yet still get a chance to be seen
    cands = _by_newest([p for p in rows.values() if v.ok(p)])
    cands.sort(key=lambda p: (not is_boosted(p), -_score(p, pl["followers"].get(p["id"], 0), v)))   # boosted first (existing behaviour), then popularity
    return cands[:limit]


def trending(v, limit):
    pl = pool()
    ranked = sorted(pl["recent"].items(), key=lambda kv: (-kv[1], -pl["followers"].get(kv[0], 0)))
    rows = _profiles([i for i, _ in ranked][:limit * 2])
    out = []
    for pid, gained in ranked:
        p = rows.get(pid)
        if v.ok(p):
            out.append({**p, "_trend": gained})
        if len(out) >= limit:
            break
    return out


def _jitter(uid, pid):
    """Tiny stable per-day wobble so equal scores don't always list in the same order."""
    digest = hashlib.sha256(f"{uid}:{pid}:{utcnow().date().toordinal()}".encode()).digest()
    return digest[0] / 255 * 0.3


def recommended(v, user, limit):
    """Accounts the viewer is likely to want: friends-of-friends, same school, people whose posts they engaged with, then popular."""
    uid, pl = v.me["id"], pool()
    score, why, via = defaultdict(float), {}, defaultdict(list)
    skip = v.following | v.asked | v.hidden | {uid}

    if v.following:                                          # 1. followed by people I follow
        for r in sb.in_("follows", "follower_id", list(v.following)[:100], cols="follower_id,followee_id"):
            if r["followee_id"] not in skip:
                via[r["followee_id"]].append(r["follower_id"])
        for pid, friends in via.items():
            score[pid] += 3 * min(len(friends), 4)
            why[pid] = ("friends", friends)

    if recommender.enabled(user):                            # 2. authors I've liked / watched / commented on (the recommender's own signal)
        authors = recommender.load_profile(uid).get("author", {})
        for pid, w in sorted(authors.items(), key=lambda kv: -kv[1])[:15]:
            if w > 0 and pid not in skip:
                score[pid] += min(w, 3) * 1.5
                why.setdefault(pid, ("liked", None))

    if user.get("school_id"):                                # 3. my school
        for p in db().table("profiles").select("id").eq("school_id", user["school_id"]).eq("account_status", "approved").order("created_at", desc=True).limit(40).execute().data or []:
            if p["id"] not in skip:
                score[p["id"]] += 2
                why.setdefault(p["id"], ("school", None))

    for pid, n in sorted(pl["followers"].items(), key=lambda kv: -kv[1])[:30]:   # 4. popular fill so new members never see an empty list
        if pid not in skip:
            score[pid] += 0.8 * math.log10(1 + n)
            why.setdefault(pid, ("popular", None))

    rows = _profiles(list(score))
    friend_ids = {f for kind, fr in why.values() if kind == "friends" for f in fr[:2]}
    names = {i: p["username"] for i, p in _profiles(friend_ids).items()} if friend_ids else {}
    out = []
    for pid, s in score.items():
        p = rows.get(pid)
        if not v.ok(p) or pid in v.following or pid in v.asked:
            continue
        s += (0.5 if is_boosted(p) else 0) + _jitter(uid, pid)
        kind, fr = why.get(pid, ("popular", None))
        if kind == "friends":
            first = [names[f] for f in fr[:2] if f in names]
            if not first:
                reason = "Followed by people you follow"
            elif len(fr) == 1:
                reason = f"Followed by {first[0]}"
            elif len(fr) == 2 and len(first) == 2:
                reason = f"Followed by {first[0]} and {first[1]}"
            else:
                reason = f"Followed by {first[0]} + {len(fr) - 1} more"
        else:
            reason = {"liked": "You've liked their posts", "school": "From your school", "popular": "Popular on CIRQ"}[kind]
        out.append(({**p, "_reason": reason}, s))
    out.sort(key=lambda t: -t[1])
    return [p for p, _ in out[:limit]]


def reel_authors():
    since = iso(utcnow() - timedelta(days=60))
    rows = (db().table("posts").select("author_id").eq("kind", "reel").eq("visibility", "public").eq("moderation_status", "ok")
            .is_("community_id", "null").gte("created_at", since).order("created_at", desc=True).limit(300).execute().data or [])
    return list(dict.fromkeys(r["author_id"] for r in rows))


# ------------------------------------------------------------------ search + categories
def _clean_term(q):
    q = (q or "").replace('"', " ").replace("\\", " ")
    return q.replace("%", "").replace("_", r"\_").replace(",", " ").replace("(", " ").replace(")", " ").strip()


def search(v, q, cat, school_id, limit=RESULT_CAP):
    me, pl = v.me, pool()
    school = school_id or (me.get("school_id") if cat == "school" else None)
    if cat == "school" and not school:
        return []
    creators = set(reel_authors()) if cat == "creators" else None
    if creators is not None and not creators:
        return []

    def narrow(query):
        if school:
            query = query.eq("school_id", school)
        if cat == "verified":
            query = query.eq("verified", True)
        return query

    query = narrow(db().table("profiles").select("*").eq("account_status", "approved"))
    term = _clean_term(q)
    if term:
        query = query.or_(f"username.ilike.%{term}%,display_name.ilike.%{term}%,bio.ilike.%{term}%")
    if creators is not None:
        query = query.in_("id", list(creators)[:RESULT_CAP])
    rows = {p["id"]: p for p in query.order("created_at", desc=True).limit(RESULT_CAP).execute().data or []}
    if not term:                                            # a category with no search: also consider the most-followed accounts, not just the newest
        for pid, p in _profiles(list(pl["followers"])[:POOL_SIZE]).items():
            ok = (not school or p.get("school_id") == school) and (cat != "verified" or p.get("verified")) and (creators is None or pid in creators)
            if ok:
                rows.setdefault(pid, p)
    cands = [p for p in rows.values() if v.ok(p)]
    counts = follower_counts([p["id"] for p in cands])
    needle = (q or "").strip().lower()

    def match(p):
        if not needle:
            return 0
        u, d = p["username"].lower(), (p.get("display_name") or "").lower()
        return 0 if u == needle else 1 if u.startswith(needle) else 2 if d.startswith(needle) else 3

    cands = _by_newest(cands)
    cands.sort(key=lambda p: (match(p), not is_boosted(p), -_score(p, counts.get(p["id"], 0), v)))
    return cands[:limit]


# ------------------------------------------------------------------ assembling a page
def _decorate(items, v):
    """Add the display-only fields every account card needs. Counts come from one fresh query for exactly the cards shown."""
    ids = [p["id"] for p in items]
    counts = follower_counts(ids)
    schools = sb.by_id("schools", [p.get("school_id") for p in items])
    for p in items:
        p["_state"] = v.state(p["id"])
        p["_followers"] = counts.get(p["id"])
        p["_school"] = schools.get(p.get("school_id"))
        tier = tier_for(p["_followers"] or 0)
        p["_tier"] = tier["name"] if tier["index"] >= 1 else None      # "Rookie" is everyone's starting point, so it isn't worth a badge
    return items


def _fresh(items, seen, want=4):
    """Prefer accounts the viewer hasn't just seen in another section, unless that would leave the section nearly empty."""
    unseen = [p for p in items if p["id"] not in seen]
    return unseen if len(unseen) >= want else items


def accounts_page(user, args):
    """Everything the Discover > Accounts tab needs. `mode` is 'home' (sections) or 'results' (one filtered, paginated grid)."""
    v = Viewer(user)
    q = (args.get("q") or "").strip()[:40]
    cat = args.get("cat") if args.get("cat") in CATEGORIES else "all"
    school_id = args.get("school") or ""
    try:
        school_id = str(uuid.UUID(school_id)) if school_id else ""
    except ValueError:
        school_id = ""
    view = args.get("view") if args.get("view") in VIEWS else ""
    try:
        page = min(max(int(args.get("page", 1)), 1), 20)
    except (TypeError, ValueError):
        page = 1
    out = {"q": q, "cat": cat, "school": school_id, "view": view, "page": page, "mode": "home", "has_more": False,
           "sections": {}, "results": [], "title": "", "total": 0}

    if q or cat != "all" or school_id or view:
        out["mode"] = "results"
        if q or cat != "all" or school_id:
            ranked = search(v, q, cat, school_id)
        elif view == "popular":
            ranked = popular(v, RESULT_CAP)
        elif view == "recommended":
            ranked = recommended(v, user, RESULT_CAP)
        else:
            ranked = trending(v, RESULT_CAP)
        start = (page - 1) * PER_PAGE
        out["total"], out["has_more"] = len(ranked), len(ranked) > start + PER_PAGE
        out["results"] = _decorate(ranked[start:start + PER_PAGE], v)
        out["title"] = (f"Results for “{q}”" if q else
                        {"school": "Your school", "verified": "Verified accounts", "creators": "Reel creators"}.get(cat) if cat != "all" else
                        "Accounts at this school" if school_id else
                        {"popular": "Popular accounts", "recommended": "Recommended for you", "trending": "Trending creators"}.get(view, "Accounts"))
        return out

    pop = popular(v, HOME_POPULAR)
    seen = {p["id"] for p in pop}
    rec = _fresh(recommended(v, user, HOME_RECOMMENDED * 2), seen)[:HOME_RECOMMENDED]
    seen |= {p["id"] for p in rec}
    trend = _fresh(trending(v, HOME_TRENDING * 2), seen, want=3)[:HOME_TRENDING]
    _decorate(pop + rec + trend, v)            # one counts query covers every card on the page
    out["sections"] = {"popular": pop, "recommended": rec, "trending": trend}
    return out
