"""App coins. Every change goes through the database function cirq_apply_coins (atomic + idempotent)."""
from flask import current_app
from app.extensions import db
from app.services import supabase_service as sb
from app.services.notification_service import notify
from app.utils import utcnow, parse_dt, iso
from datetime import timedelta

DUPLICATE, INSUFFICIENT = -1, -2


def apply(user_id, amount, reason, ref=""):
    """Return new balance (>=0), DUPLICATE (-1) if this reward was already granted, INSUFFICIENT (-2)."""
    res = db().rpc("cirq_apply_coins", {"p_user": user_id, "p_amount": int(amount), "p_reason": reason, "p_ref": str(ref or "")}).execute()
    bal = int(res.data)
    if bal >= 0 and int(amount) > 0 and not reason.startswith(NOT_GARNISHED_PREFIXES) and reason not in NOT_GARNISHED:
        bal = _garnish_for_overdue_loans(user_id, int(amount), reason, ref, bal)
    return bal


# Coins a student EARNS are shared with a lender while they have an overdue loan. Transfers between students and
# admin corrections are never touched.
NOT_GARNISHED_PREFIXES = ("loan_", "gift_")
NOT_GARNISHED = {"admin_adjust", "refund"}


def _garnish_for_overdue_loans(user_id, amount, reason, ref, bal):
    """Never lets a lending problem stop a reward from being granted. Returns the balance after any repayment."""
    try:
        from app.services import lending
        if lending.garnish(user_id, amount, reason, ref or ""):
            return balance(user_id)
    except Exception:
        current_app.logger.exception("garnishing failed for %s", user_id)
    return bal


def award(user_id, amount, reason, ref="", message=None):
    bal = apply(user_id, amount, reason, ref)
    if bal >= 0 and message:
        notify(user_id, "account", body=message, dedupe=False)
    return bal


def balance(user_id):
    p = sb.get("profiles", "coin_balance", id=user_id)
    return (p or {}).get("coin_balance", 0)


def history(user_id, limit=30):
    return sb.select("coin_ledger", order="created_at", limit=limit, user_id=user_id)


def is_boosted(profile):
    d = parse_dt((profile or {}).get("boosted_until"))
    return bool(d and d > utcnow())


def grant_boost(user_id, days, source, payment_id=None):
    """Extend (or start) a boost. Stacks after any boost that is still running."""
    p = sb.get("profiles", id=user_id)
    cur = parse_dt(p.get("boosted_until"))
    start = cur if cur and cur > utcnow() else utcnow()
    end = start + timedelta(days=days)
    sb.insert("boosts", {"user_id": user_id, "source": source, "payment_id": payment_id, "starts_at": iso(start), "ends_at": iso(end)})
    sb.update("profiles", {"boosted_until": iso(end)}, id=user_id)
    return end


def spend_on_boost(user_id):
    """Spend COIN_BOOST_COST coins for COIN_BOOST_DAYS of boost. Returns (ok, message)."""
    cfg = current_app.config
    ref = f"boost-{iso()}"
    bal = apply(user_id, -cfg["COIN_BOOST_COST"], "boost_spend", ref)
    if bal == INSUFFICIENT:
        return False, f"You need {cfg['COIN_BOOST_COST']} coins to boost."
    end = grant_boost(user_id, cfg["COIN_BOOST_DAYS"], "coins")
    return True, f"Boosted until {end.strftime('%d %b %H:%M UTC')}."
