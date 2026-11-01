import re
from tests.conftest import PASSWORD, login
from app.utils import hash_token

NEW_PW = "Brand-new-pass99"


def request_reset(client, school, username="alice", email="alice@example.com", **extra):
    return client.post("/forgot-password", data={"username": username, "school_id": school["id"], "email": email, **extra})


def admin_client(app, mk):
    mk("boss", role="admin")
    c = app.test_client(); login(c, "boss"); return c


def test_request_creates_pending_row_without_secrets(client, db, school, mk):
    mk("alice")
    r = request_reset(client, school)
    assert r.status_code == 200
    req = db.data["password_reset_requests"][0]
    assert req["status"] == "pending" and req["token_hash"] is None
    assert PASSWORD not in str(req)


def test_response_identical_for_unknown_and_wrong_details(client, db, school, mk):
    mk("alice")
    a = request_reset(client, school, username="ghost")
    b = request_reset(client, school, email="wrong@example.com")
    c = request_reset(client, school)
    assert a.data == b.data == c.data
    assert len(db.data.get("password_reset_requests", [])) == 1


def test_duplicate_open_requests_are_not_created(client, db, school, mk):
    mk("alice"); request_reset(client, school); request_reset(client, school)
    assert len(db.data["password_reset_requests"]) == 1


def test_only_admin_can_review(app, client, db, school, mk, as_user):
    mk("alice"); request_reset(client, school)
    rid = db.data["password_reset_requests"][0]["id"]
    assert client.get("/admin/password-resets").status_code == 302          # anonymous
    stu, _ = as_user("bob")
    assert stu.get("/admin/password-resets").status_code == 403              # student
    assert stu.post(f"/admin/password-resets/{rid}/approve").status_code == 403
    assert db.data["password_reset_requests"][0]["status"] == "pending"


def full_flow(app, client, db, school, mk):
    mk("alice"); request_reset(client, school)
    adm = admin_client(app, mk)
    rid = db.data["password_reset_requests"][0]["id"]
    r = adm.post(f"/admin/password-resets/{rid}/approve", data={"note": "checked"})
    assert r.status_code == 302
    mail = app.extensions["sent_mail"][-1]
    token = re.search(r"/reset-password/([\w-]+)", mail["body"]).group(1)
    return adm, rid, token, mail


def test_approval_emails_single_use_link_and_does_not_change_password(app, client, db, school, mk):
    adm, rid, token, mail = full_flow(app, client, db, school, mk)
    assert mail["to"] == "alice@example.com"
    req = db.data["password_reset_requests"][0]
    assert req["status"] == "approved" and req["token_hash"] == hash_token(token) and token not in str(req)
    alice_id = db.data["profiles"][0]["id"]
    assert db.users[alice_id]["password"] == PASSWORD            # unchanged by approval
    assert login(client, "alice").status_code == 302             # old password still works until student resets


def test_reset_completes_once_and_revokes_sessions(app, client, db, school, mk):
    adm, rid, token, _ = full_flow(app, client, db, school, mk)
    old = app.test_client(); login(old, "alice"); assert old.get("/feed").status_code == 200
    r = client.post(f"/reset-password/{token}", data={"password": NEW_PW, "password2": NEW_PW})
    assert r.status_code == 302
    assert db.data["password_reset_requests"][0]["status"] == "completed"
    assert old.get("/feed").status_code == 302                    # old session revoked
    assert login(app.test_client(), "alice", PASSWORD).status_code == 401
    assert login(app.test_client(), "alice", NEW_PW).status_code == 302
    again = client.post(f"/reset-password/{token}", data={"password": "Another-pass-77", "password2": "Another-pass-77"})
    assert again.status_code == 400                               # single use
    assert any(a["action"] == "password_reset_completed" for a in db.data["audit_logs"])
    assert "changed" in app.extensions["sent_mail"][-1]["subject"].lower()


def test_expired_token_rejected(app, client, db, school, mk):
    adm, rid, token, _ = full_flow(app, client, db, school, mk)
    db.data["password_reset_requests"][0]["token_expires_at"] = "2000-01-01T00:00:00+00:00"
    assert client.get(f"/reset-password/{token}").status_code == 400
    assert client.post(f"/reset-password/{token}", data={"password": NEW_PW, "password2": NEW_PW}).status_code == 400


def test_rejection_and_short_password_reset(app, client, db, school, mk):
    mk("alice"); request_reset(client, school)
    adm = admin_client(app, mk); rid = db.data["password_reset_requests"][0]["id"]
    adm.post(f"/admin/password-resets/{rid}/reject", data={"note": "not verified"})
    assert db.data["password_reset_requests"][0]["status"] == "rejected"
    assert not app.extensions.get("sent_mail")
    # Short passwords are accepted on a valid reset token.
    request_reset(client, school)
    rid2 = db.data["password_reset_requests"][1]["id"]
    adm.post(f"/admin/password-resets/{rid2}/approve", data={})
    token = re.search(r"/reset-password/([\w-]+)", app.extensions["sent_mail"][-1]["body"]).group(1)
    assert client.post(f"/reset-password/{token}", data={"password": "alice", "password2": "alice"}).status_code == 302
    assert db.data["password_reset_requests"][1]["status"] == "completed"
    assert db.users[db.data["profiles"][0]["id"]]["password"] == "alice"


def test_manual_review_requires_replacement_email_and_note(app, client, db, school, mk):
    mk("alice", verified_email=False)
    client.post("/forgot-password", data={"username": "alice", "school_id": school["id"], "no_email_access": "on", "display_name": "Alice", "note": "class 10B"})
    req = db.data["password_reset_requests"][0]; assert req["manual_review"]
    adm = admin_client(app, mk)
    assert adm.post(f"/admin/password-resets/{req['id']}/approve", data={"note": "x"}).status_code == 302
    assert db.data["password_reset_requests"][0]["status"] == "pending"      # refused: no replacement email
    adm.post(f"/admin/password-resets/{req['id']}/approve", data={"replacement_email": "alice.new@example.com", "note": "Confirmed by coordinator Ms Achan by phone"})
    assert app.extensions["sent_mail"][-1]["to"] == "alice.new@example.com"
    token = re.search(r"/reset-password/([\w-]+)", app.extensions["sent_mail"][-1]["body"]).group(1)
    client.post(f"/reset-password/{token}", data={"password": NEW_PW, "password2": NEW_PW})
    assert db.users[db.data["profiles"][0]["id"]]["email"] == "alice.new@example.com"
