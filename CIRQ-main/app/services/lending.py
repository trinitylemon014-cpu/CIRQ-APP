"""Coin gifts and loans between friends.

GIFTS  Instant and final. Friends (mutual followers) only, with daily limits.

LOANS  A two-party agreement.
  offered  The lender sets amount, due date and late fee. The borrower sees a plain summary and taps "I agree".
           Nothing moves until then. Either side can cancel/decline; an unanswered offer lapses after 48 hours.
  active   Coins moved lender -> borrower on acceptance. Reminders go out 3 days before, 1 day before and on the day.
  at the end of the due day (Kampala time) the app takes what the borrower has, up to what is owed, and pays the lender.
  repaid   Fully paid (on time, early, or after a late period).
  overdue  The borrower couldn't pay everything. A one-time late fee (the lender's % of the UNPAID part, never compounded)
           is added, and from then on LOAN_GARNISH_PCT of every coin the borrower earns goes to the lender until it is clear.
  Closed   repaid | declined | cancelled | expired | forgiven.

SAFETY  Every coin movement is one atomic database call (cirq_transfer_coins) keyed by a unique `ref`, and every loan event
is keyed by (loan_id, ref), so a retried cron run, a double click or a crash half-way can never charge anyone twice.
The loan's repaid_total is always recomputed from the payment events, never incremented blindly.
"""
import hashlib
import json
import math
from datetime import datetime, timedelta, timezone

from flask import current_app

from app.services import social, supabase_service as sb
from app.services.notification_service import notify
from app.utils import utcnow, parse_dt, iso

EAT = timezone(timedelta(hours=3))          # Kampala time. East Africa has no daylight saving.
DUPLICATE, INSUFFICIENT, INVALID = -1, -2, -3
OPEN = ("active", "overdue")
CLOSED = ("repaid", "declined", "cancelled", "expired", "forgiven")
STATUS_LABEL = {"offered": "Waiting for answer", "active": "Active", "overdue": "Overdue", "repaid": "Repaid",
                "declined": "Declined", "cancelled": "Cancelled", "expired": "Expired", "forgiven": "Forgiven"}


class LendingError(Exception):
    """A problem that is safe to show to the student as-is."""


def cfg(key):
    return current_app.config[key]


# ------------------------------------------------------------------ small helpers
def eat_date(dt):
    return dt.astimezone(EAT).date()


def end_of_day(d):
    """End of calendar day `d` in Kampala, as an aware UTC datetime."""
    return datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=EAT).astimezone(timezone.utc)


def due_day_start(due_at):
    d = eat_date(due_at)
    return datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=EAT).astimezone(timezone.utc)


def due_label(due_at):
    return eat_date(due_at).strftime("%a %d %b %Y")


def transfer(sender, receiver, amount, reason_out, reason_in, ref):
    res = sb.db().rpc("cirq_transfer_coins", {"p_from": sender, "p_to": receiver, "p_amount": int(amount), "p_reason_out": reason_out,
                                              "p_reason_in": reason_in, "p_ref": str(ref)}).execute()
    return int(res.data)


def ledger_amount(user_id, reason, ref):
    row = sb.get("coin_ledger", user_id=user_id, reason=reason, ref=str(ref))
    return abs(int(row["amount"])) if row else 0


def balance(user_id):
    return (sb.get("profiles", "coin_balance", id=user_id) or {}).get("coin_balance", 0)


def remaining(loan):
    """Coins still owed: principal + the late fee (once it exists) - what has been paid."""
    return max(0, int(loan["amount"]) + int(loan.get("fee_amount") or 0) - int(loan.get("repaid_total") or 0))


def late_fee_for(unpaid, fee_pct):
    return 0 if unpaid <= 0 or fee_pct <= 0 else max(1, math.ceil(unpaid * fee_pct / 100))


def are_friends(a, b):
    return social.is_following(a, b) and social.is_following(b, a) and not social.is_blocked(a, b)


