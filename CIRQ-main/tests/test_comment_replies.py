"""Replying to comments: who replied, who they answered, notifications, paging and permissions."""
import re

from tests.test_comments_panel_and_saved import FETCH, add_comments, mkphoto


def comment(client, post_id, body, parent_id=None, view="panel"):
    data = {"body": body, "view": view}
    if parent_id:
        data["parent_id"] = parent_id
    return client.post(f"/posts/{post_id}/comments", data=data, headers=FETCH)


def row(db, cid):
    return next(c for c in db.data["comments"] if c["id"] == cid)


def test_reply_is_filed_under_the_comment_and_shows_who_replied_to_whom(as_user, db, mk):
    author = mk("carol"); post = mkphoto(db, author)
    a, pa = as_user("alice"); b, pb = as_user("bob")
    top = comment(a, post["id"], "great photo").get_json()["id"]

    r = comment(b, post["id"], "agreed!", parent_id=top)
    out = r.get_json()
    assert r.status_code == 200 and out["root"] == top and out["reply_count"] == 1
    saved = row(db, out["id"])
    assert saved["parent_id"] == top and saved["reply_to_user_id"] == pa["id"]
    assert row(db, top)["reply_count"] == 1
    assert "crow-reply" in out["html"] and "Replying to" in out["html"] and "@alice" in out["html"] and "bob" in out["html"]


def test_replying_to_a_reply_stays_one_level_deep_and_names_that_person(as_user, db, mk):
    author = mk("carol"); post = mkphoto(db, author)
    a, pa = as_user("alice"); b, pb = as_user("bob"); c, pc = as_user("dave")
    top = comment(a, post["id"], "top").get_json()["id"]
    reply = comment(b, post["id"], "first reply", parent_id=top).get_json()["id"]

    nested = comment(c, post["id"], "answering bob", parent_id=reply).get_json()
    assert nested["root"] == top                                           # filed under the top-level comment
    assert row(db, nested["id"])["reply_to_user_id"] == pb["id"]           # ...but it says it answers bob
    assert "@bob" in nested["html"] and row(db, top)["reply_count"] == 2


def test_the_person_replied_to_is_notified(as_user, db, mk):
    author = mk("carol"); post = mkphoto(db, author)
    a, pa = as_user("alice"); b, pb = as_user("bob")
    top = comment(a, post["id"], "hello").get_json()["id"]
    before = len(db.data["notifications"])
    comment(b, post["id"], "hi alice", parent_id=top)
    new = db.data["notifications"][before:]
    assert [(n["user_id"], n["kind"], n["actor_id"]) for n in new] == [(pa["id"], "reply", pb["id"])]   # only the replied-to person
    assert new[0]["body"] == "replied to your comment" and new[0]["ref_id"] == post["id"]

    n_before = len(db.data["notifications"])
    comment(a, post["id"], "replying to myself", parent_id=top)             # no notification for answering yourself
    assert len(db.data["notifications"]) == n_before

    page = a.get("/notifications").get_data(as_text=True)
    assert "replied to your comment" in page


def test_replying_needs_a_real_comment_on_the_same_post_that_you_can_reach(as_user, db, mk):
    author = mk("carol"); post = mkphoto(db, author); other = mkphoto(db, author)
    a, pa = as_user("alice"); b, pb = as_user("bob")
    top = comment(a, post["id"], "hello").get_json()["id"]
    assert comment(b, other["id"], "wrong post", parent_id=top).status_code == 404
    assert comment(b, post["id"], "ghost", parent_id="00000000-0000-0000-0000-000000000000").status_code == 404
    db.table("blocks").insert({"blocker_id": pa["id"], "blocked_id": pb["id"]}).execute()
    assert comment(b, post["id"], "i am blocked", parent_id=top).status_code == 403
    assert not any(c.get("parent_id") for c in db.data["comments"])


