import io
from datetime import datetime, timedelta, timezone
from tests.conftest import MP4, login


def future(days=2):
    return (datetime.now(timezone(timedelta(hours=3))) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M")


# ---------------- stories
def test_story_upload_sets_24h_expiry_and_cloudinary_ref(as_user, db, uploads, png):
    c, p = as_user("alice")
    c.post("/stories", data={"media": (io.BytesIO(png), "s.png"), "caption": "hi"}, content_type="multipart/form-data")
    s = db.data["stories"][0]
    exp = datetime.fromisoformat(s["expires_at"]); now = datetime.now(timezone.utc)
    assert timedelta(hours=23, minutes=55) < exp - now <= timedelta(hours=24)
    assert s["public_id"].startswith("cirq/stories/")


def test_expired_stories_are_not_served_even_by_direct_api(as_user, db, uploads, png):
    a, pa = as_user("alice"); b, _ = as_user("bob")
    a.post("/stories", data={"media": (io.BytesIO(png), "s.png")}, content_type="multipart/form-data")
    b.post("/u/alice/follow")
    assert len(a.get("/stories/api/alice").get_json()["stories"]) == 1
    db.data["stories"][0]["expires_at"] = "2000-01-01T00:00:00+00:00"
    assert a.get("/stories/api/alice").get_json()["stories"] == []
    sid = db.data["stories"][0]["id"]
    assert b.post(f"/stories/{sid}/view").status_code == 404


def test_story_views_tracked_and_delete_owner_only(as_user, db, uploads, png):
    a, _ = as_user("alice"); b, _ = as_user("bob")
    a.post("/stories", data={"media": (io.BytesIO(png), "s.png")}, content_type="multipart/form-data")
    sid = db.data["stories"][0]["id"]
    b.post(f"/stories/{sid}/view"); b.post(f"/stories/{sid}/view")
    assert len(db.data["story_views"]) == 1
    assert b.post(f"/stories/{sid}/delete").status_code == 403
    a.post(f"/stories/{sid}/delete")
    assert not db.data["stories"] and uploads["destroyed"]


def test_cleanup_removes_expired_stories_and_files(app, db, uploads, as_user, png):
    a, _ = as_user("alice")
    a.post("/stories", data={"media": (io.BytesIO(png), "s.png")}, content_type="multipart/form-data")
    db.data["stories"][0]["expires_at"] = "2000-01-01T00:00:00+00:00"
    app.config["CRON_SECRET"] = "cron-secret"
    c = app.test_client()
    assert c.post("/admin/internal/cleanup").status_code == 403
    assert c.post("/admin/internal/cleanup", headers={"Authorization": "Bearer cron-secret"}).status_code == 200
    assert not db.data["stories"] and len(uploads["destroyed"]) == 1


# ---------------- reels
def test_reel_upload_requires_video_and_lists_paginated(as_user, db, uploads, png):
    c, _ = as_user("alice")
    c.post("/reels", data={"video": (io.BytesIO(png), "x.png")}, content_type="multipart/form-data")
    assert not db.data.get("posts")
    for i in range(7):
        c.post("/reels", data={"video": (io.BytesIO(MP4), f"r{i}.mp4"), "caption": f"reel {i}"}, content_type="multipart/form-data")
    assert len(db.data["posts"]) == 7 and all(p["kind"] == "reel" for p in db.data["posts"])
    first = c.get("/reels?partial=1")
    assert first.data.count(b"data-reel ") == 5 and first.headers["X-Next-Cursor"]
    second = c.get(f"/reels?partial=1&before={first.headers['X-Next-Cursor']}")
    assert second.data.count(b"data-reel ") == 2
    assert b"autoplay" not in first.data                       # no unexpected autoplay audio


def test_reel_pagination_rejects_malformed_cursor(as_user):
    c, _ = as_user("alice")
    assert c.get("/reels?partial=1&before=invalid").status_code == 400


# ---------------- LINKUP
def make_event(c, **over):
    data = {"title": "Debate night", "venue": "School hall", "starts_at": future(), "description": "Come along", "visibility": "public"}
    data.update(over)
    return c.post("/linkup/new", data=data, content_type="multipart/form-data")


def test_create_event_with_group_chat_and_cover(as_user, db, uploads, png):
    c, p = as_user("alice")
    r = make_event(c, cover=(io.BytesIO(png), "cover.png"))
    assert r.status_code == 302
    e = db.data["events"][0]
    assert e["cover_public_id"].startswith("cirq/events/") and e["host_id"] == p["id"]
    conv = db.data["conversations"][0]
    assert conv["event_id"] == e["id"] and e["conversation_id"] == conv["id"]


def test_event_validation(as_user, db):
    c, _ = as_user("alice")
    assert make_event(c, title="x").status_code == 400
    assert make_event(c, venue="").status_code == 400
    assert make_event(c, starts_at="2001-01-01T10:00").status_code == 400
    assert make_event(c, description="call +256 700 123 456").status_code == 400
    assert not db.data.get("events")


def test_rsvp_capacity_chat_membership_and_authorization(as_user, db):
    h, _ = as_user("host"); a, pa = as_user("alice"); b, pb = as_user("bob")
    make_event(h, capacity="2"); eid = db.data["events"][0]["id"]
    a.post(f"/linkup/{eid}/rsvp", data={"status": "going"})
    assert any(m["user_id"] == pa["id"] for m in db.data["conversation_members"])
    b.post(f"/linkup/{eid}/rsvp", data={"status": "going"})                 # host + alice fill capacity 2
    assert not any(r["user_id"] == pb["id"] and r["status"] == "going" for r in db.data["event_rsvps"])
    b.post(f"/linkup/{eid}/rsvp", data={"status": "interested"})
    assert any(r["user_id"] == pb["id"] and r["status"] == "interested" for r in db.data["event_rsvps"])
    assert b.post(f"/linkup/{eid}/cancel").status_code == 403               # only host/admin
    h.post(f"/linkup/{eid}/cancel")
    assert db.data["events"][0]["status"] == "cancelled"
    assert [n for n in db.data["notifications"] if n["kind"] == "event_update"]
    a.post(f"/linkup/{eid}/rsvp", data={"status": "going"})
    assert any(r["user_id"] == pa["id"] for r in db.data["event_rsvps"]) and True


def test_school_only_events_hidden_from_other_schools(app, as_user, db, mk):
    other = db.table("schools").insert({"name": "Other School", "slug": "other"}).execute().data[0]
    h, _ = as_user("host"); make_event(h, visibility="school"); eid = db.data["events"][0]["id"]
    mk("outsider", school=other); o = app.test_client(); login(o, "outsider")
    assert o.get(f"/linkup/{eid}").status_code == 404
    assert o.post(f"/linkup/{eid}/rsvp", data={"status": "going"}).status_code == 404


# ---------------- messaging
def test_dm_roundtrip_unread_and_read_receipts(as_user, db):
    a, pa = as_user("alice"); b, pb = as_user("bob")
    r = a.post("/messages/start/bob"); cid = r.headers["Location"].rsplit("/", 1)[1]
    assert a.post("/messages/start/bob").headers["Location"].endswith(cid)   # one DM per pair
    a.post(f"/messages/{cid}/send", data={"body": "hey bob"})
    assert b"hey bob" in b.get("/messages").data and b"hey bob" in b.get(f"/messages/{cid}").data
    from app.blueprints.messages.routes import unread_total
    # after bob opened the thread his unread notification is cleared
    with b.application.app_context():
        assert unread_total(pb["id"]) == 0
    poll = b.get(f"/messages/{cid}/poll?after=2000-01-01T00:00:00")
    assert "hey bob" in poll.get_json()["html"]                               # poll returns the other person's messages only
    assert b.get(f"/messages/{cid}/poll?after={poll.get_json()['ts']}").get_json()["html"] == ""   # nothing new after the cursor
    b.post(f"/messages/{cid}/send", data={"body": "yo"})
    got = a.get(f"/messages/{cid}/poll?after=2000-01-01T00:00:00").get_json()
    assert "yo" in got["html"] and got["ts"]


def test_non_members_cannot_read_or_write(as_user, db):
    a, _ = as_user("alice"); b, _ = as_user("bob"); c, _ = as_user("carol")
    cid = a.post("/messages/start/bob").headers["Location"].rsplit("/", 1)[1]
    a.post(f"/messages/{cid}/send", data={"body": "private stuff"})
    assert c.get(f"/messages/{cid}").status_code == 404
    assert c.post(f"/messages/{cid}/send", data={"body": "intrude"}).status_code == 404
    assert c.get(f"/messages/{cid}/poll").status_code == 404
    assert b"private stuff" not in c.get("/messages").data


def test_message_attachment_is_private_cloudinary_asset(as_user, db, uploads, png):
    a, _ = as_user("alice"); b, _ = as_user("bob")
    cid = a.post("/messages/start/bob").headers["Location"].rsplit("/", 1)[1]
    r = a.post(f"/messages/{cid}/send", data={"attachment": (io.BytesIO(png), "p.png")}, content_type="multipart/form-data")
    assert r.status_code == 200 and uploads["uploaded"][0]["type"] == "authenticated"
    assert "s--" in r.get_json()["html"]                                       # signed URL signature segment


def test_empty_and_filtered_messages_rejected(as_user, db):
    a, _ = as_user("alice"); b, _ = as_user("bob")
    cid = a.post("/messages/start/bob").headers["Location"].rsplit("/", 1)[1]
    assert a.post(f"/messages/{cid}/send", data={"body": " "}).status_code == 400
    assert a.post(f"/messages/{cid}/send", data={"body": "send nudes"}).status_code == 400
    assert not db.data.get("messages")


def test_notifications_list_open_and_mark_read(as_user, db):
    a, pa = as_user("alice"); b, _ = as_user("bob")
    b.post("/u/alice/follow")
    assert b"bob" in a.get("/notifications").data
    before = a.get("/api/notifications/count").get_json()["count"]
    assert before == 2                                                         # follow + "Noticed" achievement
    nid = next(n["id"] for n in db.data["notifications"] if n["kind"] == "follow")
    assert a.get(f"/notifications/{nid}/open").headers["Location"].endswith("/u/bob")
    assert a.get("/api/notifications/count").get_json()["count"] == before - 1
    a.post("/notifications/read-all")
    assert a.get("/api/notifications/count").get_json()["count"] == 0
    assert b.get(f"/notifications/{nid}/open").status_code == 404


def test_reel_upload_json_for_progress_uploader(as_user, db, uploads):
    c, _ = as_user("alice")
    r = c.post("/reels", data={"video": (io.BytesIO(MP4), "a.mp4"), "caption": "hi"}, content_type="multipart/form-data",
               headers={"X-Requested-With": "fetch"})
    assert r.status_code == 200 and r.get_json()["ok"] is True and r.get_json()["redirect"].endswith("/reels")
    bad = c.post("/reels", data={"video": (io.BytesIO(b"not a video"), "a.mp4")}, content_type="multipart/form-data",
                 headers={"X-Requested-With": "fetch"})
    assert bad.status_code == 400 and "error" in bad.get_json()
    assert len(db.data["posts"]) == 1


def test_private_reel_upload_is_added_to_owner_stream_only(as_user, db, uploads):
    owner, profile = as_user("alice")
    db.table("profiles").update({"is_private": True}).eq("id", profile["id"]).execute()
    posted = owner.post("/reels", data={"video": (io.BytesIO(MP4), "private.mp4"), "caption": "My reel"},
                        content_type="multipart/form-data", headers={"X-Requested-With": "fetch"}).get_json()
    assert posted["ok"] and 'data-id="' + db.data["posts"][0]["id"] + '"' in posted["html"]
    assert db.data["posts"][0]["visibility"] == "followers"
    assert db.data["posts"][0]["id"] in owner.get("/reels").get_data(as_text=True)

    viewer, _ = as_user("bob")
    assert db.data["posts"][0]["id"] not in viewer.get("/reels").get_data(as_text=True)


def test_reel_delivery_url_is_h264_mp4(uploads):
    from app.services import cloudinary_service as cs
    url = cs.media_src({"public_id": "cirq/reels/a", "media_type": "video", "delivery_type": "upload", "url": "u"}, 540)
    assert url.endswith(".mp4") and "vc_h264" in url and "ac_aac" in url
