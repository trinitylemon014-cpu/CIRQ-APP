from tests.conftest import PASSWORD, login, make_user


def reg(client, school, **over):
    data = {"username": "newkid", "display_name": "New Kid", "email": "new@example.com", "school_id": school["id"],
            "password": PASSWORD, "password2": PASSWORD, "terms": "on", "age_ok": "on"}
    data.update(over)
    return client.post("/register", data=data)


def test_health(client):
    assert client.get("/health").get_json()["status"] == "ok"


def test_landing_and_pages_render(client):
    for path in ("/", "/login", "/register", "/forgot-password", "/guidelines", "/privacy"):
        assert client.get(path).status_code == 200, path


def test_register_creates_pending_account_and_normalizes_username(client, db, school):
    r = reg(client, school, username="  @NewKid  ")
    assert r.status_code == 302 and r.headers["Location"].endswith("/pending")
    p = db.data["profiles"][0]
    assert p["username"] == "newkid" and p["account_status"] == "pending"
    assert db.data.get("coin_ledger") is None       # coins only after approval
    assert len(db.users) == 1 and list(db.users.values())[0]["password"] == PASSWORD  # held by (fake) Supabase Auth only
    assert "password" not in p


def test_register_validation(client, db, school):
    for bad, key in [({"username": "a"}, "username"), ({"email": "nope"}, "email"), ({"password": "short", "password2": "short"}, "pw"),
                     ({"password2": "different-pw-1"}, "match"), ({"terms": ""}, "terms"), ({"age_ok": ""}, "age"), ({"school_id": "x"}, "school")]:
        r = reg(client, school, **bad)
        assert r.status_code == 400, key
    assert not db.data.get("profiles")


def test_username_uniqueness(client, db, school, mk):
    mk("taken")
    assert reg(client, school, username="TAKEN").status_code == 400
    assert len(db.data["profiles"]) == 1


def test_username_login_and_generic_errors(client, mk):
    mk("alice")
    ok = login(client, "Alice")
    assert ok.status_code == 302
    c2 = client.application.test_client()
    wrong_pw = login(c2, "alice", "wrong-password-1")
    unknown = login(c2, "nobody-here")
    assert wrong_pw.status_code == unknown.status_code == 401
    assert b"username or password isn" in wrong_pw.data and b"username or password isn" in unknown.data


def test_login_hides_email_and_sets_secure_session(client, mk, app):
    mk("alice")
    r = login(client, "alice")
    cookie = r.headers.get("Set-Cookie", "")
    assert "HttpOnly" in cookie and "alice@example.com" not in cookie
    assert b"alice@example.com" not in client.get("/feed").data


def test_logout_and_protected_pages(client, mk):
    for path in ("/feed", "/discover", "/reels", "/messages", "/settings", "/admin"):
        r = client.get(path)
        assert r.status_code == 302 and "/login" in r.headers["Location"]
    mk("alice"); login(client, "alice")
    assert client.get("/feed").status_code == 200
    client.post("/logout")
    assert client.get("/feed").status_code == 302


def test_pending_user_cannot_use_app(client, mk):
    mk("pendy", status="pending"); login(client, "pendy")
    assert client.get("/feed").headers["Location"].endswith("/pending")
    assert client.get("/pending").status_code == 200


def test_suspended_and_rejected_cannot_login(client, mk):
    mk("sus", status="suspended"); mk("rej", status="rejected")
    assert login(client, "sus").status_code == 403
    assert login(client, "rej").status_code == 403


def test_suspension_kicks_out_active_session(app, client, db, mk):
    p = mk("alice"); login(client, "alice")
    assert client.get("/feed").status_code == 200
    db.data["profiles"][0]["account_status"] = "suspended"
    assert client.get("/feed").status_code == 302


def test_open_redirect_blocked(client, mk):
    mk("alice")
    r = client.post("/login?next=//evil.example", data={"username": "alice", "password": PASSWORD})
    assert r.headers["Location"].endswith("/feed")


def test_welcome_coins_on_approval_only_once(app, db, mk):
    from app.services.account_service import approve_account
    p = mk("alice", status="pending")
    with app.app_context():
        approve_account(p["id"]); approve_account(p["id"])
    assert db.data["profiles"][0]["coin_balance"] == 50
    assert len(db.data["coin_ledger"]) == 1
