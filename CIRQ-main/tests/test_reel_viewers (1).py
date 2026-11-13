"""'Who viewed my reel': author-only, newest first, blocked/unapproved people left out, paged."""
import re
from datetime import timedelta

from app.utils import utcnow, iso
from tests.test_reel_views import create_reel


def add_view(db, reel, viewer, minutes_ago=0):
    db.table("reel_views").insert({
        "reel_id": reel["id"], "viewer_id": viewer["id"],
        "created_at": iso(utcnow() - timedelta(minutes=minutes_ago)),
    }).execute()


def viewers_url(reel):
    return f"/api/reels/{reel['id']}/viewers"


def test_author_sees_viewers_newest_first(as_user, db, mk):
    author, profile = as_user("alice")
    bob, cat, dan = mk("bob"), mk("cat"), mk("dan")
    reel = create_reel(db, profile)
    add_view(db, reel, bob, 30); add_view(db, reel, cat, 20); add_view(db, reel, dan, 5)

    html = author.get(viewers_url(reel)).get_data(as_text=True)
    assert html.index("@dan") < html.index("@cat") < html.index("@bob")
    assert "/u/bob" in html and "No views yet" not in html
    assert "30m" in html                      # when each person watched


def test_empty_state(as_user, db):
    author, profile = as_user("alice")
    reel = create_reel(db, profile)
    assert "No views yet" in author.get(viewers_url(reel)).get_data(as_text=True)


def test_only_the_author_can_open_the_list(as_user, db):
    author, profile = as_user("alice")
    viewer, viewer_profile = as_user("bob")
    reel = create_reel(db, profile)
    add_view(db, reel, viewer_profile)

    assert viewer.get(viewers_url(reel)).status_code == 404      # a viewer can't see who else watched
    assert author.get(viewers_url(reel)).status_code == 200


def test_missing_reels_and_plain_posts_404(as_user, db):
    author, profile = as_user("alice")
    post = db.table("posts").insert({"author_id": profile["id"], "kind": "post"}).execute().data[0]
    assert author.get(viewers_url(post)).status_code == 404
    assert author.get("/api/reels/00000000-0000-0000-0000-000000000000/viewers").status_code == 404


def test_blocked_and_unapproved_people_are_left_out(as_user, db, mk):
    author, profile = as_user("alice")
    bob, cat, ghost, dan = mk("bob"), mk("cat"), mk("ghost"), mk("dan")
    next(p for p in db.data["profiles"] if p["username"] == "ghost")["account_status"] = "suspended"
    reel = create_reel(db, profile)
    for u in (bob, cat, ghost, dan):
        add_view(db, reel, u)
    db.table("blocks").insert({"blocker_id": profile["id"], "blocked_id": bob["id"]}).execute()   # I blocked bob
    db.table("blocks").insert({"blocker_id": cat["id"], "blocked_id": profile["id"]}).execute()   # cat blocked me

    html = author.get(viewers_url(reel)).get_data(as_text=True)
    assert "@bob" not in html and "@cat" not in html and "@ghost" not in html
    assert "@dan" in html


def test_list_is_paged_twenty_at_a_time(as_user, db, mk):
    author, profile = as_user("alice")
    reel = create_reel(db, profile)
    for i in range(25):
        add_view(db, reel, mk(f"user{i:02d}"), minutes_ago=100 - i)     # user24 is the newest

    first = author.get(viewers_url(reel)).get_data(as_text=True)
    assert "@user24" in first and "@user05" in first and "@user04" not in first
    cursor = re.search(r'data-next="([^"]+)"', first).group(1)

    second = author.get(viewers_url(reel), query_string={"before": cursor}).get_data(as_text=True)
    assert "@user04" in second and "@user00" in second and "@user05" not in second
    assert "data-next" not in second and "No views yet" not in second


def test_owner_gets_a_tappable_count_and_others_do_not(as_user, db):
    author, profile = as_user("alice")
    viewer, _ = as_user("bob")
    reel = create_reel(db, profile)
    db.table("posts").update({"views_count": 7}).eq("id", reel["id"]).execute()

    mine = author.get("/reels").get_data(as_text=True)
    assert viewers_url(reel) in mine and 'data-likers-title="Viewers"' in mine
    theirs = viewer.get("/reels").get_data(as_text=True)
    assert viewers_url(reel) not in theirs and 'aria-label="7 views"' in theirs
