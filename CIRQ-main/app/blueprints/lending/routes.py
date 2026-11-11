"""Gifts and loans between friends: the hub, the send form (gift or loan), and the loan agreement page."""
import uuid
from datetime import date, timedelta

from flask import Blueprint, render_template, request, redirect, url_for, flash, g, abort, session, current_app
from app.decorators import login_required
from app.extensions import limiter
from app.services import supabase_service as sb, lending
from app.services.lending import LendingError
from app.services.moderation_service import check_text
from app.utils import normalize_username, utcnow

bp = Blueprint("lending", __name__)


def _me():
    return sb.get("profiles", id=g.user["id"])


def _back_to_loan(loan_id):
    return redirect(url_for("lending.detail", loan_id=loan_id))


@bp.get("/coins/lending")
@login_required
def index():
    me = _me()
    data = lending.overview(me["id"])
    return render_template("lending/index.html", me=me, friends=lending.friends_of(me["id"]) if current_app.config["LENDING_ENABLED"] else [], **data,
                           cfg=current_app.config)


@bp.route("/coins/send/<username>", methods=["GET", "POST"])
@login_required
@limiter.limit("30 per hour", methods=["POST"])
def send(username):
    me = _me()
    target = sb.get("profiles", username=normalize_username(username))
    if not target or target.get("account_status") != "approved":
        abort(404)
    if not lending.can_send_to(me, target):
        flash("You can send coins to friends: people you follow who also follow you back.", "error")
        return redirect(url_for("profiles.view", username=target["username"]))
    cfg = current_app.config
    kind = "loan" if request.values.get("kind") == "loan" else "gift"
    form = request.form if request.method == "POST" else {}
    today = lending.eat_date(utcnow())
    if request.method == "POST":
        if not form.get("token") or form.get("token") != session.pop("send_token", None):
            flash("That form was already sent. Check your Lending page before trying again.", "info")
            return redirect(url_for("lending.index"))
        message = (form.get("message") or "").strip()
        ok, why = check_text(message) if message else (True, "")
        try:
            if not ok:
                raise LendingError(why)
            if kind == "gift":
                gift = lending.send_gift(me, target, form.get("amount"), message)
                flash(f"Sent {gift['amount']} coins to {target['display_name']}.", "success")
                return redirect(url_for("profiles.view", username=target["username"]))
            try:
                due = date.fromisoformat(form.get("due_date", ""))
            except ValueError:
                raise LendingError("Choose a due date.")
            loan = lending.offer_loan(me, target, form.get("amount"), due, form.get("fee_pct", cfg["LOAN_DEFAULT_FEE_PCT"]), message)
            flash(f"Offer sent to {target['display_name']}. They have {cfg['LOAN_OFFER_HOURS']} hours to agree.", "success")
            return _back_to_loan(loan["id"])
        except LendingError as e:
            flash(str(e), "error")
    token = uuid.uuid4().hex
    session["send_token"] = token
    return render_template("lending/send.html", me=me, target=target, kind=kind, form=form, token=token, cfg=cfg,
                           min_date=today + timedelta(days=cfg["LOAN_MIN_DAYS"]), max_date=today + timedelta(days=cfg["LOAN_MAX_DAYS"]))


@bp.get("/coins/loans/<loan_id>")
@login_required
def detail(loan_id):
    try:
        loan = lending.detail(loan_id, g.user)
    except LendingError:
        abort(404)
    return render_template("lending/detail.html", loan=loan, me=_me(), token=uuid.uuid4().hex, cfg=current_app.config)


def _act(loan_id, fn, ok_message=None):
    try:
        result = fn()
    except LendingError as e:
        flash(str(e), "error")
        return _back_to_loan(loan_id)
    if ok_message:
        flash(ok_message(result) if callable(ok_message) else ok_message, "success")
    return _back_to_loan(loan_id)


@bp.post("/coins/loans/<loan_id>/accept")
@login_required
@limiter.limit("20 per hour")
def accept(loan_id):
    return _act(loan_id, lambda: lending.accept_loan(loan_id, g.user), "You agreed. The coins are in your wallet. Pay them back by the due date.")


@bp.post("/coins/loans/<loan_id>/decline")
@login_required
def decline(loan_id):
    return _act(loan_id, lambda: lending.decline_loan(loan_id, g.user), "Offer declined.")


@bp.post("/coins/loans/<loan_id>/cancel")
@login_required
def cancel(loan_id):
    return _act(loan_id, lambda: lending.cancel_loan(loan_id, g.user), "Offer withdrawn. No coins were moved.")


@bp.post("/coins/loans/<loan_id>/forgive")
@login_required
def forgive(loan_id):
    return _act(loan_id, lambda: lending.forgive_loan(loan_id, g.user), "Debt forgiven. Kind of you.")


@bp.post("/coins/loans/<loan_id>/repay")
@login_required
@limiter.limit("30 per hour")
def repay(loan_id):
    def run():
        return lending.repay_loan(loan_id, g.user, request.form.get("amount") or None, request.form.get("token", ""))
    try:
        _, moved = run()
    except LendingError as e:
        flash(str(e), "error")
        return _back_to_loan(loan_id)
    flash(f"Paid back {moved} coins." if moved else "That payment was already recorded.", "success" if moved else "info")
    return _back_to_loan(loan_id)
