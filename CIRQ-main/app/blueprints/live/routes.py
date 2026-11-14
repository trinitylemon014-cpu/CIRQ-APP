"""CIRQ LIVE pages and JSON API. Thin by design: authenticate, validate input, call app.services.live_service, return JSON.

Pages            GET  /live/                    discovery
                 GET  /live/go                  start a live (host eligibility, title, category, mode)
                 GET  /live/<sid>               the live room (a shell; live.js loads everything through the API below)
                 GET  /live/dashboard           creator wallet
                 GET  /live/rankings            top hosts / top gifters (weekly + all time)
                 GET|POST /live/settings        privacy (live alerts, viewer-list visibility, fan clubs)
JSON API         every response is {"ok": true|false, ...}; errors carry "error" (safe to show) and sometimes "code".
  discovery      GET  /live/api/sessions?category=
  lifecycle      POST /live/api/start {title,category,mode,visibility}   -> {session_id, resumed}
                 POST /live/api/<sid>/join | leave | end
                 GET  /live/api/<sid>/state                              -> full room state
                 GET  /live/api/<sid>/poll?since=<event id>&hb=0|1&battle=<id>
                 POST /live/api/<sid>/token {target: self|opponent}      -> {url, token, role, can_publish, sources}
  chat           POST /live/api/<sid>/chat {body} | react {emoji} | pin {event_id} | unpin | delete {event_id}
  moderation     POST /live/api/<sid>/mod {action, username, minutes?, reason?} | report {reason, username?, event_id?, details?}
                 POST /live/api/<sid>/settings {chat_enabled?, slow_mode_seconds?, guests_enabled?}
  guests         POST /live/api/<sid>/guests/invite {username, media} | request {media} | leave
                 POST /live/api/<sid>/guests/<invite_id>/respond {accept} | remove | permissions {audio?, video?}
  gifts          GET  /live/api/gifts ;  POST /live/api/<sid>/gift {gift, request_id}
  PK battles     POST /live/api/<sid>/battle/invite {username, minutes} ;  POST /live/api/battle/<bid>/respond {accept} | end
  fan clubs      POST /live/api/fanclub/<username>/leave
Admin            GET /admin/live ; POST /admin/live/grant | /admin/live/<sid>/end | /admin/live/reports/<rid>/resolve
Every POST needs the CSRF token (X-CSRFToken header). Sensitive actions re-check ownership/role server-side; nothing the browser sends
(prices, host ids, scores, balances) is trusted.
"""
from flask import Blueprint, render_template, request, jsonify, g, abort, redirect, url_for, flash, current_app

from app.decorators import login_required, admin_required
from app.extensions import limiter
from app.services import supabase_service as sb, live_service as live, livekit_service as lk
from app.services.audit_service import audit
from app.utils import iso

bp = Blueprint("live", __name__)


def _ukey():
    return f"u:{g.user['id']}" if getattr(g, "user", None) else "anon"


def _ok(**kw):
    return jsonify(ok=True, **kw)


def _err(message, status=400, **kw):
    return jsonify(ok=False, error=message, **kw), status


def _body():
    return request.get_json(silent=True) or {}


def _sess(sid):
    s = live.get_session(sid)
    if not s:
        abort(404)
    return s


def _require_feature():
    if not live.feature_on():
        abort(404)


# ------------------------------------------------------------------ pages
@bp.get("/live/")
@login_required
def index():
    _require_feature()
    ok, why = live.can_host(g.user)
    return render_template("live/index.html", categories=live.CATEGORIES, can_host=ok, host_message=why, provider_ready=lk.configured(),
                           poll_seconds=current_app.config["LIVE_POLL_SECONDS"] * 3)


@bp.get("/live/go")
@login_required
def go():
    _require_feature()
    ok, why = live.can_host(g.user)
    mine = sb.get("live_sessions", host_id=g.user["id"], status="live")
    return render_template("live/go.html", categories=live.CATEGORIES, can_host=ok, host_message=why, provider_ready=lk.configured(), current=mine,
                           is_private=bool(g.user.get("is_private")))


