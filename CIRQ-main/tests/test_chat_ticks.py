"""Chat delivery ticks (sent / delivered / seen) and per-user disappearing messages."""
from datetime import timedelta

from app.utils import iso, utcnow

FETCH = {"X-Requested-With": "fetch"}


def open_dm(a_client, a_profile, b_profile, db):
    a_client.post(f"/messages/start/{b_profile['username']}")
    return next(c for c in db.data["conversations"] if c["dm_key"] == ":".join(sorted([a_profile["id"], b_profile["id"]])))["id"]


def send(client, cid, body="hello"):
    r = client.post(f"/messages/{cid}/send", data={"body": body}, headers=FETCH)
    assert r.status_code == 200, r.data
    return r.get_json()


def last_message(db):
    return db.data["messages"][-1]


def test_offline_recipient_gets_single_tick_then_double_then_blue(as_user, db):
    a, pa = as_user("alice"); b, pb = as_user("bob")
    cid = open_dm(a, pa, pb, db)

    out = send(a, cid)
    assert 'class="ticks sent"' in out["html"]                       # bob is offline: single tick
    mid = last_message(db)["id"]

    b.post("/messages/delivered", headers=FETCH)                      # bob's app checks in (open, not in this chat)
    poll = a.get(f"/messages/{cid}/poll?ids={mid}", headers=FETCH).get_json()
    assert poll["ticks"][mid] == "delivered"                          # grey double tick

    b.get(f"/messages/{cid}")                                          # bob opens the chat
    poll = a.get(f"/messages/{cid}/poll?ids={mid}", headers=FETCH).get_json()
    assert poll["ticks"][mid] == "read"                                # blue double tick
    assert 'class="ticks read"' in a.get(f"/messages/{cid}").get_data(as_text=True)


def test_online_recipient_gets_double_tick_immediately(as_user, db):
    a, pa = as_user("alice"); b, pb = as_user("bob")
    cid = open_dm(a, pa, pb, db)
    db.table("profiles").update({"last_seen_at": iso()}).eq("id", pb["id"]).execute()
    assert 'class="ticks delivered"' in send(a, cid)["html"]
    assert last_message(db)["delivered_at"]


def test_hidden_active_status_still_gets_delivery_from_check_in(as_user, db):
    a, pa = as_user("alice"); b, pb = as_user("bob")
    cid = open_dm(a, pa, pb, db)
    db.table("profiles").update({"show_active_status": False, "last_seen_at": iso()}).eq("id", pb["id"]).execute()
    assert 'class="ticks sent"' in send(a, cid)["html"]               # bob's online status is private, so no instant delivery
    b.post("/messages/delivered", headers=FETCH)
    assert last_message(db)["delivered_at"]


def test_other_peoples_messages_and_group_chats_show_no_ticks(as_user, db):
    a, pa = as_user("alice"); b, pb = as_user("bob")
    cid = open_dm(a, pa, pb, db)
    send(a, cid)
    assert 'class="ticks' not in b.get(f"/messages/{cid}").get_data(as_text=True)      # ticks are for the sender only


def test_disappearing_setting_is_validated_and_members_only(as_user, db):
    a, pa = as_user("alice"); b, pb = as_user("bob"); c, _ = as_user("carol")
    cid = open_dm(a, pa, pb, db)
    assert a.post(f"/messages/{cid}/disappearing", data={"seconds": "12345"}, headers=FETCH).status_code == 400
    assert a.post(f"/messages/{cid}/disappearing", data={"seconds": "abc"}, headers=FETCH).status_code == 400
    assert c.post(f"/messages/{cid}/disappearing", data={"seconds": "3600"}, headers=FETCH).status_code == 404
    ok = a.post(f"/messages/{cid}/disappearing", data={"seconds": "86400"}, headers=FETCH)
    assert ok.status_code == 200 and ok.get_json()["label"] == "24 hours"
    mine = next(m for m in db.data["conversation_members"] if m["conversation_id"] == cid and m["user_id"] == pa["id"])
    theirs = next(m for m in db.data["conversation_members"] if m["conversation_id"] == cid and m["user_id"] == pb["id"])
    assert mine["disappear_after"] == 86400 and not theirs.get("disappear_after")           # only for me
    a.post(f"/messages/{cid}/disappearing", data={"seconds": "0"}, headers=FETCH)
    assert not mine.get("disappear_after") and "Disappearing messages for you" in a.get(f"/messages/{cid}").get_data(as_text=True)


def test_messages_disappear_only_for_the_person_who_set_the_timer(as_user, db):
    a, pa = as_user("alice"); b, pb = as_user("bob")
    cid = open_dm(a, pa, pb, db)
    send(a, cid, "before the timer")
    a.post(f"/messages/{cid}/disappearing", data={"seconds": "3600"}, headers=FETCH)
    send(a, cid, "secret plan")

    hide = db.data["message_hides"]
    assert len(hide) == 1 and hide[0]["user_id"] == pa["id"]                                # only alice has a timer
    assert "secret plan" in a.get(f"/messages/{cid}").get_data(as_text=True)               # not expired yet

    hide[0]["hide_at"] = iso(utcnow() - timedelta(minutes=1))                               # an hour passes
    page_a = a.get(f"/messages/{cid}").get_data(as_text=True)
    assert "secret plan" not in page_a and "before the timer" in page_a                     # old messages untouched
    assert "secret plan" in b.get(f"/messages/{cid}").get_data(as_text=True)               # bob still has his copy
    assert any(m["body"] == "secret plan" for m in db.data["messages"])                     # not deleted: bob has no timer


def test_message_is_really_deleted_once_gone_for_everyone(as_user, db):
    a, pa = as_user("alice"); b, pb = as_user("bob")
    cid = open_dm(a, pa, pb, db)
    a.post(f"/messages/{cid}/disappearing", data={"seconds": "3600"}, headers=FETCH)
    b.post(f"/messages/{cid}/disappearing", data={"seconds": "86400"}, headers=FETCH)
    send(a, cid, "gone soon")
    assert len(db.data["message_hides"]) == 2
    for h in db.data["message_hides"]:
        h["hide_at"] = iso(utcnow() - timedelta(seconds=5))
    a.get(f"/messages/{cid}")                                                               # opening the chat cleans up
    assert not [m for m in db.data["messages"] if m["body"] == "gone soon"]
    assert not db.data["message_hides"]


def test_disappeared_message_leaves_the_inbox_preview(as_user, db):
    a, pa = as_user("alice"); b, pb = as_user("bob")
    cid = open_dm(a, pa, pb, db)
    a.post(f"/messages/{cid}/disappearing", data={"seconds": "3600"}, headers=FETCH)
    send(a, cid, "top secret preview")
    assert "top secret preview" in a.get("/messages").get_data(as_text=True)
    db.data["message_hides"][0]["hide_at"] = iso(utcnow() - timedelta(minutes=1))
    assert "top secret preview" not in a.get("/messages").get_data(as_text=True)
