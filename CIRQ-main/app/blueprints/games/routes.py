"""CIRQ FLIP: pick the developer's card out of a deck of real CIRQ students.

Security model: when a round starts the server shuffles a deck (real approved, non-private students plus the
developer, VEX) with `secrets`, stores the deck and the developer's position ONLY in the database, and sends the
browser nothing but the number of face-down cards. A card's identity is revealed only after the player flips it,
and the answer position is revealed only when the round ends. Every guess is validated here.
"""
import secrets
from flask import Blueprint, render_template, request, jsonify, g, current_app, abort
from app.decorators import login_required
from app.extensions import limiter, db
from app.services import supabase_service as sb, social, coin_service, cloudinary_service as cs
from app.utils import iso, utcnow, short_count

bp = Blueprint("games", __name__)

MAX_CARDS, MIN_CARDS, MAX_GUESSES = 12, 6, 5
_rng = secrets.SystemRandom()


def _developer_card():
    """The game-only developer card, edited by admins and independent of any user account."""
    cfg = current_app.config
    settings = sb.get("developer_card_settings", id="developer") or {}
    image = {"url": settings.get("image_url"), "public_id": settings.get("image_public_id"), "media_type": "image"}
    return {"developer": True, "name": settings.get("display_name") or cfg["DEVELOPER_DISPLAY_NAME"],
            "username": None, "school": None, "avatar": cs.media_src(image, 400) if image["url"] else "",
            "followers": 0, "tier": "Developer", "tier_index": 99}


def _student_card(uid):
    p = sb.get("profiles", id=uid)
    if not p:
        return {"developer": False, "name": "Former student", "username": None, "school": None, "avatar": "", "followers": 0, "tier": "Rookie", "tier_index": 0}
    from app.utils import tier_for
    n = social.follower_count(p["id"])
    sch = sb.get("schools", id=p["school_id"]) if p.get("school_id") else None
    t = tier_for(n)
    return {"developer": False, "name": p["display_name"], "username": p["username"], "school": sch["name"] if sch else None,
            "avatar": cs.avatar_src(p, 400), "followers": n, "tier": t["name"], "tier_index": t["index"]}


def _build_deck(me_id):
    """Return (deck, answer_index) or None when there aren't enough real students yet. Never invents users."""
    hidden = social.blocked_between(me_id)
    rows = db().table("profiles").select("id,is_private,account_status").eq("account_status", "approved").limit(400).execute().data or []
    pool = [r["id"] for r in rows if r["id"] != me_id and r["id"] not in hidden and not r["is_private"]]
    if len(pool) + 1 < MIN_CARDS:
        return None
    entries = [{"user_id": uid, "developer": False} for uid in _rng.sample(pool, min(MAX_CARDS - 1, len(pool)))]
    entries.append({"user_id": None, "developer": True})
    _rng.shuffle(entries)
    return entries, next(i for i, e in enumerate(entries) if e["developer"])


def _active_round(uid):
    rows = sb.select("game_rounds", user_id=uid, status="active", limit=1)
    return rows[0] if rows else None


def _card_for(rnd, idx):
    entry = rnd["deck"][idx]
    return _developer_card() if entry["developer"] else _student_card(entry["user_id"])


def _public_state(rnd, guesses):
    out = {"round_id": rnd["id"], "status": rnd["status"], "cards": len(rnd["deck"]), "guesses_used": rnd["guesses_used"],
           "guesses_left": MAX_GUESSES - rnd["guesses_used"],
           "flipped": [{"card": gu["card_index"], "correct": gu["correct"], "profile": _card_for(rnd, gu["card_index"])} for gu in guesses]}
    if rnd["status"] != "active":      # the answer is only ever revealed once the round is over
        out["answer_index"] = rnd["answer_index"]
        out["developer"] = _card_for(rnd, rnd["answer_index"])
    return out


