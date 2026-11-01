"""Personalised suggestions: learning from behaviour, ranking, safety rules and student controls."""
import io
from tests.conftest import MP4
from app.services import recommender as rec


def post(db, author, caption, kind="post", **kw):
    return db.table("posts").insert({"author_id": author["id"], "kind": kind, "caption": caption, **kw}).execute().data[0]


def private(db, user):
    db.table("profiles").update({"is_private": True}).eq("id", user["id"]).execute()
    return user


def interests(db, user):
    return {(r["dim"], r["key"]): r["weight"] for r in db.data.get("user_interests", []) if r["user_id"] == user["id"]}


# ---------------------------------------------------------------- pure functions
def test_topics_hashtags_are_strong_keywords_weaker_and_stopwords_dropped():
    t = rec.topics_of("Great football match today! #Football #KampalaDerby the team played well")
    assert t["#football"] == 1.0 and t["#kampaladerby"] == 1.0
    assert t.get("football") == 0.5 or "football" in t      # keyword form is allowed but weaker
    assert "today" not in t and "the" not in t
    assert rec.topics_of("") == {} and rec.topics_of(None) == {}


def test_signal_weights_rank_actions_by_strength():
    assert rec.weight_for("comment") > rec.weight_for("like") > rec.weight_for("view", 5000) > 0
    assert rec.weight_for("view", 300) == 0                 # glancing doesn't count
    assert rec.weight_for("watch", 1.0) > rec.weight_for("watch", 0.6) > rec.weight_for("watch", 0.3) > 0
    assert rec.weight_for("watch", 0.05) < 0                # swiped away fast = "not for me"
    assert rec.weight_for("watch", 1.6) > rec.weight_for("watch", 1.0)   # replays are the strongest
    assert rec.weight_for("hide") < 0


# ---------------------------------------------------------------- learning
def test_liking_a_post_teaches_author_topic_and_school(as_user, db, mk):
    a, alice = as_user("alice")
    bob = mk("bob")
    p = post(db, bob, "Football practice was great #football")
    assert a.post(f"/posts/{p['id']}/like").status_code == 200
    w = interests(db, alice)
    assert w[("author", bob["id"])] > 0 and w[("topic", "#football")] > 0 and w[("school", bob["school_id"])] > 0


def test_viewing_signals_endpoint_validates_and_learns(as_user, db, mk):
    a, alice = as_user("alice")
    bob, carol = mk("bob"), private(db, mk("carol"))
    ok = post(db, bob, "Robotics club #robotics")
    secret = post(db, carol, "Private #secret", visibility="followers")
    r = a.post("/api/signals", json={"events": [
        {"post_id": ok["id"], "event": "view", "value": 6000},
        {"post_id": ok["id"], "event": "like", "value": 1},          # not client-reportable: ignored
        {"post_id": secret["id"], "event": "view", "value": 9000},   # can't see it, so can't teach from it
        {"post_id": "does-not-exist", "event": "view", "value": 9000},
    ]})
    assert r.status_code == 200 and r.get_json()["stored"] == 1
    w = interests(db, alice)
    assert w[("topic", "#robotics")] > 0 and ("topic", "#secret") not in w


def test_not_interested_hides_forever_and_lowers_the_topic(as_user, db, mk, app):
    a, alice = as_user("alice")
    bob = mk("bob")
    liked = post(db, mk("dan"), "Chess club #chess")
    a.post(f"/posts/{liked['id']}/like")
    p = post(db, bob, "More chess #chess")
    before = interests(db, alice)[("topic", "#chess")]
    assert a.post(f"/posts/{p['id']}/not-interested").status_code == 200
    assert interests(db, alice)[("topic", "#chess")] < before
    with app.app_context():
        eligible = [r["id"] for r, _, _ in rec._eligible(alice["id"], [p])]
    assert p["id"] not in eligible


# ---------------------------------------------------------------- ranking & suggestions
def test_suggestions_prefer_what_the_student_likes_and_explain_why(as_user, db, mk, app):
    a, alice = as_user("alice")
    bob, carol, dave = mk("bob"), mk("carol"), mk("dave")
    seeds = [post(db, bob, f"Football training {i} #football") for i in range(2)]
    for s in seeds:
        a.post(f"/posts/{s['id']}/like")
    foot = post(db, carol, "Derby night #football")
    chess = post(db, dave, "Chess notes #chess")
    with app.app_context():
        got = rec.suggest(alice["id"], alice, exclude_ids={s["id"] for s in seeds}, n=2)
    ids = [c["id"] for c in got]
    assert ids[0] == foot["id"] and set(ids) == {foot["id"], chess["id"]}
    assert got[0]["suggested"] and "#football" in got[0]["reason"]


