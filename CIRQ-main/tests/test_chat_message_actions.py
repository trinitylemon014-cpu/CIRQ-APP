"""Chat message actions, WhatsApp / Instagram style: reply to a message, edit a sent message, delete for me / for everyone,
and delete a whole chat (for me only)."""
import io
import re
from datetime import timedelta

import pytest
from postgrest.exceptions import APIError

from app.blueprints.messages import routes as msg_routes
from app.utils import iso, utcnow
from tests.test_chat_attachments import install_storage, send_file

FETCH = {"X-Requested-With": "fetch"}


# ------------------------------------------------------------------ helpers
def open_dm(a_client, pa, pb, db):
    a_client.post(f"/messages/start/{pb['username']}")
    return next(c for c in db.data["conversations"] if c["dm_key"] == ":".join(sorted([pa["id"], pb["id"]])))["id"]


def send(client, cid, body="hello", **extra):
    r = client.post(f"/messages/{cid}/send", data={"body": body, **extra}, headers=FETCH)
    assert r.status_code == 200, r.get_data(as_text=True)
    return r.get_json()


def last(db):
    return db.data["messages"][-1]


def row(db, mid):
    return next(m for m in db.data["messages"] if m["id"] == mid)


def act(client, cid, mid, action, **data):
    return client.post(f"/messages/{cid}/messages/{mid}/{action}", data=data, headers=FETCH)


def age(db, mid, **delta):
    row(db, mid)["created_at"] = iso(utcnow() - timedelta(**delta))


def thread(client, cid):
    return client.get(f"/messages/{cid}").get_data(as_text=True)


def pair(as_user, db):
    a, pa = as_user("alice")
    b, pb = as_user("bob")
    return a, pa, b, pb, open_dm(a, pa, pb, db)


def group(db, *profiles, title="Study group"):
    conv = db.table("conversations").insert({"kind": "group", "title": title}).execute().data[0]
    for p in profiles:
        db.table("conversation_members").insert({"conversation_id": conv["id"], "user_id": p["id"]}).execute()
    return conv["id"]


# ================================================================== REPLY
def test_reply_is_saved_and_the_quote_shows_who_and_what(as_user, db):
    a, pa, b, pb, cid = pair(as_user, db)
    send(a, cid, "Are we meeting at 4?")
    original = last(db)["id"]
    out = send(b, cid, "Yes, see you there", reply_to_id=original)
    assert last(db)["reply_to_id"] == original
    assert 'class="msg-quote"' in out["html"] and "Are we meeting at 4?" in out["html"] and f'data-quote-to="{original}"' in out["html"]
    assert "<b>alice</b>" in out["html"]                                  # bob sees who he replied to
    page = thread(a, cid)                                                 # and alice sees bob's reply quoting her own message as "You"
    assert "<b>You</b>" in page and "Are we meeting at 4?" in page


def test_replying_to_my_own_message_says_you(as_user, db):
    a, pa, b, pb, cid = pair(as_user, db)
    send(a, cid, "first")
    out = send(a, cid, "second", reply_to_id=last(db)["id"])
    assert "<b>You</b>" in out["html"]


def test_reply_to_a_message_from_another_chat_is_refused(as_user, db, mk):
    a, pa, b, pb, cid = pair(as_user, db)
    carol = mk("carol")
    other = db.table("conversations").insert({"kind": "dm", "dm_key": ":".join(sorted([pa["id"], carol["id"]]))}).execute().data[0]
    db.table("conversation_members").insert({"conversation_id": other["id"], "user_id": pa["id"]}).execute()
    foreign = db.table("messages").insert({"conversation_id": other["id"], "sender_id": pa["id"], "body": "private"}).execute().data[0]
    r = a.post(f"/messages/{cid}/send", data={"body": "sneaky", "reply_to_id": foreign["id"]}, headers=FETCH)
    assert r.status_code == 400 and "no longer available" in r.get_json()["error"]
    assert not any(m.get("body") == "sneaky" for m in db.data["messages"])             # nothing was sent


