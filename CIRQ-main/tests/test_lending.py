"""Coin gifts and loans between friends: rules, automatic collection, late fee, garnishing, idempotency, pages."""
from datetime import timedelta

import pytest

from app.services import lending, coin_service
from app.services.lending import LendingError
from app.utils import utcnow, parse_dt
from tests.conftest import login


# ---------------------------------------------------------------- helpers
def prof(db, p):
    return next(r for r in db.data["profiles"] if r["id"] == p["id"])


def bal(db, p):
    return prof(db, p)["coin_balance"]


def give(db, p, n):
    prof(db, p)["coin_balance"] = n


def befriend(db, a, b):
    for x, y in ((a, b), (b, a)):
        db.table("follows").insert({"follower_id": x["id"], "followee_id": y["id"]}).execute()


@pytest.fixture
def pair(app, db, mk):
    """Two friends: Lena (lender, 500 coins) and Ben (borrower, 0 coins)."""
    lena, ben = mk("lena"), mk("ben")
    befriend(db, lena, ben)
    give(db, lena, 500)
    with app.app_context():
        yield lena, ben


def due_in(days):
    return lending.eat_date(utcnow()) + timedelta(days=days)


def make_active(db, lena, ben, amount=100, fee=20, days=5, ben_coins=None):
    loan = lending.offer_loan(prof(db, lena), prof(db, ben), amount, due_in(days), fee, "for the trip")
    loan = lending.accept_loan(loan["id"], prof(db, ben))
    if ben_coins is not None:
        give(db, ben, ben_coins)
    return loan


def after_due(loan):
    return parse_dt(loan["due_at"]) + timedelta(seconds=5)


def notes(db, user, kind=None):
    return [n for n in db.data.get("notifications", []) if n["user_id"] == user["id"] and (kind is None or n["kind"] == kind)]


def total_coins(db):
    return sum(p["coin_balance"] for p in db.data["profiles"])


# ---------------------------------------------------------------- gifts
def test_gift_moves_coins_and_notifies(db, pair):
    lena, ben = pair
    g = lending.send_gift(prof(db, lena), prof(db, ben), 50, "good luck")
    assert (bal(db, lena), bal(db, ben)) == (450, 50) and g["amount"] == 50
    assert [r["reason"] for r in db.data["coin_ledger"]] == ["gift_out", "gift_in"]
    assert "50 coins" in notes(db, ben, "gift_received")[0]["body"]


def test_gift_needs_mutual_follow(db, pair, mk):
    lena, _ = pair
    stranger = mk("sam")
    db.table("follows").insert({"follower_id": lena["id"], "followee_id": stranger["id"]}).execute()     # one-way is not enough
    with pytest.raises(LendingError, match="friends"):
        lending.send_gift(prof(db, lena), prof(db, stranger), 10)
    assert bal(db, lena) == 500


def test_gift_blocked_users_cannot_send(db, pair):
    lena, ben = pair
    db.table("blocks").insert({"blocker_id": ben["id"], "blocked_id": lena["id"]}).execute()
    with pytest.raises(LendingError):
        lending.send_gift(prof(db, lena), prof(db, ben), 10)


def test_gift_amount_and_funds_rules(db, pair):
    lena, ben = pair
    for bad in (0, -5, 1001, "abc"):
        with pytest.raises(LendingError):
            lending.send_gift(prof(db, lena), prof(db, ben), bad)
    give(db, lena, 20)
    with pytest.raises(LendingError, match="enough coins"):
        lending.send_gift(prof(db, lena), prof(db, ben), 50)
    assert (bal(db, lena), bal(db, ben), db.data.get("coin_gifts", [])) == (20, 0, [])      # a failed gift leaves no trace


def test_gift_daily_limits(app, db, pair):
    lena, ben = pair
    give(db, lena, 10_000)
    app.config["GIFT_DAILY_COUNT"] = 3
    for _ in range(3):
        lending.send_gift(prof(db, lena), prof(db, ben), 5)
    with pytest.raises(LendingError, match="limit"):
        lending.send_gift(prof(db, lena), prof(db, ben), 5)
    app.config["GIFT_DAILY_COUNT"] = 99
    lending.send_gift(prof(db, lena), prof(db, ben), 1000)           # 15 + 1000 is inside the 2000 daily coin cap
    with pytest.raises(LendingError, match="limit"):
        lending.send_gift(prof(db, lena), prof(db, ben), 1000)       # 1015 + 1000 is not