def friends_of(user_id):
    mine = set(social.following_ids(user_id))
    back = {r["follower_id"] for r in sb.select("follows", "follower_id", followee_id=user_id)}
    ids = (mine & back) - social.blocked_between(user_id)
    rows = [p for p in sb.in_("profiles", "id", list(ids)) if p.get("account_status") == "approved"]
    return sorted(rows, key=lambda p: (p.get("display_name") or p["username"]).lower())


def can_send_to(me, other):
    """Is the 'Send coins' button available between these two students?"""
    return bool(cfg("LENDING_ENABLED") and me["id"] != other["id"] and other.get("account_status") == "approved"
                and me.get("account_status") == "approved" and are_friends(me["id"], other["id"]))


def _name(p):
    return p.get("display_name") or p["username"]


def _event(loan_id, kind, amount=0, actor_id=None, note=None, ref=None):
    """Record a history event once. Returns False if this ref was already recorded."""
    ref = ref or kind
    if sb.get("coin_loan_events", loan_id=loan_id, ref=ref):
        return False
    try:
        sb.insert("coin_loan_events", {"loan_id": loan_id, "kind": kind, "amount": int(amount), "actor_id": actor_id, "note": note, "ref": ref})
    except Exception:       # a concurrent worker recorded it first (unique key): that is the idempotency guarantee working
        return False
    return True


def _loan(loan_id):
    return sb.get("coin_loans", id=loan_id)


def _profiles(*ids):
    return sb.by_id("profiles", list(ids))


# ------------------------------------------------------------------ gifts
def send_gift(sender, recipient, amount, message=""):
    if not cfg("LENDING_ENABLED"):
        raise LendingError("Sending coins is switched off right now.")
    amount = _int(amount, "gift amount")
    if not 1 <= amount <= cfg("GIFT_MAX_COINS"):
        raise LendingError(f"A gift can be 1 to {cfg('GIFT_MAX_COINS')} coins.")
    if not can_send_to(sender, recipient):
        raise LendingError("You can only send coins to friends: people you follow who also follow you back.")
    if sb.count("coin_loans", borrower_id=sender["id"], status="overdue"):
        raise LendingError("Finish paying back your overdue loan before gifting coins.")
    since = utcnow() - timedelta(hours=24)
    recent = [g for g in sb.select("coin_gifts", sender_id=sender["id"], limit=200) if (parse_dt(g["created_at"]) or since) >= since]
    if len(recent) >= cfg("GIFT_DAILY_COUNT") or sum(g["amount"] for g in recent) + amount > cfg("GIFT_DAILY_COINS"):
        raise LendingError(f"Gift limit reached: up to {cfg('GIFT_DAILY_COINS')} coins and {cfg('GIFT_DAILY_COUNT')} gifts in 24 hours. Try again later.")
    message = (message or "").strip()[:200]
    gift = sb.insert("coin_gifts", {"sender_id": sender["id"], "recipient_id": recipient["id"], "amount": amount, "message": message or None})
    code = transfer(sender["id"], recipient["id"], amount, "gift_out", "gift_in", gift["id"])
    if code < 0:
        sb.delete("coin_gifts", id=gift["id"])
        raise LendingError("You don't have enough coins for that gift." if code == INSUFFICIENT else "That gift couldn't be sent.")
    note = f"sent you {amount} coins" + (f": “{message}”" if message else "")
    notify(recipient["id"], "gift_received", actor_id=sender["id"], ref_type="gift", ref_id=gift["id"], body=note, dedupe=False)
    return gift


