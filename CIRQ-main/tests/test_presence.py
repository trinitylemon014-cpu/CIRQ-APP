from tests.conftest import login
from app.utils import iso, utcnow


def test_presence_ping_updates_last_seen_and_throttles_writes(client, db, mk):
    alice = mk("alice")
    login(client, "alice")

    first = client.post("/api/presence/ping")
    assert first.status_code == 200 and first.get_json()["updated"] is True
    stored = next(p for p in db.data["profiles"] if p["id"] == alice["id"])["last_seen_at"]
    second = client.post("/api/presence/ping")
    assert second.status_code == 200 and second.get_json()["updated"] is False
    assert next(p for p in db.data["profiles"] if p["id"] == alice["id"])["last_seen_at"] == stored


def test_presence_status_shows_active_and_offline_users(client, db, mk):
    alice = mk("alice")
    bob = mk("bob")
    alice_row = next(p for p in db.data["profiles"] if p["id"] == alice["id"])
    alice_row["last_seen_at"] = iso(utcnow())
    login(client, "bob")

    response = client.get(f"/api/presence/status?user_id={alice['id']}&user_id={bob['id']}")
    assert response.status_code == 200
    assert response.get_json()["statuses"][alice["id"]] == "active"
    assert response.get_json()["statuses"][bob["id"]] == "offline"
    assert response.get_json()["last_seen_at"][alice["id"]] == alice_row["last_seen_at"]
    assert response.get_json()["last_seen_at"][bob["id"]] is None


def test_presence_status_obeys_profile_privacy_and_blocks(client, db, mk):
    alice = mk("alice")
    bob = mk("bob")
    alice_row = next(p for p in db.data["profiles"] if p["id"] == alice["id"])
    alice_row["last_seen_at"] = iso(utcnow())
    login(client, "bob")

    alice_row["show_active_status"] = False
    response = client.get(f"/api/presence/status?user_id={alice['id']}")
    assert response.get_json()["statuses"][alice["id"]] == "hidden"
    assert alice["id"] not in response.get_json()["last_seen_at"]

    alice_row["show_active_status"] = True
    db.table("blocks").insert({"blocker_id": alice["id"], "blocked_id": bob["id"]}).execute()
    response = client.get(f"/api/presence/status?user_id={alice['id']}")
    assert response.get_json()["statuses"][alice["id"]] == "hidden"


def test_disabled_profile_does_not_render_presence_dot(client, db, mk):
    alice = mk("alice")
    mk("bob")
    alice_row = next(p for p in db.data["profiles"] if p["id"] == alice["id"])
    alice_row["show_active_status"] = False
    login(client, "bob")

    page = client.get("/u/alice").get_data(as_text=True)
    assert f'data-presence-dot data-user-id="{alice["id"]}"' not in page


def test_disabling_presence_hides_everyone_and_stops_heartbeats(client, db, mk):
    alice = mk("alice")
    bob = mk("bob")
    login(client, "alice")

    saved = client.post("/settings", data={"action": "privacy"})
    assert saved.status_code == 302
    response = client.get(f"/api/presence/status?user_id={bob['id']}")
    assert response.get_json()["statuses"][bob["id"]] == "hidden"
    assert response.get_json()["last_seen_at"] == {}
    assert client.post("/api/presence/ping").get_json()["updated"] is False
    assert next(p for p in db.data["profiles"] if p["id"] == alice["id"])["last_seen_at"] is None
