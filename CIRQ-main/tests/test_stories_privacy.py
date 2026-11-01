"""Story likes / insights / replies-to-inbox, and private accounts with follow requests + approval-gated chat."""
from tests.conftest import MP4  # noqa: F401  (kept so this file imports the same helpers as its siblings)
from app.utils import iso, hours_from_now

FETCH = {"X-Requested-With": "fetch"}


def story(db, author, hours=24, **kw):
    row = {"author_id": author["id"], "media_type": "image", "url": "https://res.cloudinary.com/demo/image/upload/x.jpg",
           "public_id": "cirq/stories/x", "expires_at": iso(hours_from_now(hours)), **kw}
    return db.table("stories").insert(row).execute().data[0]


def follow(c, username):
    return c.post(f"/u/{username}/follow", headers=FETCH).get_json()


def make_private(c):
    c.post("/settings", data={"action": "privacy", "is_private": "on"})


# ------------------------------------------------------------------ story likes
def test_like_story_toggles_and_notifies_owner_once(as_user, db):
    a, pa = as_user("alice"); b, pb = as_user("bob")
    s = story(db, pa)
    assert b.post(f"/stories/{s['id']}/like", headers=FETCH).get_json() == {"liked": True}
    assert len(db.data["story_likes"]) == 1
    notes = [n for n in db.data["notifications"] if n["kind"] == "story_like" and n["user_id"] == pa["id"]]
    assert len(notes) == 1 and notes[0]["actor_id"] == pb["id"] and notes[0]["body"] == "liked your story"
    # unliking removes the like and withdraws the still-unread alert; liking again does not stack alerts
    assert b.post(f"/stories/{s['id']}/like", headers=FETCH).get_json() == {"liked": False}
    assert not db.data["story_likes"] and not [n for n in db.data["notifications"] if n["kind"] == "story_like"]
    b.post(f"/stories/{s['id']}/like", headers=FETCH)
    assert len([n for n in db.data["notifications"] if n["kind"] == "story_like"]) == 1


def test_cannot_like_own_story(as_user, db):
    a, pa = as_user("alice")
    s = story(db, pa)
    assert a.post(f"/stories/{s['id']}/like", headers=FETCH).status_code == 400
    assert not db.data.get("story_likes")


def test_story_like_respects_expiry_blocks_and_private_accounts(as_user, db):
    a, pa = as_user("alice"); b, pb = as_user("bob"); c, pc = as_user("carol")
    expired = story(db, pa, hours=-1)
    assert b.post(f"/stories/{expired['id']}/like", headers=FETCH).status_code == 404
    live = story(db, pa)
    c.post("/u/alice/follow"); c.post("/u/alice/block")                       # carol blocks alice
    assert c.post(f"/stories/{live['id']}/like", headers=FETCH).status_code == 404
    make_private(a)                                                           # bob is not an approved follower
    assert b.post(f"/stories/{live['id']}/like", headers=FETCH).status_code == 404
    assert not db.data.get("story_likes")


def test_story_api_shows_counts_only_to_the_owner(as_user, db):
    a, pa = as_user("alice"); b, pb = as_user("bob"); c, pc = as_user("carol")
    s = story(db, pa)
    for who in (b, c):
        who.post(f"/stories/{s['id']}/view"); who.post(f"/stories/{s['id']}/like", headers=FETCH)
    mine = a.get("/stories/api/alice").get_json()
    assert mine["me"] is True and mine["stories"][0]["likes"] == 2 and mine["stories"][0]["views"] == 2
    theirs = b.get("/stories/api/alice").get_json()
    assert theirs["me"] is False and theirs["stories"][0]["likes"] is None and theirs["stories"][0]["views"] is None
    assert theirs["stories"][0]["liked"] is True and theirs["can_reply"] is True


def test_insights_lists_who_liked_and_is_owner_only(as_user, db):
    a, pa = as_user("alice"); b, pb = as_user("bob"); c, pc = as_user("carol")
    s = story(db, pa)
    b.post(f"/stories/{s['id']}/view"); b.post(f"/stories/{s['id']}/like", headers=FETCH)
    c.post(f"/stories/{s['id']}/view")
    r = a.get(f"/stories/{s['id']}/insights").get_json()
    assert r["views"] == 2 and r["likes"] == 1
    assert [x["username"] for x in r["likers"]] == ["bob"]
    assert {x["username"]: x["liked"] for x in r["viewers"]} == {"bob": True, "carol": False}
    assert b.get(f"/stories/{s['id']}/insights", headers=FETCH).status_code == 403      # a viewer can't see the audience
    assert a.get("/stories/00000000-0000-0000-0000-000000000000/insights").status_code == 404


