"""'Who liked this' for posts, reels and comments: visibility, blocking, ordering, paging, and the markup that opens it."""
from datetime import timedelta

from app.utils import utcnow, iso
from tests.conftest import login


def mkpost(db, author, kind="post", **kw):
    return db.table("posts").insert({"author_id": author["id"], "kind": kind, "caption": "hi", "visibility": "public", **kw}).execute().data[0]


def like(db, post, user, minutes_ago=0):
    db.table("likes").insert({"post_id": post["id"], "user_id": user["id"], "created_at": iso(utcnow() - timedelta(minutes=minutes_ago))}).execute()


def comment(db, post, author, **kw):
    return db.table("comments").insert({"post_id": post["id"], "author_id": author["id"], "body": "nice", **kw}).execute().data[0]


def clike(db, c, user, minutes_ago=0):
    db.table("comment_likes").insert({"comment_id": c["id"], "user_id": user["id"], "created_at": iso(utcnow() - timedelta(minutes=minutes_ago))}).execute()


def test_post_likers_lists_people_newest_first(client, db, mk):
    me, a, b, c = mk("me"), mk("ann"), mk("bob"), mk("cat")
    p = mkpost(db, a)
    like(db, p, a, 30); like(db, p, b, 20); like(db, p, c, 5)
    login(client, "me")
    html = client.get(f"/posts/{p['id']}/likes").get_data(as_text=True)
    assert html.index("@cat") < html.index("@bob") < html.index("@ann")
    assert '/u/bob' in html and "No likes yet" not in html


def test_empty_state_and_your_own_like_is_marked(client, db, mk):
    me, a = mk("me"), mk("ann")
    p = mkpost(db, a)
    login(client, "me")
    assert "No likes yet" in client.get(f"/posts/{p['id']}/likes").get_data(as_text=True)
    like(db, p, me)
    assert '<span class="chip">You</span>' in client.get(f"/posts/{p['id']}/likes").get_data(as_text=True)


def test_reels_use_the_same_list(client, db, mk):
    mk("me"), mk("ann"), mk("bob")
    reel = mkpost(db, next(p for p in db.data["profiles"] if p["username"] == "ann"), kind="reel")
    like(db, reel, next(p for p in db.data["profiles"] if p["username"] == "bob"))
    login(client, "me")
    assert "@bob" in client.get(f"/posts/{reel['id']}/likes").get_data(as_text=True)


def test_blocked_people_are_left_out_both_ways(client, db, mk):
    me, a, b, c = mk("me"), mk("ann"), mk("bob"), mk("cat")
    p = mkpost(db, a)
    for u in (b, c):
        like(db, p, u)
    db.table("blocks").insert({"blocker_id": me["id"], "blocked_id": b["id"]}).execute()      # I blocked bob
    db.table("blocks").insert({"blocker_id": c["id"], "blocked_id": me["id"]}).execute()      # cat blocked me
    login(client, "me")
    html = client.get(f"/posts/{p['id']}/likes").get_data(as_text=True)
    assert "@bob" not in html and "@cat" not in html


def test_unapproved_accounts_never_appear(client, db, mk):
    mk("me"); a = mk("ann"); ghost = mk("ghost")
    next(p for p in db.data["profiles"] if p["username"] == "ghost")["account_status"] = "suspended"
    p = mkpost(db, a); like(db, p, ghost)
    login(client, "me")
    assert "@ghost" not in client.get(f"/posts/{p['id']}/likes").get_data(as_text=True)


def test_cannot_list_likers_of_a_post_you_cannot_see(client, db, mk):
    me, a, b = mk("me"), mk("ann"), mk("bob")
    next(x for x in db.data["profiles"] if x["username"] == "ann")["is_private"] = True       # private account, and I don't follow her
    p = mkpost(db, a); like(db, p, b)
    login(client, "me")
    assert client.get(f"/posts/{p['id']}/likes").status_code == 404
    assert client.get("/posts/00000000-0000-0000-0000-000000000000/likes").status_code == 404
    db.table("follows").insert({"follower_id": me["id"], "followee_id": a["id"]}).execute()
    assert client.get(f"/posts/{p['id']}/likes").status_code == 200