@bp.get("/games")
@login_required
def hub():
    top = sb.select("coin_ledger", reason="dev_card", limit=500)
    wins = {}
    for r in top:
        wins[r["user_id"]] = wins.get(r["user_id"], 0) + 1
    people = sb.by_id("profiles", list(wins))
    board = sorted(({"user": people[u], "wins": n} for u, n in wins.items() if u in people), key=lambda r: -r["wins"])[:5]
    return render_template("games/hub.html", board=board)


@bp.get("/games/flip")
@login_required
def flip():
    me = g.user["id"]
    rounds = sb.select("game_rounds", user_id=me)
    finished = [r for r in rounds if r["status"] != "active"]
    wins = sum(1 for r in finished if r["status"] == "won")
    # leaderboard = students who found the developer, counted from real finished rounds
    allwins = {}
    for r in db().table("game_rounds").select("user_id,status").eq("status", "won").limit(2000).execute().data or []:
        allwins[r["user_id"]] = allwins.get(r["user_id"], 0) + 1
    people = sb.by_id("profiles", list(allwins))
    board = sorted(({"user": people[u], "wins": n} for u, n in allwins.items() if u in people and people[u]["account_status"] == "approved"),
                   key=lambda r: -r["wins"])[:5]
    dev = _developer_card()
    return render_template("games/flip.html", max_guesses=MAX_GUESSES, history=sorted(finished, key=lambda r: r["created_at"], reverse=True)[:6],
                           wins=wins, rounds=len(finished), dev_coins=current_app.config["DEV_CARD_COINS"], dev_name=dev["name"],
                           daily_cap=current_app.config["DEV_CARD_REWARDS_PER_DAY"], board=board)


@bp.get("/api/games/flip/state")
@login_required
def state():
    rnd = _active_round(g.user["id"])
    if not rnd:
        return jsonify(active=False, pending_claim=_pending_claim(g.user["id"]))
    return jsonify(active=True, **_public_state(rnd, sb.select("game_guesses", order="created_at", desc=False, round_id=rnd["id"])))


@bp.post("/api/games/flip/start")
@login_required
@limiter.limit("60 per hour")
def start():
    me = g.user["id"]
    rnd = _active_round(me)
    if not rnd:
        if _pending_claim(me):
            return jsonify(error="Claim your developer reward before starting another round."), 409
        built = _build_deck(me)
        if not built:
            return jsonify(error="CIRQ FLIP needs a few more students on CIRQ before it can deal a deck. Check back soon!"), 409
        deck, answer = built
        rnd = sb.insert("game_rounds", {"user_id": me, "answer_index": answer, "deck": deck, "status": "active", "guesses_used": 0})
    return jsonify(**_public_state(rnd, sb.select("game_guesses", order="created_at", desc=False, round_id=rnd["id"])))


@bp.post("/api/games/flip/guess")
@login_required
@limiter.limit("60 per minute")
def guess():
    me = g.user["id"]
    idx = (request.get_json(silent=True) or {}).get("card")
    rnd = _active_round(me)
    if not rnd:
        return jsonify(error="Start a round first."), 409
    if not isinstance(idx, int) or isinstance(idx, bool) or not 0 <= idx < len(rnd["deck"]):
        return jsonify(error="Pick one of the cards on the table."), 400
    if rnd["guesses_used"] >= MAX_GUESSES:
        return jsonify(error="No guesses left in this round."), 409
    if sb.get("game_guesses", round_id=rnd["id"], card_index=idx):
        return jsonify(error="You already flipped that card this round."), 409
    correct = idx == rnd["answer_index"]
    try:
        sb.insert("game_guesses", {"round_id": rnd["id"], "user_id": me, "card_index": idx, "correct": correct})
    except Exception:   # unique(round_id, card_index) also stops a double-click race
        return jsonify(error="You already flipped that card this round."), 409
    used = rnd["guesses_used"] + 1
    status = "won" if correct else ("lost" if used >= MAX_GUESSES else "active")
    values = {"guesses_used": used, "status": status}
    if status != "active":
        values["ended_at"] = iso()
    sb.update("game_rounds", values, id=rnd["id"])
    rnd = {**rnd, **values}
    reward = {"coins": 0, "claimable": False, "note": None}
    if status == "won":
        reward = _prepare_win(me)
    elif status == "lost":
        _after_round(me)
    guesses = sb.select("game_guesses", order="created_at", desc=False, round_id=rnd["id"])
    return jsonify(**_public_state(rnd, guesses), reward=reward)