def test_insights_hide_people_the_owner_blocked(as_user, db):
    a, pa = as_user("alice"); b, pb = as_user("bob")
    s = story(db, pa)
    b.post(f"/stories/{s['id']}/view"); b.post(f"/stories/{s['id']}/like", headers=FETCH)
    a.post("/u/bob/block")
    r = a.get(f"/stories/{s['id']}/insights").get_json()
    assert r["likes"] == 0 and r["views"] == 0


# ------------------------------------------------------------------ story replies -> inbox
def test_story_reply_lands_in_owners_inbox(as_user, db):
    a, pa = as_user("alice"); b, pb = as_user("bob")
    s = story(db, pa)
    r = b.post(f"/stories/{s['id']}/reply", data={"body": "this is so cool"}, headers=FETCH)
    assert r.status_code == 200 and r.get_json()["ok"] is True
    msg = db.data["messages"][0]
    assert msg["body"] == "this is so cool" and msg["story_reply"] is True and msg["story_id"] == s["id"] and msg["sender_id"] == pb["id"]
    # one DM conversation, both people are members, the owner has an unread message alert (nav badge)
    assert len(db.data["conversations"]) == 1 and len(db.data["conversation_members"]) == 2
    assert [n for n in db.data["notifications"] if n["kind"] == "message" and n["user_id"] == pa["id"] and not n["read_at"]]
    inbox = a.get("/messages").get_data(as_text=True)
    assert "Replied to your story" in inbox and "this is so cool" in inbox and "Bob" in inbox
    thread = a.get(r.get_json()["conversation"]).get_data(as_text=True)
    assert "Replied to your story" in thread and "this is so cool" in thread


def test_story_reply_reuses_existing_chat(as_user, db):
    a, pa = as_user("alice"); b, pb = as_user("bob")
    cid = b.post("/messages/start/alice").headers["Location"].rsplit("/", 1)[1]
    s = story(db, pa)
    b.post(f"/stories/{s['id']}/reply", data={"body": "hi"}, headers=FETCH)
    b.post(f"/stories/{s['id']}/reply", data={"body": "again"}, headers=FETCH)
    assert len(db.data["conversations"]) == 1 and db.data["conversations"][0]["id"] == cid
    assert len(db.data["messages"]) == 2


def test_story_reply_validation_and_permissions(as_user, db):
    a, pa = as_user("alice"); b, pb = as_user("bob")
    s = story(db, pa)
    assert b.post(f"/stories/{s['id']}/reply", data={"body": "   "}, headers=FETCH).status_code == 400
    assert b.post(f"/stories/{s['id']}/reply", data={"body": "text me on +256 700 123 456"}, headers=FETCH).status_code == 400
    assert a.post(f"/stories/{s['id']}/reply", data={"body": "me"}, headers=FETCH).status_code == 400     # not your own story
    old = story(db, pa, hours=-2)
    assert b.post(f"/stories/{old['id']}/reply", data={"body": "late"}, headers=FETCH).status_code == 404
    a.post("/u/bob/block")
    assert b.post(f"/stories/{s['id']}/reply", data={"body": "hey"}, headers=FETCH).status_code == 404
    assert not db.data.get("messages")


def test_reply_to_expired_story_still_shows_label_in_chat(as_user, db):
    a, pa = as_user("alice"); b, pb = as_user("bob")
    s = story(db, pa)
    r = b.post(f"/stories/{s['id']}/reply", data={"body": "nice"}, headers=FETCH).get_json()
    db.data["stories"][0]["expires_at"] = iso(hours_from_now(-1))              # the 24h are up
    html = a.get(r["conversation"]).get_data(as_text=True)
    assert "Replied to your story" in html and "no longer available" in html and "nice" in html


# ------------------------------------------------------------------ private accounts + follow requests
def test_following_a_public_account_is_instant(as_user, db):
    a, pa = as_user("alice"); b, pb = as_user("bob")
    assert follow(b, "alice") == {"state": "following", "following": True, "requested": False, "followers": 1}
    assert not db.data.get("follow_requests")


def test_following_a_private_account_sends_a_request(as_user, db):
    a, pa = as_user("alice"); b, pb = as_user("bob")
    make_private(a)
    r = follow(b, "alice")
    assert r["state"] == "requested" and r["followers"] == 0
    assert not db.data.get("follows") and len(db.data["follow_requests"]) == 1
    n = [n for n in db.data["notifications"] if n["kind"] == "follow_request"]
    assert len(n) == 1 and n[0]["user_id"] == pa["id"] and n[0]["actor_id"] == pb["id"]
    assert not [x for x in db.data["notifications"] if x["kind"] == "follow"]            # no premature "started following you"