def test_comment_list_shows_only_top_level_with_reply_counts_and_loads_replies_on_demand(as_user, db, mk):
    author = mk("carol"); post = mkphoto(db, author)
    a, pa = as_user("alice"); b, pb = as_user("bob")
    top = comment(a, post["id"], "the thread").get_json()["id"]
    for i in range(13):
        comment(b, post["id"], f"reply number {i}", parent_id=top)

    page = a.get(f"/api/posts/{post['id']}/comments", headers=FETCH).get_json()["html"]
    assert page.count('class="cthread"') == 1 and "reply number" not in page          # replies are not in the main list
    assert "View 13 replies" in page and f'data-replies-for="{top}"' in page

    first = a.get(f"/api/comments/{top}/replies", headers=FETCH).get_json()
    ids = re.findall(r'data-comment="([0-9a-f-]{36})"', first["html"])
    assert len(ids) == 10 and first["has_more"] is True and first["count"] == 13
    assert first["html"].index("reply number 0") < first["html"].index("reply number 9")       # oldest first
    second = a.get(f"/api/comments/{top}/replies?after={first['next_cursor']}", headers=FETCH).get_json()
    more = re.findall(r'data-comment="([0-9a-f-]{36})"', second["html"])
    assert len(more) == 3 and second["has_more"] is False and not set(ids) & set(more)       # no repeats, nothing missing


def test_replies_from_blocked_people_are_hidden_and_replies_follow_post_visibility(as_user, db, mk):
    author = mk("carol"); post = mkphoto(db, author)
    a, pa = as_user("alice"); b, pb = as_user("bob")
    top = comment(a, post["id"], "thread").get_json()["id"]
    comment(b, post["id"], "from bob", parent_id=top)
    assert "from bob" in a.get(f"/api/comments/{top}/replies", headers=FETCH).get_json()["html"]
    db.table("blocks").insert({"blocker_id": pa["id"], "blocked_id": pb["id"]}).execute()
    assert "from bob" not in a.get(f"/api/comments/{top}/replies", headers=FETCH).get_json()["html"]

    reply_id = next(c["id"] for c in db.data["comments"] if c.get("parent_id"))
    assert a.get(f"/api/comments/{reply_id}/replies", headers=FETCH).status_code == 404       # replies can't have replies of their own list
    assert a.get("/api/comments/00000000-0000-0000-0000-000000000000/replies", headers=FETCH).status_code == 404


def test_deleting_a_reply_updates_the_count_and_deleting_the_comment_takes_replies_with_it(as_user, db, mk):
    author = mk("carol"); post = mkphoto(db, author)
    a, pa = as_user("alice"); b, pb = as_user("bob")
    top = comment(a, post["id"], "thread").get_json()["id"]
    r1 = comment(b, post["id"], "one", parent_id=top).get_json()["id"]
    comment(b, post["id"], "two", parent_id=top)
    assert row(db, top)["reply_count"] == 2

    gone = b.post(f"/comments/{r1}/delete", headers=FETCH).get_json()
    assert gone["root"] == top and gone["reply_count"] == 1 and row(db, top)["reply_count"] == 1
    assert a.post(f"/comments/{r1}/delete", headers=FETCH).status_code == 404                    # already gone

    plain = a.post(f"/comments/{top}/delete", headers=FETCH).get_json()
    assert "root" not in plain                                                                    # a top-level delete reports like before


def test_post_page_shows_replies_nested_with_who_they_answer(as_user, db, mk):
    author = mk("carol"); post = mkphoto(db, author)
    a, pa = as_user("alice"); b, pb = as_user("bob")
    top = comment(a, post["id"], "the first comment").get_json()["id"]
    comment(b, post["id"], "bob answers alice", parent_id=top)
    html = a.get(f"/post/{post['id']}").get_data(as_text=True)
    assert f'data-creplies-for="{top}"' in html and "Replying to" in html and "bob answers alice" in html
    assert 'data-reply-to="' in html and 'name="parent_id"' in html and "data-reply-chip" in html
    inside = html.split(f'data-creplies-for="{top}"')[1].split("</div>\n")[0]
    assert "bob answers alice" in inside                                                          # nested under its comment


def test_panel_markup_has_reply_controls(as_user, db, mk):
    author = mk("carol"); post = mkphoto(db, author)
    a, _ = as_user("alice")
    comment(a, post["id"], "hello")
    page = a.get(f"/api/posts/{post['id']}/comments", headers=FETCH).get_json()["html"]
    assert 'data-comment-reply="' in page and 'data-replies-toggle="' in page
    assert "data-cpanel-replying" in a.get("/feed").get_data(as_text=True)


def test_plain_comments_still_work_exactly_as_before(as_user, db, mk):
    author = mk("carol"); post = mkphoto(db, author)
    a, pa = as_user("alice")
    out = comment(a, post["id"], "just a comment").get_json()
    assert set(out) == {"html", "id", "count"}                                   # no reply fields for normal comments
    saved = row(db, out["id"])
    assert not saved.get("parent_id") and not saved.get("reply_to_user_id")
    assert any(n["kind"] == "comment" and n["user_id"] == author["id"] for n in db.data["notifications"])