@bp.post("/api/games/flip/claim")
@login_required
def claim():
    me = g.user["id"]
    round_id = (request.get_json(silent=True) or {}).get("round_id")
    rnd = sb.get("game_rounds", id=round_id, user_id=me) if isinstance(round_id, str) else None
    if not rnd:
        return jsonify(error="That game round could not be found."), 404
    if rnd["status"] != "won":
        return jsonify(error="Find the developer before claiming a reward."), 409

    entry = sb.get("coin_ledger", user_id=me, reason="dev_card", ref=round_id)
    if entry:
        return jsonify(claimed=True, coins=entry["amount"], balance=coin_service.balance(me))

    reward = _reward_preview(me)
    if not reward["claimable"]:
        return jsonify(claimed=False, coins=0, note=reward["note"])

    cfg = current_app.config
    amount = cfg["DEV_CARD_COINS"]
    bal = coin_service.award(me, amount, "dev_card", rnd["id"],
                             message=f"+{amount} app coins for finding the developer!")
    if bal == coin_service.DUPLICATE:
        entry = sb.get("coin_ledger", user_id=me, reason="dev_card", ref=round_id)
        if entry:
            return jsonify(claimed=True, coins=entry["amount"], balance=coin_service.balance(me))
    if bal < 0:
        current_app.logger.error("Could not claim developer-card reward for round %s (result %s)", round_id, bal)
        return jsonify(error="Your coins could not be claimed. Please try again."), 500
    return jsonify(claimed=True, coins=amount, balance=bal)


def _prepare_win(me):
    social.grant_achievement(me, "dev_finder")
    social.grant_achievement(me, "flip_first_win")
    _after_round(me)
    return _reward_preview(me)


def _reward_preview(me):
    cfg = current_app.config
    today = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    rewarded_today = len([c for c in sb.select("coin_ledger", user_id=me, reason="dev_card") if c["created_at"] >= iso(today)])
    if rewarded_today >= cfg["DEV_CARD_REWARDS_PER_DAY"]:
        return {"coins": 0, "claimable": False,
                "note": f"Great find! The daily coin reward ({cfg['DEV_CARD_REWARDS_PER_DAY']} per day) is used up. Come back tomorrow."}
    return {"coins": cfg["DEV_CARD_COINS"], "claimable": True,
            "note": f"Claim your {cfg['DEV_CARD_COINS']} app coins!"}


def _pending_claim(me):
    rounds = sb.select("game_rounds", order="ended_at", desc=True, limit=20, user_id=me, status="won")
    for rnd in rounds:
        if sb.get("coin_ledger", user_id=me, reason="dev_card", ref=rnd["id"]):
            continue
        reward = _reward_preview(me)
        if reward["claimable"]:
            guesses = sb.select("game_guesses", order="created_at", desc=False, round_id=rnd["id"])
            return {**_public_state(rnd, guesses), "reward": reward}
    return None


def _after_round(me):
    finished = [r for r in sb.select("game_rounds", user_id=me) if r["status"] != "active"]
    if len(finished) >= 5:
        social.grant_achievement(me, "flip_five")
    if len([r for r in finished if r["status"] == "won"]) >= 3:
        social.grant_achievement(me, "flip_streak")


@bp.post("/games/flip/paid")
@login_required
def paid():
    """Paid FLIP rounds are not offered. The flag is enforced here on the server, not just in the UI."""
    if not current_app.config["PAID_COMPETITIONS_ENABLED"]:
        abort(403)
    return jsonify(error="Paid CIRQ FLIP rounds are not implemented."), 501