def test_requester_can_cancel_a_request(as_user, db):
    a, pa = as_user("alice"); b, pb = as_user("bob")
    make_private(a); follow(b, "alice")
    assert follow(b, "alice")["state"] == "none"
    assert not db.data["follow_requests"] and not [n for n in db.data["notifications"] if n["kind"] == "follow_request"]


def test_approve_creates_follow_and_notifies_requester(as_user, db):
    a, pa = as_user("alice"); b, pb = as_user("bob")
    make_private(a); follow(b, "alice")
    r = a.post("/follow-requests/bob/approve", headers=FETCH).get_json()
    assert r["ok"] and r["approved"] and r["remaining"] == 0 and r["followers"] == 1
    assert len(db.data["follows"]) == 1 and not db.data["follow_requests"]
    assert [n for n in db.data["notifications"] if n["kind"] == "follow_accepted" and n["user_id"] == pb["id"]]
    assert not [n for n in db.data["notifications"] if n["kind"] == "follow_request"]    # the pending alert is cleared
    assert {x["code"] for x in db.data["user_achievements"] if x["user_id"] == pb["id"]} >= {"first_follow"}
    # the button on bob's side now reads "Following"
    assert b"Following" in b.get("/u/alice").data


def test_decline_removes_request_without_following(as_user, db):
    a, pa = as_user("alice"); b, pb = as_user("bob")
    make_private(a); follow(b, "alice")
    assert a.post("/follow-requests/bob/decline", headers=FETCH).get_json()["approved"] is False
    assert not db.data.get("follows") and not db.data["follow_requests"]
    assert follow(b, "alice")["state"] == "requested"                                      # bob may ask again


def test_only_the_account_owner_can_resolve_a_request(as_user, db):
    a, pa = as_user("alice"); b, pb = as_user("bob"); c, pc = as_user("carol")
    make_private(a); follow(b, "alice")
    assert c.post("/follow-requests/bob/approve", headers=FETCH).status_code == 404       # carol has no such request
    assert b.post("/follow-requests/bob/approve", headers=FETCH).status_code == 404       # can't approve yourself
    assert a.post("/follow-requests/carol/approve", headers=FETCH).status_code == 404     # no request from carol
    assert not db.data.get("follows") and len(db.data["follow_requests"]) == 1


def test_private_account_content_stays_hidden_until_approved(as_user, db):
    a, pa = as_user("alice"); b, pb = as_user("bob")
    a.post("/posts", data={"caption": "secret thoughts"}); s = story(db, pa)
    make_private(a); follow(b, "alice")
    page = b.get("/u/alice").get_data(as_text=True)
    assert "secret thoughts" not in page and "This account is private" in page and "Requested" in page
    assert b.post(f"/stories/{s['id']}/view").status_code == 404
    a.post("/follow-requests/bob/approve", headers=FETCH)
    assert "secret thoughts" in b.get("/u/alice").get_data(as_text=True)
    assert b.post(f"/stories/{s['id']}/view").status_code == 200


def test_cannot_chat_with_private_account_until_approved(as_user, db):
    a, pa = as_user("alice"); b, pb = as_user("bob")
    make_private(a); follow(b, "alice")
    r = b.post("/messages/start/alice")
    assert r.status_code == 302 and r.headers["Location"].endswith("/u/alice")
    assert not db.data.get("conversations")
    assert "private account" in b.get("/u/alice").get_data(as_text=True)                  # explained, not silently refused
    a.post("/follow-requests/bob/approve", headers=FETCH)
    assert "/messages/" in b.post("/messages/start/alice").headers["Location"]
    assert len(db.data["conversations"]) == 1


def test_story_reply_blocked_for_unapproved_but_inbox_unaffected(as_user, db):
    a, pa = as_user("alice"); b, pb = as_user("bob")
    s = story(db, pa); make_private(a); follow(b, "alice")
    # a pending requester can't even open the story, so cannot reply
    assert b.post(f"/stories/{s['id']}/reply", data={"body": "hi"}, headers=FETCH).status_code == 404
    a.post("/follow-requests/bob/approve", headers=FETCH)
    assert b.post(f"/stories/{s['id']}/reply", data={"body": "hi"}, headers=FETCH).status_code == 200


