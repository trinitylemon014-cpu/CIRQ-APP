"""Trivia. Admin-authored sets; accuracy and speed are scored on the server clock.
Correct answers are never sent to the browser until the student has finished the whole set."""
from flask import Blueprint, render_template, request, redirect, url_for, flash, g, jsonify, abort, current_app
from app.decorators import login_required
from app.extensions import limiter, db
from app.services import supabase_service as sb, coin_service, social
from app.utils import iso, utcnow, parse_dt

bp = Blueprint("trivia", __name__)
GRACE_MS = 1500   # allowance for network latency


def score_answer(correct, ms, limit_s):
    """100 points for a correct answer + up to 100 speed bonus (linear in how early you answered)."""
    if not correct:
        return 0
    frac = max(0.0, 1.0 - min(ms, limit_s * 1000) / (limit_s * 1000))
    return 100 + round(100 * frac)


def _set_or_404(set_id, allow_draft=False):
    s = sb.get("trivia_sets", id=set_id)
    if not s or (s["status"] == "draft" and not (allow_draft and g.user.get("role") == "admin")):
        abort(404)
    return s


def _questions(set_id):
    return sb.select("trivia_questions", order="position", desc=False, set_id=set_id)


def _unused_credit(uid):
    for c in sb.select("trivia_entry_credits", user_id=uid):
        if not c.get("used_at"):
            return c
    return None


@bp.get("/trivia")
@login_required
def index():
    me = g.user["id"]
    paid = current_app.config["PAID_COMPETITIONS_ENABLED"]
    sets = [s for s in sb.select("trivia_sets", order="published_at") if s["status"] in ("published", "closed")]
    attempts = {a["set_id"]: a for a in sb.select("trivia_attempts", user_id=me)}
    for s in sets:
        s["attempt"] = attempts.get(s["id"])
        s["q_count"] = sb.count("trivia_questions", set_id=s["id"])
    credits = len([c for c in sb.select("trivia_entry_credits", user_id=me) if not c.get("used_at")])
    return render_template("trivia/index.html", sets=sets, paid=paid, credits=credits,
                           fee=current_app.config["TRIVIA_ENTRY_FEE_UGX"], board=overall_board())


def overall_board(limit=10):
    totals = {}
    q = db().table("trivia_attempts").select("*").eq("status", "completed")
    if current_app.config["PAID_COMPETITIONS_ENABLED"]:
        q = q.eq("paid", True)      # in paid mode the board is for paid entries only; in free mode every finished set counts
    for a in q.limit(2000).execute().data or []:
        t = totals.setdefault(a["user_id"], {"points": 0, "sets": 0})
        t["points"] += a["points"]
        t["sets"] += 1
    people = sb.by_id("profiles", list(totals))
    rows = [{"user": people[u], **t} for u, t in totals.items() if u in people and people[u]["account_status"] == "approved"]
    return sorted(rows, key=lambda r: -r["points"])[:limit]


@bp.get("/trivia/<set_id>")
@login_required
def intro(set_id):
    s = _set_or_404(set_id)
    me = g.user["id"]
    attempt = sb.get("trivia_attempts", set_id=set_id, user_id=me)
    if attempt and attempt["status"] == "completed":
        return redirect(url_for("trivia.results", set_id=set_id))
    qs = _questions(set_id)
    paid = current_app.config["PAID_COMPETITIONS_ENABLED"]
    return render_template("trivia/intro.html", s=s, attempt=attempt, q_count=len(qs), paid=paid,
                           credit=_unused_credit(me) if paid else None, fee=current_app.config["TRIVIA_ENTRY_FEE_UGX"],
                           secs=sum(q["time_limit_seconds"] for q in qs))


@bp.post("/trivia/<set_id>/start")
@login_required
@limiter.limit("30 per hour")
def start(set_id):
    s = _set_or_404(set_id)
    me = g.user["id"]
    if s["status"] != "published":
        flash("This trivia set is closed.", "error")
        return redirect(url_for("trivia.index"))
    existing = sb.get("trivia_attempts", set_id=set_id, user_id=me)
    if existing:
        return redirect(url_for("trivia.play", set_id=set_id) if existing["status"] == "in_progress" else url_for("trivia.results", set_id=set_id))
    qs = _questions(set_id)
    if not qs:
        flash("This set has no questions yet.", "error")
        return redirect(url_for("trivia.index"))
    paid = current_app.config["PAID_COMPETITIONS_ENABLED"]
    if paid:
        credit = _unused_credit(me)
        # claim the credit atomically: the update only matches while used_at is still NULL
        claimed = sb.update("trivia_entry_credits", {"used_set_id": set_id, "used_at": iso()}, id=credit["id"], used_at=None) if credit else []
        if not claimed:
            flash("You need a trivia entry first. Pay on the Pay page and an admin will credit it.", "error")
            return redirect(url_for("wallet.pay"))
    try:
        sb.insert("trivia_attempts", {"set_id": set_id, "user_id": me, "paid": paid, "total_questions": len(qs),
                                      "current_position": 1, "question_served_at": iso()})
    except Exception:
        pass   # unique(set_id, user_id): a double-click can't create a second attempt
    return redirect(url_for("trivia.play", set_id=set_id))


@bp.get("/trivia/<set_id>/play")
@login_required
def play(set_id):
    s = _set_or_404(set_id)
    attempt = sb.get("trivia_attempts", set_id=set_id, user_id=g.user["id"])
    if not attempt:
        return redirect(url_for("trivia.intro", set_id=set_id))
    if attempt["status"] == "completed":
        return redirect(url_for("trivia.results", set_id=set_id))
    return render_template("trivia/play.html", s=s, total=attempt["total_questions"])