@pytest.mark.parametrize("bad", ["not-a-uuid", "00000000-0000-0000-0000-000000000000"])
def test_reply_to_a_junk_or_missing_id_is_refused(as_user, db, bad):
    a, pa, b, pb, cid = pair(as_user, db)
    assert a.post(f"/messages/{cid}/send", data={"body": "x", "reply_to_id": bad}, headers=FETCH).status_code == 400


def test_cannot_reply_to_a_deleted_or_hidden_message(as_user, db):
    a, pa, b, pb, cid = pair(as_user, db)
    send(a, cid, "to be deleted"); gone = last(db)["id"]
    send(a, cid, "to be hidden"); hidden = last(db)["id"]
    act(a, cid, gone, "delete", scope="everyone")
    act(b, cid, hidden, "delete", scope="me")
    assert b.post(f"/messages/{cid}/send", data={"body": "r1", "reply_to_id": gone}, headers=FETCH).status_code == 400
    assert b.post(f"/messages/{cid}/send", data={"body": "r2", "reply_to_id": hidden}, headers=FETCH).status_code == 400


def test_quote_follows_the_original_when_it_is_deleted_for_everyone(as_user, db):
    a, pa, b, pb, cid = pair(as_user, db)
    send(a, cid, "secret plan"); original = last(db)["id"]
    send(b, cid, "ok!", reply_to_id=original)
    assert "secret plan" in thread(b, cid)
    act(a, cid, original, "delete", scope="everyone")
    page = thread(b, cid)
    assert "secret plan" not in page and 'class="msg-quote gone"' in page and "This message was deleted" in page


def test_a_reply_whose_original_was_purged_shows_a_neutral_quote(as_user, db):
    a, pa, b, pb, cid = pair(as_user, db)
    send(a, cid, "old"); original = last(db)["id"]
    send(b, cid, "reply", reply_to_id=original)
    db.data["messages"].remove(row(db, original))                          # e.g. removed by a disappearing-message timer
    page = thread(b, cid)
    assert "Original message unavailable" in page and "reply" in page


def test_reply_works_with_attachments_too(as_user, db, monkeypatch, png):
    install_storage(monkeypatch, db)
    a, pa, b, pb, cid = pair(as_user, db)
    send(a, cid, "look at this"); original = last(db)["id"]
    r = b.post(f"/api/attachments/conversations/{cid}/messages", headers=FETCH, content_type="multipart/form-data",
               data={"message_id": "6f1c1c2e-0000-4000-8000-000000000001", "body": "nice", "reply_to_id": original,
                     "attachments": (io.BytesIO(png), "p.png", "image/png")})
    assert r.status_code == 200 and "look at this" in r.get_json()["html"]
    assert last(db)["reply_to_id"] == original


# ================================================================== EDIT
def test_sender_can_edit_and_everyone_sees_the_edited_label(as_user, db):
    a, pa, b, pb, cid = pair(as_user, db)
    send(a, cid, "see you at 5"); mid = last(db)["id"]
    r = act(a, cid, mid, "edit", body="see you at 6")
    assert r.status_code == 200 and "see you at 6" in r.get_json()["html"] and "edited" in r.get_json()["html"]
    assert row(db, mid)["body"] == "see you at 6" and row(db, mid)["edited_at"]
    page = thread(b, cid)
    assert "see you at 6" in page and "see you at 5" not in page and 'class="edited"' in page


def test_every_earlier_version_is_kept_for_moderators(as_user, db):
    a, pa, b, pb, cid = pair(as_user, db)
    send(a, cid, "version one"); mid = last(db)["id"]
    act(a, cid, mid, "edit", body="version two")
    act(a, cid, mid, "edit", body="version three")
    assert [e["old_body"] for e in db.data["message_edits"]] == ["version one", "version two"]
    assert all(e["editor_id"] == pa["id"] and e["message_id"] == mid for e in db.data["message_edits"])


