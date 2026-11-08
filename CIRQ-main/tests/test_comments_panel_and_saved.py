"""Side/sheet comment panel API, per-viewer like/save flags, and the private 'Saved posts and reels' profile tab."""
import re
from datetime import datetime, timedelta, timezone

from app.services import social

FETCH = {"X-Requested-With": "fetch"}


def mkpost(db, author, caption="hello", kind="post", **kw):
    return db.table("posts").insert({"author_id": author["id"], "kind": kind, "caption": caption, **kw}).execute().data[0]


def mkreel(db, author, caption="a reel"):
    p = mkpost(db, author, caption, kind="reel")
    db.table("post_media").insert({"post_id": p["id"], "owner_id": author["id"], "media_type": "video", "url": "u",
                                   "public_id": f"cirq/reels/{p['id'][:8]}"}).execute()
    return p


def mkphoto(db, author, caption="a photo"):
    p = mkpost(db, author, caption)
    db.table("post_media").insert({"post_id": p["id"], "owner_id": author["id"], "media_type": "image", "url": "u",
                                   "public_id": f"cirq/posts/{p['id'][:8]}"}).execute()
    return p


def add_comments(db, post, author, n, prefix="comment"):
    """n comments, each one second apart (oldest first) so ordering is deterministic."""
    start = datetime.now(timezone.utc) - timedelta(hours=1)
    ids = []
    for i in range(n):
        row = db.table("comments").insert({"post_id": post["id"], "author_id": author["id"], "body": f"{prefix} {i}",
                                           "created_at": (start + timedelta(seconds=i)).isoformat()}).execute().data[0]
        ids.append(row["id"])
    return ids


# ------------------------------------------------------------------ comments API
def test_comments_page_is_newest_first_and_paginates_without_repeats(as_user, db, mk):
    viewer, _ = as_user("alice")
    author = mk("carol")
    p = mkreel(db, author)
    add_comments(db, p, author, 45)

    r1 = viewer.get(f"/api/posts/{p['id']}/comments").get_json()
    assert r1["has_more"] and r1["next_cursor"] and r1["count"] == 45
    first = re.findall(r'data-comment="([^"]+)"', r1["html"])
    assert len(first) == 20
    assert "comment 44" in r1["html"] and "comment 25" in r1["html"] and "comment 24" not in r1["html"]   # newest 20 of 45

    r2 = viewer.get(f"/api/posts/{p['id']}/comments", query_string={"before": r1["next_cursor"]}).get_json()
    second = re.findall(r'data-comment="([^"]+)"', r2["html"])
    assert len(second) == 20 and not set(first) & set(second) and r2["has_more"]

    r3 = viewer.get(f"/api/posts/{p['id']}/comments", query_string={"before": r2["next_cursor"]}).get_json()
    third = re.findall(r'data-comment="([^"]+)"', r3["html"])
    assert len(third) == 5 and not r3["has_more"] and r3["next_cursor"] == ""
    assert len(set(first) | set(second) | set(third)) == 45


def test_comments_page_handles_comments_sharing_a_timestamp(as_user, db, mk):
    viewer, _ = as_user("alice")
    author = mk("carol")
    p = mkpost(db, author)
    same = datetime.now(timezone.utc).isoformat()
    for i in range(30):
        db.table("comments").insert({"post_id": p["id"], "author_id": author["id"], "body": f"tie {i}", "created_at": same}).execute()
    r1 = viewer.get(f"/api/posts/{p['id']}/comments").get_json()
    r2 = viewer.get(f"/api/posts/{p['id']}/comments", query_string={"before": r1["next_cursor"]}).get_json()
    ids = re.findall(r'data-comment="([^"]+)"', r1["html"]) + re.findall(r'data-comment="([^"]+)"', r2["html"])
    assert len(ids) == 30 and len(set(ids)) == 30          # nothing skipped, nothing repeated


def test_comments_page_rejects_bad_cursor_and_respects_post_privacy(as_user, db, mk):
    viewer, _ = as_user("alice")
    owner = mk("carol", )
    db.table("profiles").update({"is_private": True}).eq("id", owner["id"]).execute()
    secret = mkpost(db, owner, visibility="followers")
    assert viewer.get(f"/api/posts/{secret['id']}/comments").status_code == 404     # not a follower
    public_author = mk("dave")
    p = mkpost(db, public_author)
    assert viewer.get(f"/api/posts/{p['id']}/comments", query_string={"before": "not-a-cursor"}).status_code == 400
    assert viewer.get("/api/posts/00000000-0000-0000-0000-000000000000/comments").status_code == 404


def test_comments_page_hides_blocked_authors_and_flagged_comments(as_user, db, mk):
    viewer, me = as_user("alice")
    author, troll = mk("carol"), mk("troll")
    p = mkpost(db, author)
    add_comments(db, p, author, 1, "friendly")
    add_comments(db, p, troll, 1, "rude")
    db.table("comments").insert({"post_id": p["id"], "author_id": author["id"], "body": "removed one", "moderation_status": "removed"}).execute()
    db.table("blocks").insert({"blocker_id": me["id"], "blocked_id": troll["id"]}).execute()
    html = viewer.get(f"/api/posts/{p['id']}/comments").get_json()["html"]
    assert "friendly" in html and "rude" not in html and "removed one" not in html


