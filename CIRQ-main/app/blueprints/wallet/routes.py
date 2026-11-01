"""App coins, boosts and the manual mobile-money payment flow (no in-app payment processor)."""
import re
from flask import Blueprint, render_template, request, redirect, url_for, flash, g, current_app, abort
from app.decorators import login_required
from app.extensions import limiter
from app.services import supabase_service as sb, coin_service, social
from app.utils import clean_text

bp = Blueprint("wallet", __name__)


def normalize_phone(raw):
    """Accept 07XXXXXXXX, 2567XXXXXXXX or +2567XXXXXXXX; return +256XXXXXXXXX or None."""
    digits = re.sub(r"[^\d+]", "", raw or "")
    if digits.startswith("+"):
        digits = digits[1:]
    if digits.startswith("0") and len(digits) == 10:
        digits = "256" + digits[1:]
    return "+" + digits if re.fullmatch(r"256[37]\d{8}", digits) else None


@bp.get("/coins")
@login_required
def coins():
    me = sb.get("profiles", id=g.user["id"])
    cfg = current_app.config
    return render_template("wallet/coins.html", me=me, history=coin_service.history(me["id"]), boosted=coin_service.is_boosted(me),
                           cost=cfg["COIN_BOOST_COST"], days=cfg["COIN_BOOST_DAYS"])


@bp.post("/coins/boost")
@login_required
@limiter.limit("10 per hour")
def boost_with_coins():
    ok, msg = coin_service.spend_on_boost(g.user["id"])
    if ok:
        social.grant_achievement(g.user["id"], "booster")
    flash(msg, "success" if ok else "error")
    return redirect(url_for("wallet.coins"))


@bp.get("/pay")
@login_required
def pay():
    cfg = current_app.config
    numbers = sb.select("payer_numbers", order="created_at", user_id=g.user["id"])
    payments = sb.select("payments", order="created_at", limit=20, user_id=g.user["id"])
    credits = len([c for c in sb.select("trivia_entry_credits", user_id=g.user["id"]) if not c.get("used_at")])
    return render_template("wallet/pay.html", numbers=numbers, payments=payments, credits=credits,
                           enabled=cfg["PAID_COMPETITIONS_ENABLED"], plans=cfg["BOOST_PLANS"], fee=cfg["TRIVIA_ENTRY_FEE_UGX"])


@bp.post("/pay/number")
@login_required
@limiter.limit("5 per hour")
def register_number():
    cfg = current_app.config
    if not cfg["PAID_COMPETITIONS_ENABLED"]:
        abort(403)
    phone = normalize_phone(request.form.get("phone"))
    name = clean_text(request.form.get("account_name"), 80)
    network = request.form.get("network", "mobile_money")
    network = network if network in ("mtn", "airtel", "mobile_money") else "mobile_money"
    if not phone or len(name) < 2:
        flash("Enter a valid Uganda mobile number and the name that shows when you send money.", "error")
    elif not request.form.get("guardian_confirmed"):
        flash("Please confirm you are 18+ or have a parent/guardian's permission to pay.", "error")
    elif len([n for n in sb.select("payer_numbers", user_id=g.user["id"]) if n["status"] in ("pending", "approved")]) >= 2:
        flash("You can have up to 2 active numbers.", "error")
    else:
        try:
            sb.insert("payer_numbers", {"user_id": g.user["id"], "phone": phone, "account_name": name, "network": network,
                                        "guardian_confirmed": True})
            flash("Number submitted. An admin will verify it, usually within a day.", "success")
        except Exception:
            flash("That number is already registered. If it's yours, contact support.", "error")
    return redirect(url_for("wallet.pay"))
