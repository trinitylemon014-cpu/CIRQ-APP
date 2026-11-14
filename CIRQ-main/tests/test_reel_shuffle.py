"""The ordering rules behind the shuffled reels feed (no database needed)."""
import base64
import uuid

import pytest

from app.services import reel_shuffle as rs

T0 = "2026-11-14T05:00:00+00:00"


def ids(n=107):
    return [str(uuid.uuid4()) for _ in range(n)]


def walk(all_ids, seed, back=frozenset(), per=5):
    """Page through the whole queue the way the route does, returning ids in served order."""
    served, position = [], None
    while True:
        queue = rs.remaining(rs.order(all_ids, seed, back), seed, back, position)
        page = queue[:per + 1]
        served += page[:per]
        if len(page) <= per:
            return served
        last = page[per - 1]
        position = rs.decode_cursor(rs.encode_cursor(seed, T0, last, last in back))["position"]


def test_every_reel_is_served_exactly_once():
    pool = ids()
    served = walk(pool, rs.new_seed())
    assert sorted(served) == sorted(pool)


def test_each_visit_gets_a_different_order_but_one_seed_is_repeatable():
    pool = ids()
    a, b = rs.new_seed(), rs.new_seed()
    assert rs.order(pool, a, set()) == rs.order(pool, a, set())
    assert rs.order(pool, a, set()) != rs.order(pool, b, set())


def test_watched_and_own_reels_always_come_after_unseen_ones():
    pool = ids()
    back = set(pool[:40])
    served = walk(pool, rs.new_seed(), back)
    assert set(served[:67]) == set(pool[40:]) and set(served[67:]) == back


def test_a_reel_added_mid_visit_never_causes_repeats_or_skips():
    pool, seed = ids(), rs.new_seed()
    served, position, extra = [], None, str(uuid.uuid4())
    for turn in range(40):
        current = pool + ([extra] if turn >= 3 else [])
        queue = rs.remaining(rs.order(current, seed, set()), seed, set(), position)
        page = queue[:5]
        served += page
        if len(queue) <= 5:
            break
        position = rs.decode_cursor(rs.encode_cursor(seed, T0, page[-1], False))["position"]
    assert len(served) == len(set(served)) and set(pool) <= set(served)


def test_no_reel_is_favoured_on_the_first_page():
    pool = ids(50)
    firsts = set()
    for _ in range(300):
        firsts.update(rs.order(pool, rs.new_seed(), set())[:5])
    assert len(firsts) == len(pool)


def test_cursor_round_trip_and_rejection_of_bad_cursors():
    rid = str(uuid.uuid4())
    decoded = rs.decode_cursor(rs.encode_cursor("abcdef12", T0, rid, True))
    assert decoded["seed"] == "abcdef12" and decoded["t0"] == T0 and decoded["position"][0] == 1 and decoded["position"][2] == rid
    legacy = base64.urlsafe_b64encode(f"{T0}|{rid}".encode()).decode().rstrip("=")
    for bad in ("invalid", legacy, base64.urlsafe_b64encode(b"v2|zz|x|9|y|z").decode()):
        with pytest.raises(rs.BadCursor):
            rs.decode_cursor(bad)
