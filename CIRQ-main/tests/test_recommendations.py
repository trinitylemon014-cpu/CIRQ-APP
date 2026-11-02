"""Personalised suggestions: learning from behaviour, ranking, safety rules and student controls."""
import io
import re
import uuid
import httpx
from datetime import datetime, timedelta, timezone
from tests.conftest import MP4
from app.services import recommender as rec
from app.services import supabase_service as sb


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
    def test_signals_returns_json_503_when_database_transport_fails(as_user, monkeypatch):
        client, _ = as_user("signalerror")

        def fail(*args, **kwargs):
            raise httpx.ReadError("temporary failure")

        monkeypatch.setattr(sb, "by_id", fail)
        response = client.post("/api/signals", json={"events": [
            {"post_id": str(uuid.uuid4()), "event": "view", "value": 1500},
        ]})

        assert response.status_code == 503
        assert response.is_json and "temporarily unavailable" in response.get_json()["error"]
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


def reel_ids(response):
    return re.findall(r'data-id="([^"]+)"', response.get_data(as_text=True))


def test_reels_are_newest_first_and_paginate_without_repeats(as_user, db, mk):
    a, _ = as_user("alice")
    creator = mk("carol")
    old = [reel(db, creator, f"reel {i}") for i in range(9)]
    start = datetime.now(timezone.utc) - timedelta(days=2)
    for i, item in enumerate(old):
        db.table("posts").update({"created_at": (start + timedelta(seconds=i)).isoformat()}).eq("id", item["id"]).execute()

    page = a.get("/reels").get_data(as_text=True)
    assert 'data-open="reel-create-dialog"' in page and "New reel" in page
    first = a.get("/reels?partial=1")
    assert reel_ids(first) == [item["id"] for item in reversed(old[-5:])]
    cursor = first.headers["X-Next-Cursor"]
    second = a.get(f"/reels?partial=1&before={cursor}")
    assert reel_ids(second) == [item["id"] for item in reversed(old[:4])]


def test_reels_archive_reaches_old_content_and_new_uploads_do_not_shift_pages(as_user, db, mk):
    a, _ = as_user("alice")
    creator = mk("carol")
    old = [reel(db, creator, f"archive {i}") for i in range(107)]
    start = datetime.now(timezone.utc) - timedelta(days=90)
    for i, item in enumerate(old):
        db.table("posts").update({"created_at": (start + timedelta(seconds=i)).isoformat()}).eq("id", item["id"]).execute()

    first = a.get("/reels?partial=1")
    assert reel_ids(first) == [item["id"] for item in reversed(old[-5:])]
    cursor = first.headers["X-Next-Cursor"]
    new = reel(db, creator, "new upload")

    seen = reel_ids(first)
    while cursor:
        page = a.get(f"/reels?partial=1&before={cursor}")
        ids = reel_ids(page)
        assert not set(ids).intersection(seen)
        seen.extend(ids)
        cursor = page.headers["X-Next-Cursor"]

    assert seen == [item["id"] for item in reversed(old)]
    assert reel_ids(a.get("/reels?partial=1"))[0] == new["id"]


def test_private_reel_author_can_page_through_all_own_reels(as_user, db, mk):
    a, alice = as_user("alice")
    db.table("profiles").update({"is_private": True}).eq("id", alice["id"]).execute()
    carol = mk("carol")
    own = [reel(db, alice, f"private {i}") for i in range(7)]
    public = [reel(db, carol, f"public {i}") for i in range(7)]
    start = datetime.now(timezone.utc) - timedelta(days=1)
    for i, item in enumerate(own + public):
        changes = {"created_at": (start + timedelta(seconds=i)).isoformat()}
        if item in own:
            changes["visibility"] = "followers"
        db.table("posts").update(changes).eq("id", item["id"]).execute()

    seen = []
    cursor = ""
    while True:
        page = a.get(f"/reels?partial=1&before={cursor}" if cursor else "/reels?partial=1")
        ids = reel_ids(page)
        assert not set(ids).intersection(seen)
        seen.extend(ids)
        cursor = page.headers["X-Next-Cursor"]
        if not cursor:
            break
    assert set(seen) == {item["id"] for item in own + public}

    b, _ = as_user("bob")
    bob_seen, cursor = [], ""
    while True:
        page = b.get(f"/reels?partial=1&before={cursor}" if cursor else "/reels?partial=1")
        bob_seen.extend(reel_ids(page))
        cursor = page.headers["X-Next-Cursor"]
        if not cursor:
            break
    assert set(bob_seen) == {item["id"] for item in public}