def test_overdue_borrower_cannot_gift(db, pair, mk):
    lena, ben = pair
    make_active(db, lena, ben, ben_coins=0)
    lending.run_due(after_due(db.data["coin_loans"][0]))
    assert db.data["coin_loans"][0]["status"] == "overdue"
    carl = mk("carl"); befriend(db, ben, carl); give(db, ben, 30)
    with pytest.raises(LendingError, match="overdue"):
        lending.send_gift(prof(db, ben), prof(db, carl), 5)


# ---------------------------------------------------------------- offering
def test_offer_creates_agreement_without_moving_coins(db, pair):
    lena, ben = pair
    loan = lending.offer_loan(prof(db, lena), prof(db, ben), 100, due_in(7), 20, "hello")
    assert loan["status"] == "offered" and (bal(db, lena), bal(db, ben)) == (500, 0)
    assert loan["terms"]["amount"] == 100 and len(loan["terms_hash"]) == 64
    assert "100 app coins" in loan["terms"]["clauses"][0] and "20%" in " ".join(loan["terms"]["clauses"])
    assert "50%" in " ".join(loan["terms"]["clauses"])                            # the garnish share is part of what both people agree to
    assert parse_dt(loan["due_at"]).astimezone(lending.EAT).strftime("%H:%M:%S") == "23:59:59"
    assert notes(db, ben, "loan_offer")


@pytest.mark.parametrize("kw,msg", [
    (dict(amount=5), "10 to"), (dict(amount=5000), "10 to"), (dict(fee=21), "0% to 20%"), (dict(fee=-1), "0% to 20%"),
    (dict(days=1), "between"), (dict(days=200), "between"), (dict(amount="x"), "whole number")])
def test_offer_validation(db, pair, kw, msg):
    lena, ben = pair
    args = dict(amount=100, fee=20, days=7); args.update(kw)
    with pytest.raises(LendingError, match=msg):
        lending.offer_loan(prof(db, lena), prof(db, ben), args["amount"], due_in(args["days"]), args["fee"])


def test_offer_requires_friends_and_funds(db, pair, mk):
    lena, ben = pair
    sam = mk("sam")
    with pytest.raises(LendingError, match="friends"):
        lending.offer_loan(prof(db, lena), prof(db, sam), 50, due_in(7), 20)
    give(db, lena, 40)
    with pytest.raises(LendingError, match="enough coins"):
        lending.offer_loan(prof(db, lena), prof(db, ben), 50, due_in(7), 20)


def test_one_open_loan_per_borrower_and_one_offer_per_pair(db, pair, mk):
    lena, ben = pair
    lending.offer_loan(prof(db, lena), prof(db, ben), 50, due_in(7), 20)
    with pytest.raises(LendingError, match="already have an open offer"):
        lending.offer_loan(prof(db, lena), prof(db, ben), 60, due_in(7), 20)
    other = mk("zed"); befriend(db, other, ben); give(db, other, 200)
    loan = db.data["coin_loans"][0]; lending.accept_loan(loan["id"], prof(db, ben))
    with pytest.raises(LendingError, match="already has a loan"):
        lending.offer_loan(prof(db, other), prof(db, ben), 60, due_in(7), 20)


# ---------------------------------------------------------------- answering
def test_accept_moves_coins_and_records_the_signature(db, pair):
    lena, ben = pair
    loan = make_active(db, lena, ben)
    assert loan["status"] == "active" and (bal(db, lena), bal(db, ben)) == (400, 100)
    kinds = [e["kind"] for e in db.data["coin_loan_events"]]
    assert kinds == ["offered", "accepted"]
    accepted = db.data["coin_loan_events"][1]
    assert accepted["note"] == loan["terms_hash"] and accepted["actor_id"] == ben["id"]       # the "signature" ties Ben to the exact terms
    assert notes(db, lena, "loan_accepted")


def test_double_accept_is_harmless(db, pair):
    lena, ben = pair
    loan = make_active(db, lena, ben)
    with pytest.raises(LendingError, match="already agreed"):
        lending.accept_loan(loan["id"], prof(db, ben))
    assert (bal(db, lena), bal(db, ben)) == (400, 100)


