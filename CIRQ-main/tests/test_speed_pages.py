"""Page speed: independent lookups run together, and the same lookup is never repeated within a page."""
import threading
import time
from collections import Counter
from datetime import timedelta

import pytest

from app.services import social, supabase_service as sb
from app.utils import iso, utcnow


# ------------------------------------------------------------------ the parallel helper
def test_parallel_runs_at_the_same_time_and_keeps_order(app):
    def slow(label):
        def run():
            time.sleep(0.25)
            return label
        return run
    with app.test_request_context("/"):
        started = time.perf_counter()
        assert sb.parallel(slow("a"), slow("b"), slow("c"), slow("d")) == ["a", "b", "c", "d"]
        assert time.perf_counter() - started < 0.6          # four 0.25s waits overlap (in turn they'd take a full second)


def test_parallel_raises_the_first_error_after_everything_finished(app):
    done = []
    def good():
        time.sleep(0.1); done.append(1); return 1
    def bad():
        raise ValueError("boom")
    with app.test_request_context("/"):
        with pytest.raises(ValueError):
            sb.parallel(good, bad, good)
    assert len(done) == 2                                    # the other lookups were not abandoned half-way


def test_parallel_tasks_can_nest_without_deadlocking(app):
    def inner():
        return sb.parallel(lambda: 1, lambda: 2, lambda: sb.parallel(lambda: 3, lambda: 4))
    with app.test_request_context("/"):
        assert sb.parallel(inner, inner, inner, inner) == [[1, 2, [3, 4]]] * 4


def test_parallel_tasks_see_the_app_user_and_share_the_page_cache(app):
    with app.test_request_context("/"):
        from flask import g
        g.user = {"id": "u1"}
        seen = sb.parallel(lambda: g.get("user"), lambda: threading.current_thread().name)
        assert seen[0] == {"id": "u1"} and seen[1] != threading.current_thread().name   # really ran on another thread
        cache = sb.request_cache()
        cache["k"] = 1
        assert sb.parallel(lambda: sb.request_cache() is cache, lambda: sb.request_cache() is cache) == [True, True]


def test_request_cache_only_exists_for_reads(app):
    with app.test_request_context("/", method="GET"):
        assert sb.request_cache() is not None
    with app.test_request_context("/", method="POST"):
        assert sb.request_cache() is None                   # a request that writes always reads fresh data


# ------------------------------------------------------------------ cached social lookups stay correct
def test_block_and_follow_lookups_are_cached_per_page_but_never_across_pages(as_user, db, app):
    a, pa = as_user("alice"); b, pb = as_user("bob")
    calls = Counter()
    real = db.table
    def counting(name):
        calls[name] += 1
        return real(name)
    with app.test_request_context("/"):
        from flask import g
        g.user = pa
        db.table = counting
        try:
            for _ in range(5):
                assert social.blocked_between(pa["id"]) == set()
                assert social.is_blocked(pa["id"], pb["id"]) is False
                assert social.is_following(pa["id"], pb["id"]) is False
                assert social.following_ids(pa["id"]) == []
        finally:
            db.table = real
    assert calls["blocks"] == 2 and calls["follows"] == 1   # one lookup each, however often the page asks

    db.table("follows").insert({"follower_id": pa["id"], "followee_id": pb["id"]}).execute()
    db.table("blocks").insert({"blocker_id": pb["id"], "blocked_id": pa["id"]}).execute()
    with app.test_request_context("/"):                     # a new page sees the new data straight away
        from flask import g
        g.user = pa
        assert social.is_following(pa["id"], pb["id"]) is True
        assert social.is_blocked(pa["id"], pb["id"]) is True and social.blocked_between(pa["id"]) == {pb["id"]}


def test_writes_are_never_served_from_the_cache(as_user, db):
    a, pa = as_user("alice"); b, pb = as_user("bob")
    a.post("/u/bob/follow")
    assert a.get("/u/bob").status_code == 200
    a.post("/u/bob/block")                                   # follow -> block in the same session: state is always fresh
    assert a.get("/u/bob").status_code == 404
    a.post("/u/bob/block")
    assert a.get("/u/bob").status_code == 200


# ------------------------------------------------------------------ the four slow pages
def seed(db, mk, me, n_posts=40):
    people = [mk(f"user{i}") for i in range(10)]
    for u in people[:6]:
        db.table("follows").insert({"follower_id": me["id"], "followee_id": u["id"]}).execute()
    now = utcnow()
    for i in range(n_posts):
        u = people[i % len(people)]
        kind = "reel" if i % 3 == 0 else "post"
        row = db.table("posts").insert({"author_id": u["id"], "kind": kind, "caption": f"post {i}", "visibility": "public",
                                        "moderation_status": "ok", "community_id": None, "created_at": iso(now - timedelta(minutes=i)),
                                        "likes_count": 1, "comments_count": 0, "views_count": 0}).execute().data[0]
        db.table("post_media").insert({"post_id": row["id"], "owner_id": u["id"], "url": "https://x/y.jpg", "public_id": f"p{i}",
                                       "media_type": "video" if kind == "reel" else "image"}).execute()
    return people


@pytest.mark.parametrize("path,budget", [("/feed", 40), ("/discover", 20), ("/reels", 16), ("/u/user1", 28)])
def test_pages_stay_within_their_request_budget_and_do_not_repeat_lookups(as_user, db, mk, monkeypatch, path, budget):
    a, pa = as_user("alice")
    seed(db, mk, pa)
    Query = type(db.table("posts"))
    real, hits = Query.execute, Counter()
    def counting(self):
        hits[getattr(self, "table", None) or getattr(self, "name", "?")] += 1
        return real(self)
    monkeypatch.setattr(Query, "execute", counting)
    page = a.get(path)
    assert page.status_code == 200
    assert sum(hits.values()) <= budget, dict(hits)
    assert hits["blocks"] <= 2, dict(hits)                  # who is blocked: asked once (two directions), not once per component


def test_feed_still_hides_blocked_people_and_shows_followed_posts(as_user, db, mk):
    a, pa = as_user("alice")
    people = seed(db, mk, pa, n_posts=20)
    db.table("blocks").insert({"blocker_id": pa["id"], "blocked_id": people[1]["id"]}).execute()
    html = a.get("/feed").get_data(as_text=True)
    assert "@user0" in html and "@user1" not in html.split("<main")[1]


def test_reels_page_still_filters_blocked_authors(as_user, db, mk):
    a, pa = as_user("alice")
    people = seed(db, mk, pa, n_posts=30)
    blocked_author = next(p for p in people if any(x["author_id"] == p["id"] and x["kind"] == "reel" for x in db.data["posts"]))
    db.table("blocks").insert({"blocker_id": pa["id"], "blocked_id": blocked_author["id"]}).execute()
    html = a.get("/reels").get_data(as_text=True)
    assert f'/u/{blocked_author["username"]}' not in html
