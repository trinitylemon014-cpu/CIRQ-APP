from datetime import timedelta

from app.utils import iso, utcnow
from tests.conftest import PASSWORD


def test_pending_status_endpoint_lazily_approves_verified_user(app, db, mk, client):
    pending = mk("almostin", status="pending", verified_email=True)
    next(p for p in db.data["profiles"] if p["id"] == pending["id"])["created_at"] = iso(utcnow() - timedelta(minutes=6))
    client.post("/login", data={"username": pending["username"], "password": PASSWORD})

    response = client.get("/api/auth/approval-status")
    assert response.status_code == 200
    assert response.get_json()["status"] == "approved"
    row = next(p for p in db.data["profiles"] if p["id"] == pending["id"])
    assert row["approval_source"] == "system" and row["approved_at"]


def test_rejected_and_flagged_accounts_are_never_lazily_approved(app, db, mk):
    rejected = mk("rejectednow", status="pending", verified_email=True)
    blocked = mk("flagged", status="pending", verified_email=True)
    client = app.test_client()
    client.post("/login", data={"username": rejected["username"], "password": PASSWORD})
    for profile in db.data["profiles"]:
        if profile["id"] in (rejected["id"], blocked["id"]):
            profile["created_at"] = iso(utcnow() - timedelta(minutes=6))
    next(p for p in db.data["profiles"] if p["id"] == rejected["id"]).update(
        account_status="rejected", status_note="Needs review")
    next(p for p in db.data["profiles"] if p["id"] == blocked["id"])["auto_approval_blocked"] = True

    response = client.get("/api/auth/approval-status")
    assert response.get_json()["status"] == "rejected"

    with app.app_context():
        from app.services.account_service import auto_approve_stale_accounts
        assert auto_approve_stale_accounts() == 0
    assert next(p for p in db.data["profiles"] if p["id"] == blocked["id"])["account_status"] == "pending"
