"""The 'Follow requests' page of a private account. It had no test, so a wrong template (trivia-results markup) shipped
and /follow-requests returned an internal server error."""
from pathlib import Path

FETCH = {"X-Requested-With": "fetch"}


def ask(db, requester, target):
    db.table("follow_requests").insert({"requester_id": requester["id"], "target_id": target["id"]}).execute()


def go_private(db, user):
    db.table("profiles").update({"is_private": True}).eq("id", user["id"]).execute()


def test_page_lists_pending_requests_with_confirm_and_delete(as_user, db, mk):
    c, me = as_user("alice")
    go_private(db, me)
    bob = mk("bob")
    db.table("profiles").update({"display_name": "Bobby B"}).eq("id", bob["id"]).execute()
    ask(db, bob, me)
    r = c.get("/follow-requests")
    html = r.get_data(as_text=True)
    assert r.status_code == 200
    assert "Follow requests" in html and "Bobby B" in html and "@bob" in html
    assert 'data-req-approve="bob"' in html and 'data-req-decline="bob"' in html
    assert "Your account is public" not in html


def test_page_shows_an_empty_state_when_nobody_is_waiting(as_user, db):
    c, me = as_user("alice")
    go_private(db, me)
    r = c.get("/follow-requests")
    assert r.status_code == 200 and "No pending requests" in r.get_data(as_text=True)


def test_public_accounts_see_an_explanation_not_an_error(as_user):
    c, _ = as_user("alice")
    r = c.get("/follow-requests")
    assert r.status_code == 200 and "Your account is public" in r.get_data(as_text=True)


def test_blocked_and_unapproved_requesters_are_not_listed(as_user, db, mk):
    c, me = as_user("alice")
    go_private(db, me)
    blocked, pending, ok = mk("blocked"), mk("pending"), mk("okay")
    db.table("profiles").update({"account_status": "pending"}).eq("id", pending["id"]).execute()
    db.table("blocks").insert({"blocker_id": me["id"], "blocked_id": blocked["id"]}).execute()
    for u in (blocked, pending, ok):
        ask(db, u, me)
    html = c.get("/follow-requests").get_data(as_text=True)
    assert "@okay" in html and "@blocked" not in html and "@pending" not in html


def test_confirm_and_delete_work_from_the_page(as_user, db, mk):
    c, me = as_user("alice")
    go_private(db, me)
    bob, cara = mk("bob"), mk("cara")
    ask(db, bob, me); ask(db, cara, me)
    assert c.post("/follow-requests/bob/approve", headers=FETCH).status_code == 200
    assert c.post("/follow-requests/cara/decline", headers=FETCH).status_code == 200
    html = c.get("/follow-requests").get_data(as_text=True)
    assert "No pending requests" in html
    assert any(f["follower_id"] == bob["id"] and f["followee_id"] == me["id"] for f in db.data["follows"])      # bob now follows alice
    assert not any(f["follower_id"] == cara["id"] for f in db.data.get("follows", []))                           # cara does not


def test_requires_login(client):
    assert client.get("/follow-requests").status_code in (302, 401)


def test_requests_template_is_the_follow_requests_template():
    """Guard against a pasted-over file: this template must be about follow requests, not another page."""
    src = Path(__file__).resolve().parents[1].joinpath("app/templates/profiles/requests.html").read_text()
    assert "data-requests" in src and "data-req-approve" in src and "Follow requests" in src
    assert "attempt." not in src and "{{ s.title }}" not in src                  # trivia-results variables never belong here