@bp.get("/live/<uuid:sid>")
@login_required
def room(sid):
    _require_feature()
    s = _sess(str(sid))
    host = sb.get("profiles", "username,display_name", id=s["host_id"]) or {}
    return render_template("live/room.html", sid=s["id"], title=s["title"], host=host, is_host=s["host_id"] == g.user["id"])


@bp.get("/live/dashboard")
@login_required
def dashboard():
    _require_feature()
    return render_template("live/dashboard.html", d=live.creator_dashboard(g.user), clubs=live.my_fan_clubs(g.user))


@bp.get("/live/rankings")
@login_required
def rankings():
    _require_feature()
    return render_template("live/rankings.html", r=live.rankings(20))


@bp.route("/live/settings", methods=["GET", "POST"])
@login_required
def settings():
    _require_feature()
    if request.method == "POST":
        live.update_privacy(g.user, request.form.get("notify") == "on", request.form.get("show_in_list") == "on")
        flash("Live settings saved.", "success")
        return redirect(url_for("live.settings"))
    return render_template("live/settings.html", me=sb.get("profiles", id=g.user["id"]), clubs=live.my_fan_clubs(g.user))


# ------------------------------------------------------------------ API: discovery and lifecycle
@bp.get("/live/api/sessions")
@login_required
@limiter.limit("60 per minute", key_func=_ukey)
def api_sessions():
    _require_feature()
    return _ok(sessions=live.active_sessions(g.user, request.args.get("category")))


@bp.get("/live/api/gifts")
@login_required
def api_gifts():
    return _ok(gifts=live.gifts_catalog(), balance=g.user.get("coin_balance", 0))


@bp.post("/live/api/start")
@login_required
@limiter.limit("12 per hour", key_func=_ukey)
def api_start():
    _require_feature()
    d = _body()
    sess, why, resumed = live.start_session(g.user, d.get("title"), d.get("category"), d.get("mode"), d.get("visibility"))
    if not sess:
        return _err(why, 403 if "verified" in why or "approved" in why or "switched off" in why else 400)
    return _ok(session_id=sess["id"], resumed=resumed, url=url_for("live.room", sid=sess["id"]))


@bp.post("/live/api/<uuid:sid>/join")
@login_required
@limiter.limit("30 per minute", key_func=_ukey)
def api_join(sid):
    s = _sess(str(sid))
    err = live.join(s, g.user)
    if err:
        return _err(err[1], 410 if err[0] == "ended" else 403, code=err[0])
    return _ok(state=live.room_state(s, g.user))


@bp.post("/live/api/<uuid:sid>/leave")
@login_required
def api_leave(sid):
    live.leave(_sess(str(sid)), g.user)
    return _ok()


@bp.get("/live/api/<uuid:sid>/state")
@login_required
@limiter.limit("60 per minute", key_func=_ukey)
def api_state(sid):
    s = _sess(str(sid))
    err = live.access_error(s, g.user) if s["status"] == "live" else None
    if err:
        return _err(err[1], 403, code=err[0])
    return _ok(state=live.room_state(s, g.user))


@bp.get("/live/api/<uuid:sid>/poll")
@login_required
@limiter.limit("90 per minute", key_func=_ukey)
def api_poll(sid):
    s = _sess(str(sid))
    try:
        since = max(int(request.args.get("since", 0)), 0)
    except ValueError:
        since = 0
    out = live.poll(s, g.user, since, heartbeat=request.args.get("hb") == "1", battle_id=request.args.get("battle") or None)
    if out["status"] in ("denied", "removed"):
        return jsonify(ok=False, **out), 403
    return jsonify(ok=True, **out)


@bp.post("/live/api/<uuid:sid>/token")
@login_required
@limiter.limit("20 per minute", key_func=_ukey)
def api_token(sid):
    s = _sess(str(sid))
    payload, why = live.issue_token(s, g.user, "opponent" if _body().get("target") == "opponent" else "self")
    if not payload:
        return _err(why, 503 if not lk.configured() else 403)
    resp = jsonify(ok=True, **payload)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@bp.post("/live/api/<uuid:sid>/end")