def test_saving_identical_text_is_not_an_edit(as_user, db):
    a, pa, b, pb, cid = pair(as_user, db)
    send(a, cid, "same"); mid = last(db)["id"]
    assert act(a, cid, mid, "edit", body="same").status_code == 200
    assert not row(db, mid).get("edited_at") and not db.data.get("message_edits")


def test_only_the_sender_can_edit(as_user, db):
    a, pa, b, pb, cid = pair(as_user, db)
    send(a, cid, "mine"); mid = last(db)["id"]
    r = act(b, cid, mid, "edit", body="hijacked")
    assert r.status_code == 403 and "your own" in r.get_json()["error"]
    assert row(db, mid)["body"] == "mine"


def test_outsiders_get_404_not_a_hint_the_message_exists(as_user, db, mk):
    a, pa, b, pb, cid = pair(as_user, db)
    c, pc = as_user("carol")
    send(a, cid, "private"); mid = last(db)["id"]
    for action, data in (("edit", {"body": "x"}), ("delete", {"scope": "me"}), ("delete", {"scope": "everyone"})):
        assert act(c, cid, mid, action, **data).status_code == 404
    assert row(db, mid)["body"] == "private"


def test_edit_window_closes(as_user, db, app):
    app.config["MESSAGE_EDIT_MINUTES"] = 15
    a, pa, b, pb, cid = pair(as_user, db)
    send(a, cid, "too late"); mid = last(db)["id"]
    age(db, mid, minutes=20)
    r = act(a, cid, mid, "edit", body="changed")
    assert r.status_code == 403 and "15 minutes" in r.get_json()["error"]
    assert row(db, mid)["body"] == "too late"
    app.config["MESSAGE_EDIT_MINUTES"] = 0                                    # 0 = no limit
    assert act(a, cid, mid, "edit", body="changed").status_code == 200


def test_edit_button_is_only_offered_inside_the_window_to_the_sender(as_user, db, app):
    app.config["MESSAGE_EDIT_MINUTES"] = 15
    a, pa, b, pb, cid = pair(as_user, db)
    send(a, cid, "fresh"); fresh = last(db)["id"]
    send(a, cid, "stale"); stale = last(db)["id"]
    age(db, stale, minutes=30)
    mine = thread(a, cid)
    assert re.search(rf'data-id="{fresh}"[^>]*data-can-edit="1"', mine) and not re.search(rf'data-id="{stale}"[^>]*data-can-edit="1"', mine)
    assert 'data-can-edit="1"' not in thread(b, cid)                           # the other person is never offered Edit


def test_empty_and_blocked_text_cannot_be_saved_as_an_edit(as_user, db):
    a, pa, b, pb, cid = pair(as_user, db)
    send(a, cid, "ok text"); mid = last(db)["id"]
    assert act(a, cid, mid, "edit", body="   ").status_code == 400
    assert act(a, cid, mid, "edit", body="call me on 0772 123 456").status_code == 400      # same moderation as sending
    assert row(db, mid)["body"] == "ok text" and not db.data.get("message_edits")


def test_photos_files_and_story_replies_cannot_be_edited(as_user, db, monkeypatch, png):
    install_storage(monkeypatch, db)
    a, pa, b, pb, cid = pair(as_user, db)
    send_file(a, cid, "p.png", png, "image/png", body="caption"); with_file = last(db)["id"]
    r = act(a, cid, with_file, "edit", body="x")
    assert r.status_code == 403 and "Only text messages" in r.get_json()["error"]
    send(a, cid, "re: story"); sr = last(db)
    sr["story_reply"] = True
    assert act(a, cid, sr["id"], "edit", body="x").status_code == 403


def test_a_deleted_message_cannot_be_edited(as_user, db):
    a, pa, b, pb, cid = pair(as_user, db)
    send(a, cid, "bye"); mid = last(db)["id"]
    act(a, cid, mid, "delete", scope="everyone")
    assert act(a, cid, mid, "edit", body="back").status_code == 403