def test_signed_out_visitors_are_sent_to_login(client, db, mk):
    a = mk("ann"); p = mkpost(db, a)
    r = client.get(f"/posts/{p['id']}/likes")
    assert r.status_code in (302, 401)


def test_paging_20_at_a_time_with_a_working_cursor(client, db, mk):
    mk("me"); a = mk("ann"); p = mkpost(db, a)
    for i in range(25):
        like(db, p, mk(f"fan{i:02d}"), minutes_ago=100 - i)           # fan24 is the newest
    login(client, "me")
    first = client.get(f"/posts/{p['id']}/likes").get_data(as_text=True)
    assert first.count('class="person liker"') == 20 and "@fan24" in first and "@fan05" in first and "@fan04" not in first
    cursor = first.split('data-next="')[1].split('"')[0]
    second = client.get(f"/posts/{p['id']}/likes", query_string={"before": cursor}).get_data(as_text=True)
    assert second.count('class="person liker"') == 5 and "@fan04" in second and "@fan00" in second
    assert "data-next" not in second and "No likes yet" not in second                      # last page: no more cursor, no empty-state


def test_a_garbage_cursor_is_ignored_not_an_error(client, db, mk):
    mk("me"); a = mk("ann"); p = mkpost(db, a); like(db, p, a)
    login(client, "me")
    assert client.get(f"/posts/{p['id']}/likes", query_string={"before": "not-a-date"}).status_code == 200


def test_comment_likers_list_and_rules(client, db, mk):
    me, a, b = mk("me"), mk("ann"), mk("bob")
    p = mkpost(db, a); c = comment(db, p, a)
    clike(db, c, b, 5); clike(db, c, a, 20)
    login(client, "me")
    html = client.get(f"/comments/{c['id']}/likes").get_data(as_text=True)
    assert html.index("@bob") < html.index("@ann")
    removed = comment(db, p, a, moderation_status="removed")
    assert client.get(f"/comments/{removed['id']}/likes").status_code == 404
    db.table("blocks").insert({"blocker_id": me["id"], "blocked_id": a["id"]}).execute()     # blocked the comment's author
    assert client.get(f"/comments/{c['id']}/likes").status_code == 404


def test_comment_likers_follow_post_visibility(client, db, mk):
    mk("me"); a, b = mk("ann"), mk("bob")
    next(x for x in db.data["profiles"] if x["username"] == "ann")["is_private"] = True
    p = mkpost(db, a); c = comment(db, p, a); clike(db, c, b)
    login(client, "me")
    assert client.get(f"/comments/{c['id']}/likes").status_code == 404


def test_markup_opens_the_sheet_from_posts_reels_and_comments(app, client, db, mk):
    me, a = mk("me"), mk("ann")
    p = mkpost(db, a); like(db, p, a)
    c = comment(db, p, a); clike(db, c, me)
    reel = mkpost(db, a, kind="reel")
    db.table("post_media").insert({"post_id": reel["id"], "owner_id": a["id"], "media_type": "video", "url": "u", "public_id": "cirq/reels/x"}).execute()
    login(client, "me")
    feed = client.get("/feed").get_data(as_text=True)
    assert f'data-likers="/posts/{p["id"]}/likes"' in feed and 'id="likers-dialog"' in feed and "js/likers.js" in feed
    with app.test_request_context():
        from flask import url_for
        detail_url = url_for("feed.detail", post_id=p["id"])
    detail = client.get(detail_url).get_data(as_text=True)
    assert f'data-likers="/comments/{c["id"]}/likes"' in detail and f'data-clikes-of="{c["id"]}"' in detail
    reels = client.get("/reels").get_data(as_text=True)
    assert f'data-likers="/posts/{reel["id"]}/likes"' in reels