def test_comment_rows_show_author_tag_likes_and_permissions(as_user, db, mk):
    owner_c, owner = as_user("carol")
    viewer, me = as_user("alice")
    p = mkpost(db, owner)
    c_owner = db.table("comments").insert({"post_id": p["id"], "author_id": owner["id"], "body": "from the author"}).execute().data[0]
    c_mine = db.table("comments").insert({"post_id": p["id"], "author_id": me["id"], "body": "my own words"}).execute().data[0]
    db.table("comment_likes").insert({"comment_id": c_owner["id"], "user_id": me["id"]}).execute()
    db.table("comment_likes").insert({"comment_id": c_owner["id"], "user_id": owner["id"]}).execute()

    html = viewer.get(f"/api/posts/{p['id']}/comments").get_json()["html"]
    rows = {m.group(1): m.group(0) for m in re.finditer(r'<article class="crow" id="c-([^"]+)".*?</article>', html, re.S)}
    theirs = rows[c_owner["id"]]
    assert "Author" in theirs and "from the author" in theirs
    assert 'liked' in theirs and ">2<" in theirs                  # I liked it, 2 likes in total
    assert f'data-comment-delete="{c_mine["id"]}"' in html           # I can delete my own
    assert f'data-comment-delete="{c_owner["id"]}"' not in html      # ...but not the author's
    assert f'data-report="comment:{c_owner["id"]}"' in html

    # the post's owner can delete anybody's comment from the panel
    owner_html = owner_c.get(f"/api/posts/{p['id']}/comments").get_json()["html"]
    assert f'data-comment-delete="{c_mine["id"]}"' in owner_html


def test_posting_from_the_panel_returns_a_panel_row_and_count(as_user, db, mk):
    viewer, _ = as_user("alice")
    p = mkpost(db, mk("carol"))
    r = viewer.post(f"/posts/{p['id']}/comments", data={"body": "first! <b>x</b>", "view": "panel"}, headers=FETCH)
    j = r.get_json()
    assert r.status_code == 200 and j["count"] == 1 and j["id"]
    assert 'class="crow"' in j["html"] and "&lt;b&gt;x&lt;/b&gt;" in j["html"]       # escaped, never raw HTML
    assert viewer.post(f"/posts/{p['id']}/comments", data={"body": "   ", "view": "panel"}, headers=FETCH).status_code == 400


def test_deleting_a_comment_returns_the_fresh_count(as_user, db, mk):
    viewer, me = as_user("alice")
    p = mkpost(db, mk("carol"))
    ids = add_comments(db, p, me, 3)
    r = viewer.post(f"/comments/{ids[0]}/delete", headers=FETCH)
    assert r.get_json() == {"ok": True, "count": 2}


# ------------------------------------------------------------------ per-viewer flags
def test_hydrate_flags_only_the_viewers_own_likes_and_saves(app, as_user, db, mk):
    _, me = as_user("alice")
    other, author = mk("bob"), mk("carol")
    p = mkpost(db, author)
    db.table("likes").insert({"post_id": p["id"], "user_id": other["id"]}).execute()
    db.table("saved_posts").insert({"post_id": p["id"], "user_id": other["id"]}).execute()
    with app.app_context():
        card = social.hydrate_posts([p], me["id"])[0]
        assert card["liked"] is False and card["saved"] is False       # somebody else's like/save never leaks onto my card
        db.table("likes").insert({"post_id": p["id"], "user_id": me["id"]}).execute()
        db.table("saved_posts").insert({"post_id": p["id"], "user_id": me["id"]}).execute()
        card = social.hydrate_posts([p], me["id"])[0]
        assert card["liked"] is True and card["saved"] is True


# ------------------------------------------------------------------ reels page markup
def test_reel_actions_are_all_present_and_comment_button_opens_the_panel(as_user, db, mk):
    viewer, _ = as_user("alice")
    r = mkreel(db, mk("carol"))
    html = viewer.get("/reels").get_data(as_text=True)
    side = html[html.index('class="side"'):html.index("reel-options")]
    for needle in ("data-like=", f'data-comments="{r["id"]}"', "reel-view-count", "data-copy=", "data-save="):
        assert needle in side
    assert 'data-comments-host' in html and 'data-cpanel ' in html and "js/comments.js" in html


# ------------------------------------------------------------------ Saved posts and reels
def save(db, user, post):
    db.table("saved_posts").insert({"post_id": post["id"], "user_id": user["id"]}).execute()


