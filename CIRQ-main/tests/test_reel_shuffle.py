"""Reels are shuffled: a different order on every visit, unwatched reels first, one visit never repeats a reel."""
import base64

from app.utils import iso
from tests.test_recommendations import private, reel, reel_ids, walk_reels


def make_reels(db, creator, n):
    return [reel(db, creator, f"reel {i}") for i in range(n)]


def mark_watched(db, viewer, reels):
    for r in reels:
        db.table("post_signals").insert({"user_id": viewer["id"], "post_id": r["id"], "event": "watch", "created_at": iso()}).execute()


def test_every_visit_has_a_different_order(as_user, db, mk):
    a, _ = as_user("alice")
    reels = make_reels(db, mk("carol"), 12)
    orders = {tuple(walk_reels(a)) for _ in range(10)}
    assert len(orders) > 1                                              # not the same list every time
    assert all(set(o) == {r["id"] for r in reels} and len(o) == 12 for o in orders)      # but always every reel, once


def test_first_page_changes_between_visits(as_user, db, mk):
    a, _ = as_user("alice")
    make_reels(db, mk("carol"), 12)
    firsts = {tuple(reel_ids(a.get("/reels?partial=1"))) for _ in range(10)}
    assert len(firsts) > 1


def test_scrolling_continues_the_same_order_and_a_cursor_is_repeatable(as_user, db, mk):
    a, _ = as_user("alice")
    make_reels(db, mk("carol"), 14)
    first = a.get("/reels?partial=1")
    cursor = first.headers["X-Next-Cursor"]
    again = [reel_ids(a.get(f"/reels?partial=1&before={cursor}")) for _ in range(3)]
    assert again[0] == again[1] == again[2]                              # the same cursor always gives the same next page
    assert not set(again[0]) & set(reel_ids(first))


def test_reels_you_have_not_watched_come_first_even_when_shuffled(as_user, db, mk):
    a, alice = as_user("alice")
    reels = make_reels(db, mk("carol"), 12)
    watched, fresh = reels[:7], reels[7:]
    mark_watched(db, alice, watched)
    for _ in range(6):
        order = walk_reels(a)
        positions = {rid: i for i, rid in enumerate(order)}
        assert max(positions[r["id"]] for r in fresh) < min(positions[r["id"]] for r in watched)


def test_when_everything_is_watched_the_order_still_changes_every_visit(as_user, db, mk):
    a, alice = as_user("alice")
    reels = make_reels(db, mk("carol"), 12)
    mark_watched(db, alice, reels)
    orders = {tuple(walk_reels(a)) for _ in range(10)}
    assert len(orders) > 1 and all(len(o) == 12 for o in orders)        # all watched, yet never the same list


def test_watching_is_per_person(as_user, db, mk):
    a, alice = as_user("alice"); b, _ = as_user("bob")
    reels = make_reels(db, mk("carol"), 12)
    mark_watched(db, alice, reels[:6])
    order_b = {tuple(walk_reels(b)) for _ in range(8)}
    assert len(order_b) > 1                                             # bob has watched nothing: nothing is pushed back for him


def test_blocked_and_private_reels_never_appear_in_the_shuffle(as_user, db, mk):
    a, alice = as_user("alice")
    carol, rude, secret = mk("carol"), mk("rude"), mk("secret")
    ok = make_reels(db, carol, 6)
    blocked = make_reels(db, rude, 4)
    hidden = make_reels(db, private(db, secret), 4)
    db.table("blocks").insert({"blocker_id": alice["id"], "blocked_id": rude["id"]}).execute()
    for _ in range(5):
        seen = set(walk_reels(a))
        assert seen == {r["id"] for r in ok}
        assert not seen & {r["id"] for r in blocked + hidden}


def test_a_follower_sees_a_private_creators_reels_in_the_shuffle(as_user, db, mk):
    a, _ = as_user("alice")
    creator = mk("creator")
    a.post("/u/creator/follow")
    private(db, creator)
    mine = make_reels(db, creator, 3)
    assert set(walk_reels(a)) == {r["id"] for r in mine}


def test_own_reels_are_included(as_user, db, mk):
    a, alice = as_user("alice")
    own = make_reels(db, alice, 3)
    assert set(walk_reels(a)) == {r["id"] for r in own}


def test_bad_cursors_are_rejected_and_old_cursors_still_parse(as_user, db, mk):
    a, _ = as_user("alice")
    make_reels(db, mk("carol"), 3)
    enc = lambda raw: base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")
    assert a.get(f"/reels?partial=1&before={enc('s1|abc|-5')}").status_code == 400
    assert a.get(f"/reels?partial=1&before={enc('s1|not valid|3')}").status_code == 400
    assert a.get(f"/reels?partial=1&before={enc('s1|abc|notanumber')}").status_code == 400
    assert a.get("/reels?partial=1&before=garbage!!").status_code == 400
    assert a.get(f"/reels?partial=1&before={enc('s1|abc|0')}").status_code == 200
    assert a.get(f"/reels?partial=1&before={enc('s1|abc|0')}&after={enc('s1|abc|0')}").status_code == 400


def test_a_cursor_past_the_end_ends_the_feed_cleanly(as_user, db, mk):
    a, _ = as_user("alice")
    make_reels(db, mk("carol"), 3)
    enc = base64.urlsafe_b64encode(b"s1|abc|999").decode().rstrip("=")
    r = a.get(f"/reels?partial=1&before={enc}")
    assert r.status_code == 200 and reel_ids(r) == [] and r.headers["X-Next-Cursor"] == ""


def test_empty_feed_still_shows_the_empty_state(as_user):
    a, _ = as_user("alice")
    page = a.get("/reels").get_data(as_text=True)
    assert "No reels yet" in page
