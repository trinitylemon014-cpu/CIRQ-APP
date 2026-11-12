"""Account recovery code: sign-up, viewing/renewing, resetting a password with it, lockout, and what is (not) stored."""
import html
import json
import re
from datetime import timedelta

import pytest

from app.services import recovery_service as rs
from app.utils import utcnow, iso
from tests.conftest import PASSWORD, login

PATTERN = re.compile(r"^[A-HJ-KM-NP-Z2-9]{4}-[A-HJ-KM-NP-Z2-9]{4}-[A-HJ-KM-NP-Z2-9]{4}$")
NEW_PW = "Brand-new-pw-123"


def reg(client, school, **over):
    data = {"username": "newkid", "display_name": "New Kid", "email": "new@example.com", "school_choice": school["name"],
            "password": PASSWORD, "password2": PASSWORD, "terms": "on", "age_ok": "on"}
    data.update(over)
    return client.post("/register", data=data)


def row(db, p):
    return next(r for r in db.data["recovery_codes"] if r["user_id"] == p["id"])


def prof(db, p):
    return next(r for r in db.data["profiles"] if r["id"] == p["id"])


def issue(app, p):
    with app.app_context():
        return rs.issue(p["id"])


def recover(client, identifier, code, pw=NEW_PW, pw2=None):
    return client.post("/recover", data={"identifier": identifier, "code": code, "password": pw, "password2": pw if pw2 is None else pw2})


def can_log_in(client, username, password):
    return client.post("/login", data={"identifier": username, "password": password}).status_code == 302


@pytest.fixture
def sent(monkeypatch):
    mails = []
    monkeypatch.setattr("app.blueprints.auth.routes.send_mail", lambda to, subject, body: mails.append((to, subject, body)))
    return mails


# ---------------------------------------------------------------- the code itself
def test_codes_are_simple_unique_and_free_of_lookalike_characters(app):
    codes = {rs.generate() for _ in range(300)}
    assert len(codes) == 300 and all(PATTERN.match(c) for c in codes)
    assert not any(ch in "".join(codes) for ch in "01ILO")


def test_input_is_forgiving_about_case_dashes_and_spaces(app):
    with app.app_context():
        code = "K7M2-9QXD-4TFH"
        for variant in ("k7m2-9qxd-4tfh", "K7M29QXD4TFH", " k7m2 9qxd 4tfh ", "K7M2–9QXD–4TFH"):
            assert rs.code_hash(variant) == rs.code_hash(code)
        assert rs.looks_valid(code) and not rs.looks_valid("K7M2-9QXD") and not rs.looks_valid("IIII-0000-OOOO")


def test_stored_values_never_contain_the_code(app, db, mk):
    p = mk("zoe")
    code = issue(app, p)
    stored = json.dumps(row(db, p))
    assert code not in stored and code.replace("-", "") not in stored
    assert row(db, p)["code_hash"] != code and row(db, p)["code_enc"] != code
    with app.app_context():
        assert rs.reveal(p["id"]) == code                                    # the owner can still get it back
    assert not any("recovery" in k and k != "recovery_saved_at" for k in prof(db, p))   # nothing secret on the profile row


def test_changing_the_server_key_degrades_gracefully(app, db, mk):
    p = mk("zoe"); issue(app, p)
    app.config["RECOVERY_CODE_KEY"] = "a-completely-different-key"
    with app.app_context():
        assert rs.reveal(p["id"]) is None                                    # can't decrypt: the student is offered a new code instead of an error


# ---------------------------------------------------------------- sign-up
def test_signup_creates_a_code_and_shows_it_once_before_the_pending_page(client, db, school, app):
    r = reg(client, school)
    assert r.status_code == 302 and r.headers["Location"].endswith("/recovery-code")
    me = db.data["profiles"][0]
    page = client.get("/recovery-code").get_data(as_text=True)
    shown = re.search(r"[A-HJ-KM-NP-Z2-9]{4}-[A-HJ-KM-NP-Z2-9]{4}-[A-HJ-KM-NP-Z2-9]{4}", page).group(0)
    with app.app_context():
        assert shown == rs.reveal(me["id"])
    assert "I&#39;ve saved my recovery code" in page or "I've saved my recovery code" in page      # the confirm step is on the page


