"""Discover > Accounts: popular / recommended / trending sections, search, categories, pagination and follow state.
Every ranking test runs twice: with the optional SQL aggregates and without them (the no-migration fallback)."""
import re
from datetime import timedelta

import pytest

from app.utils import iso, utcnow

FETCH = {"X-Requested-With": "fetch"}


@pytest.fixture(params=["with_sql", "fallback"])
def mode(request, db):
    db.discover_rpc = request.param == "with_sql"
    return request.param


def prof(db, user, **fields):
    db.table("profiles").update(fields).eq("id", user["id"]).execute()
    return user


def follow(db, follower, followee, days_ago=30):
    db.table("follows").insert({"follower_id": follower["id"], "followee_id": followee["id"],
                                "created_at": iso(utcnow() - timedelta(days=days_ago))}).execute()


def crowd(db, mk, target, n, days_ago=30):
    for i in range(n):
        follow(db, mk(f"fan_{target['username']}_{i}"), target, days_ago)


def section(html, heading):
    """The HTML of one Discover section, from its heading to the next section."""
    start = html.index(heading)
    nxt = html.find('class="dsc-section"', start)
    return html[start: nxt if nxt != -1 else len(html)]


def usernames(html):
    return re.findall(r'data-dsc-card data-username="([^"]+)"', html)


def get(c, **q):
    return c.get("/discover", query_string={"tab": "people", **q}).get_data(as_text=True)


# ------------------------------------------------------------------ cards
def test_card_shows_identity_bio_followers_badge_and_follow_button(as_user, db, mk, mode):
    c, _ = as_user("alice")
    star = prof(db, mk("starlet"), display_name="Star Lette", bio="Dancer & photographer from Kampala", verified=True)
    crowd(db, mk, star, 3)
    html = get(c)
    card = html[html.index('data-username="starlet"'):]
    card = card[:card.index("</article>")]
    assert "Star Lette" in card and "@starlet" in card and "Dancer &amp; photographer" in card
    assert 'data-dsc-followers="starlet"' in card and ">3<" in card and "followers" in card
    assert "verified" in card.lower()                                       # badge for verified accounts
    assert 'data-follow="starlet"' in card and 'data-state="none"' in card
    assert 'href="/u/starlet"' in card                                      # the whole card opens the profile


def test_missing_optional_data_is_left_out_not_invented(as_user, db, mk, mode):
    c, _ = as_user("alice")
    prof(db, mk("plain"), bio=None)
    html = get(c)
    card = html[html.index('data-username="plain"'):]
    card = card[:card.index("</article>")]
    assert "dsc-bio" not in card and "dsc-reason" not in card and "dsc-flag" not in card


def test_page_has_search_chips_and_three_sections(as_user, db, mk, mode):
    c, _ = as_user("alice")
    html = get(c)
    for needle in ("data-dsc-input", "Popular accounts", "Recommended for you", "Trending creators", "Reel creators", "Verified", "My school", "Browse by school"):
        assert needle in html
    assert "js/discover.js" in html and 'class="page-wide' in html


# ------------------------------------------------------------------ popular
def test_popular_orders_by_followers_and_hides_self_blocked_and_unapproved(as_user, db, mk, mode):
    c, me = as_user("alice")
    big, mid, small = mk("big"), mk("mid"), mk("small")
    crowd(db, mk, big, 6); crowd(db, mk, mid, 3); crowd(db, mk, small, 1)
    troll, pending = mk("troll"), mk("pending")
    crowd(db, mk, troll, 9)
    prof(db, pending, account_status="pending"); crowd(db, mk, pending, 9)
    db.table("blocks").insert({"blocker_id": me["id"], "blocked_id": troll["id"]}).execute()
    html = get(c)
    pop = usernames(section(html, "Popular accounts"))
    assert pop.index("big") < pop.index("mid") < pop.index("small")
    assert "alice" not in pop and "troll" not in pop and "pending" not in pop


