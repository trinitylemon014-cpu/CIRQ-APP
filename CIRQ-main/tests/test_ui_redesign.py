"""The light "lime" redesign: feed tabs, new post-card markup, bottom navigation, landing, brand tile."""
from tests.conftest import login, make_user


def mkpost(db, author, caption, **kw):
    return db.table("posts").insert({"author_id": author["id"], "kind": "post", "caption": caption, "visibility": "public", **kw}).execute().data[0]


def test_landing_is_the_onboarding_hero_with_get_started_and_login(client):
    html = client.get("/").get_data(as_text=True)
    assert "Best Social App to Make New Friends" in html
    assert "Get Started" in html and ">Login<" in html
    assert 'class="brand-tile' in html and "images/face-1.jpg" in html


def test_feed_has_home_and_for_you_tabs_home_active_by_default(client, mk):
    mk("amina"); login(client, "amina")
    html = client.get("/feed").get_data(as_text=True)
    assert 'class="feed-tabs"' in html and "For you" in html
    assert 'class="active" aria-current="page">Home' in html
    assert 'id="composer"' in html


def test_for_you_tab_never_errors_and_falls_back_to_the_normal_feed(client, mk, db):
    me, other = mk("amina"), mk("bea")
    mkpost(db, me, "my own post")
    login(client, "amina")
    r = client.get("/feed?feed=foryou")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert 'class="active" aria-current="page">For you' in html
    assert 'id="composer"' not in html                      # browsing mode: no composer
    assert "my own post" in html                            # nothing to recommend, so the normal feed is shown instead
    assert "Nothing new to suggest" in html


def test_for_you_tab_survives_a_recommender_failure(client, mk, monkeypatch):
    mk("amina"); login(client, "amina")
    from app.services import recommender
    monkeypatch.setattr(recommender, "suggest", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert client.get("/feed?feed=foryou").status_code == 200


def test_post_card_has_inline_author_caption_and_labelled_counts(client, mk, db):
    me = mk("amina"); p = mkpost(db, me, "hello world")
    login(client, "amina")
    html = client.get("/feed").get_data(as_text=True)
    assert '<b class="post-author">amina</b>' in html and "hello world" in html
    assert 'data-count>0</span><span class="act-lbl"> Likes' in html
    assert 'data-comment-count-for="%s">0</span><span class="act-lbl"> Comments' % p["id"] in html


def test_singular_labels_for_one_like(client, mk, db):
    me = mk("amina"); p = mkpost(db, me, "x", like_count=1)
    login(client, "amina")
    html = client.get("/feed").get_data(as_text=True)
    assert 'data-count>1</span><span class="act-lbl"> Like<' in html


def test_bottom_nav_has_home_discover_create_reels_profile_and_messages_live_in_header(client, mk):
    mk("amina"); login(client, "amina")
    html = client.get("/feed").get_data(as_text=True)
    nav = html[html.index('<nav class="bottom-nav"'):]
    nav = nav[:nav.index("</nav>")]
    for label in ('aria-label="Home"', 'aria-label="Discover"', 'aria-label="Create"', 'aria-label="Reels"', 'aria-label="Profile"'):
        assert label in nav
    assert "/u/amina" in nav
    assert 'aria-label="Messages"' in html                 # inbox moved to the header, like the reference


def test_profile_uses_the_new_layout(client, mk):
    mk("amina"); mk("bea"); login(client, "amina")
    r = client.get("/u/bea")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert 'class="card profile"' in html and 'class="profile-cover"' in html and "tabs icons" in html