# ------------------------------------------------------------------ the agreement
def build_terms(lender, borrower, amount, fee_pct, due_at, message):
    L, B = _name(lender), _name(borrower)
    clauses = [
        f"{L} (@{lender['username']}) lends {B} (@{borrower['username']}) {amount} app coins. The coins move only when {B} taps “I agree”.",
        f"{B} promises to pay back all {amount} coins by the end of {due_label(due_at)} (Kampala time).",
        f"On that day CIRQ automatically takes the coins from {B}'s balance and pays them to {L}. Neither of you needs to do anything.",
    ]
    if fee_pct > 0:
        clauses.append(f"If {B} doesn't have enough coins on the due date, CIRQ takes whatever is available. A one-time late fee of {fee_pct}% is added "
                       f"to the part still unpaid and is paid to {L}. The fee is charged once and never grows.")
    else:
        clauses.append("There is no late fee on this loan.")
    clauses.append(f"Until the debt is fully paid, {cfg('LOAN_GARNISH_PCT')}% of every coin {B} earns in CIRQ goes automatically to {L}. {B} keeps the rest.")
    clauses.append(f"{B} may pay back early at any time. {L} may forgive the debt at any time. Neither of you can change these terms after signing.")
    terms = {"version": 1, "lender": {"id": lender["id"], "username": lender["username"], "name": L},
             "borrower": {"id": borrower["id"], "username": borrower["username"], "name": B},
             "amount": amount, "fee_pct": fee_pct, "due_at": iso(due_at), "due_label": due_label(due_at),
             "garnish_pct": cfg("LOAN_GARNISH_PCT"), "message": message or "", "clauses": clauses}
    return terms, hashlib.sha256(json.dumps(terms, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


# ------------------------------------------------------------------ offering, answering
def offer_loan(lender, borrower, amount, due_date, fee_pct, message=""):
    if not cfg("LENDING_ENABLED"):
        raise LendingError("Lending is switched off right now.")
    amount = _int(amount, "loan amount")
    fee_pct = _int(fee_pct, "late fee")
    if not cfg("LOAN_MIN_COINS") <= amount <= cfg("LOAN_MAX_COINS"):
        raise LendingError(f"A loan can be {cfg('LOAN_MIN_COINS')} to {cfg('LOAN_MAX_COINS')} coins.")
    if not 0 <= fee_pct <= cfg("LOAN_MAX_FEE_PCT"):
        raise LendingError(f"The late fee can be 0% to {cfg('LOAN_MAX_FEE_PCT')}%.")
    if not can_send_to(lender, borrower):
        raise LendingError("You can only lend to friends: people you follow who also follow you back.")
    today = eat_date(utcnow())
    if not (today + timedelta(days=cfg("LOAN_MIN_DAYS")) <= due_date <= today + timedelta(days=cfg("LOAN_MAX_DAYS"))):
        raise LendingError(f"Pick a due date between {cfg('LOAN_MIN_DAYS')} and {cfg('LOAN_MAX_DAYS')} days from today.")
    if balance(lender["id"]) < amount:
        raise LendingError("You don't have enough coins to lend that much.")
    if sb.count("coin_loans", borrower_id=lender["id"], status="overdue"):
        raise LendingError("Finish paying back your overdue loan before lending coins.")
    if any(sb.count("coin_loans", borrower_id=borrower["id"], status=s) for s in OPEN):
        raise LendingError(f"{_name(borrower)} already has a loan to pay back. One at a time keeps things fair.")
    pending = [l for l in sb.select("coin_loans", lender_id=lender["id"], status="offered", limit=50) if (parse_dt(l["expires_at"]) or utcnow()) > utcnow()]
    if any(l["borrower_id"] == borrower["id"] for l in pending):
        raise LendingError(f"You already have an open offer waiting for {_name(borrower)}.")
    since = utcnow() - timedelta(hours=24)
    todays = [l for l in sb.select("coin_loans", lender_id=lender["id"], limit=100) if (parse_dt(l["created_at"]) or since) >= since]
    if len(todays) >= cfg("LOAN_OFFERS_PER_DAY"):
        raise LendingError(f"You can make up to {cfg('LOAN_OFFERS_PER_DAY')} loan offers a day.")
    message = (message or "").strip()[:200]
    due_at = end_of_day(due_date)
    terms, digest = build_terms(lender, borrower, amount, fee_pct, due_at, message)
    loan = sb.insert("coin_loans", {"lender_id": lender["id"], "borrower_id": borrower["id"], "amount": amount, "fee_pct": fee_pct, "due_at": iso(due_at),
                                    "message": message or None, "status": "offered", "expires_at": iso(utcnow() + timedelta(hours=cfg("LOAN_OFFER_HOURS"))),
                                    "terms": terms, "terms_hash": digest})
    _event(loan["id"], "offered", amount, lender["id"], ref="offered")
    notify(borrower["id"], "loan_offer", actor_id=lender["id"], ref_type="loan", ref_id=loan["id"],
           body=f"offered to lend you {amount} coins, due {due_label(due_at)}. Review and agree within {cfg('LOAN_OFFER_HOURS')} hours.", dedupe=False)
    return loan


def _close_offer(loan, status, kind, actor_id, note=None):
    """offered -> a final state, exactly once."""
    done = sb.update("coin_loans", {"status": status, "closed_at": iso()}, id=loan["id"], status="offered")
    if done:
        _event(loan["id"], kind, 0, actor_id, note, ref=kind)
    return bool(done)


def expire_if_needed(loan):
    if loan["status"] == "offered" and (parse_dt(loan["expires_at"]) or utcnow()) <= utcnow():
        if _close_offer(loan, "expired", "expired", None):
            for uid, who in ((loan["lender_id"], "Your"), (loan["borrower_id"], "The")):
                notify(uid, "loan_expired", ref_type="loan", ref_id=loan["id"],
                       body=f"{who} loan offer of {loan['amount']} coins expired because it wasn't answered in time.", dedupe=False)
        return _loan(loan["id"])
    return loan


def accept_loan(loan_id, user):
    loan = _must_loan(loan_id, user, role="borrower")
    loan = expire_if_needed(loan)
    if loan["status"] != "offered":
        raise LendingError("This offer is no longer available." if loan["status"] != "active" else "You already agreed to this loan.")
    due_at = parse_dt(loan["due_at"])
    if due_at - utcnow() < timedelta(hours=24):
        _close_offer(loan, "expired", "expired", None, "due date too close")
        raise LendingError("The due date is too close now. Ask for a new offer.")
    lender = sb.get("profiles", id=loan["lender_id"])
    if not can_send_to(lender, user):
        raise LendingError("You and the lender need to still follow each other to continue.")
    if any(sb.count("coin_loans", borrower_id=user["id"], status=s) for s in OPEN):
        raise LendingError("You already have a loan to pay back.")
    code = transfer(loan["lender_id"], user["id"], loan["amount"], "loan_out", "loan_in", f"{loan['id']}:disburse")
    if code == INSUFFICIENT:
        raise LendingError("The lender doesn't have enough coins for this loan right now.")
    if code == INVALID:
        raise LendingError("This loan can't be completed.")
    won = sb.update("coin_loans", {"status": "active", "accepted_at": iso()}, id=loan["id"], status="offered")
    if not won:
        if _loan(loan_id)["status"] == "active":                  # a double click: the first one already did it all
            return _loan(loan_id)
        transfer(user["id"], loan["lender_id"], loan["amount"], "loan_repay_out", "loan_repay_in", f"{loan['id']}:undo")    # lender cancelled at the same moment
        raise LendingError("This offer was withdrawn just now. No coins were moved.")
    _event(loan["id"], "accepted", loan["amount"], user["id"], loan["terms_hash"], ref="accepted")
    notify(loan["lender_id"], "loan_accepted", actor_id=user["id"], ref_type="loan", ref_id=loan["id"],
           body=f"agreed to your loan. {loan['amount']} coins were sent. Due {due_label(due_at)}.", dedupe=False)
    return _loan(loan_id)


def decline_loan(loan_id, user):
    loan = _must_loan(loan_id, user, role="borrower")
    if loan["status"] != "offered":
        raise LendingError("This offer is no longer available.")
    if _close_offer(loan, "declined", "declined", user["id"]):
        notify(loan["lender_id"], "loan_declined", actor_id=user["id"], ref_type="loan", ref_id=loan["id"],
               body=f"declined your loan offer of {loan['amount']} coins.", dedupe=False)
    return _loan(loan_id)


def cancel_loan(loan_id, user):
    loan = _must_loan(loan_id, user, role="lender")
    if loan["status"] != "offered":
        raise LendingError("Only an offer that hasn't been answered yet can be cancelled.")
    if _close_offer(loan, "cancelled", "cancelled", user["id"]):
        notify(loan["borrower_id"], "loan_cancelled", actor_id=user["id"], ref_type="loan", ref_id=loan["id"],
               body=f"withdrew their loan offer of {loan['amount']} coins.", dedupe=False)
    return _loan(loan_id)


def forgive_loan(loan_id, user):
    loan = _must_loan(loan_id, user, role="lender")
    if loan["status"] not in OPEN:
        raise LendingError("There is nothing to forgive on this loan.")
    owed = remaining(loan)
    done = sb.update("coin_loans", {"status": "forgiven", "forgiven_amount": owed, "closed_at": iso()}, id=loan["id"], status=loan["status"])
    if done:
        _event(loan["id"], "forgiven", owed, user["id"], ref="forgiven")
        notify(loan["borrower_id"], "loan_forgiven", actor_id=user["id"], ref_type="loan", ref_id=loan["id"],
               body=f"forgave the {owed} coins you still owed. You're all clear!", dedupe=False)
    return _loan(loan_id)


# ------------------------------------------------------------------ paying
def _pay(loan, amount, ref, kind):
    """Move up to `amount` coins borrower -> lender once per `ref`. Returns the coins moved by THIS call."""
    if sb.get("coin_loan_events", loan_id=loan["id"], ref=ref):
        return 0
    orphan = ledger_amount(loan["borrower_id"], "loan_repay_out", ref)
    if orphan:                      # an earlier attempt moved the coins but died before recording it: record it now, move nothing
        _event(loan["id"], "payment", orphan, loan["borrower_id"], kind, ref=ref)
        _recompute(loan["id"])
        return orphan
    amount = min(int(amount), remaining(_loan(loan["id"])))
    if amount <= 0:
        return 0
    code = transfer(loan["borrower_id"], loan["lender_id"], amount, "loan_repay_out", "loan_repay_in", ref)
    if code in (INSUFFICIENT, INVALID, DUPLICATE):
        return 0
    moved = amount
    _event(loan["id"], "payment", moved, loan["borrower_id"], kind, ref=ref)
    _recompute(loan["id"])
    return moved


def _recompute(loan_id):
    """repaid_total always comes from the recorded payments. Close the loan if it is now fully paid."""
    loan = _loan(loan_id)
    total = sum(e["amount"] for e in sb.select("coin_loan_events", loan_id=loan_id, kind="payment", limit=2000))
    if total != loan["repaid_total"]:
        sb.update("coin_loans", {"repaid_total": total}, id=loan_id)
        loan["repaid_total"] = total
    if loan["status"] in OPEN and remaining(loan) <= 0:
        if sb.update("coin_loans", {"status": "repaid", "closed_at": iso()}, id=loan_id, status=loan["status"]):
            _event(loan_id, "repaid", total, None, ref="repaid")
            late = loan["status"] == "overdue"
            notify(loan["lender_id"], "loan_repaid", actor_id=loan["borrower_id"], ref_type="loan", ref_id=loan_id,
                   body=f"has paid back all {total} coins" + (" including the late fee." if late and loan["fee_amount"] else "."), dedupe=False)
            notify(loan["borrower_id"], "loan_repaid", ref_type="loan", ref_id=loan_id,
                   body=f"You've paid back your loan of {loan['amount']} coins in full. Thank you!", dedupe=False)
    return _loan(loan_id)


def repay_loan(loan_id, user, amount=None, token=""):
    """Early (or any-time) repayment by the borrower. amount=None pays as much as possible."""
    loan = _must_loan(loan_id, user, role="borrower")
    if loan["status"] not in OPEN:
        raise LendingError("This loan doesn't need paying back right now.")
    owed, have = remaining(loan), balance(user["id"])
    if have <= 0:
        raise LendingError("You don't have any coins to pay with yet.")
    want = owed if amount in (None, "") else _int(amount, "repayment")
    if want < 1:
        raise LendingError("Enter how many coins to pay back.")
    want = min(want, owed, have)
    ref = f"{loan['id']}:early:{token or utcnow().timestamp()}"
    if sb.get("coin_loan_events", loan_id=loan["id"], ref=ref):          # the same form sent twice
        return _loan(loan_id), 0
    moved = _pay(loan, want, ref, "early")
    if not moved:
        raise LendingError("That payment couldn't be completed.")
    fresh = _loan(loan_id)
    if fresh["status"] in OPEN:
        notify(loan["lender_id"], "loan_collected", actor_id=user["id"], ref_type="loan", ref_id=loan_id,
               body=f"paid you {moved} coins. {remaining(fresh)} still to come.", dedupe=False)
    return fresh, moved


# ------------------------------------------------------------------ the due date
def collect_due(loan, now=None):
    """Called once the due day has ended. Takes what the borrower has; starts the late period for the rest."""
    now = now or utcnow()
    loan = _loan(loan["id"])
    if loan["status"] != "active" or loan["frozen"] or now < parse_dt(loan["due_at"]):
        return loan
    before = loan["repaid_total"]
    _pay(loan, min(balance(loan["borrower_id"]), remaining(loan)), f"{loan['id']}:due", "due date")
    loan = _recompute(loan["id"])
    taken = loan["repaid_total"] - before
    if loan["status"] == "repaid":                              # paid in full on the due date
        return loan
    unpaid = remaining(loan)                                    # no fee exists yet, so this is unpaid principal
    fee = late_fee_for(unpaid, loan["fee_pct"])
    if not sb.update("coin_loans", {"status": "overdue", "overdue_at": iso(now), "fee_amount": fee}, id=loan["id"], status="active"):
        return _loan(loan["id"])
    _event(loan["id"], "overdue", unpaid, None, f"{taken} collected", ref="overdue")
    if fee:
        _event(loan["id"], "fee_applied", fee, None, f"{loan['fee_pct']}% of {unpaid}", ref="fee")
    owed = unpaid + fee
    pct = cfg("LOAN_GARNISH_PCT")
    notify(loan["lender_id"], "loan_overdue", actor_id=loan["borrower_id"], ref_type="loan", ref_id=loan["id"],
           body=(f"couldn't pay everything on the due date. {taken} coins were collected for you. " if taken else "couldn't pay on the due date. ") +
                f"{owed} coins are still owed" + (f" (includes a {fee}-coin late fee)" if fee else "") + f". {pct}% of what they earn will be sent to you until it's clear.",
           dedupe=False)
    notify(loan["borrower_id"], "loan_overdue", ref_type="loan", ref_id=loan["id"],
           body=(f"Your loan was due. {taken} coins were paid to the lender automatically. " if taken else "Your loan was due and you had no coins to pay it. ") +
                f"You still owe {owed} coins" + (f" (includes a {fee}-coin late fee)" if fee else "") + f". {pct}% of the coins you earn now go to the lender until it's clear.",
           dedupe=False)
    return _loan(loan["id"])


def garnish(user_id, credit, reason, ref):
    """Called after a student EARNS coins. Sends the lender's share to any overdue loans, oldest first."""
    pct = cfg("LOAN_GARNISH_PCT")
    budget = int(credit) * pct // 100
    if budget <= 0:
        return 0
    loans = sorted([l for l in sb.select("coin_loans", borrower_id=user_id, status="overdue", limit=20) if not l["frozen"]], key=lambda l: l["overdue_at"] or "")
    paid_total = 0
    for loan in loans:
        if budget <= 0:
            break
        moved = _pay(loan, min(budget, remaining(loan)), f"{loan['id']}:g:{reason}:{ref}", "garnish")
        if not moved:
            continue
        budget -= moved
        paid_total += moved
        fresh = _loan(loan["id"])
        if fresh["status"] == "overdue":
            notify(loan["lender_id"], "loan_collected", actor_id=user_id, ref_type="loan", ref_id=loan["id"],
                   body=f"{moved} coins from what they just earned were paid to you. {remaining(fresh)} still owed.", dedupe=False)
            notify(user_id, "loan_collected", ref_type="loan", ref_id=loan["id"],
                   body=f"{moved} of the coins you just earned went to repay your loan. {remaining(fresh)} still owed.", dedupe=False)
    return paid_total


# ------------------------------------------------------------------ reminders + the scheduled run
def send_reminders(loan, now):
    """3 days before, 1 day before and on the due day. Sends only the latest reminder that applies (so a late scheduler
    never replays old ones) and never for a moment that was already past when the loan was accepted."""
    start, accepted = due_day_start(parse_dt(loan["due_at"])), parse_dt(loan["accepted_at"]) or now
    steps = [("reminder_3d", start - timedelta(days=3), "is due in 3 days"), ("reminder_1d", start - timedelta(days=1), "is due tomorrow"),
             ("reminder_due", start, "is due today (by midnight Kampala time)")]
    reached = [step for step in steps if now >= step[1]]
    if not reached:
        return None
    for kind, _, _ in reached[:-1]:
        _event(loan["id"], kind, 0, None, "skipped", ref=kind)
    kind, when, text = reached[-1]
    if accepted <= when and _event(loan["id"], kind, 0, None, ref=kind):
        notify(loan["borrower_id"], "loan_reminder", ref_type="loan", ref_id=loan["id"],
               body=f"Your loan of {loan['amount']} coins {text}. CIRQ will collect {remaining(loan)} coins automatically, so make sure you have them.", dedupe=False)
        return kind
    _event(loan["id"], kind, 0, None, "skipped", ref=kind)
    return None


def run_due(now=None):
    """The scheduled job (every few minutes). Safe to run twice or in parallel: every step is idempotent."""
    now = now or utcnow()
    out = {"expired": 0, "reminders": 0, "collected": 0, "overdue": 0}
    for loan in sb.select("coin_loans", status="offered", limit=1000):
        if expire_if_needed(loan)["status"] == "expired":
            out["expired"] += 1
    for loan in sb.select("coin_loans", status="active", limit=2000):
        if loan["frozen"]:
            continue
        if send_reminders(loan, now):
            out["reminders"] += 1
        if now >= parse_dt(loan["due_at"]):
            after = collect_due(loan, now)
            if after["status"] == "repaid":
                out["collected"] += 1
            elif after["status"] == "overdue":
                out["overdue"] += 1
    return out


def process_user(user_id, now=None):
    """Safety net: settle anything that is due for this student when they open their wallet."""
    now = now or utcnow()
    for role in ("borrower_id", "lender_id"):
        for loan in sb.select("coin_loans", limit=100, **{role: user_id}):
            if loan["status"] == "offered":
                expire_if_needed(loan)
            elif loan["status"] == "active" and not loan["frozen"] and now >= parse_dt(loan["due_at"]):
                collect_due(loan, now)


# ------------------------------------------------------------------ admin
def set_frozen(loan_id, frozen, admin_id):
    loan = _loan(loan_id)
    if not loan or loan["status"] not in OPEN:
        raise LendingError("Only an active or overdue loan can be frozen.")
    sb.update("coin_loans", {"frozen": bool(frozen)}, id=loan_id)
    _event(loan_id, "frozen" if frozen else "unfrozen", 0, admin_id, ref=f"{'frozen' if frozen else 'unfrozen'}:{int(utcnow().timestamp())}")


# ------------------------------------------------------------------ reading
def _must_loan(loan_id, user, role=None):
    loan = _loan(loan_id)
    if not loan or user["id"] not in (loan["lender_id"], loan["borrower_id"]):
        raise LendingError("We couldn't find that loan.")
    if role == "borrower" and loan["borrower_id"] != user["id"] or role == "lender" and loan["lender_id"] != user["id"]:
        raise LendingError("Only the " + role + " can do that.")
    return loan


def decorate(loan, viewer_id, people=None):
    people = people or _profiles(loan["lender_id"], loan["borrower_id"])
    paid, owed = int(loan["repaid_total"]), int(loan["amount"]) + int(loan["fee_amount"])
    due_at = parse_dt(loan["due_at"])
    loan = dict(loan)
    loan.update(lender=people.get(loan["lender_id"]), borrower=people.get(loan["borrower_id"]), role="lender" if loan["lender_id"] == viewer_id else "borrower",
                remaining=remaining(loan), total_due=owed, paid=paid, pct=min(100, paid * 100 // owed) if owed else 100,
                status_label=STATUS_LABEL.get(loan["status"], loan["status"]), due_label=due_label(due_at), due_dt=due_at,
                days_left=(eat_date(due_at) - eat_date(utcnow())).days, is_open=loan["status"] in OPEN,
                expires_dt=parse_dt(loan["expires_at"]))
    other = loan["borrower"] if loan["role"] == "lender" else loan["lender"]
    loan["other"] = other
    return loan


def overview(user_id):
    process_user(user_id)
    mine = sb.select("coin_loans", order="created_at", limit=200, borrower_id=user_id) + sb.select("coin_loans", order="created_at", limit=200, lender_id=user_id)
    ids = {x for l in mine for x in (l["lender_id"], l["borrower_id"])}
    people = _profiles(*ids)
    rows = [decorate(l, user_id, people) for l in sorted(mine, key=lambda l: l["created_at"], reverse=True)]
    return {
        "offers_in": [l for l in rows if l["status"] == "offered" and l["role"] == "borrower"],
        "offers_out": [l for l in rows if l["status"] == "offered" and l["role"] == "lender"],
        "owing": [l for l in rows if l["is_open"] and l["role"] == "borrower"],
        "owed": [l for l in rows if l["is_open"] and l["role"] == "lender"],
        "history": [l for l in rows if l["status"] in CLOSED],
        "gifts": gifts_for(user_id),
    }


def gifts_for(user_id, limit=15):
    sent = sb.select("coin_gifts", order="created_at", limit=limit, sender_id=user_id)
    got = sb.select("coin_gifts", order="created_at", limit=limit, recipient_id=user_id)
    people = _profiles(*{x for g in sent + got for x in (g["sender_id"], g["recipient_id"])})
    rows = [dict(g, direction="out", other=people.get(g["recipient_id"])) for g in sent] + [dict(g, direction="in", other=people.get(g["sender_id"])) for g in got]
    return sorted(rows, key=lambda g: g["created_at"], reverse=True)[:limit]


def detail(loan_id, user):
    loan = _must_loan(loan_id, user)
    loan = expire_if_needed(loan)
    if loan["status"] == "active" and not loan["frozen"] and utcnow() >= parse_dt(loan["due_at"]):
        loan = collect_due(loan)
    view = decorate(loan, user["id"])
    view["events"] = sb.select("coin_loan_events", order="created_at", desc=False, limit=200, loan_id=loan_id)
    return view


def _int(value, label):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        raise LendingError(f"Enter a whole number for the {label}.")