def test_only_the_borrower_can_accept(db, pair):
    lena, ben = pair
    loan = lending.offer_loan(prof(db, lena), prof(db, ben), 100, due_in(7), 20)
    with pytest.raises(LendingError, match="borrower"):
        lending.accept_loan(loan["id"], prof(db, lena))


def test_accept_fails_cleanly_if_lender_spent_the_coins(db, pair):
    lena, ben = pair
    loan = lending.offer_loan(prof(db, lena), prof(db, ben), 100, due_in(7), 20)
    give(db, lena, 30)
    with pytest.raises(LendingError, match="lender doesn't have enough"):
        lending.accept_loan(loan["id"], prof(db, ben))
    assert db.data["coin_loans"][0]["status"] == "offered" and bal(db, ben) == 0


def test_decline_cancel_and_expiry(db, pair, mk):
    lena, ben = pair
    a = lending.offer_loan(prof(db, lena), prof(db, ben), 50, due_in(7), 20)
    assert lending.decline_loan(a["id"], prof(db, ben))["status"] == "declined" and notes(db, lena, "loan_declined")
    b = lending.offer_loan(prof(db, lena), prof(db, ben), 50, due_in(7), 20)
    assert lending.cancel_loan(b["id"], prof(db, lena))["status"] == "cancelled" and notes(db, ben, "loan_cancelled")
    c = lending.offer_loan(prof(db, lena), prof(db, ben), 50, due_in(7), 20)
    next(r for r in db.data["coin_loans"] if r["id"] == c["id"])["expires_at"] = (utcnow() - timedelta(minutes=1)).isoformat()
    with pytest.raises(LendingError, match="no longer available"):
        lending.accept_loan(c["id"], prof(db, ben))
    assert next(r for r in db.data["coin_loans"] if r["id"] == c["id"])["status"] == "expired"
    assert (bal(db, lena), bal(db, ben)) == (500, 0)               # none of these ever moved a coin


def test_scheduled_run_expires_stale_offers_for_both_people(db, pair):
    lena, ben = pair
    loan = lending.offer_loan(prof(db, lena), prof(db, ben), 50, due_in(7), 20)
    db.data["coin_loans"][0]["expires_at"] = (utcnow() - timedelta(hours=1)).isoformat()
    assert lending.run_due()["expired"] == 1 and lending.run_due()["expired"] == 0
    assert notes(db, lena, "loan_expired") and notes(db, ben, "loan_expired")


# ---------------------------------------------------------------- the due date
def test_due_date_collects_in_full_and_pays_the_lender(db, pair):
    lena, ben = pair
    loan = make_active(db, lena, ben, ben_coins=130)
    before = total_coins(db)
    assert lending.run_due(after_due(loan) - timedelta(hours=2))["collected"] == 0     # not due yet
    assert lending.run_due(after_due(loan))["collected"] == 1
    row = db.data["coin_loans"][0]
    assert row["status"] == "repaid" and row["repaid_total"] == 100 and row["fee_amount"] == 0
    assert (bal(db, lena), bal(db, ben)) == (500, 30) and total_coins(db) == before   # coins are conserved
    assert notes(db, lena, "loan_repaid") and notes(db, ben, "loan_repaid")


def test_due_date_partial_payment_adds_one_time_fee_on_the_unpaid_part(db, pair):
    lena, ben = pair
    loan = make_active(db, lena, ben, ben_coins=60)
    lending.run_due(after_due(loan))
    row = db.data["coin_loans"][0]
    assert row["status"] == "overdue" and row["repaid_total"] == 60
    assert row["fee_amount"] == 8                                        # 20% of the 40 still unpaid, not of the 100
    assert lending.remaining(row) == 48 and (bal(db, lena), bal(db, ben)) == (460, 0)
    assert "fee_applied" in [e["kind"] for e in db.data["coin_loan_events"]]
    body = notes(db, lena, "loan_overdue")[0]["body"]
    assert "60 coins were collected" in body and "48 coins are still owed" in body
    assert "50%" in notes(db, ben, "loan_overdue")[0]["body"]


