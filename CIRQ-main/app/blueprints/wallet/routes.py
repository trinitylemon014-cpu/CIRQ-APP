"""App coins, boosts and the manual mobile-money payment flow (no in-app payment processor)."""
import re
from flask import Blueprint, render_template, request, redirect, url_for, flash, g, current_app, abort
from app.decorators import login_required
from app.extensions import limiter
from app.services import supabase_service as sb, coin_service, social, cloudinary_service as cs
from app.services.cloudinary_service import UploadError
from app.services.moderation_service import check_text
from app.utils import clean_text, iso

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
    ad = sb.get("boost_ads", user_id=me["id"])
    if ad:
        ad["image_src"] = cs.media_src({"url": ad["image_url"], "public_id": ad["image_public_id"], "media_type": "image"}, 480)
    return render_template("wallet/coins.html", me=me, history=coin_service.history(me["id"]), boosted=coin_service.is_boosted(me),
                           cost=cfg["COIN_BOOST_COST"], days=cfg["COIN_BOOST_DAYS"], ad=ad)


@bp.post("/coins/boost")
@login_required
@limiter.limit("10 per hour")
def boost_with_coins():
    ok, msg = coin_service.spend_on_boost(g.user["id"])
    if ok:
        social.grant_achievement(g.user["id"], "booster")
    flash(msg, "success" if ok else "error")
    return redirect(url_for("wallet.coins"))


@bp.post("/coins/boost-ad")
@login_required
@limiter.limit("6 per hour")
def save_boost_ad():
    me = sb.get("profiles", id=g.user["id"])
    if not coin_service.is_boosted(me):
        flash("An active account boost is required to create or edit a promoted ad.", "error")
        return redirect(url_for("wallet.coins"))

    existing = sb.get("boost_ads", user_id=me["id"])
    if request.form.get("remove"):
        if existing:
            sb.delete("boost_ads", user_id=me["id"])
            cs.destroy(existing.get("image_public_id"))
        flash("Your promoted ad was removed.", "info")
        return redirect(url_for("wallet.coins"))

    headline = clean_text(request.form.get("headline"), 80)
    body = clean_text(request.form.get("body"), 300)
    if len(headline) < 2 or len(body) < 5:
        flash("Enter an ad headline and a message of at least 5 characters.", "error")
        return redirect(url_for("wallet.coins"))
    for text in (headline, body):
        ok, why = check_text(text)
        if not ok:
            flash(why, "error")
            return redirect(url_for("wallet.coins"))

    image = request.files.get("image")
    uploaded = None
    if image and image.filename:
        try:
            uploaded = cs.upload(image, "boost-ads", me["id"], allow_video=False)
        except UploadError as e:
            flash(str(e), "error")
            return redirect(url_for("wallet.coins"))
    if not uploaded and not existing:
        flash("Choose an image for your promoted ad.", "error")
        return redirect(url_for("wallet.coins"))

    values = {"user_id": me["id"], "headline": headline, "body": body, "updated_at": iso()}
    if uploaded:
        values.update(image_url=uploaded["url"], image_public_id=uploaded["public_id"])
    else:
        values.update(image_url=existing["image_url"], image_public_id=existing["image_public_id"])
    if existing:
        sb.update("boost_ads", values, user_id=me["id"])
        if uploaded and existing.get("image_public_id"):
            cs.destroy(existing["image_public_id"])
    else:
        sb.insert("boost_ads", values)
    flash("Your promoted ad is saved. It will be shown while your boost is active.", "success")
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
