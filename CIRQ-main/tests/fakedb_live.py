"""Python mirror of the cirq_live_* SQL functions (migrations/2026_11_14_live_streaming.sql) for the in-memory test database.

The REAL behaviour (atomicity, locking, concurrency) is proven against PostgreSQL by tests/test_live_sql.py; this mirror exists so the
Flask routes can be tested without a database driver. It is a straight transcription of the SQL, kept deliberately simple.
"""
import re
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace


def _now():
    return datetime.now(timezone.utc)


def _iso(d):
    return d.isoformat()


def _dt(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if isinstance(s, str) else s


def seed_gifts(db, path):
    """Load the gift catalogue straight from the migration so tests can't drift from it."""
    text = open(path).read().split("insert into live_gifts")[1].split("on conflict")[0]
    for code, name, price, tier, dn, dv, icon, color, order in re.findall(
            r"\('(\w+)',\s*'([^']+)',\s*(\d+),\s*'(\w+)',\s*(\d+),\s*(\d+),\s*'(\w+)',\s*'(#\w+)',\s*(\d+)\)", text):
        db.data.setdefault("live_gifts", []).append({"code": code, "name": name, "price": int(price), "host_share_pct": 50, "tier": tier,
            "display_diamonds": int(dn), "diamond_display_value": int(dv), "icon": icon, "color": color, "sort_order": int(order), "active": True})


def _t(db, name):
    return db.data.setdefault(name, [])


def _event(db, session_id, kind, actor_id=None, payload=None, body=None):
    db._insert("live_events", {"session_id": session_id, "kind": kind, "actor_id": actor_id, "body": body, "payload": payload or {}})


def _tick(db, battle_id, force=False):
    b = next((x for x in _t(db, "live_battles") if x["id"] == battle_id), None)
    if not b:
        return "missing"
    parts = [p for p in _t(db, "live_battle_participants") if p["battle_id"] == b["id"]]
    both = (b["session_a"], b["session_b"])
    now = _now()
    if b["status"] == "invited" and (_dt(b["expires_at"]) <= now or force):
        b.update(status="cancelled" if force else "expired", ended_at=_iso(now))
        return b["status"]
    if b["status"] == "countdown" and force:
        for p in parts: p["active"] = False
        b.update(status="cancelled", ended_at=_iso(now))
        for s in both: _event(db, s, "battle_end", None, {"battle_id": b["id"], "outcome": "cancelled"})
        return "cancelled"
    if b["status"] == "countdown" and now >= _dt(b["starts_at"]):
        b["status"] = "active"
        for s in both: _event(db, s, "battle_start", None, {"battle_id": b["id"], "starts_at": b["starts_at"], "ends_at": b["ends_at"]})
    if b["status"] == "active" and (force or now >= _dt(b["ends_at"])):
        sc = {p["side"]: p["score"] for p in parts}
        outcome = "a" if sc["a"] > sc["b"] else "b" if sc["b"] > sc["a"] else "draw"
        win = {"a": b["host_a"], "b": b["host_b"]}.get(outcome)
        for p in parts: p["active"] = False
        b.update(status="ended", ended_at=_iso(now), outcome=outcome, winner_host_id=win)
        for s in both: _event(db, s, "battle_end", None, {"battle_id": b["id"], "outcome": outcome, "winner_host_id": win, "scores": sc})
        return "ended"
    return b["status"]


def _end_session(db, sid, reason):
    s = next((x for x in _t(db, "live_sessions") if x["id"] == sid), None)
    if not s or s["status"] != "live":
        return False
    s.update(status="ended", ended_at=_iso(_now()), end_reason=reason, pinned_event_id=None)
    for bid in {p["battle_id"] for p in _t(db, "live_battle_participants") if p["session_id"] == sid}:
        _tick(db, bid, True)
    for i in _t(db, "live_guest_invites"):
        if i["session_id"] == sid and i["status"] in ("invited", "requested", "accepted"):
            i.update(status="left" if i["status"] == "accepted" else "removed", ended_at=_iso(_now()))
    for v in _t(db, "live_viewers"):
        if v["session_id"] == sid and not v.get("left_at"): v["left_at"] = _iso(_now())
    _event(db, sid, "ended", None, {"reason": reason})
    return True


def _gift(db, p):
    sender, sid, code, req, cap = p["p_sender"], p["p_session"], p["p_gift"], p["p_request"], p.get("p_daily_cap", 0)
    if not req or not 8 <= len(req) <= 80: return {"status": "bad_request"}
    s = next((x for x in _t(db, "live_sessions") if x["id"] == sid), None)
    if not s: return {"status": "session_ended"}
    host = s["host_id"]
    if host == sender: return {"status": "self_gift"}
    prof = {x["id"]: x for x in _t(db, "profiles")}
    tx = _t(db, "live_gift_transactions")
    old = next((t for t in tx if t["sender_id"] == sender and t["request_id"] == req), None)
    if old:
        if old["session_id"] != sid or old["gift_code"] != code: return {"status": "request_reuse"}
        return {"status": "duplicate", "txn_id": old["id"], "gift_code": code, "gross": old["gross_coins"], "host_coins": old["host_coins"],
                "platform_coins": old["platform_coins"], "diamonds": old["displayed_diamonds"], "diamond_value": old["displayed_diamond_value"],
                "balance": prof[sender]["coin_balance"], "host_id": host, "battle_id": old.get("battle_id")}
    if s["status"] != "live": return {"status": "session_ended"}
    if any(b["host_id"] == host and b["user_id"] == sender for b in _t(db, "live_host_blocks")) or \
       any(v["session_id"] == sid and v["user_id"] == sender and v.get("removed") for v in _t(db, "live_viewers")) or \
       any((b["blocker_id"], b["blocked_id"]) in ((host, sender), (sender, host)) for b in _t(db, "blocks")):
        return {"status": "blocked"}
    g = next((x for x in _t(db, "live_gifts") if x["code"] == code and x["active"]), None)
    if not g: return {"status": "gift_unavailable"}
    bal = prof[sender]["coin_balance"]
    if bal < g["price"]: return {"status": "insufficient", "balance": bal, "price": g["price"]}
    if cap:
        spent = sum(t["gross_coins"] for t in tx if t["sender_id"] == sender and _dt(t["created_at"]) > _now() - timedelta(hours=24))
        if spent + g["price"] > cap: return {"status": "cap_reached", "cap": cap, "spent": spent}
    host_coins = g["price"] * g["host_share_pct"] // 100
    platform = g["price"] - host_coins
    txn = str(uuid.uuid4())
    part = next((x for x in _t(db, "live_battle_participants") if x["host_id"] == host and x["active"]), None)
    battle = part["battle_id"] if part else None
    if battle and _tick(db, battle) != "active": battle = None
    db._insert("live_gift_transactions", {"id": txn, "request_id": req, "session_id": sid, "sender_id": sender, "host_id": host, "gift_code": code,
        "gross_coins": g["price"], "host_coins": host_coins, "platform_coins": platform, "displayed_diamonds": g["display_diamonds"],
        "displayed_diamond_value": g["diamond_display_value"], "battle_id": battle})
    led = _t(db, "coin_ledger")
    led.append({"id": str(uuid.uuid4()), "user_id": sender, "amount": -g["price"], "reason": "live_gift_out", "ref": txn, "created_at": _iso(_now())})
    prof[sender]["coin_balance"] -= g["price"]
    if host_coins:
        led.append({"id": str(uuid.uuid4()), "user_id": host, "amount": host_coins, "reason": "live_gift_in", "ref": txn, "created_at": _iso(_now())})
        prof[host]["coin_balance"] += host_coins
    db._insert("live_creator_earnings", {"host_id": host, "session_id": sid, "transaction_id": txn, "coins": host_coins, "ledger_ref": txn})
    for i in range(1, g["display_diamonds"] + 1):
        db._insert("live_diamond_records", {"transaction_id": txn, "host_id": host, "seq": i, "display_value": g["diamond_display_value"], "counts_toward_earnings": False})
    sup = next((x for x in _t(db, "live_supporters") if x["host_id"] == host and x["user_id"] == sender), None)
    if sup: sup.update(total_coins=sup["total_coins"] + g["price"], gifts_count=sup["gifts_count"] + 1, last_gift_at=_iso(_now()))
    else: db._insert("live_supporters", {"host_id": host, "user_id": sender, "total_coins": g["price"], "gifts_count": 1, "last_gift_at": _iso(_now())})
    if not any(x["host_id"] == host and x["user_id"] == sender for x in _t(db, "live_fan_club_members")):
        db._insert("live_fan_club_members", {"host_id": host, "user_id": sender})
    s["gifts_count"] += 1; s["gift_gross"] += g["price"]; s["host_earned"] += host_coins; s["platform_share"] += platform; s["diamonds_displayed"] += g["display_diamonds"]
    scores = None
    if battle:
        for x in _t(db, "live_battle_participants"):
            if x["battle_id"] == battle and x["host_id"] == host: x["score"] += g["price"]
        scores = {x["side"]: x["score"] for x in _t(db, "live_battle_participants") if x["battle_id"] == battle}
    sp = prof[sender]
    _event(db, sid, "gift", sender, {"txn_id": txn, "gift_code": code, "name": g["name"], "icon": g["icon"], "tier": g["tier"], "gross": g["price"],
        "host_coins": host_coins, "diamonds": g["display_diamonds"], "diamond_value": g["diamond_display_value"], "sender": sp["username"],
        "sender_name": sp.get("display_name"), "host_id": host, "battle_id": battle, "scores": scores})
    return {"status": "ok", "txn_id": txn, "gift_code": code, "gross": g["price"], "host_coins": host_coins, "platform_coins": platform,
            "diamonds": g["display_diamonds"], "diamond_value": g["diamond_display_value"], "balance": sp["coin_balance"], "host_id": host,
            "battle_id": battle, "scores": scores}


def _battle_accept(db, p):
    b = next((x for x in _t(db, "live_battles") if x["id"] == p["p_battle"]), None)
    if not b: return "missing"
    if b["host_b"] != p["p_actor"]: return "forbidden"
    if b["status"] != "invited": return "bad_state"
    if _dt(b["expires_at"]) <= _now():
        b.update(status="expired", ended_at=_iso(_now())); return "expired"
    if sum(1 for s in _t(db, "live_sessions") if s["id"] in (b["session_a"], b["session_b"]) and s["status"] == "live") != 2: return "not_live"
    parts = [x for x in _t(db, "live_battle_participants") if x["battle_id"] == b["id"]]
    for part in parts:
        if any(o["host_id"] == part["host_id"] and o["active"] for o in _t(db, "live_battle_participants")): return "busy"
    for part in parts: part["active"] = True
    st = _now() + timedelta(seconds=max(0, p.get("p_countdown", 5)))
    en = st + timedelta(seconds=b["duration_seconds"])
    b.update(status="countdown", accepted_at=_iso(_now()), countdown_seconds=max(0, p.get("p_countdown", 5)), starts_at=_iso(st), ends_at=_iso(en))
    for s in (b["session_a"], b["session_b"]): _event(db, s, "battle_accept", None, {"battle_id": b["id"], "starts_at": _iso(st), "ends_at": _iso(en)})
    return "ok"


def _guest_accept(db, p):
    inv = next((x for x in _t(db, "live_guest_invites") if x["id"] == p["p_invite"]), None)
    if not inv: return "missing"
    s = next(x for x in _t(db, "live_sessions") if x["id"] == inv["session_id"])
    if s["status"] != "live": return "ended"
    actor = p["p_actor"]
    if not ((inv["status"] == "invited" and actor == inv["guest_id"]) or (inv["status"] == "requested" and actor == s["host_id"])):
        return "forbidden" if inv["status"] in ("invited", "requested") else "bad_state"
    if sum(1 for x in _t(db, "live_guest_invites") if x["session_id"] == s["id"] and x["status"] == "accepted") >= p.get("p_max", 4): return "full"
    inv.update(status="accepted", accepted_at=_iso(_now()), consent_at=inv.get("consent_at") or _iso(_now()))
    return "ok"


def handle(db, name, params):
    """Returns the rpc() result object, or None if `name` isn't a live function."""
    res = lambda v: SimpleNamespace(execute=lambda: SimpleNamespace(data=v))
    if name == "cirq_live_send_gift": return res(_gift(db, params))
    if name == "cirq_live_battle_tick": return res(_tick(db, params["p_battle"], params.get("p_force", False)))
    if name == "cirq_live_battle_accept": return res(_battle_accept(db, params))
    if name == "cirq_live_end_session": return res(_end_session(db, params["p_session"], params.get("p_reason", "host_ended")))
    if name == "cirq_live_guest_accept": return res(_guest_accept(db, params))
    if name == "cirq_live_creator_summary":
        tx = [t for t in _t(db, "live_gift_transactions") if t["host_id"] == params["p_host"]]
        return res({"gifts_received": len(tx), "gross_value": sum(t["gross_coins"] for t in tx), "host_earnings": sum(t["host_coins"] for t in tx),
                    "platform_share": sum(t["platform_coins"] for t in tx), "diamonds_displayed": sum(t["displayed_diamonds"] for t in tx),
                    "sessions": sum(1 for s in _t(db, "live_sessions") if s["host_id"] == params["p_host"])})
    if name in ("cirq_live_top_gifters", "cirq_live_host_ranking"):
        since = params.get("p_since")
        agg = {}
        for t in _t(db, "live_gift_transactions"):
            if since and t["created_at"] < since: continue
            if name == "cirq_live_top_gifters":
                if params.get("p_host") and t["host_id"] != params["p_host"]: continue
                key, val = t["sender_id"], t["gross_coins"]
            else:
                key, val = t["host_id"], t["host_coins"]
            a = agg.setdefault(key, [0, 0]); a[0] += val; a[1] += 1
        rows = sorted(agg.items(), key=lambda kv: (-kv[1][0], -kv[1][1]))[:params["p_limit"]]
        if name == "cirq_live_top_gifters": return res([{"user_id": k, "total_coins": v[0], "gifts": v[1]} for k, v in rows])
        return res([{"host_id": k, "host_coins": v[0], "gifts": v[1]} for k, v in rows])
    return None