def test_due_date_with_no_coins_at_all(db, pair):
    lena, ben = pair
    loan = make_active(db, lena, ben, ben_coins=0)
    lending.run_due(after_due(loan))
    row = db.data["coin_loans"][0]
    assert row["status"] == "overdue" and row["repaid_total"] == 0 and row["fee_amount"] == 20 and lending.remaining(row) == 120


def test_zero_percent_fee_means_no_fee(db, pair):
    lena, ben = pair
    loan = make_active(db, lena, ben, fee=0, ben_coins=0)
    lending.run_due(after_due(loan))
    assert db.data["coin_loans"][0]["fee_amount"] == 0 and lending.remaining(db.data["coin_loans"][0]) == 100


def test_running_the_job_again_never_charges_twice(db, pair):
    lena, ben = pair
    loan = make_active(db, lena, ben, ben_coins=60)
    when = after_due(loan)
    for _ in range(4):
        lending.run_due(when)
    row = db.data["coin_loans"][0]
    assert (row["repaid_total"], row["fee_amount"], bal(db, lena), bal(db, ben)) == (60, 8, 460, 0)
    assert len([e for e in db.data["coin_loan_events"] if e["kind"] == "fee_applied"]) == 1
    assert len([r for r in db.data["coin_ledger"] if r["reason"] == "loan_repay_out"]) == 1


def test_a_crash_between_the_transfer_and_the_record_is_repaired_not_repeated(db, pair):
    lena, ben = pair
    loan = make_active(db, lena, ben, ben_coins=100)
    # simulate: the coins moved, then the worker died before writing the payment event
    assert lending.transfer(ben["id"], lena["id"], 100, "loan_repay_out", "loan_repay_in", f"{loan['id']}:due") >= 0
    assert db.data["coin_loans"][0]["repaid_total"] == 0
    lending.run_due(after_due(loan))
    row = db.data["coin_loans"][0]
    assert row["status"] == "repaid" and row["repaid_total"] == 100
    assert (bal(db, lena), bal(db, ben)) == (500, 0)                    # one payment of 100, not two


def test_frozen_loans_are_not_collected_until_unfrozen(db, pair):
    lena, ben = pair
    loan = make_active(db, lena, ben, ben_coins=100)
    lending.set_frozen(loan["id"], True, lena["id"])
    lending.run_due(after_due(loan))
    assert db.data["coin_loans"][0]["status"] == "active" and bal(db, ben) == 100
    lending.set_frozen(loan["id"], False, lena["id"])
    lending.run_due(after_due(loan))
    assert db.data["coin_loans"][0]["status"] == "repaid"


# ---------------------------------------------------------------- garnishing
def overdue_loan(db, lena, ben, coins_at_due=60):
    loan = make_active(db, lena, ben, ben_coins=coins_at_due)
    lending.run_due(after_due(loan))
    return loan["id"]


def test_half_of_every_earning_goes_to_the_lender_until_clear(db, pair):
    lena, ben = pair
    overdue_loan(db, lena, ben)                       # 48 still owed
    lena_before = bal(db, lena)
    coin_service.award(ben["id"], 40, "trivia", "round-1")
    assert (bal(db, ben), bal(db, lena)) == (20, lena_before + 20)         # 50% of 40
    row = db.data["coin_loans"][0]
    assert row["repaid_total"] == 80 and lending.remaining(row) == 28
    coin_service.award(ben["id"], 100, "dev_card", "round-2")              # 50 would be offered, but only 28 is owed
    row = db.data["coin_loans"][0]
    assert row["status"] == "repaid" and lending.remaining(row) == 0
    assert bal(db, ben) == 20 + 100 - 28 and bal(db, lena) == lena_before + 20 + 28
    assert notes(db, lena, "loan_collected") and notes(db, lena, "loan_repaid")
    coin_service.award(ben["id"], 100, "trivia", "round-3")                # debt is clear: nothing more is taken
    assert bal(db, ben) == 20 + 100 - 28 + 100


def test_awarding_the_same_reward_twice_garnishes_once(db, pair):
    lena, ben = pair
    overdue_loan(db, lena, ben)
    coin_service.award(ben["id"], 20, "trivia", "same")
    assert coin_service.award(ben["id"], 20, "trivia", "same") == coin_service.DUPLICATE
    assert db.data["coin_loans"][0]["repaid_total"] == 70                  # 60 at due + 10 (half of one 20-coin reward)


