from datetime import timedelta

from app.utils import iso, utcnow
from tests.conftest import PASSWORD, login


def register(client, username, school_choice, **extra):
    data = {
        "username": username,
        "display_name": username.title(),
        "email": f"{username}@example.com",
        "school_choice": school_choice,
        "password": PASSWORD,
        "password2": PASSWORD,
        "terms": "on",
        "age_ok": "on",
    }
    data.update(extra)
    return client.post("/register", data=data)


def test_unlisted_school_submission_is_reused_and_counted(client, app, db, school):
    response = register(client, "firststudent", "My school isn't listed",
                        submitted_school_name="  St.   Mary's College  ")
    assert response.status_code == 302
    first = next(p for p in db.data["profiles"] if p["username"] == "firststudent")
    submission = db.data["school_submissions"][0]
    assert first["school_id"] is None
    assert first["school_verified"] is False
    assert first["submitted_school_name"] == "St. Mary's College"
    assert first["school_submission_id"] == submission["id"]

    second_client = app.test_client()
    response = register(second_client, "secondstudent", "My school isn't listed",
                        submitted_school_name="st marys college")
    assert response.status_code == 302
    second = next(p for p in db.data["profiles"] if p["username"] == "secondstudent")
    assert len(db.data["school_submissions"]) == 1
    assert second["school_submission_id"] == submission["id"]


def test_typed_school_matching_existing_school_uses_official_entry(client, db, school):
    existing = db.table("schools").insert({
        "name": "St. Mary's College", "slug": "st-marys-college", "country": "Uganda",
    }).execute().data[0]
    response = register(client, "matchedstudent", "My school isn't listed",
                        submitted_school_name="st marys college")
    assert response.status_code == 302
    profile = next(p for p in db.data["profiles"] if p["username"] == "matchedstudent")
    assert profile["school_id"] == existing["id"]
    assert profile["school_submission_id"] is None
    assert profile["submitted_school_name"] is None
    assert not db.data.get("school_submissions")


def test_school_submission_ip_daily_cap_is_persistent(app, db, school):
    responses = []
    for index in range(4):
        client = app.test_client()
        responses.append(register(client, f"schoolkid{index}", "My school isn't listed",
                                  submitted_school_name=f"New Campus {index}").status_code)
    assert responses == [302, 302, 302, 429]
    assert len(db.data["school_submissions"]) == 3


def test_admin_can_approve_school_and_auto_approved_accounts_are_listed(client, app, db, mk, school):
    register(client, "schoolkid", "My school isn't listed", submitted_school_name="North Campus")
    submission = db.data["school_submissions"][0]
    admin = mk("schooladmin", role="admin")
    admin_client = app.test_client()
    login(admin_client, admin["username"])

    response = admin_client.post(f"/admin/school-submissions/{submission['id']}/approve")
    assert response.status_code == 302
    profile = next(p for p in db.data["profiles"] if p["username"] == "schoolkid")
    assert profile["school_id"]
    assert profile["school_verified"] is True
    assert db.data["school_submissions"][0]["status"] == "approved"

    old_user = mk("autouser", status="pending", verified_email=True)
    row = next(p for p in db.data["profiles"] if p["id"] == old_user["id"])
    row["created_at"] = iso(utcnow() - timedelta(minutes=6))
    with app.app_context():
        from app.services.account_service import auto_approve_stale_accounts
        assert auto_approve_stale_accounts() == 1
    response = admin_client.get("/admin/auto-approved")
    assert response.status_code == 200
    assert b"@autouser" in response.data
