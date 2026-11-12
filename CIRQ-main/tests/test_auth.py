from tests.conftest import PASSWORD, login, make_user


def reg(client, school, **over):
    data = {"username": "newkid", "display_name": "New Kid", "email": "new@example.com", "school_choice": school["name"],
            "password": PASSWORD, "password2": PASSWORD, "terms": "on", "age_ok": "on"}
    data.update(over)
    return client.post("/register", data=data)


def test_health(client):
    assert client.get("/health").get_json()["status"] == "ok"


def test_landing_and_pages_render(client):
    for path in ("/", "/login", "/register", "/forgot-password", "/guidelines", "/privacy"):
        assert client.get(path).status_code == 200, path


def test_register_creates_pending_account_and_normalizes_username(client, db, school):
    r = reg(client, school, username="  NewKid  ")
    assert r.status_code == 302 and r.headers["Location"].endswith("/recovery-code")   # sign-up shows the recovery code first, then the pending page
    p = db.data["profiles"][0]
    assert p["username"] == "newkid" and p["account_status"] == "pending"
    assert db.data.get("coin_ledger") is None       # coins only after approval
    assert len(db.users) == 1 and list(db.users.values())[0]["password"] == PASSWORD  # held by (fake) Supabase Auth only
    assert "password" not in p


def test_register_validation(client, db, school):
    for bad, key in [({"username": "a"}, "username"), ({"email": "nope"}, "email"),
                     ({"password": "", "password2": ""}, "password"),
                     ({"password2": "different-pw-1"}, "match"), ({"terms": ""}, "terms"),
                     ({"age_ok": ""}, "age"), ({"school_choice": "x"}, "school")]:
        r = reg(client, school, **bad)
        assert r.status_code == 400, key
    assert not db.data.get("profiles")


def test_registration_accepts_short_password_containing_username(client, db, school):
    r = reg(client, school, password="newkid", password2="newkid")
    assert r.status_code == 302
    assert list(db.users.values())[0]["password"] == "newkid"


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
    assert b"Invalid username/email or password" in wrong_pw.data
    assert b"Invalid username/email or password" in unknown.data


def test_email_login_is_case_insensitive_and_uses_same_generic_error(client, mk):
    mk("alice", email="Alice@Example.com")
    assert login(client, "  ALICE@EXAMPLE.COM  ").status_code == 302
    invalid = client.application.test_client()
    wrong_password = login(invalid, "ALICE@example.com", "wrong-password-1")
    unknown_email = login(invalid, "missing@example.com")
    assert wrong_password.status_code == unknown_email.status_code == 401
    assert b"Invalid username/email or password" in wrong_password.data
    assert b"Invalid username/email or password" in unknown_email.data


def test_registration_rejects_at_sign_in_username(client, db, school):
    response = reg(client, school, username="name@example")
    leading = reg(client, school, username="@leading")
    assert response.status_code == 400
    assert leading.status_code == 400
    assert not db.data.get("profiles")


def test_change_password_accepts_short_password_containing_username(client, db, mk):
    mk("alice")
    login(client, "alice")
    response = client.post("/settings", data={
        "action": "password", "current": PASSWORD, "new": "alice", "new2": "alice",
    })
    assert response.status_code == 302
    assert db.users[db.data["profiles"][0]["id"]]["password"] == "alice"


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
    admin = mk("boss")
    with app.app_context():
        approve_account(p["id"], admin["id"]); approve_account(p["id"], admin["id"])
    assert db.data["profiles"][0]["coin_balance"] == 50
    assert len(db.data["coin_ledger"]) == 1
    assert db.data["profiles"][0]["approval_source"] == "admin"
    assert db.data["profiles"][0]["approved_by"] == admin["id"]
    assert db.data["profiles"][0]["approved_at"]


def test_auto_approval_after_five_minutes_notifies_admin_and_grants_welcome_coins(app, db, mk):
    from datetime import timedelta
    from app.services.account_service import auto_approve_stale_accounts
    from app.utils import iso, utcnow

    admin = mk("boss", role="admin")
    pending = mk("verified", status="pending", verified_email=True)
    pending_row = next(p for p in db.data["profiles"] if p["id"] == pending["id"])
    pending_row["created_at"] = iso(utcnow() - timedelta(minutes=6))
    unverified = mk("unverified", status="pending", verified_email=False)
    unverified_row = next(p for p in db.data["profiles"] if p["id"] == unverified["id"])
    unverified_row["created_at"] = iso(utcnow() - timedelta(minutes=6))
    recent = mk("recent", status="pending")
    rejected = mk("rejected", status="rejected")
    rejected_row = next(p for p in db.data["profiles"] if p["id"] == rejected["id"])
    rejected_row["created_at"] = iso(utcnow() - timedelta(minutes=6))

    with app.app_context():
        assert auto_approve_stale_accounts() == 1
        assert auto_approve_stale_accounts() == 0

    approved_row = next(p for p in db.data["profiles"] if p["id"] == pending["id"])
    assert approved_row["account_status"] == "approved"
    assert approved_row["approval_source"] == "system" and approved_row["approved_at"]
    assert next(p for p in db.data["profiles"] if p["id"] == unverified["id"])["account_status"] == "pending"
    assert next(p for p in db.data["profiles"] if p["id"] == recent["id"])["account_status"] == "pending"
    assert next(p for p in db.data["profiles"] if p["id"] == rejected["id"])["account_status"] == "rejected"
    assert next(p for p in db.data["profiles"] if p["id"] == pending["id"])["coin_balance"] == app.config["WELCOME_COINS"]
    admin_notices = [n for n in db.data["notifications"] if n["user_id"] == admin["id"]]
    assert len(admin_notices) == 1
    assert admin_notices[0]["ref_type"] == "auto_approved_account"
    assert "verified" in admin_notices[0]["body"]
    from app.blueprints.notifications.routes import target_url
    with app.test_request_context():
        assert target_url(admin_notices[0]).endswith("/u/verified")
    assert len([r for r in db.data["audit_logs"] if r["action"] == "user_auto_approved"]) == 1


def test_auto_approval_cron_endpoint_requires_secret_and_runs_job(app, db, mk):
    from datetime import timedelta
    from app.utils import iso, utcnow

    pending = mk("olduser", status="pending")
    next(p for p in db.data["profiles"] if p["id"] == pending["id"])["created_at"] = iso(utcnow() - timedelta(minutes=6))
    app.config["CRON_SECRET"] = "cron-secret"
    client = app.test_client()
    assert client.post("/admin/internal/auto-approve-pending").status_code == 403
    response = client.post("/admin/internal/auto-approve-pending", headers={"Authorization": "Bearer cron-secret"})
    assert response.status_code == 200 and response.get_json() == {"approved": 1}