def test_garnish_ignores_tiny_credits_gifts_loans_and_admin_adjustments(db, pair, mk):
    lena, ben = pair
    overdue_loan(db, lena, ben)
    coin_service.award(ben["id"], 1, "trivia", "tiny")                     # half of 1 rounds down to nothing
    coin_service.award(ben["id"], 100, "admin_adjust", "fix-1")
    assert db.data["coin_loans"][0]["repaid_total"] == 60 and bal(db, ben) == 101
    carl = mk("carl"); befriend(db, carl, ben); give(db, carl, 100)
    lending.send_gift(prof(db, carl), prof(db, ben), 50)                    # a friend's gift reaches Ben whole
    assert bal(db, ben) == 151 and db.data["coin_loans"][0]["repaid_total"] == 60


def test_garnish_never_blocks_a_reward_if_lending_breaks(db, pair, monkeypatch):
    lena, ben = pair
    overdue_loan(db, lena, ben)
    monkeypatch.setattr(lending, "garnish", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert coin_service.award(ben["id"], 40, "trivia", "x") == 40


def test_frozen_loans_are_not_garnished(db, pair):
    lena, ben = pair
    lid = overdue_loan(db, lena, ben)
    lending.set_frozen(lid, True, lena["id"])
    coin_service.award(ben["id"], 40, "trivia", "x")
    assert bal(db, ben) == 40


# ---------------------------------------------------------------- repaying and forgiving
def test_early_repayment_partial_then_full_without_any_fee(db, pair):
    lena, ben = pair
    loan = make_active(db, lena, ben, ben_coins=80)
    row, moved = lending.repay_loan(loan["id"], prof(db, ben), 30, "t1")
    assert moved == 30 and lending.remaining(row) == 70 and notes(db, lena, "loan_collected")
    give(db, ben, 500)
    row, moved = lending.repay_loan(loan["id"], prof(db, ben), None, "t2")             # None = pay as much as owed
    assert moved == 70 and row["status"] == "repaid" and row["fee_amount"] == 0
    lending.run_due(after_due(loan))                                                   # the due date has nothing left to collect
    assert db.data["coin_loans"][0]["repaid_total"] == 100 and bal(db, lena) == 500


def test_repay_is_capped_by_balance_and_ignores_a_resent_form(db, pair):
    lena, ben = pair
    loan = make_active(db, lena, ben, ben_coins=25)
    row, moved = lending.repay_loan(loan["id"], prof(db, ben), 99, "same")
    assert moved == 25
    give(db, ben, 50)
    _, again = lending.repay_loan(loan["id"], prof(db, ben), 10, "same")
    assert again == 0 and db.data["coin_loans"][0]["repaid_total"] == 25                # same token: recorded once
    with pytest.raises(LendingError, match="don't have any coins"):
        give(db, ben, 0); lending.repay_loan(loan["id"], prof(db, ben), 5, "new")


def test_forgive_closes_the_loan_without_moving_coins(db, pair):
    lena, ben = pair
    lid = overdue_loan(db, lena, ben)
    before = (bal(db, lena), bal(db, ben))
    row = lending.forgive_loan(lid, prof(db, lena))
    assert row["status"] == "forgiven" and row["forgiven_amount"] == 48 and (bal(db, lena), bal(db, ben)) == before
    assert notes(db, ben, "loan_forgiven")
    coin_service.award(ben["id"], 40, "trivia", "after")
    assert bal(db, ben) == 40                                                             # nothing is taken once forgiven
    with pytest.raises(LendingError):
        lending.forgive_loan(lid, prof(db, lena))
    with pytest.raises(LendingError, match="lender"):
        lending.forgive_loan(lid, prof(db, ben))


# ---------------------------------------------------------------- reminders
def test_reminders_are_sent_once_each_at_3_days_1_day_and_on_the_day(db, pair):
    lena, ben = pair
    loan = make_active(db, lena, ben, days=10, ben_coins=100)
    start = lending.due_day_start(parse_dt(loan["due_at"]))
    for when, expected in ((start - timedelta(days=3) - timedelta(hours=1), 0), (start - timedelta(days=3) + timedelta(hours=1), 1),
                           (start - timedelta(days=3) + timedelta(hours=5), 1), (start - timedelta(days=1) + timedelta(hours=1), 2), (start + timedelta(hours=1), 3)):
        lending.run_due(when)
        assert len(notes(db, ben, "loan_reminder")) == expected
    lending.run_due(start + timedelta(hours=2))
    assert len(notes(db, ben, "loan_reminder")) == 3


def test_a_late_scheduler_sends_only_the_latest_reminder(db, pair):
    lena, ben = pair
    loan = make_active(db, lena, ben, days=10, ben_coins=100)
    lending.run_due(lending.due_day_start(parse_dt(loan["due_at"])) + timedelta(hours=2))
    assert [n["body"] for n in notes(db, ben, "loan_reminder")] == [notes(db, ben, "loan_reminder")[0]["body"]] and "due today" in notes(db, ben, "loan_reminder")[0]["body"]


# ---------------------------------------------------------------- conservation
def test_coins_are_never_created_or_destroyed_by_lending(db, pair, mk):
    lena, ben = pair
    carl = mk("carl"); befriend(db, carl, lena); give(db, carl, 77)
    lending.send_gift(prof(db, carl), prof(db, lena), 10)
    loan = make_active(db, lena, ben, ben_coins=40)       # (the helper sets Ben's balance: count coins from here on)
    total = total_coins(db)
    lending.run_due(after_due(loan))
    for i in range(3):
        coin_service.apply(ben["id"], 30, "trivia", f"r{i}")
        total += 30                                            # earnings are genuinely new coins; everything else must net to zero
    lending.forgive_loan(loan["id"], prof(db, lena))
    assert total_coins(db) == total
    assert sum(r["amount"] for r in db.data["coin_ledger"] if r["reason"].startswith(("loan_", "gift_"))) == 0


# ---------------------------------------------------------------- pages and routes
def test_profile_shows_send_coins_only_for_friends(client, db, pair, mk):
    lena, ben = pair
    sam = mk("sam")
    login(client, "lena")
    assert "Send coins to @ben" in client.get("/u/ben").get_data(as_text=True)
    assert "Send coins to @sam" not in client.get("/u/sam").get_data(as_text=True)
    assert client.get("/coins/send/sam").status_code == 302


def test_send_forms_render_for_gift_and_loan(client, pair):
    login(client, "lena")
    gift = client.get("/coins/send/ben").get_data(as_text=True)
    assert "Send gift" in gift and 'name="amount"' in gift
    loan = client.get("/coins/send/ben?kind=loan").get_data(as_text=True)
    assert "Send loan offer" in loan and 'name="due_date"' in loan and 'name="fee_pct"' in loan and "What you are agreeing to" in loan


def _token(client, url):
    html = client.get(url).get_data(as_text=True)
    return html.split('name="token" value="')[1].split('"')[0]


def test_full_flow_through_the_web_pages(client, app, db, pair):
    lena, ben = pair
    login(client, "lena")
    tok = _token(client, "/coins/send/ben?kind=loan")
    r = client.post("/coins/send/ben", data={"kind": "loan", "token": tok, "amount": "120", "due_date": due_in(6).isoformat(), "fee_pct": "15", "message": "thanks!"})
    assert r.status_code == 302 and "/coins/loans/" in r.headers["Location"]
    loan_id = db.data["coin_loans"][0]["id"]
    page = client.get(r.headers["Location"]).get_data(as_text=True)
    assert "Coin loan agreement" in page and "Withdraw offer" in page and "Waiting" in page
    assert client.post("/coins/send/ben", data={"kind": "loan", "token": tok, "amount": "120"}).status_code == 302      # a re-sent form is ignored
    assert len(db.data["coin_loans"]) == 1

    client.post("/logout"); login(client, "ben")
    hub = client.get("/coins/lending").get_data(as_text=True)
    assert "Waiting for your answer" in hub and "You borrowed 120 coins" in hub
    page = client.get(f"/coins/loans/{loan_id}").get_data(as_text=True)
    assert "I agree" in page and "Decline" in page
    r = client.post(f"/coins/loans/{loan_id}/accept")
    assert r.status_code == 302 and bal(db, ben) == 120
    page = client.get(f"/coins/loans/{loan_id}").get_data(as_text=True)
    assert "You still owe" in page and "Pay back now" in page and "Agreed by" in page
    client.post(f"/coins/loans/{loan_id}/repay", data={"amount": "20", "token": "abc"})
    assert db.data["coin_loans"][0]["repaid_total"] == 20
    assert "100" in client.get("/coins/lending").get_data(as_text=True)

    client.post("/logout"); login(client, "lena")
    assert "Forgive 100 coins" in client.get(f"/coins/loans/{loan_id}").get_data(as_text=True)
    assert "Gifts" in client.get("/coins").get_data(as_text=True)


def test_gift_via_the_web_form_and_notification_link(client, db, pair):
    lena, ben = pair
    login(client, "lena")
    tok = _token(client, "/coins/send/ben")
    r = client.post("/coins/send/ben", data={"kind": "gift", "token": tok, "amount": "35", "message": "lunch on me"})
    assert r.status_code == 302 and (bal(db, lena), bal(db, ben)) == (465, 35)
    client.post("/logout"); login(client, "ben")
    n = notes(db, ben, "gift_received")[0]
    assert client.get(f"/notifications/{n['id']}/open").headers["Location"].endswith("/coins/lending")
    assert "Gift from" in client.get("/coins/lending").get_data(as_text=True)


def test_strangers_cannot_open_a_loan_or_act_on_it(client, db, pair, mk):
    lena, ben = pair
    loan = lending.offer_loan(prof(db, lena), prof(db, ben), 50, due_in(7), 20)
    mk("eve"); login(client, "eve")
    assert client.get(f"/coins/loans/{loan['id']}").status_code == 404
    client.post(f"/coins/loans/{loan['id']}/accept"); client.post(f"/coins/loans/{loan['id']}/forgive")
    assert db.data["coin_loans"][0]["status"] == "offered" and bal(db, lena) == 500


def test_message_is_moderated(client, db, pair):
    login(client, "lena")
    tok = _token(client, "/coins/send/ben")
    r = client.post("/coins/send/ben", data={"kind": "gift", "token": tok, "amount": "5", "message": "call me 0772123456"})
    assert r.status_code == 200 and bal(db, pair[0]) == 500


def test_lending_can_be_switched_off(client, app, db, pair):
    app.config["LENDING_ENABLED"] = False
    login(client, "lena")
    assert "Send coins to @ben" not in client.get("/u/ben").get_data(as_text=True)
    with pytest.raises(LendingError):
        with app.app_context():
            lending.send_gift(prof(db, pair[0]), prof(db, pair[1]), 5)


# ---------------------------------------------------------------- admin + scheduler endpoint
def test_cron_endpoint_requires_the_secret_and_runs_the_job(client, app, db, pair):
    lena, ben = pair
    app.config["CRON_SECRET"] = "cron-secret"
    make_active(db, lena, ben, ben_coins=100)
    assert client.post("/admin/internal/lending-run").status_code == 403
    assert client.post("/admin/internal/lending-run", headers={"Authorization": "Bearer nope"}).status_code == 403
    r = client.post("/admin/internal/lending-run", headers={"Authorization": "Bearer cron-secret"})
    assert r.status_code == 200 and set(r.get_json()["result"]) == {"expired", "reminders", "collected", "overdue"}


def test_admin_can_list_and_freeze_loans(client, db, pair, mk):
    lena, ben = pair
    loan = make_active(db, lena, ben)
    mk("boss", role="admin"); login(client, "boss")
    page = client.get("/admin/loans").get_data(as_text=True)
    assert "Loans" in page and "Freeze" in page
    client.post(f"/admin/loans/{loan['id']}/freeze", data={"frozen": "1"})
    assert db.data["coin_loans"][0]["frozen"] is True
    assert any(a["action"] == "loan_frozen" for a in db.data.get("audit_logs", []))
    client.post(f"/admin/loans/{loan['id']}/freeze", data={"frozen": "0"})
    assert db.data["coin_loans"][0]["frozen"] is False
    client.post("/logout"); login(client, "lena")
    assert client.get("/admin/loans").status_code in (302, 403, 404)
