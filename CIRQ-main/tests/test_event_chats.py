"""Link Up: see who is going before you join, and the event group chat that closes after the event day."""
from datetime import timedelta

from app.services import event_chat
from app.utils import iso, utcnow
from tests.test_media_events_messages import make_event

FETCH = {"X-Requested-With": "fetch"}


def event_id(db):
    return db.data["events"][0]["id"]


def chat_ids(db):
    return [c["id"] for c in db.data.get("conversations", [])]


def test_event_gets_a_group_chat_that_expires_after_the_event_day(as_user, db):
    h, ph = as_user("host")
    make_event(h)
    ev, conv = db.data["events"][0], db.data["conversations"][0]
    assert conv["kind"] == "group" and conv["event_id"] == ev["id"] and ev["conversation_id"] == conv["id"]
    assert conv["expires_at"] and conv["expires_at"] >= ev["starts_at"]                     # never before the event starts
    assert any(m["user_id"] == ph["id"] for m in db.data["conversation_members"])           # host is in it


def test_chat_closes_at_end_of_the_event_day_in_east_africa_time(app):
    from app.utils import parse_dt
    e = {"starts_at": "2026-12-05T20:00:00+00:00"}              # 23:00 EAT on 5 Dec
    assert event_chat.expires_at_for(e) == parse_dt("2026-12-05T21:00:00+00:00")             # midnight EAT
    e["ends_at"] = "2026-12-06T02:00:00+00:00"                  # a late event that runs past midnight
    assert event_chat.expires_at_for(e) == parse_dt("2026-12-06T02:00:00+00:00")


def test_see_who_is_going_before_joining(as_user, db):
    h, ph = as_user("host"); a, pa = as_user("alice"); b, pb = as_user("bob")
    make_event(h); eid = event_id(db)
    a.post(f"/linkup/{eid}/rsvp", data={"status": "going"})
    b.post(f"/linkup/{eid}/rsvp", data={"status": "interested"})

    page = b.get(f"/linkup/{eid}").get_data(as_text=True)                  # bob hasn't joined
    assert "Who&#39;s going" in page or "Who's going" in page
    assert "@alice" in page and "@host" in page
    full = b.get(f"/linkup/{eid}/going").get_data(as_text=True)
    assert "@alice" in full and "@host" in full and "Host" in full and "Join this event" in full
    import re
    rows = re.findall(r'going-row" href="/u/(\w+)"', full)
    assert rows == ["host", "alice"]                                       # host first, then in joining order; interested isn't going


def test_going_list_hides_blocked_people_and_private_accounts_for_non_followers(as_user, db):
    h, ph = as_user("host"); a, pa = as_user("alice"); b, pb = as_user("bob"); c, pc = as_user("carol")
    make_event(h); eid = event_id(db)
    for client in (a, b):
        client.post(f"/linkup/{eid}/rsvp", data={"status": "going"})
    db.table("profiles").update({"is_private": True}).eq("id", pb["id"]).execute()
    full = c.get(f"/linkup/{eid}/going").get_data(as_text=True)
    assert "@alice" in full and "@bob" not in full and "1 more person with private accounts" in full
    db.table("follows").insert({"follower_id": pc["id"], "followee_id": pb["id"]}).execute()
    assert "@bob" in c.get(f"/linkup/{eid}/going").get_data(as_text=True)    # followers can see a private account
    db.table("blocks").insert({"blocker_id": pc["id"], "blocked_id": pa["id"]}).execute()
    assert "@alice" not in c.get(f"/linkup/{eid}/going").get_data(as_text=True)


def test_going_list_follows_event_visibility(as_user, db, app):
    h, _ = as_user("host")
    make_event(h); eid = event_id(db)
    outsider, _ = as_user("zed")
    assert outsider.get(f"/linkup/{eid}/going").status_code == 200
    db.data["events"][0]["moderation_status"] = "removed"
    assert outsider.get(f"/linkup/{eid}/going").status_code == 404