def test_an_edit_reaches_the_other_persons_open_chat_through_the_poll(as_user, db):
    a, pa, b, pb, cid = pair(as_user, db)
    send(a, cid, "draft"); mid = last(db)["id"]
    start = b.get(f"/messages/{cid}/poll", headers=FETCH).get_json()["now"]
    act(a, cid, mid, "edit", body="final")
    r = b.get(f"/messages/{cid}/poll", query_string={"upd": start}, headers=FETCH).get_json()
    assert [u["id"] for u in r["updates"]] == [mid] and "final" in r["updates"][0]["html"] and "edited" in r["updates"][0]["html"]
    again = b.get(f"/messages/{cid}/poll", query_string={"upd": r["now"]}, headers=FETCH).get_json()
    assert again["updates"] == []                                                # reported once, not on every poll


# ================================================================== DELETE FOR ME
def test_delete_for_me_hides_it_only_from_me(as_user, db):
    a, pa, b, pb, cid = pair(as_user, db)
    send(a, cid, "awkward"); mid = last(db)["id"]
    r = act(b, cid, mid, "delete", scope="me")
    assert r.status_code == 200 and r.get_json()["scope"] == "me"
    assert "awkward" not in thread(b, cid) and "awkward" in thread(a, cid)
    assert row(db, mid)["body"] == "awkward" and not row(db, mid).get("deleted_at")        # not deleted for the other person


def test_the_sender_can_also_delete_for_me_only(as_user, db):
    a, pa, b, pb, cid = pair(as_user, db)
    send(a, cid, "mine"); mid = last(db)["id"]
    act(a, cid, mid, "delete", scope="me")
    assert "mine" not in thread(a, cid) and "mine" in thread(b, cid)


def test_delete_for_me_is_idempotent_and_replaces_a_disappearing_timer(as_user, db):
    a, pa, b, pb, cid = pair(as_user, db)
    send(a, cid, "x"); mid = last(db)["id"]
    db.table("message_hides").insert({"message_id": mid, "user_id": pb["id"], "conversation_id": cid, "hide_at": iso(utcnow() + timedelta(days=7))}).execute()
    assert act(b, cid, mid, "delete", scope="me").status_code == 200
    assert act(b, cid, mid, "delete", scope="me").status_code == 200
    assert len([h for h in db.data["message_hides"] if h["message_id"] == mid and h["user_id"] == pb["id"]]) == 1
    assert "x" not in re.findall(r'class="msg-text">([^<]*)', thread(b, cid))


def test_deleted_for_me_stays_hidden_for_live_polls(as_user, db):
    a, pa, b, pb, cid = pair(as_user, db)
    send(a, cid, "hide me"); mid = last(db)["id"]
    act(b, cid, mid, "delete", scope="me")
    assert b.get(f"/messages/{cid}/poll", headers=FETCH).get_json()["html"] == ""


def test_when_both_delete_for_me_the_message_is_finally_removed_unless_reported(as_user, db):
    a, pa, b, pb, cid = pair(as_user, db)
    send(a, cid, "gone for both"); plain = last(db)["id"]
    send(a, cid, "evidence"); reported = last(db)["id"]
    db.table("reports").insert({"reporter_id": pb["id"], "target_type": "message", "target_id": reported, "reason": "harassment"}).execute()
    for mid in (plain, reported):
        act(a, cid, mid, "delete", scope="me"); act(b, cid, mid, "delete", scope="me")
    thread(a, cid)                                                              # opening a chat runs the purge
    ids = {m["id"] for m in db.data["messages"]}
    assert plain not in ids and reported in ids                                 # a reported message is never destroyed