def test_existing_chat_is_locked_when_account_goes_private(as_user, db):
    a, pa = as_user("alice"); b, pb = as_user("bob")
    cid = b.post("/messages/start/alice").headers["Location"].rsplit("/", 1)[1]
    assert b.post(f"/messages/{cid}/send", data={"body": "hello"}).status_code == 200
    make_private(a)                                                                        # bob never followed alice
    r = b.post(f"/messages/{cid}/send", data={"body": "still there?"})
    assert r.status_code == 403 and "private account" in r.get_json()["error"]
    assert b"approve your follow request" in b.get(f"/messages/{cid}").data and b'id="chat-form"' not in b.get(f"/messages/{cid}").data
    assert a.post(f"/messages/{cid}/send", data={"body": "hi bob"}).status_code == 200    # the owner can still write
    assert b'id="chat-form"' in a.get(f"/messages/{cid}").data


def test_private_owner_can_message_people_they_follow_and_vice_versa(as_user, db):
    a, pa = as_user("alice"); b, pb = as_user("bob")
    make_private(a)
    follow(a, "bob")                                                                       # alice follows bob (public)
    assert "/messages/" in b.post("/messages/start/alice").headers["Location"]             # bob can reach alice: she follows him


def test_going_public_approves_waiting_requests(as_user, db):
    a, pa = as_user("alice"); b, pb = as_user("bob"); c, pc = as_user("carol")
    make_private(a); follow(b, "alice"); follow(c, "alice")
    a.post("/settings", data={"action": "privacy"})                                        # untick = public
    assert db.data["profiles"][0]["is_private"] is False
    assert len(db.data["follows"]) == 2 and not db.data["follow_requests"]


def test_going_private_keeps_followers_and_locks_public_posts(as_user, db):
    a, pa = as_user("alice"); b, pb = as_user("bob")
    follow(b, "alice"); a.post("/posts", data={"caption": "hello"})
    make_private(a)
    assert db.data["posts"][0]["visibility"] == "followers" and len(db.data["follows"]) == 1
    assert "hello" in b.get("/u/alice").get_data(as_text=True)                              # existing follower still sees it


def test_requests_page_and_activity_row(as_user, db):
    a, pa = as_user("alice"); b, pb = as_user("bob"); c, pc = as_user("carol")
    make_private(a); follow(b, "alice"); follow(c, "alice")
    page = a.get("/follow-requests").get_data(as_text=True)
    assert "@bob" in page and "@carol" in page and "Confirm" in page
    act = a.get("/notifications").get_data(as_text=True)
    assert "Follow requests" in act and "2 people want to follow you" in act
    nid = next(n["id"] for n in db.data["notifications"] if n["kind"] == "follow_request")
    assert a.get(f"/notifications/{nid}/open").headers["Location"].endswith("/follow-requests")


def test_blocking_clears_pending_requests(as_user, db):
    a, pa = as_user("alice"); b, pb = as_user("bob")
    make_private(a); follow(b, "alice")
    a.post("/u/bob/block")
    assert not db.data["follow_requests"]
    assert 'data-request="bob"' not in a.get("/follow-requests").get_data(as_text=True)


def test_discover_and_home_show_requested_state(as_user, db):
    a, pa = as_user("alice"); b, pb = as_user("bob")
    make_private(a); follow(b, "alice")
    assert 'data-state="requested"' in b.get("/discover?tab=people").get_data(as_text=True)
    assert "alice" not in b.get("/feed").get_data(as_text=True).split("From your school")[-1]   # no second "Follow" prompt


def test_cannot_request_yourself_or_a_blocked_account(as_user, db):
    a, pa = as_user("alice"); b, pb = as_user("bob")
    make_private(a)
    assert a.post("/u/alice/follow", headers=FETCH).status_code == 400
    a.post("/u/bob/block")
    assert b.post("/u/alice/follow", headers=FETCH).status_code in (400, 404)
    assert not db.data.get("follow_requests")


def test_private_account_never_appears_in_suggestions(app, as_user, db):
    from app.services import recommender
    a, pa = as_user("alice"); b, pb = as_user("bob"); c, pc = as_user("carol")
    a.post("/posts", data={"caption": "#football match today"})
    c.post("/posts", data={"caption": "#football training tomorrow"})
    with app.app_context():
        before = {x["author_id"] for x in recommender.suggest(pb["id"], pb, n=5)}
    assert before == {pa["id"], pc["id"]}                    # sanity: while public, both are suggestable
    make_private(a)
    db.data["post_signals"] = []                             # forget impressions so the same posts can be offered again
    with app.app_context():
        after = {x["author_id"] for x in recommender.suggest(pb["id"], pb, n=5)}
    assert pa["id"] not in after and pc["id"] in after       # alice (now private) is never recommended to strangers