@login_required
def api_end(sid):
    s = _sess(str(sid))
    if live.authority(s, g.user) not in ("host", "staff"):
        return _err("Only the host can end this live.", 403)
    ended = live.end_session(s, g.user, "host_ended" if s["host_id"] == g.user["id"] else "ended_by_staff")
    return _ok(ended=ended)


# ------------------------------------------------------------------ API: chat and moderation
@bp.post("/live/api/<uuid:sid>/chat")
@login_required
@limiter.limit("45 per minute", key_func=_ukey)
def api_chat(sid):
    s = _sess(str(sid))
    if g.user["id"] != s["host_id"] and live.access_error(s, g.user):
        return _err("You can't chat in this live.", 403)
    ev, why = live.post_chat(s, g.user, _body().get("body"))
    return _ok(id=ev["id"]) if ev else _err(why, 429 if "fast" in why or "Slow" in why or "Too many" in why else 400)


@bp.post("/live/api/<uuid:sid>/react")
@login_required
@limiter.limit("60 per minute", key_func=_ukey)
def api_react(sid):
    s = _sess(str(sid))
    if g.user["id"] != s["host_id"] and live.access_error(s, g.user):
        return _err("You can't react in this live.", 403)
    ok, why = live.post_reaction(s, g.user, _body().get("emoji"))
    return _ok() if ok else _err(why, 429)


def _result(res):
    ok, why = res[0], res[1]
    return _ok() if ok else _err(why, 403 if "can't" in why or "Only" in why or "Not allowed" in why else 400)


@bp.post("/live/api/<uuid:sid>/pin")
@login_required
def api_pin(sid):
    return _result(live.pin_message(_sess(str(sid)), g.user, _body().get("event_id")))


@bp.post("/live/api/<uuid:sid>/unpin")
@login_required
def api_unpin(sid):
    return _result(live.unpin_message(_sess(str(sid)), g.user))


@bp.post("/live/api/<uuid:sid>/delete")
@login_required
def api_delete(sid):
    return _result(live.delete_message(_sess(str(sid)), g.user, _body().get("event_id")))


@bp.post("/live/api/<uuid:sid>/mod")
@login_required
@limiter.limit("60 per minute", key_func=_ukey)
def api_mod(sid):
    d = _body()
    return _result(live.moderate(_sess(str(sid)), g.user, d.get("action"), d.get("username"), d.get("minutes"), d.get("reason")))


@bp.post("/live/api/<uuid:sid>/report")
@login_required
@limiter.limit("20 per hour", key_func=_ukey)
def api_report(sid):
    d = _body()
    s = _sess(str(sid))
    ok, msg = live.report(s, g.user, d.get("reason"), d.get("username"), d.get("event_id"), d.get("details"))
    return _ok(message=msg) if ok else _err(msg)


@bp.post("/live/api/<uuid:sid>/settings")
@login_required
def api_settings(sid):
    d = _body()
    return _result(live.update_settings(_sess(str(sid)), g.user, d.get("chat_enabled"), d.get("slow_mode_seconds"), d.get("guests_enabled")))


# ------------------------------------------------------------------ API: guests
@bp.post("/live/api/<uuid:sid>/guests/invite")
@login_required
@limiter.limit("30 per minute", key_func=_ukey)
def api_guest_invite(sid):
    d = _body()
    return _result(live.invite_guest(_sess(str(sid)), g.user, d.get("username"), d.get("media", "video")))


@bp.post("/live/api/<uuid:sid>/guests/request")
@login_required
@limiter.limit("10 per minute", key_func=_ukey)
def api_guest_request(sid):
    return _result(live.request_guest(_sess(str(sid)), g.user, _body().get("media", "video")))


@bp.post("/live/api/<uuid:sid>/guests/leave")
@login_required
def api_guest_leave(sid):
    return _ok(left=live.leave_guest(_sess(str(sid)), g.user))


@bp.post("/live/api/<uuid:sid>/guests/<uuid:iid>/respond")
@login_required
def api_guest_respond(sid, iid):
    return _result(live.respond_guest(_sess(str(sid)), g.user, str(iid), bool(_body().get("accept"))))