def test_the_confirm_step_marks_it_saved_and_hides_the_code_afterwards(client, db, school, app):
    reg(client, school)
    me = db.data["profiles"][0]
    page = client.get("/recovery-code").get_data(as_text=True)
    assert re.search(r"[A-HJ-KM-NP-Z2-9]{4}-[A-HJ-KM-NP-Z2-9]{4}-[A-HJ-KM-NP-Z2-9]{4}", page)
    r = client.post("/recovery-code/saved", data={})                       # the box wasn't ticked
    assert r.status_code == 302 and not prof(db, me).get("recovery_saved_at")
    r = client.post("/recovery-code/saved", data={"saved": "on"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/pending") and prof(db, me)["recovery_saved_at"]
    again = client.get("/recovery-code").get_data(as_text=True)
    assert "Enter your password to see it" in again and not re.search(r"[A-HJ-KM-NP-Z2-9]{4}-[A-HJ-KM-NP-Z2-9]{4}-[A-HJ-KM-NP-Z2-9]{4}", again)


def test_signup_is_all_or_nothing_if_the_code_cannot_be_made(client, db, school, monkeypatch):
    monkeypatch.setattr(rs, "issue", lambda uid: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        reg(client, school)
    assert db.data.get("profiles", []) == [] and db.users == {}              # no half-created account is left behind


# ---------------------------------------------------------------- viewing, renewing
def test_revealing_needs_the_password_and_the_page_hides_the_code_otherwise(client, app, db, mk):
    p = mk("zoe"); code = issue(app, p); login(client, "zoe")
    page = client.get("/recovery-code").get_data(as_text=True)
    assert code not in page and "••••-••••-••••" in page
    bad = client.post("/recovery-code", data={"action": "reveal", "password": "wrong"})
    assert bad.status_code == 400 and code not in bad.get_data(as_text=True)
    good = client.post("/recovery-code", data={"action": "reveal", "password": PASSWORD})
    assert code in good.get_data(as_text=True)


def test_renewing_replaces_the_code_and_tells_the_student(client, app, db, mk):
    p = mk("zoe"); old = issue(app, p); login(client, "zoe")
    assert client.post("/recovery-code", data={"action": "renew", "password": "wrong"}).status_code == 400
    r = client.post("/recovery-code", data={"action": "renew", "password": PASSWORD})
    new = re.search(r"[A-HJ-KM-NP-Z2-9]{4}-[A-HJ-KM-NP-Z2-9]{4}-[A-HJ-KM-NP-Z2-9]{4}", r.get_data(as_text=True)).group(0)
    assert new != old and "old one no longer works" in r.get_data(as_text=True)
    with app.app_context():
        assert rs.check(p, old) == "bad" and rs.check(p, new) == "ok"
    assert any("recovery code was changed" in n["body"] for n in db.data["notifications"] if n["user_id"] == p["id"])
    assert prof(db, p)["recovery_saved_at"] is None                        # must confirm the new one


def test_a_student_with_no_code_can_create_one(client, app, db, mk):
    p = mk("zoe"); login(client, "zoe")                                    # an existing account from before this feature
    assert "Create my recovery code" in client.get("/recovery-code").get_data(as_text=True)
    r = client.post("/recovery-code", data={"action": "reveal", "password": PASSWORD})
    assert PATTERN.match(re.search(r"[A-HJ-KM-NP-Z2-9]{4}-[A-HJ-KM-NP-Z2-9]{4}-[A-HJ-KM-NP-Z2-9]{4}", r.get_data(as_text=True)).group(0))
    assert row(db, p)


def test_signed_out_visitors_are_sent_to_log_in(client):
    assert client.get("/recovery-code").status_code == 302 and "/login" in client.get("/recovery-code").headers["Location"]


# ---------------------------------------------------------------- recovering
def test_reset_with_the_code_changes_the_password_signs_out_devices_and_rotates_the_code(client, app, db, mk, sent):
    p = mk("zoe"); code = issue(app, p)
    epoch = prof(db, p).get("session_epoch", 0)
    r = recover(client, "zoe", code)
    assert r.status_code == 200 and "Password updated" in r.get_data(as_text=True)
    new = re.search(r"[A-HJ-KM-NP-Z2-9]{4}-[A-HJ-KM-NP-Z2-9]{4}-[A-HJ-KM-NP-Z2-9]{4}", r.get_data(as_text=True)).group(0)
    assert new != code
    assert prof(db, p)["session_epoch"] == epoch + 1
    assert can_log_in(client, "zoe", NEW_PW)
    client.post("/logout")
    assert not can_log_in(client, "zoe", PASSWORD)                          # the old password is gone
    client.post("/logout")
    assert recover(client, "zoe", code).status_code == 401                 # single use
    assert recover(client, "zoe", new, pw="Another-pw-456").status_code == 200
    assert any("recovery code" in n["body"] for n in db.data["notifications"] if n["user_id"] == p["id"])
    assert sent and "password was just reset" in sent[0][2] and any(a["action"] == "password_reset_with_recovery_code" for a in db.data.get("audit_logs", []))


def test_the_code_works_with_the_email_address_too(client, app, db, mk, sent):
    p = mk("zoe"); code = issue(app, p)
    email = db.users[p["id"]]["email"]
    assert recover(client, email, code.lower().replace("-", "")).status_code == 200


def test_wrong_code_and_unknown_user_look_identical(client, app, db, mk):
    p = mk("zoe"); issue(app, p)
    wrong = recover(client, "zoe", "AAAA-BBBB-CCCC")
    ghost = recover(client, "nobody", "AAAA-BBBB-CCCC")
    clean = lambda r: re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(r.get_data(as_text=True)))).strip()
    assert wrong.status_code == ghost.status_code == 401
    assert "don't match" in clean(wrong) and "don't match" in clean(ghost)
    assert not can_log_in(client, "zoe", NEW_PW)


def test_someone_elses_code_cannot_reset_my_account(client, app, db, mk):
    zoe, amy = mk("zoe"), mk("amy"); issue(app, zoe); amy_code = issue(app, amy)
    assert recover(client, "zoe", amy_code).status_code == 401
    assert row(db, zoe)["failed_attempts"] == 1
    assert not can_log_in(client, "zoe", NEW_PW)


def test_five_wrong_codes_lock_recovery_even_for_the_right_code(client, app, db, mk):
    p = mk("zoe"); code = issue(app, p)
    for _ in range(4):
        assert recover(client, "zoe", "AAAA-BBBB-CCCC").status_code == 401
    assert row(db, p)["failed_attempts"] == 4
    assert recover(client, "zoe", "AAAA-BBBB-CCCC").status_code == 401     # the 5th wrong try triggers the lock
    r = recover(client, "zoe", code)
    assert r.status_code == 429 and "paused" in r.get_data(as_text=True)
    assert not can_log_in(client, "zoe", NEW_PW)
    row(db, p)["locked_until"] = iso(utcnow() - timedelta(minutes=1))     # time passes
    assert recover(client, "zoe", code).status_code == 200


def test_a_correct_code_resets_the_failure_counter(client, app, db, mk):
    p = mk("zoe"); code = issue(app, p)
    for _ in range(3):
        recover(client, "zoe", "AAAA-BBBB-CCCC")
    with app.app_context():
        assert rs.check(p, code) == "ok"
    assert row(db, p)["failed_attempts"] == 0


def test_a_typo_in_the_new_password_does_not_burn_an_attempt(client, app, db, mk):
    p = mk("zoe"); code = issue(app, p)
    r = recover(client, "zoe", code, pw=NEW_PW, pw2="different")
    assert r.status_code == 400 and "match" in r.get_data(as_text=True) and row(db, p)["failed_attempts"] == 0
    assert recover(client, "zoe", code).status_code == 200                 # the same code still works


@pytest.mark.parametrize("status,expected", [("pending", 200), ("approved", 200), ("suspended", 401), ("rejected", 401)])
def test_only_active_or_pending_accounts_can_recover(client, app, db, mk, status, expected):
    p = mk("zoe"); code = issue(app, p)
    prof(db, p)["account_status"] = status
    assert recover(client, "zoe", code).status_code == expected


def test_signed_in_students_are_sent_to_settings_instead(client, mk):
    mk("zoe"); login(client, "zoe")
    assert client.get("/recover").status_code == 302


# ---------------------------------------------------------------- pages
def test_public_pages_render_and_link_to_each_other(client):
    assert "Use your recovery code" in client.get("/recover").get_data(as_text=True)
    assert "Have your recovery code?" in client.get("/forgot-password").get_data(as_text=True)
    assert "Use recovery code" in client.get("/login").get_data(as_text=True)


def test_settings_profile_and_banner_point_to_the_code(client, app, db, mk):
    p = mk("zoe"); issue(app, p); login(client, "zoe")
    assert "Account recovery" in client.get("/settings").get_data(as_text=True)
    assert "Recovery code" in client.get("/u/zoe").get_data(as_text=True)
    feed = client.get("/feed").get_data(as_text=True)
    assert "Save your recovery code." in feed and 'class="recovery-banner"' in feed
    client.post("/recovery-code/saved", data={"saved": "on"})
    assert 'class="recovery-banner"' not in client.get("/feed").get_data(as_text=True)
    assert 'class="recovery-banner"' not in client.get("/recovery-code").get_data(as_text=True)