def test_bad_scope_and_bad_ids_are_rejected(as_user, db):
    a, pa, b, pb, cid = pair(as_user, db)
    send(a, cid, "x"); mid = last(db)["id"]
    assert act(a, cid, mid, "delete", scope="nope").status_code == 400
    assert act(a, cid, mid, "delete").status_code == 400
    assert act(a, cid, "junk", "delete", scope="me").status_code == 404
    assert act(a, cid, "00000000-0000-0000-0000-000000000000", "delete", scope="me").status_code == 404


def test_a_message_from_another_chat_cannot_be_touched_through_this_one(as_user, db, mk):
    a, pa, b, pb, cid = pair(as_user, db)
    carol = mk("carol")
    other = db.table("conversations").insert({"kind": "dm", "dm_key": ":".join(sorted([pa["id"], carol["id"]]))}).execute().data[0]
    db.table("conversation_members").insert({"conversation_id": other["id"], "user_id": pa["id"]}).execute()
    foreign = db.table("messages").insert({"conversation_id": other["id"], "sender_id": pa["id"], "body": "elsewhere"}).execute().data[0]
    assert act(a, cid, foreign["id"], "delete", scope="everyone").status_code == 404
    assert not row(db, foreign["id"]).get("deleted_at")


# ================================================================== DELETE FOR EVERYONE
def test_sender_deletes_for_everyone_and_both_see_a_tombstone(as_user, db):
    a, pa, b, pb, cid = pair(as_user, db)
    send(a, cid, "oops wrong chat"); mid = last(db)["id"]
    r = act(a, cid, mid, "delete", scope="everyone")
    assert r.status_code == 200 and "You deleted this message" in r.get_json()["html"]
    m = row(db, mid)
    assert m["deleted_at"] and m["deleted_by"] == pa["id"] and m["body"] == ""              # the content is really gone
    assert "oops wrong chat" not in thread(a, cid) and "oops wrong chat" not in thread(b, cid)
    assert "You deleted this message" in thread(a, cid) and "This message was deleted" in thread(b, cid)
    assert 'data-can-edit' not in thread(a, cid).split("You deleted this message")[0].rsplit('<div class="msg', 1)[-1]


def test_receiver_cannot_delete_for_everyone(as_user, db):
    a, pa, b, pb, cid = pair(as_user, db)
    send(a, cid, "stay"); mid = last(db)["id"]
    r = act(b, cid, mid, "delete", scope="everyone")
    assert r.status_code == 403 and "Only the person who sent" in r.get_json()["error"]
    assert not row(db, mid).get("deleted_at") and row(db, mid)["body"] == "stay"


def test_nobody_but_the_sender_can_unsend_in_a_group_either(as_user, db):
    a, pa = as_user("alice"); b, pb = as_user("bob"); c, pc = as_user("carol")
    cid = group(db, pa, pb, pc)
    send(a, cid, "group message"); mid = last(db)["id"]
    assert act(b, cid, mid, "delete", scope="everyone").status_code == 403
    assert act(a, cid, mid, "delete", scope="everyone").status_code == 200
    for client in (b, c):
        assert "This message was deleted" in thread(client, cid) and "group message" not in thread(client, cid)


def test_unsend_window(as_user, db, app):
    app.config["MESSAGE_UNSEND_HOURS"] = 48
    a, pa, b, pb, cid = pair(as_user, db)
    send(a, cid, "old"); mid = last(db)["id"]
    age(db, mid, hours=50)
    r = act(a, cid, mid, "delete", scope="everyone")
    assert r.status_code == 403 and "48 hours" in r.get_json()["error"] and "for yourself" in r.get_json()["error"]
    assert row(db, mid)["body"] == "old"
    assert act(a, cid, mid, "delete", scope="me").status_code == 200           # deleting for myself is always allowed
    app.config["MESSAGE_UNSEND_HOURS"] = 0
    assert act(a, cid, mid, "delete", scope="everyone").status_code == 200