def _current_question(attempt):
    return sb.get("trivia_questions", set_id=attempt["set_id"], position=attempt["current_position"])


def _finish(attempt):
    cfg = current_app.config
    answers = sb.select("trivia_answers", attempt_id=attempt["id"])
    correct = sum(1 for a in answers if a["correct"])
    total = attempt["total_questions"]
    points = sum(a["points"] for a in answers)
    # Real app coins for every finished set, free or paid. The ledger reference is the attempt id and there is only one
    # attempt per student per set, so refreshing or re-submitting can never pay twice.
    coins = min(cfg["TRIVIA_MAX_COINS"], points // max(1, cfg["TRIVIA_POINTS_PER_COIN"]))
    if coins > 0:
        bal = coin_service.award(attempt["user_id"], coins, "trivia", attempt["id"], message=f"+{coins} app coins from trivia!")
        if bal == coin_service.DUPLICATE:
            coins = attempt.get("coins_awarded", 0)   # already paid for this attempt: keep what was recorded
    sb.update("trivia_attempts", {"status": "completed", "completed_at": iso(), "correct_count": correct, "points": points,
                                  "accuracy": round(100 * correct / total, 1) if total else 0, "coins_awarded": coins}, id=attempt["id"])
    social.grant_achievement(attempt["user_id"], "trivia_first")
    if total and correct == total:
        social.grant_achievement(attempt["user_id"], "trivia_ace")


@bp.get("/api/trivia/<set_id>/question")
@login_required
def question(set_id):
    attempt = sb.get("trivia_attempts", set_id=set_id, user_id=g.user["id"])
    if not attempt:
        abort(404)
    if attempt["status"] == "completed":
        return jsonify(done=True, redirect=url_for("trivia.results", set_id=set_id))
    q = _current_question(attempt)
    served = parse_dt(attempt["question_served_at"]) or utcnow()
    left_ms = int(q["time_limit_seconds"] * 1000 - (utcnow() - served).total_seconds() * 1000)
    # NOTE: correct_index is deliberately not included.
    return jsonify(done=False, position=attempt["current_position"], total=attempt["total_questions"], prompt=q["prompt"],
                   options=q["options"], time_limit=q["time_limit_seconds"], remaining_ms=max(0, left_ms), points=attempt["points"])


@bp.post("/api/trivia/<set_id>/answer")
@login_required
@limiter.limit("120 per minute")
def answer(set_id):
    me = g.user["id"]
    attempt = sb.get("trivia_attempts", set_id=set_id, user_id=me)
    if not attempt or attempt["status"] != "in_progress":
        return jsonify(error="No active attempt."), 409
    data = request.get_json(silent=True) or {}
    choice = data.get("choice")
    if choice is not None and (not isinstance(choice, int) or isinstance(choice, bool) or not 0 <= choice <= 3):
        return jsonify(error="Invalid choice."), 400
    q = _current_question(attempt)
    if data.get("position") != attempt["current_position"]:
        return jsonify(error="That question has already been answered."), 409
    ms = int((utcnow() - (parse_dt(attempt["question_served_at"]) or utcnow())).total_seconds() * 1000)
    limit_ms = q["time_limit_seconds"] * 1000
    timed_out = ms > limit_ms + GRACE_MS
    if timed_out:
        choice = None
    correct = (choice is not None) and choice == q["correct_index"]
    pts = score_answer(correct, ms, q["time_limit_seconds"])
    try:
        sb.insert("trivia_answers", {"attempt_id": attempt["id"], "question_id": q["id"], "choice": choice, "correct": correct,
                                     "ms_taken": min(ms, limit_ms), "points": pts})
    except Exception:
        return jsonify(error="That question has already been answered."), 409
    nxt = attempt["current_position"] + 1
    values = {"current_position": nxt, "question_served_at": iso(), "correct_count": attempt["correct_count"] + (1 if correct else 0),
              "points": attempt["points"] + pts}
    sb.update("trivia_attempts", values, id=attempt["id"])
    attempt = {**attempt, **values}
    if nxt > attempt["total_questions"]:
        _finish(attempt)
        return jsonify(recorded=True, done=True, redirect=url_for("trivia.results", set_id=set_id))
    return jsonify(recorded=True, done=False, timed_out=timed_out)


@bp.get("/trivia/<set_id>/results")
@login_required
def results(set_id):
    s = _set_or_404(set_id)
    attempt = sb.get("trivia_attempts", set_id=set_id, user_id=g.user["id"])
    if not attempt or attempt["status"] != "completed":
        return redirect(url_for("trivia.intro", set_id=set_id))
    qs = _questions(set_id)
    ans = {a["question_id"]: a for a in sb.select("trivia_answers", attempt_id=attempt["id"])}
    review = [{"q": q, "a": ans.get(q["id"])} for q in qs]   # answers revealed only now that the set is finished
    done = [a for a in sb.select("trivia_attempts", set_id=set_id, status="completed")]
    done.sort(key=lambda a: (-a["points"], a["completed_at"]))
    people = sb.by_id("profiles", [a["user_id"] for a in done[:10]])
    board = [{"user": people.get(a["user_id"]), **a} for a in done[:10] if people.get(a["user_id"])]
    rank = next((i + 1 for i, a in enumerate(done) if a["user_id"] == g.user["id"]), None)
    avg_ms = int(sum(a["ms_taken"] for a in ans.values()) / max(1, len(ans)))
    return render_template("trivia/results.html", s=s, attempt=attempt, review=review, board=board, rank=rank, avg_s=round(avg_ms / 1000, 1))
