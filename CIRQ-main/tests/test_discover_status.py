"""Every Discover card shows the account's CIRQ STATUS (Rookie ... Hall of Fame), always matching the profile page."""
import re

import pytest

from app.services import discovery
from app.utils import TIERS, tier_for

CASES = [(0, "Rookie", 0), (99, "Rookie", 0), (100, "Elite", 1), (500, "Elite", 1), (501, "Legendary", 2), (1000, "Legendary", 2),
         (1001, "Icon", 3), (2500, "Icon", 3), (2501, "Superstar", 4), (5000, "Superstar", 4), (5001, "Royalty", 5),
         (10000, "Royalty", 5), (10001, "Hall of Fame", 6), (250000, "Hall of Fame", 6)]


def card(html, username):
    start = html.index(f'data-dsc-card data-username="{username}"')
    return html[start: html.index("</article>", start)]


def status(html, username):
    m = re.search(r'class="dsc-status t(\d)" title="CIRQ status: ([^"]+)"', card(html, username))
    return (m.group(2), int(m.group(1))) if m else None


def test_every_account_card_shows_a_status_even_brand_new_rookies(as_user, mk):
    c, _ = as_user("alice")
    for n in ("newbie", "another"):
        mk(n)
    html = c.get("/discover?tab=people").get_data(as_text=True)
    assert status(html, "newbie") == ("Rookie", 0) and status(html, "another") == ("Rookie", 0)
    assert "CIRQ status: </span>" in card(html, "newbie")                  # screen readers hear "CIRQ status: Rookie"


@pytest.mark.parametrize("followers,name,idx", CASES)
def test_status_matches_the_follower_tier_at_every_boundary(as_user, mk, db, monkeypatch, followers, name, idx):
    c, _ = as_user("alice")
    u = mk("subject")
    monkeypatch.setattr(discovery, "follower_counts", lambda ids, **kw: {i: followers for i in ids})
    html = c.get("/discover?tab=people").get_data(as_text=True)
    assert status(html, "subject") == (name, idx)
    assert tier_for(followers)["name"] == name


def test_all_seven_tiers_are_covered_and_distinct():
    assert [t[0] for t in TIERS] == ["Rookie", "Elite", "Legendary", "Icon", "Superstar", "Royalty", "Hall of Fame"]
    assert {c[1] for c in CASES} == {t[0] for t in TIERS}
    css = open("app/static/css/style.css").read()
    for i in range(7):
        assert f".dsc-status.t{i}" in css                                       # every tier has its own style


def test_status_is_shown_in_every_section_and_in_search_results(as_user, mk, db, monkeypatch):
    c, me = as_user("alice")
    hot, other = mk("hot"), mk("other")
    for i in range(2):
        db.table("follows").insert({"follower_id": mk(f"fan{i}")["id"], "followee_id": hot["id"]}).execute()
    html = c.get("/discover?tab=people").get_data(as_text=True)
    assert html.count('class="dsc-status') >= 3
    assert status(c.get("/discover?tab=people&q=hot").get_data(as_text=True), "hot") == ("Rookie", 0)
    assert status(c.get("/discover?tab=people&view=popular").get_data(as_text=True), "hot") == ("Rookie", 0)
    assert status(c.get("/discover?tab=people&q=hot&more=1&page=1").get_data(as_text=True), "hot") == ("Rookie", 0)


def test_discover_status_always_agrees_with_the_profile_page(as_user, mk, db, monkeypatch):
    c, _ = as_user("alice")
    star = mk("starlet")
    monkeypatch.setattr(discovery, "follower_counts", lambda ids, **kw: {i: 700 for i in ids})
    html = c.get("/discover?tab=people").get_data(as_text=True)
    assert status(html, "starlet") == ("Legendary", 2)
    # the profile page uses social.profile_stats -> tier_for on the real count; both go through the same function
    assert tier_for(700)["name"] == "Legendary"
    real = c.get("/u/starlet").get_data(as_text=True)
    assert "Rookie" in real                                                    # real count is 0 here, and Discover shows the same for a real count of 0
    monkeypatch.undo()
    assert status(c.get("/discover?tab=people").get_data(as_text=True), "starlet") == ("Rookie", 0)