def test_unsend_is_idempotent(as_user, db):
    a, pa, b, pb, cid = pair(as_user, db)
    send(a, cid, "x"); mid = last(db)["id"]
    first = act(a, cid, mid, "delete", scope="everyone")
    stamp = row(db, mid)["deleted_at"]
    second = act(a, cid, mid, "delete", scope="everyone")
    assert first.status_code == second.status_code == 200 and row(db, mid)["deleted_at"] == stamp


def test_unsend_removes_uploaded_files_and_attachments(as_user, db, monkeypatch, png, uploads):
    storage = install_storage(monkeypatch, db)
    a, pa, b, pb, cid = pair(as_user, db)
    send_file(a, cid, "p.png", png, "image/png", body="a photo"); mid = last(db)["id"]
    paths = [x["storage_path"] for x in db.data["message_attachments"] if x["message_id"] == mid]
    assert paths
    act(a, cid, mid, "delete", scope="everyone")
    assert set(paths) <= set(storage["removed"])                                 # files removed from storage
    assert not [x for x in db.data["message_attachments"] if x["message_id"] == mid]
    assert row(db, mid)["attachment_count"] == 0 and row(db, mid)["body"] == ""
    assert "a photo" not in thread(b, cid) and "attachments_api" not in thread(b, cid).split("This message was deleted")[0][-400:]


def test_a_reported_message_is_kept_for_moderators_even_after_unsend(as_user, db):
    a, pa, b, pb, cid = pair(as_user, db)
    send(a, cid, "something nasty"); mid = last(db)["id"]
    b.post(f"/messages/{cid}/report", data={"reason": "harassment"}, headers=FETCH)     # files the last messages as evidence
    assert any(r["target_id"] == mid for r in db.data["reports"])
    act(a, cid, mid, "delete", scope="everyone")
    assert row(db, mid)["body"] == "something nasty" and row(db, mid)["deleted_at"]       # kept, and the sender isn't told why
    assert "something nasty" not in thread(b, cid) and "something nasty" not in thread(a, cid)   # but nobody in the chat sees it


def test_moderators_see_the_edit_history_and_the_sender_deleted_note(as_user, db):
    a, pa, b, pb, cid = pair(as_user, db)
    admin, padmin = as_user("modadmin")
    db.table("profiles").update({"role": "admin"}).eq("id", padmin["id"]).execute()
    send(a, cid, "original nasty words"); mid = last(db)["id"]
    act(a, cid, mid, "edit", body="harmless words")
    b.post(f"/messages/{cid}/report", data={"reason": "harassment"}, headers=FETCH)
    act(a, cid, mid, "delete", scope="everyone")
    page = admin.get("/admin/reports").get_data(as_text=True)
    assert "original nasty words" in page and "Edited 1 time" in page and "Deleted by sender" in page


def test_deleting_for_everyone_is_pushed_to_the_other_persons_open_chat(as_user, db):
    a, pa, b, pb, cid = pair(as_user, db)
    send(a, cid, "retract"); mid = last(db)["id"]
    start = b.get(f"/messages/{cid}/poll", headers=FETCH).get_json()["now"]
    act(a, cid, mid, "delete", scope="everyone")
    u = b.get(f"/messages/{cid}/poll", query_string={"upd": start}, headers=FETCH).get_json()["updates"]
    assert [x["id"] for x in u] == [mid] and "This message was deleted" in u[0]["html"] and "retract" not in u[0]["html"]


def test_inbox_shows_the_tombstone_and_does_not_count_it_as_unread(as_user, db):
    a, pa, b, pb, cid = pair(as_user, db)
    send(a, cid, "will vanish"); mid = last(db)["id"]
    assert 'class="convo unread"' in b.get("/messages").get_data(as_text=True)
    act(a, cid, mid, "delete", scope="everyone")
    inbox = b.get("/messages").get_data(as_text=True)
    assert "This message was deleted" in inbox and "will vanish" not in inbox and 'class="convo unread"' not in inbox
    assert "You deleted this message" in a.get("/messages").get_data(as_text=True)