def test_suggestions_never_include_unsafe_or_followed_content(as_user, db, mk, app):
    a, alice = as_user("alice")
    friend, blocked, priv, pending, flagged, ok = mk("friend"), mk("blocked"), private(db, mk("priv")), mk("pend", status="pending"), mk("flag"), mk("fine")
    a.post("/u/friend/follow"); a.post("/u/blocked/block")
    for who, cap in ((friend, "followed"), (blocked, "blocked"), (priv, "private"), (pending, "pending")):
        post(db, who, cap)
    post(db, flagged, "flagged", moderation_status="flagged")
    post(db, ok, "fine post")
    post(db, alice, "my own")
    with app.app_context():
        caps = [c["caption"] for c in rec.suggest(alice["id"], alice, n=10)]
    assert caps == ["fine post"]


def test_one_suggestion_per_author_and_no_repeats(as_user, db, mk, app):
    a, alice = as_user("alice")
    bob, carol = mk("bob"), mk("carol")
    for i in range(3):
        post(db, bob, f"bob {i}")
    post(db, carol, "carol 0")
    with app.app_context():
        first = rec.suggest(alice["id"], alice, n=5)
        assert sorted(c["author"]["username"] for c in first) == ["bob", "carol"]
        assert rec.suggest(alice["id"], alice, n=5)[0]["author"]["username"] == "bob"   # a second bob post, never a repeat
        again = rec.suggest(alice["id"], alice, n=5)
    assert all(c["id"] not in {x["id"] for x in first} for c in again)


def test_feed_weaves_in_suggested_posts(as_user, db, mk):
    a, alice = as_user("alice")
    authors = [mk(n) for n in ("bob", "carol", "dave", "erin")]
    for u in authors:
        post(db, u, f"old post by {u['username']}")
    for i in range(10):
        post(db, authors[0], f"newer {i}")
    html = a.get("/feed").get_data(as_text=True)
    assert "Suggested for you" in html and "Not interested" in html


def reel(db, author, caption):
    p = post(db, author, caption, kind="reel")
    db.table("post_media").insert({"post_id": p["id"], "owner_id": author["id"], "media_type": "video", "url": "u",
                                   "public_id": f"cirq/reels/{p['id'][:8]}"}).execute()
    return p


def test_reels_are_ranked_by_interest_and_paginate_without_repeats(as_user, db, mk):
    a, alice = as_user("alice")
    carol, dave = mk("carol"), mk("dave")
    watched = reel(db, carol, "Derby highlights #football")
    a.post("/api/signals", json={"events": [{"post_id": watched["id"], "event": "watch", "value": 1.3}]})   # replayed it
    chess, foot = reel(db, dave, "Chess #chess"), reel(db, carol, "Cup final #football")
    page = a.get("/reels").get_data(as_text=True)
    assert 'href="/reels/new"' in page and "New reel" in page
    first = a.get("/reels?partial=1").get_data(as_text=True)
    assert first.index(foot["id"]) < first.index(chess["id"]) < first.index(watched["id"])   # liked topic first, already-watched last
    for i in range(6):
        reel(db, carol, f"filler {i}")
    one = a.get("/reels?partial=1")
    cur = one.headers["X-Next-Cursor"]
    shown = set(cur.split(","))
    two = a.get(f"/reels?partial=1&before={cur}").get_data(as_text=True)
    assert len(shown) == 5 and not any(i in two for i in shown) and two.count("data-reel ") == 4


# ---------------------------------------------------------------- student controls
def test_turning_personalisation_off_forgets_and_stops_learning(as_user, db, mk):
    a, alice = as_user("alice")
    p = post(db, mk("bob"), "Music #music")
    a.post(f"/posts/{p['id']}/like")
    assert interests(db, alice)
    a.post("/settings", data={"action": "recs"})                  # checkbox missing = off
    assert not interests(db, alice) and not [s for s in db.data.get("post_signals", []) if s["user_id"] == alice["id"]]
    q = post(db, mk("carol"), "More music #music")
    a.post(f"/posts/{q['id']}/like")
    assert not interests(db, alice)


def test_reset_clears_everything(as_user, db, mk):
    a, alice = as_user("alice")
    p = post(db, mk("bob"), "Art #art")
    a.post(f"/posts/{p['id']}/like")
    a.post("/settings", data={"action": "recs", "reset": "1"})
    assert not interests(db, alice)
    assert b"Suggestions" in a.get("/settings").data


def test_interests_fade_over_time(app, db, as_user):
    from datetime import timedelta
    from app.utils import iso, utcnow
    import pytest
    _, alice = as_user("alice")
    db.table("user_interests").insert({"user_id": alice["id"], "dim": "topic", "key": "#old", "weight": 8.0,
                                       "updated_at": iso(utcnow() - timedelta(days=28))}).execute()
    db.table("user_interests").insert({"user_id": alice["id"], "dim": "topic", "key": "#new", "weight": 8.0, "updated_at": iso()}).execute()
    with app.app_context():
        prof = rec.load_profile(alice["id"])
    assert prof["topic"]["#old"] == pytest.approx(2.0, rel=0.05) and prof["topic"]["#new"] == pytest.approx(8.0, rel=0.05)   # 14-day half-life