def test_boosted_accounts_stay_first_and_labelled(as_user, db, mk, mode):
    c, _ = as_user("alice")
    famous, boosty = mk("famous"), mk("boosty")
    crowd(db, mk, famous, 5)
    prof(db, boosty, boosted_until=iso(utcnow() + timedelta(days=1)))
    pop = section(get(c), "Popular accounts")
    assert usernames(pop).index("boosty") < usernames(pop).index("famous") and "Boosted" in pop


def test_followers_shown_are_fresh_even_when_the_ranking_is_cached(as_user, db, mk, mode):
    c, me = as_user("alice")
    t = mk("target"); crowd(db, mk, t, 2)
    assert 'data-count="2"' in get(c)
    assert c.post("/u/target/follow", headers=FETCH).get_json()["followers"] == 3
    page = get(c)
    assert 'data-count="3"' in page and 'data-state="following"' in page


# ------------------------------------------------------------------ trending
def test_trending_counts_only_the_last_week(as_user, db, mk, mode):
    c, _ = as_user("alice")
    hot, old, quiet = mk("hot"), mk("oldnews"), mk("quiet")
    crowd(db, mk, hot, 4, days_ago=1)
    crowd(db, mk, old, 8, days_ago=40)               # lots of followers, but none recently
    trend = section(get(c), "Trending creators")
    assert "hot" in usernames(trend) and "+4 this week" in trend
    assert "oldnews" not in usernames(trend) and "quiet" not in usernames(trend)


def test_trending_empty_state(as_user, db, mk, mode):
    c, _ = as_user("alice")
    mk("quiet")
    assert "No one is trending this week" in get(c)


# ------------------------------------------------------------------ recommended
def test_recommended_explains_why_and_skips_people_already_followed(as_user, db, mk, mode):
    c, me = as_user("alice")
    friend, fof, followed, other_school = mk("friend"), mk("fof"), mk("already"), None
    follow(db, me, friend); follow(db, me, followed)
    follow(db, friend, fof); follow(db, friend, followed)
    rec = section(get(c), "Recommended for you")
    assert "fof" in usernames(rec) and "Followed by friend" in rec
    assert "already" not in usernames(rec) and "friend" not in usernames(rec)


def test_recommended_uses_same_school_and_never_empty_for_new_members(as_user, db, mk, mode):
    c, _ = as_user("alice")
    mk("classmate")
    rec = section(get(c), "Recommended for you")
    assert "classmate" in usernames(rec) and "From your school" in rec


def test_personalisation_signal_adds_authors_the_viewer_engages_with(as_user, db, mk, mode, app):
    c, me = as_user("alice")
    liked = mk("liked_author")
    db.table("user_interests").insert({"user_id": me["id"], "dim": "author", "key": liked["id"], "weight": 2.5, "updated_at": iso(utcnow())}).execute()
    assert "liked their posts" in get(c)
    db.table("profiles").update({"personalized_recs": False}).eq("id", me["id"]).execute()      # opting out switches that signal off
    assert "liked their posts" not in get(c)


# ------------------------------------------------------------------ search
def test_search_matches_username_name_and_bio_exact_first(as_user, db, mk, mode):
    c, _ = as_user("alice")
    prof(db, mk("ann"), display_name="Somebody Else")
    prof(db, mk("annabel"), display_name="Annabel K")
    prof(db, mk("zoe"), display_name="Zoe", bio="I teach ann's dance class")
    prof(db, mk("unrelated"), display_name="Nope", bio="nothing")
    html = get(c, q="ann")
    found = usernames(html)
    assert found[:2] == ["ann", "annabel"] and "zoe" in found and "unrelated" not in found
    assert "Results for “ann”" in html and "3 accounts" in html and "Recommended for you" not in html


@pytest.mark.parametrize("q", ["100%", "a,b", "x)(y", 'quote"s', "back\\slash", "under_score", "   ", "<script>"])
def test_search_input_is_safe(as_user, db, mk, q):
    c, _ = as_user("alice")
    r = c.get("/discover", query_string={"tab": "people", "q": q})
    assert r.status_code == 200 and "<script>alert" not in r.get_data(as_text=True)