def test_saved_tab_lists_posts_and_reels_newest_saved_first_and_filters(as_user, db, mk):
    me_c, me = as_user("alice")
    author = mk("carol")
    photo, clip, text = mkphoto(db, author, "sunset"), mkreel(db, author, "dance"), mkpost(db, author, "just words")
    base = datetime.now(timezone.utc) - timedelta(minutes=10)
    for i, item in enumerate((photo, clip, text)):
        db.table("saved_posts").insert({"post_id": item["id"], "user_id": me["id"], "created_at": (base + timedelta(minutes=i)).isoformat()}).execute()

    html = me_c.get("/u/alice?tab=saved").get_data(as_text=True)
    assert "Saved posts and reels" in html and "?tab=saved" in html
    order = [html.index(f"/post/{x['id']}") for x in (text, clip, photo)]
    assert order == sorted(order)                                     # last saved appears first

    reels_only = me_c.get("/u/alice?tab=saved&type=reels").get_data(as_text=True)
    assert f"/post/{clip['id']}" in reels_only and f"/post/{photo['id']}" not in reels_only
    posts_only = me_c.get("/u/alice?tab=saved&type=posts").get_data(as_text=True)
    assert f"/post/{photo['id']}" in posts_only and f"/post/{clip['id']}" not in posts_only


def test_saved_tab_is_private_to_the_owner(as_user, db, mk):
    me_c, me = as_user("alice")
    other_c, _ = as_user("bob")
    clip = mkreel(db, mk("carol"))
    save(db, me, clip)
    assert "Saved posts and reels" not in other_c.get("/u/alice?tab=saved").get_data(as_text=True)     # falls back to Posts
    assert f"/post/{clip['id']}" not in other_c.get("/u/alice?tab=saved").get_data(as_text=True)
    assert ">Saved<" not in other_c.get("/u/alice").get_data(as_text=True).replace("<small>Saved</small>", "")
    assert "Saved posts and reels" in me_c.get("/saved", follow_redirects=True).get_data(as_text=True)


def test_saved_tab_skips_posts_you_can_no_longer_see(as_user, db, mk):
    me_c, me = as_user("alice")
    author, blocked_author, shy = mk("carol"), mk("troll"), mk("shy")
    ok, hidden_by_block, was_public = mkpost(db, author, "keep me"), mkpost(db, blocked_author, "blocked one"), mkpost(db, shy, "was public")
    removed = mkpost(db, author, "removed by mods", moderation_status="removed")
    for item in (ok, hidden_by_block, was_public, removed):
        save(db, me, item)
    db.table("blocks").insert({"blocker_id": me["id"], "blocked_id": blocked_author["id"]}).execute()
    db.table("profiles").update({"is_private": True}).eq("id", shy["id"]).execute()          # went private after being saved
    html = me_c.get("/u/alice?tab=saved").get_data(as_text=True)
    assert f"/post/{ok['id']}" in html
    for gone in (hidden_by_block, was_public, removed):
        assert f"/post/{gone['id']}" not in html


def test_saved_tab_paginates_with_a_cursor(as_user, db, mk):
    me_c, me = as_user("alice")
    author = mk("carol")
    base = datetime.now(timezone.utc) - timedelta(hours=1)
    items = []
    for i in range(30):
        item = mkpost(db, author, f"saved {i}")
        db.table("saved_posts").insert({"post_id": item["id"], "user_id": me["id"], "created_at": (base + timedelta(minutes=i)).isoformat()}).execute()
        items.append(item["id"])
    first = me_c.get("/u/alice?tab=saved").get_data(as_text=True)
    cursor = re.search(r'data-more-cursor="([^"]+)"', first).group(1)
    page1 = set(re.findall(r'/post/([0-9a-f-]{36})', first)) & set(items)
    assert len(page1) == 18
    second = me_c.get("/u/alice", query_string={"tab": "saved", "before": cursor, "partial": 1}).get_data(as_text=True)
    page2 = set(re.findall(r'/post/([0-9a-f-]{36})', second)) & set(items)
    assert len(page2) == 12 and not page1 & page2 and "data-more-cursor" not in second
    assert me_c.get("/u/alice", query_string={"tab": "saved", "before": "junk", "partial": 1}).status_code == 400


def test_saved_tab_empty_state_and_unsave_roundtrip(as_user, db, mk):
    me_c, me = as_user("alice")
    html = me_c.get("/u/alice?tab=saved").get_data(as_text=True)
    assert "Nothing saved yet" in html and "data-saved-empty" in html
    assert re.search(r'id="load-more"[^>]*hidden', html)                  # nothing more to load
    p = mkphoto(db, mk("carol"))
    assert me_c.post(f"/posts/{p['id']}/save", headers=FETCH).get_json() == {"saved": True}
    assert f'data-save="{p["id"]}"' in me_c.get("/u/alice?tab=saved").get_data(as_text=True)
    assert me_c.post(f"/posts/{p['id']}/save", headers=FETCH).get_json() == {"saved": False}
    assert f"/post/{p['id']}" not in me_c.get("/u/alice?tab=saved").get_data(as_text=True)


def test_post_cards_have_a_bookmark_that_reflects_saved_state(as_user, db, mk):
    me_c, me = as_user("alice")
    p = mkpost(db, mk("carol"))
    assert 'aria-label="Save"' in me_c.get(f"/post/{p['id']}").get_data(as_text=True)
    save(db, me, p)
    assert 'aria-label="Remove from saved"' in me_c.get(f"/post/{p['id']}").get_data(as_text=True)