@bp.post("/live/api/<uuid:sid>/guests/<uuid:iid>/remove")
@login_required
def api_guest_remove(sid, iid):
    return _result(live.remove_guest(_sess(str(sid)), g.user, str(iid)))


@bp.post("/live/api/<uuid:sid>/guests/<uuid:iid>/permissions")
@login_required
def api_guest_permissions(sid, iid):
    d = _body()
    return _result(live.set_guest_permissions(_sess(str(sid)), g.user, str(iid), d.get("audio"), d.get("video")))


# ------------------------------------------------------------------ API: gifts
@bp.post("/live/api/<uuid:sid>/gift")
@login_required
@limiter.limit("60 per minute", key_func=_ukey)
def api_gift(sid):
    d = _body()
    s = _sess(str(sid))
    code, req = str(d.get("gift") or "")[:30], str(d.get("request_id") or "")[:80]
    payload, status = live.send_gift(g.user, s, code, req)
    resp = jsonify(payload)
    resp.status_code = status
    return resp


# ------------------------------------------------------------------ API: PK battles and fan clubs
@bp.post("/live/api/<uuid:sid>/battle/invite")
@login_required
@limiter.limit("20 per hour", key_func=_ukey)
def api_battle_invite(sid):
    d = _body()
    ok, why, bid = live.battle_invite(_sess(str(sid)), g.user, d.get("username"), d.get("minutes"))
    return _ok(battle_id=bid) if ok else _err(why)


@bp.post("/live/api/battle/<uuid:bid>/respond")
@login_required
def api_battle_respond(bid):
    return _result(live.battle_respond(str(bid), g.user, bool(_body().get("accept"))))


@bp.post("/live/api/battle/<uuid:bid>/end")
@login_required
def api_battle_end(bid):
    return _result(live.battle_end(str(bid), g.user))


@bp.post("/live/api/fanclub/<username>/leave")
@login_required
def api_fanclub_leave(username):
    live.leave_fan_club(g.user, username)
    return _ok()


# ------------------------------------------------------------------ admin
@bp.get("/admin/live")
@admin_required
def admin_live():
    sessions = sb.select("live_sessions", order="started_at", limit=40)
    hosts = sb.by_id("profiles", [s["host_id"] for s in sessions])
    reports = sb.select("live_reports", order="created_at", limit=60, status="open")
    ppl = sb.by_id("profiles", [r["reporter_id"] for r in reports] + [r["target_user_id"] for r in reports if r.get("target_user_id")])
    granted = sb.select("profiles", "username,display_name", live_host_granted=True)
    return render_template("admin/live.html", sessions=sessions, hosts=hosts, reports=reports, people=ppl, granted=granted,
                           provider_ready=lk.configured(), eligibility=current_app.config["LIVE_ELIGIBILITY"])


@bp.post("/admin/live/grant")
@admin_required
def admin_live_grant():
    name = (request.form.get("username") or "").strip().lower().lstrip("@")
    p = sb.get("profiles", username=name)
    if not p:
        flash("No such user.", "error")
    else:
        on = request.form.get("grant") == "1"
        sb.update("profiles", {"live_host_granted": on}, id=p["id"])
        audit(g.user["id"], "live_access_granted" if on else "live_access_revoked", "profile", p["id"])
        flash(f"Live hosting {'enabled' if on else 'disabled'} for @{p['username']}.", "success")
    return redirect(url_for("live.admin_live"))


@bp.post("/admin/live/<uuid:sid>/end")
@admin_required
def admin_live_end(sid):
    s = _sess(str(sid))
    live.end_session(s, g.user, "ended_by_staff")
    flash("Live ended.", "success")
    return redirect(url_for("live.admin_live"))


@bp.post("/admin/live/reports/<uuid:rid>/resolve")
@admin_required
def admin_live_report_resolve(rid):
    status = request.form.get("status")
    if status in ("actioned", "dismissed"):
        sb.update("live_reports", {"status": status, "resolved_by": g.user["id"], "resolved_at": iso()}, id=str(rid))
    return redirect(url_for("live.admin_live"))