# ================================================================== DELETE CHAT
def test_delete_chat_clears_my_side_only(as_user, db):
    a, pa, b, pb, cid = pair(as_user, db)
    send(a, cid, "hello bob"); send(b, cid, "hello alice")
    r = a.post(f"/messages/{cid}/clear", follow_redirects=False)
    assert r.status_code == 302 and r.headers["Location"].endswith("/messages")
    assert "hello bob" not in thread(a, cid) and "hello alice" not in thread(a, cid)
    assert "hello bob" in thread(b, cid) and "hello alice" in thread(b, cid)         # the other person keeps everything
    assert len(db.data["messages"]) == 2                                              # nothing was destroyed


def test_a_deleted_chat_leaves_my_list_but_not_theirs(as_user, db):
    a, pa, b, pb, cid = pair(as_user, db)
    send(b, cid, "ping")
    a.post(f"/messages/{cid}/clear")
    assert f"/messages/{cid}" not in a.get("/messages").get_data(as_text=True)
    assert f"/messages/{cid}" in b.get("/messages").get_data(as_text=True)


def test_a_new_message_brings_the_chat_back_starting_empty(as_user, db):
    a, pa, b, pb, cid = pair(as_user, db)
    send(a, cid, "old one"); send(b, cid, "old two")
    a.post(f"/messages/{cid}/clear")
    for m in db.data["messages"]:                                                      # make sure the new message is clearly later
        m["created_at"] = iso(utcnow() - timedelta(minutes=5))
    send(b, cid, "brand new")
    inbox = a.get("/messages").get_data(as_text=True)
    assert f"/messages/{cid}" in inbox and "brand new" in inbox and 'class="convo unread"' in inbox
    page = thread(a, cid)
    assert "brand new" in page and "old one" not in page and "old two" not in page
    poll = a.get(f"/messages/{cid}/poll", headers=FETCH).get_json()
    assert "old two" not in poll["html"]


def test_delete_chat_marks_notifications_read_and_clears_the_unread_badge(as_user, db):
    a, pa, b, pb, cid = pair(as_user, db)
    send(b, cid, "unread one")
    assert any(n["user_id"] == pa["id"] and not n.get("read_at") for n in db.data["notifications"] if n["kind"] == "message")
    a.post(f"/messages/{cid}/clear")
    assert all(n.get("read_at") for n in db.data["notifications"] if n["user_id"] == pa["id"] and n["kind"] == "message")


def test_starting_a_chat_again_opens_an_empty_page(as_user, db):
    a, pa, b, pb, cid = pair(as_user, db)
    send(a, cid, "before"); a.post(f"/messages/{cid}/clear")
    r = a.post(f"/messages/start/{pb['username']}", follow_redirects=True)
    assert "before" not in r.get_data(as_text=True) and "No messages yet" in r.get_data(as_text=True)


def test_group_chats_are_cleared_not_left(as_user, db):
    a, pa = as_user("alice"); b, pb = as_user("bob")
    cid = group(db, pa, pb)
    send(a, cid, "hi all")
    assert "Clear chat" in thread(a, cid) and "Delete chat" not in thread(a, cid)
    a.post(f"/messages/{cid}/clear")
    assert "hi all" not in thread(a, cid) and "hi all" in thread(b, cid)
    assert any(m["user_id"] == pa["id"] for m in db.data["conversation_members"] if m["conversation_id"] == cid)     # still a member


def test_strangers_cannot_delete_someone_elses_chat(as_user, db):
    a, pa, b, pb, cid = pair(as_user, db)
    c, pc = as_user("carol")
    send(a, cid, "keep me")
    assert c.post(f"/messages/{cid}/clear").status_code == 404
    assert "keep me" in thread(a, cid)


def test_delete_chat_can_be_called_from_the_app_without_a_redirect(as_user, db):
    a, pa, b, pb, cid = pair(as_user, db)
    r = a.post(f"/messages/{cid}/clear", headers=FETCH)
    assert r.get_json() == {"ok": True, "redirect": "/messages"}


