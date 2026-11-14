"""Blocking and reporting from inside a chat (WhatsApp style)."""
from tests.test_chat_ticks import FETCH, open_dm, send


def page(client, cid):
    return client.get(f"/messages/{cid}").get_data(as_text=True)


def block(client, cid):
    return client.post(f"/messages/{cid}/block", data={"action": "block"})


def blocks(db, blocker, blocked):
    return [b for b in db.data.get("blocks", []) if b["blocker_id"] == blocker["id"] and b["blocked_id"] == blocked["id"]]


def test_chat_menu_has_report_and_block(as_user, db):
    a, pa = as_user("alice"); b, pb = as_user("bob")
    cid = open_dm(a, pa, pb, db)
    html = page(a, cid)
    assert "Report @bob" in html and "Block @bob" in html and "chat-report-dialog" in html


def test_blocking_from_chat_stops_messages_both_ways_but_keeps_history(as_user, db):
    a, pa = as_user("alice"); b, pb = as_user("bob")
    cid = open_dm(a, pa, pb, db)
    send(a, cid, "hi bob"); send(b, cid, "hi alice")
    db.table("follows").insert({"follower_id": pa["id"], "followee_id": pb["id"]}).execute()

    assert block(a, cid).status_code == 302
    assert blocks(db, pa, pb) and not db.data["follows"]                      # follows removed like a profile block

    mine = page(a, cid)
    assert "You blocked @bob" in mine and "Unblock" in mine and 'id="chat-form"' not in mine
    assert "hi bob" in mine and "hi alice" in mine                            # history stays
    sent = a.post(f"/messages/{cid}/send", data={"body": "x"}, headers=FETCH)
    assert sent.status_code == 403 and "You blocked" in sent.get_json()["error"]

    theirs = page(b, cid)                                                     # bob is not told he was blocked
    assert "You can&#39;t send messages in this chat" in theirs or "You can't send messages in this chat" in theirs
    assert "blocked" not in theirs.lower()
    refused = b.post(f"/messages/{cid}/send", data={"body": "hello?"}, headers=FETCH)
    assert refused.status_code == 403 and "blocked" not in refused.get_json()["error"].lower()


def test_unblock_restores_chat(as_user, db):
    a, pa = as_user("alice"); b, pb = as_user("bob")
    cid = open_dm(a, pa, pb, db)
    block(a, cid)
    assert a.post(f"/messages/{cid}/block", data={"action": "unblock"}).status_code == 302
    assert not blocks(db, pa, pb)
    assert 'id="chat-form"' in page(a, cid)
    assert a.post(f"/messages/{cid}/send", data={"body": "back"}, headers=FETCH).status_code == 200


def test_inbox_keeps_chats_you_blocked_and_hides_chats_that_blocked_you(as_user, db):
    a, pa = as_user("alice"); b, pb = as_user("bob")
    cid = open_dm(a, pa, pb, db)
    send(a, cid, "hello")
    block(a, cid)
    assert ">Blocked<" in a.get("/messages").get_data(as_text=True)           # alice can reopen it to unblock
    assert f"/messages/{cid}" not in b.get("/messages").get_data(as_text=True)   # bob's list hides it, no hint


def test_blocked_contact_never_sees_seen_ticks(as_user, db):
    a, pa = as_user("alice"); b, pb = as_user("bob")
    cid = open_dm(a, pa, pb, db)
    send(a, cid, "before the block")
    mid = db.data["messages"][-1]["id"]
    block(a, cid)
    b.get(f"/messages/{cid}")                                                  # bob opens the chat
    b.post("/messages/delivered", headers=FETCH)
    poll = a.get(f"/messages/{cid}/poll?ids={mid}", headers=FETCH).get_json()
    assert poll["ticks"][mid] == "sent"                                        # never 'delivered' or 'read'


def test_report_from_chat_files_profile_and_last_five_messages(as_user, db):
    a, pa = as_user("alice"); b, pb = as_user("bob")
    cid = open_dm(a, pa, pb, db)
    for i in range(7):
        send(b, cid, f"rude {i}")
    r = a.post(f"/messages/{cid}/report", data={"reason": "harassment", "details": "keeps insulting me"}, headers=FETCH)
    assert r.status_code == 200 and r.get_json()["blocked"] is False
    reports = db.data["reports"]
    assert sum(1 for x in reports if x["target_type"] == "profile" and x["target_id"] == pb["id"]) == 1
    assert sum(1 for x in reports if x["target_type"] == "message") == 5       # last five only
    assert all(x["reporter_id"] == pa["id"] and x["reason"] == "harassment" for x in reports)
    assert "keeps insulting me" in next(x for x in reports if x["target_type"] == "profile")["details"]
    assert not blocks(db, pa, pb)

    again = a.post(f"/messages/{cid}/report", data={"reason": "harassment"}, headers=FETCH)       # no duplicates
    assert again.status_code == 200 and len(db.data["reports"]) == 6


def test_report_with_also_block_blocks_and_bad_reason_becomes_other(as_user, db):
    a, pa = as_user("alice"); b, pb = as_user("bob")
    cid = open_dm(a, pa, pb, db)
    r = a.post(f"/messages/{cid}/report", data={"reason": "not-a-reason", "also_block": "1"}, headers=FETCH)
    assert r.get_json()["blocked"] is True and blocks(db, pa, pb)
    assert db.data["reports"][0]["reason"] == "other"


def test_only_members_can_block_or_report_and_not_in_groups(as_user, db):
    a, pa = as_user("alice"); b, pb = as_user("bob"); c, _ = as_user("carol")
    cid = open_dm(a, pa, pb, db)
    assert c.post(f"/messages/{cid}/block", data={"action": "block"}).status_code == 404
    assert c.post(f"/messages/{cid}/report", data={"reason": "spam"}, headers=FETCH).status_code == 404
    group = db.table("conversations").insert({"kind": "group", "title": "Study group"}).execute().data[0]
    db.table("conversation_members").insert({"conversation_id": group["id"], "user_id": pa["id"]}).execute()
    assert a.post(f"/messages/{group['id']}/block", data={"action": "block"}).status_code == 404


def test_profile_block_still_works_through_shared_helper(as_user, db):
    a, pa = as_user("alice"); b, pb = as_user("bob")
    a.post("/u/bob/block")
    assert blocks(db, pa, pb)
    a.post("/u/bob/block")                                                      # second tap unblocks (unchanged behaviour)
    assert not blocks(db, pa, pb)