def test_uploads_from_different_users_preserve_the_shared_reel_archive(as_user, db, mk, uploads):
    alice_client, alice = as_user("alice")
    bob_client, bob = as_user("bob")
    archive_owner = mk("archive")
    older = reel(db, archive_owner, "older reel")
    base = datetime.now(timezone.utc) - timedelta(days=1)
    db.table("posts").update({"created_at": base.isoformat()}).eq("id", older["id"]).execute()

    alice_client.post("/reels", data={"video": (io.BytesIO(MP4), "alice.mp4"), "caption": "Alice's reel"},
                      content_type="multipart/form-data")
    alice_reel = next(post for post in db.data["posts"] if post["author_id"] == alice["id"])
    db.table("posts").update({"created_at": (base + timedelta(days=1)).isoformat()}).eq("id", alice_reel["id"]).execute()
    media = next(row for row in db.data["post_media"] if row["post_id"] == alice_reel["id"])
    assert media["owner_id"] == alice["id"] and media["url"].startswith("https://res.cloudinary.com/")

    bob_view = reel_ids(bob_client.get("/reels?partial=1"))
    assert bob_view[:2] == [alice_reel["id"], older["id"]]
    bob_client.post("/reels", data={"video": (io.BytesIO(MP4), "bob.mp4"), "caption": "Bob's reel"},
                    content_type="multipart/form-data")
    bob_reel = next(post for post in db.data["posts"] if post["author_id"] == bob["id"])
    db.table("posts").update({"created_at": (base + timedelta(days=2)).isoformat()}).eq("id", bob_reel["id"]).execute()

    alice_view = reel_ids(alice_client.get("/reels?partial=1"))
    assert alice_view == [bob_reel["id"], alice_reel["id"], older["id"]]
    assert len(db.data["posts"]) == 3
    assert {row["post_id"] for row in db.data["post_media"]} == {older["id"], alice_reel["id"], bob_reel["id"]}


def test_followers_can_see_private_creators_reels_but_other_users_cannot(as_user, db, mk):
    viewer, _ = as_user("viewer")
    outsider, _ = as_user("outsider")
    creator = mk("creator")
    viewer.post("/u/creator/follow")
    private(db, creator)
    item = reel(db, creator, "followers-only reel")

    assert item["id"] in reel_ids(viewer.get("/reels?partial=1"))
    assert item["id"] not in reel_ids(outsider.get("/reels?partial=1"))


def test_new_reel_refresh_is_keyset_paginated_newest_first_without_duplicates(as_user, db, mk):
    client, _ = as_user("viewer")
    creator = mk("creator")
    old = [reel(db, creator, f"old {i}") for i in range(9)]
    start = datetime.now(timezone.utc) - timedelta(days=2)
    for i, item in enumerate(old):
        db.table("posts").update({"created_at": (start + timedelta(seconds=i)).isoformat()}).eq("id", item["id"]).execute()

    first = client.get("/reels?partial=1")
    marker = first.headers["X-Newest-Cursor"]
    new = [reel(db, creator, f"new {i}") for i in range(12)]
    for i, item in enumerate(new):
        db.table("posts").update({"created_at": (start + timedelta(days=1, seconds=i)).isoformat()}).eq("id", item["id"]).execute()

    refreshed, after = [], marker
    while True:
        page = client.get(f"/reels?partial=1&after={after}")
        ids = reel_ids(page)
        assert not set(ids).intersection(refreshed)
        refreshed = ids + refreshed
        after = page.headers["X-After-Cursor"]
        if page.headers["X-More-Newer"] != "1":
            break
    assert refreshed == [item["id"] for item in reversed(new)]
    assert reel_ids(client.get("/reels?partial=1&before=" + first.headers["X-Next-Cursor"])) == [item["id"] for item in reversed(old[:4])]


def test_reel_saves_are_private_and_visibility_checked(as_user, db, mk):
    owner, owner_profile = as_user("owner")
    viewer, _ = as_user("viewer")
    creator = mk("creator")
    item = reel(db, creator, "bookmarkable")

    response = viewer.post(f"/posts/{item['id']}/save")
    assert response.get_json() == {"saved": True}
    assert len(db.data["saved_posts"]) == 1
    assert b"Remove saved reel" in viewer.get("/reels").data
    assert viewer.post(f"/posts/{item['id']}/save").get_json() == {"saved": False}
    assert not db.data["saved_posts"]

    private_item = reel(db, private(db, owner_profile), "private reel")
    assert viewer.post(f"/posts/{private_item['id']}/save").status_code == 404


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