def test_going_joins_event_chat_interested_and_remove_leave_it(as_user, db):
    h, _ = as_user("host"); a, pa = as_user("alice")
    make_event(h); eid = event_id(db); cid = db.data["events"][0]["conversation_id"]
    members = lambda: {m["user_id"] for m in db.data["conversation_members"] if m["conversation_id"] == cid}
    a.post(f"/linkup/{eid}/rsvp", data={"status": "going"})
    assert pa["id"] in members() and a.get(f"/messages/{cid}").status_code == 200
    page = a.get(f"/linkup/{eid}").get_data(as_text=True)
    assert "Event chat" in page and "in the event chat" in page
    a.post(f"/linkup/{eid}/rsvp", data={"status": "interested"})
    assert pa["id"] not in members() and a.get(f"/messages/{cid}").status_code == 404
    assert "Tap <b>Going</b> to join" in a.get(f"/linkup/{eid}").get_data(as_text=True)


def test_chat_is_deleted_with_everything_in_it_once_the_date_is_gone(as_user, db, app):
    h, ph = as_user("host"); a, pa = as_user("alice")
    make_event(h); eid = event_id(db); cid = db.data["events"][0]["conversation_id"]
    a.post(f"/linkup/{eid}/rsvp", data={"status": "going"})
    assert a.post(f"/messages/{cid}/send", data={"body": "see you there"}, headers=FETCH).status_code == 200
    with app.app_context():
        assert event_chat.expire_due(force=True) == 0 and cid in chat_ids(db)               # not yet

    db.data["conversations"][0]["expires_at"] = iso(utcnow() - timedelta(minutes=1))       # the day is over
    with app.app_context():
        assert event_chat.expire_due(force=True) == 1
    assert cid not in chat_ids(db)
    assert not [m for m in db.data["messages"] if m["conversation_id"] == cid]
    assert not [m for m in db.data["conversation_members"] if m["conversation_id"] == cid]
    assert db.data["events"][0]["conversation_id"] is None
    assert db.data["events"][0]["id"] == eid                                                # the event itself stays


def test_expired_chat_is_closed_the_moment_someone_opens_it(as_user, db):
    h, _ = as_user("host"); a, _ = as_user("alice")
    make_event(h); eid = event_id(db); cid = db.data["events"][0]["conversation_id"]
    a.post(f"/linkup/{eid}/rsvp", data={"status": "going"})
    db.data["conversations"][0]["expires_at"] = iso(utcnow() - timedelta(minutes=1))
    assert a.post(f"/messages/{cid}/send", data={"body": "late"}, headers=FETCH).status_code == 404
    assert cid not in chat_ids(db) and not db.data["messages"]


def test_cancel_or_complete_closes_the_chat_right_away(as_user, db):
    h, _ = as_user("host"); a, _ = as_user("alice")
    make_event(h); eid = event_id(db); cid = db.data["events"][0]["conversation_id"]
    a.post(f"/linkup/{eid}/rsvp", data={"status": "going"})
    h.post(f"/linkup/{eid}/cancel")
    assert cid not in chat_ids(db) and db.data["events"][0]["conversation_id"] is None
    a.post(f"/linkup/{eid}/rsvp", data={"status": "going"})
    assert not db.data["conversations"]                                                    # no chat is made for a cancelled event


def test_nobody_can_rejoin_once_the_event_is_over_and_old_events_get_a_chat_lazily(as_user, db, app):
    h, _ = as_user("host"); a, pa = as_user("alice")
    make_event(h); eid = event_id(db)
    ev = db.data["events"][0]
    cid = ev["conversation_id"]
    db.data["conversations"].clear(); db.data["conversation_members"].clear(); ev["conversation_id"] = None   # an event from before this feature
    a.post(f"/linkup/{eid}/rsvp", data={"status": "going"})
    assert ev["conversation_id"] and ev["conversation_id"] != cid                           # first RSVP creates the chat
    assert {m["user_id"] for m in db.data["conversation_members"]} >= {pa["id"], ev["host_id"]}

    with app.app_context():
        event_chat.close_event_chat(ev)
    ev["starts_at"] = iso(utcnow() - timedelta(days=2))                                     # the date has gone
    r = a.post(f"/linkup/{eid}/rsvp", data={"status": "going"}, follow_redirects=True)
    assert "no longer open" in r.get_data(as_text=True) and not db.data["conversations"]


def test_cleanup_job_closes_finished_event_chats(as_user, db, app):
    h, _ = as_user("host")
    make_event(h)
    db.data["conversations"][0]["expires_at"] = iso(utcnow() - timedelta(hours=1))
    from app import run_cleanup
    with app.app_context():
        assert "closed 1 finished event chats" in run_cleanup()
    assert not db.data["conversations"]