# ================================================================== the pages, and staying safe
def test_thread_page_has_the_reply_bar_options_menu_and_delete_dialogs(as_user, db):
    a, pa, b, pb, cid = pair(as_user, db)
    send(a, cid, "hello")
    page = thread(a, cid)
    for needle in ("data-chat-context", 'name="reply_to_id"', 'id="msg-menu"', 'id="msg-delete-dialog"', "data-del-everyone", "data-del-me",
                   "data-msg-menu", 'data-msg-base="', 'data-unsend-hours="48"', 'data-edit-minutes="15"', "data-now=", "Delete chat",
                   f'action="/messages/{cid}/clear"'):
        assert needle in page, needle
    assert "Delete chat" in page and "block" in page.lower()                      # the existing menu items are still there


def test_inbox_has_a_delete_button_on_every_row(as_user, db):
    a, pa, b, pb, cid = pair(as_user, db)
    send(a, cid, "hi")
    inbox = a.get("/messages").get_data(as_text=True)
    assert f'action="/messages/{cid}/clear"' in inbox and 'aria-label="Delete chat with' in inbox and 'class="convo-row"' in inbox


def test_message_text_is_escaped_everywhere_new(as_user, db):
    a, pa, b, pb, cid = pair(as_user, db)
    send(a, cid, '<img src=x onerror=alert(1)> "quote"'); mid = last(db)["id"]
    send(b, cid, "re", reply_to_id=mid)
    page = thread(b, cid)
    assert "<img src=x" not in page and "&lt;img src=x" in page
    assert 'data-body="&lt;img' in page                                            # the raw-text attribute is escaped too


def test_poll_ignores_a_forged_update_marker(as_user, db):
    a, pa, b, pb, cid = pair(as_user, db)
    send(a, cid, "x")
    r = b.get(f"/messages/{cid}/poll", query_string={"upd": "2020-01-01T00:00:00+00:00,id.neq.0"}, headers=FETCH)
    assert r.status_code == 200 and r.get_json()["updates"] == []                  # not a date: ignored, never put into a database filter
    assert b.get(f"/messages/{cid}/poll", query_string={"upd": "' or 1=1 --"}, headers=FETCH).status_code == 200


def test_live_updates_degrade_quietly_before_the_migration_is_applied(app, as_user, db, monkeypatch):
    a, pa, b, pb, cid = pair(as_user, db)

    class Boom:
        def __getattr__(self, name):
            return lambda *args, **kw: self

        def execute(self):
            raise APIError({"message": "column messages.edited_at does not exist", "code": "42703"})

    monkeypatch.setattr(msg_routes, "db", lambda: Boom())
    with app.app_context():
        assert msg_routes._changed_since(cid, iso(utcnow()), None) == []


def test_everything_requires_login(client):
    cid = "00000000-0000-0000-0000-000000000000"
    for path in (f"/messages/{cid}/clear", f"/messages/{cid}/messages/{cid}/delete", f"/messages/{cid}/messages/{cid}/edit"):
        assert client.post(path).status_code in (302, 401)


def test_existing_sending_still_works_and_returns_a_complete_bubble(as_user, db):
    a, pa, b, pb, cid = pair(as_user, db)
    out = send(a, cid, "plain old message")
    assert 'class="ticks sent"' in out["html"] and "plain old message" in out["html"] and out["ts"]
    assert "reply_to_id" not in last(db)                                           # no new column is written unless it is used


def test_the_empty_chat_note_is_tagged_so_it_disappears_when_a_message_arrives(as_user, db):
    a, pa, b, pb, cid = pair(as_user, db)
    assert 'class="muted chat-empty"' in thread(a, cid)                            # the page removes it as soon as a bubble is added
    send(a, cid, "first!")
    assert "chat-empty" not in thread(a, cid)