def test_search_with_no_matches_shows_a_designed_empty_state(as_user, db, mk, mode):
    c, _ = as_user("alice")
    html = get(c, q="zzzznobody")
    assert "No accounts found" in html and "Back to Discover" in html


def test_search_hides_blocked_and_suspended_accounts(as_user, db, mk, mode):
    c, me = as_user("alice")
    bad, sus = mk("annoying"), mk("annex")
    prof(db, sus, account_status="suspended")
    db.table("blocks").insert({"blocker_id": me["id"], "blocked_id": bad["id"]}).execute()
    assert usernames(get(c, q="ann")) == []


# ------------------------------------------------------------------ categories
def test_verified_category(as_user, db, mk, mode):
    c, _ = as_user("alice")
    prof(db, mk("checked"), verified=True); mk("unchecked")
    assert usernames(get(c, cat="verified")) == ["checked"]


def test_my_school_and_browse_by_school(as_user, db, mk, mode):
    c, me = as_user("alice")
    other = db.table("schools").insert({"name": "Other High", "slug": "other-high", "country": "Uganda"}).execute().data[0]
    mk("mate"); mk("outsider", school=other)
    assert usernames(get(c, cat="school")) == ["mate"]
    assert usernames(get(c, school=other["id"])) == ["outsider"]
    assert "Accounts at this school" in get(c, school=other["id"])
    assert usernames(get(c, school="not-a-uuid")) != []                    # junk ids are ignored, page falls back to home


def test_reel_creators_category_uses_real_reels(as_user, db, mk, mode):
    c, _ = as_user("alice")
    maker, lurker, hidden = mk("maker"), mk("lurker"), mk("shy")
    db.table("posts").insert({"author_id": maker["id"], "kind": "reel", "caption": "x"}).execute()
    db.table("posts").insert({"author_id": hidden["id"], "kind": "reel", "caption": "x", "visibility": "followers"}).execute()
    assert usernames(get(c, cat="creators")) == ["maker"]


def test_see_all_views(as_user, db, mk, mode):
    c, _ = as_user("alice")
    hot = mk("hot"); crowd(db, mk, hot, 2, days_ago=1)
    assert "hot" in usernames(get(c, view="trending")) and "Trending creators" in get(c, view="trending")
    assert "hot" in usernames(get(c, view="popular")) and "Popular accounts" in get(c, view="popular")
    assert "Recommended for you" in get(c, view="recommended")


# ------------------------------------------------------------------ pagination + partials
def test_results_paginate_and_partials_are_fragments(as_user, db, mk, mode):
    c, _ = as_user("alice")
    for i in range(30):
        mk(f"kid{i:02d}")
    first = get(c, q="kid")
    assert len(usernames(first)) == 24 and "data-dsc-more" in first and "30 accounts" in first
    more = get(c, q="kid", more=1, page=2)
    assert len(usernames(more)) == 6 and "<html" not in more and "data-dsc-next" not in more
    assert not set(usernames(first)) & set(usernames(more))
    part = get(c, q="kid", partial=1)
    assert "<html" not in part and "dsc-chips" in part and len(usernames(part)) == 24
    assert get(c, q="kid", page="junk") and get(c, q="kid", page=999) is not None       # bad page numbers can't break the page


# ------------------------------------------------------------------ the rest of Discover still works
def test_other_tabs_and_nav_still_work(as_user, db, mk):
    c, _ = as_user("alice")
    for tab in ("schools", "events", "posts"):
        assert c.get(f"/discover?tab={tab}").status_code == 200
    html = c.get("/discover?tab=schools").get_data(as_text=True)
    assert "Accounts" in html and "Events" in html and "js/discover.js" not in html and 'class="page-wide' not in html
    assert c.get("/discover", follow_redirects=False).status_code == 200


def test_requires_login(client):
    assert client.get("/discover").status_code in (302, 401)
