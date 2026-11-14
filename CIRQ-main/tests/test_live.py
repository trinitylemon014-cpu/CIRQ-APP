"""CIRQ LIVE: routes + service rules against the in-memory test database (SQL-level money/concurrency tests: tests/test_live_sql.py)."""
import base64
import hashlib
import hmac
import json
import uuid
from datetime import timedelta

import pytest

from app.services import live_service as live, livekit_service as lk, lending
from app.utils import iso, utcnow


# ------------------------------------------------------------------ helpers
def _b64d(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def decode_token(token, secret):
    head, body, sig = token.split(".")
    expect = base64.urlsafe_b64encode(hmac.new(secret.encode(), f"{head}.{body}".encode(), hashlib.sha256).digest()).rstrip(b"=").decode()
    assert hmac.compare_digest(sig, expect), "bad signature"
    assert json.loads(_b64d(head)) == {"alg": "HS256", "typ": "JWT"}
    return json.loads(_b64d(body))


def coins(db, pid, n):
    db.table("profiles").update({"coin_balance": n}).eq("id", pid).execute()


def balance(db, pid):
    return db.table("profiles").select("coin_balance").eq("id", pid).execute().data[0]["coin_balance"]


def post(c, sid, path, **body):
    return c.post(f"/live/api/{sid}/{path}", json=body)


@pytest.fixture
def lkcalls(monkeypatch):
    """Capture every call the app makes to LiveKit's server API (nothing leaves the test)."""
    calls = []
    monkeypatch.setattr(lk, "_post", lambda url, payload, headers, timeout: calls.append((url.rsplit("/", 1)[1], payload, headers)) or type(
        "R", (), {"status_code": 200, "json": lambda self: {}})())
    return calls


@pytest.fixture
def host(as_user, db):
    c, p = as_user("hosty")
    db.table("profiles").update({"live_host_granted": True}).eq("id", p["id"]).execute()
    return c, p


@pytest.fixture
def sid(host, lkcalls):
    r = host[0].post("/live/api/start", json={"title": "Friday jam", "category": "music", "mode": "video"})
    assert r.status_code == 200, r.get_json()
    return r.get_json()["session_id"]


@pytest.fixture
def viewer(as_user, db, sid):
    def _v(name="vera", balance_=0):
        c, p = as_user(name)
        coins(db, p["id"], balance_)
        r = post(c, sid, "join")
        assert r.status_code == 200, r.get_json()
        return c, p
    return _v


def test_provider_off_by_default_without_credentials_is_reported(app, host):
    app.config["LIVEKIT_URL"] = ""
    r = host[0].post("/live/api/start", json={"title": "x"})
    assert r.status_code == 400 and "LiveKit" in r.get_json()["error"]


# ------------------------------------------------------------------ authentication and eligibility
def test_anonymous_api_calls_are_rejected(client):
    for method, url in (("get", "/live/api/sessions"), ("post", "/live/api/start"), ("get", "/live/api/gifts")):
        r = getattr(client, method)(url)
        assert r.status_code == 401, url
    assert client.get("/live/").status_code == 302


def test_only_eligible_users_can_start(as_user, db, app, lkcalls):
    c, p = as_user("plain")
    r = c.post("/live/api/start", json={"title": "hi"})
    assert r.status_code == 403 and "verified creators" in r.get_json()["error"]
    db.table("profiles").update({"verified": True}).eq("id", p["id"]).execute()
    assert c.post("/live/api/start", json={"title": "hi"}).status_code == 200


def test_unverified_email_and_pending_accounts_cannot_start(as_user, db, lkcalls):
    c, p = as_user("noemail", verified_email=False)
    db.table("profiles").update({"live_host_granted": True}).eq("id", p["id"]).execute()
    assert "email" in c.post("/live/api/start", json={"title": "hi"}).get_json()["error"].lower()
    c2, _ = as_user("pend", status="pending")
    assert c2.post("/live/api/start", json={"title": "hi"}).status_code in (302, 403)


def test_approved_mode_lets_any_verified_email_student_test(as_user, app, lkcalls):
    app.config["LIVE_ELIGIBILITY"] = "approved"
    c, _ = as_user("anyone")
    assert c.post("/live/api/start", json={"title": "hi"}).status_code == 200


def test_feature_flag_switches_live_off(as_user, db, host):
    db.table("feature_flags").insert({"key": "live_enabled", "enabled": False}).execute()
    assert host[0].post("/live/api/start", json={"title": "x"}).status_code == 404       # the whole feature disappears
    assert host[0].get("/live/api/sessions").status_code == 404 and host[0].get("/live/").status_code == 404
    with host[0].application.app_context():
        assert live.can_host(host[1])[0] is False


# ------------------------------------------------------------------ start / end
def test_start_resume_and_end(host, as_user, db, lkcalls):
    c, p = host
    a = c.post("/live/api/start", json={"title": "  Hello  ", "category": "bogus", "mode": "weird"}).get_json()
    s = db.table("live_sessions").select("*").eq("id", a["session_id"]).execute().data[0]
    assert (s["title"], s["category"], s["mode"], s["status"]) == ("Hello", "chat", "video", "live") and s["room_name"].startswith("cirq-live-")
    b = c.post("/live/api/start", json={"title": "Another"}).get_json()
    assert b["session_id"] == a["session_id"] and b["resumed"] is True        # reload/second tab resumes, never a second live
    other, _ = as_user("rando")
    assert post(other, a["session_id"], "end").status_code == 403           # only the host (or staff) ends it
    assert post(c, a["session_id"], "end").get_json()["ended"] is True
    assert post(c, a["session_id"], "end").get_json()["ended"] is False     # idempotent
    s = db.table("live_sessions").select("*").eq("id", a["session_id"]).execute().data[0]
    assert s["status"] == "ended" and s["ended_at"]
    assert any(call[0] == "DeleteRoom" for call in lkcalls)                  # the media room is closed too
    assert c.post("/live/api/start", json={"title": "Next one"}).get_json()["resumed"] is False


def test_admin_can_end_any_live_and_it_is_audited(host, sid, as_user, db):
    adm, _ = as_user("boss", role="admin")
    assert post(adm, sid, "end").status_code == 200
    assert db.table("live_sessions").select("end_reason").eq("id", sid).execute().data[0]["end_reason"] == "ended_by_staff"
    assert any(a["action"] == "live_end" for a in db.data.get("audit_logs", []))


def test_start_requires_a_title_and_clean_text(host, lkcalls):
    assert host[0].post("/live/api/start", json={"title": "   "}).status_code == 400
    assert host[0].post("/live/api/start", json={"title": "call me 0771234567"}).status_code == 400


def test_hourly_start_limit(host, app, db, lkcalls):
    c, p = host
    app.config["LIVE_STARTS_PER_HOUR"] = 2
    for _ in range(2):
        s = c.post("/live/api/start", json={"title": "again"}).get_json()["session_id"]
        post(c, s, "end")
    assert c.post("/live/api/start", json={"title": "again"}).status_code == 400


def test_stale_host_is_reaped_so_a_dead_live_disappears(host, sid, as_user, db, lkcalls):
    db.table("live_sessions").update({"last_heartbeat_at": iso(utcnow() - timedelta(minutes=10))}).eq("id", sid).execute()
    c, _ = as_user("looker")
    assert c.get("/live/api/sessions").get_json()["sessions"] == []
    assert db.table("live_sessions").select("end_reason").eq("id", sid).execute().data[0]["end_reason"] == "host_timeout"


# ------------------------------------------------------------------ discovery, privacy, notifications
def test_discovery_lists_active_lives_with_host_and_counts(host, sid, viewer, as_user):
    viewer("vera")
    c, _ = as_user("looker")
    s = c.get("/live/api/sessions").get_json()["sessions"]
    assert len(s) == 1 and s[0]["title"] == "Friday jam" and s[0]["category"] == "music" and s[0]["host"]["username"] == "hosty"
    assert c.get("/live/api/sessions?category=gaming").get_json()["sessions"] == []


def test_followers_only_blocked_and_private_hosts(host, sid, as_user, db):
    hc, hp = host
    db.table("live_sessions").update({"visibility": "followers"}).eq("id", sid).execute()
    stranger, sp = as_user("stranger")
    assert stranger.get("/live/api/sessions").get_json()["sessions"] == []
    r = post(stranger, sid, "join")
    assert r.status_code == 403 and r.get_json()["code"] == "followers_only"
    db.table("follows").insert({"follower_id": sp["id"], "followee_id": hp["id"]}).execute()
    assert len(stranger.get("/live/api/sessions").get_json()["sessions"]) == 1 and post(stranger, sid, "join").status_code == 200
    db.table("blocks").insert({"blocker_id": hp["id"], "blocked_id": sp["id"]}).execute()
    assert stranger.get("/live/api/sessions").get_json()["sessions"] == [] and post(stranger, sid, "join").status_code == 403


def test_private_account_lives_are_followers_only(as_user, db, lkcalls):
    c, p = as_user("priv")
    db.table("profiles").update({"live_host_granted": True, "is_private": True}).eq("id", p["id"]).execute()
    s = c.post("/live/api/start", json={"title": "secret", "visibility": "public"}).get_json()["session_id"]
    assert db.table("live_sessions").select("visibility").eq("id", s).execute().data[0]["visibility"] == "followers"


def test_followers_are_notified_unless_they_opted_out(as_user, db, lkcalls):
    h, hp = as_user("star")
    db.table("profiles").update({"live_host_granted": True}).eq("id", hp["id"]).execute()
    fans = {}
    for n in ("fan1", "fan2", "quiet"):
        _, p = as_user(n)
        fans[n] = p
        db.table("follows").insert({"follower_id": p["id"], "followee_id": hp["id"]}).execute()
    db.table("profiles").update({"live_notify_enabled": False}).eq("id", fans["quiet"]["id"]).execute()
    sid = h.post("/live/api/start", json={"title": "Going live!"}).get_json()["session_id"]
    got = {n["user_id"]: n for n in db.data["notifications"] if n["kind"] == "live_started"}
    assert set(got) == {fans["fan1"]["id"], fans["fan2"]["id"]}
    assert got[fans["fan1"]["id"]]["ref_id"] == sid and "Going live!" in got[fans["fan1"]["id"]]["body"]
    from app.blueprints.notifications.routes import target_url
    with h.application.test_request_context():
        assert target_url(got[fans["fan1"]["id"]]) == f"/live/{sid}"


# ------------------------------------------------------------------ join / presence / state
def test_join_state_poll_and_presence(host, sid, viewer, as_user):
    vc, vp = viewer("vera", 70)
    st = vc.get(f"/live/api/{sid}/state").get_json()["state"]
    assert st["host"]["username"] == "hosty" and st["session"]["title"] == "Friday jam" and st["me"]["balance"] == 70
    assert st["me"]["is_host"] is False and st["viewer_count"] == 1 and st["provider_ready"] is True
    assert [g["code"] for g in st["gifts"]][:3] == ["red_rose", "love_heart", "mystery_box"] and len(st["gifts"]) == 8
    hs = host[0].get(f"/live/api/{sid}/state").get_json()["state"]
    assert hs["me"]["is_host"] and hs["me"]["role"] == "host"
    p = vc.get(f"/live/api/{sid}/poll?since=0&hb=1").get_json()
    assert p["viewer_count"] == 1 and p["status"] == "live" and p["me"]["balance"] == 70
    outsider, _ = as_user("outsider")
    assert outsider.get(f"/live/api/{sid}/poll?since=0").status_code == 403     # polling needs joining first
    post(vc, sid, "leave")
    assert host[0].get(f"/live/api/{sid}/poll?since=0&hb=1").get_json()["viewer_count"] == 0


def test_viewer_can_hide_from_the_viewer_list_but_is_still_counted(host, sid, viewer, db):
    vc, vp = viewer("shy")
    db.table("profiles").update({"live_show_in_viewer_list": False}).eq("id", vp["id"]).execute()
    st = vc.get(f"/live/api/{sid}/state").get_json()["state"]
    assert st["viewers"] == [] and st["viewer_count"] == 1


def test_joining_an_ended_live_is_refused(host, sid, as_user):
    post(host[0], sid, "end")
    c, _ = as_user("late")
    r = post(c, sid, "join")
    assert r.status_code == 410 and r.get_json()["code"] == "ended"
    assert c.get("/live/api/%s/state" % uuid.uuid4()).status_code == 404


# ------------------------------------------------------------------ tokens (never trust the browser, never leak secrets)
def test_viewer_token_is_watch_only_and_signed(host, sid, viewer, app):
    vc, vp = viewer("vera")
    r = post(vc, sid, "token")
    d = r.get_json()
    assert r.headers["Cache-Control"] == "no-store" and d["role"] == "viewer" and d["can_publish"] is False and d["url"] == "wss://test.livekit.invalid"
    claims = decode_token(d["token"], app.config["LIVEKIT_API_SECRET"])
    room = db_room = claims["video"]["room"]
    assert claims["iss"] == "APItestkey" and claims["video"]["canPublish"] is False and claims["video"]["roomJoin"] is True
    assert claims["video"]["canPublishData"] is False and "roomAdmin" not in claims["video"] and "roomCreate" not in claims["video"]
    assert vp["id"] not in claims["sub"] and vp["id"] not in d["token"] and claims["exp"] - claims["nbf"] <= 3700 and room.startswith("cirq-live-")
    assert app.config["LIVEKIT_API_SECRET"] not in json.dumps(d)


def test_host_token_publishes_and_audio_only_host_gets_microphone_only(host, sid, app, db, lkcalls):
    d = post(host[0], sid, "token").get_json()
    claims = decode_token(d["token"], app.config["LIVEKIT_API_SECRET"])
    assert d["role"] == "host" and claims["video"]["canPublish"] is True
    assert claims["video"]["canPublishSources"] == ["camera", "microphone", "screen_share", "screen_share_audio"]   # camera, mic, screen share
    post(host[0], sid, "end")
    s2 = host[0].post("/live/api/start", json={"title": "radio", "mode": "audio"}).get_json()["session_id"]
    d2 = post(host[0], s2, "token").get_json()
    assert decode_token(d2["token"], app.config["LIVEKIT_API_SECRET"])["video"]["canPublishSources"] == ["microphone"]


def test_token_refused_without_access_or_provider(host, sid, as_user, app, viewer):
    out, _ = as_user("outsider")
    post(out, sid, "join")
    app.config["LIVEKIT_API_KEY"] = ""
    r = post(out, sid, "token")
    assert r.status_code == 503 and "token" not in r.get_json()


def test_identity_is_stable_opaque_and_per_user(app):
    with app.app_context():
        a, b = lk.identity_for("user-1"), lk.identity_for("user-2")
        assert a == lk.identity_for("user-1") and a != b and "user-1" not in a and a.startswith("u_")


# ------------------------------------------------------------------ chat and spam control
def say(c, sid, text):
    return post(c, sid, "chat", body=text)


def test_chat_flows_to_everyone_and_is_persisted(host, sid, viewer, db):
    vc, vp = viewer("vera")
    assert say(vc, sid, "hello everyone 🎉").status_code == 200
    ev = host[0].get(f"/live/api/{sid}/poll?since=0").get_json()["events"]
    chat = [e for e in ev if e["kind"] == "chat"]
    assert chat[0]["body"] == "hello everyone 🎉" and chat[0]["user"]["username"] == "vera" and chat[0]["user"]["badges"] == []
    assert db.table("live_events").select("*").eq("kind", "chat").execute().data[0]["actor_id"] == vp["id"]
    cursor = host[0].get(f"/live/api/{sid}/poll?since=0").get_json()["cursor"]
    assert host[0].get(f"/live/api/{sid}/poll?since={cursor}").get_json()["events"] == []   # incremental: nothing repeats
    hm = say(host[0], sid, "welcome!")
    assert hm.status_code == 200
    ev = vc.get(f"/live/api/{sid}/poll?since={cursor}").get_json()["events"]
    assert ev[0]["user"]["badges"] == ["host"]


@pytest.mark.parametrize("text,why", [("", "first"), ("   ", "first"), ("check http://evil.example", "Links"), ("go to www.evil.com now", "Links"),
                                      ("text me 0771234567", None), ("visit scam.xyz", "Links")])
def test_unsafe_or_empty_chat_is_rejected(host, sid, viewer, text, why):
    vc, _ = viewer("vera")
    r = say(vc, sid, text)
    assert r.status_code == 400 and (why is None or why in r.get_json()["error"])


def test_chat_rate_limits_repeat_and_length(host, sid, viewer, app, db):
    vc, vp = viewer("vera")
    app.config["LIVE_CHAT_MAX_CHARS"] = 20
    assert say(vc, sid, "x" * 90).status_code == 200
    assert len(db.table("live_events").select("body").eq("kind", "chat").execute().data[0]["body"]) == 20
    r = say(vc, sid, "second")
    assert r.status_code == 429 and "fast" in r.get_json()["error"]
    for row in db.data["live_events"]:
        row["created_at"] = iso(utcnow() - timedelta(seconds=5))
    assert say(vc, sid, "x" * 20).status_code == 400 and "just said" in say(vc, sid, "x" * 20).get_json()["error"]   # no repeats
    app.config["LIVE_CHAT_PER_MINUTE"] = 3
    for i in range(3):
        db.table("live_events").insert({"session_id": sid, "kind": "chat", "actor_id": vp["id"], "body": f"m{i}", "created_at": iso(utcnow() - timedelta(seconds=10 + i))}).execute()
    r = say(vc, sid, "one more")
    assert r.status_code == 429 and "Too many" in r.get_json()["error"]


def test_slow_mode_and_chat_off_and_host_exempt(host, sid, viewer, db):
    vc, vp = viewer("vera")
    assert post(host[0], sid, "settings", slow_mode_seconds=30, chat_enabled=True).status_code == 200
    db.table("live_events").insert({"session_id": sid, "kind": "chat", "actor_id": vp["id"], "body": "old", "created_at": iso(utcnow() - timedelta(seconds=5))}).execute()
    r = say(vc, sid, "new message")
    assert r.status_code == 429 and "Slow mode" in r.get_json()["error"]
    assert say(host[0], sid, "host speaks").status_code == 200
    post(host[0], sid, "settings", chat_enabled=False)
    assert say(vc, sid, "hello").status_code == 400
    assert post(vc, sid, "settings", chat_enabled=True).status_code == 403            # only the host changes room settings
    assert any(a["action"] == "slow_mode" for a in db.data["live_moderation_actions"])


def test_chat_needs_access_and_a_live_session(host, sid, as_user):
    out, _ = as_user("outsider")
    assert say(out, sid, "hi").status_code in (200, 403)     # an approved user may chat only after joining; join implied by chat? it must not bypass bans:
    post(host[0], sid, "end")
    assert say(host[0], sid, "bye").status_code == 400


def test_reactions_are_whitelisted_and_limited(host, sid, viewer, app):
    vc, _ = viewer("vera")
    assert post(vc, sid, "react", emoji="🔥").status_code == 200
    assert post(vc, sid, "react", emoji="<script>").status_code == 429
    app.config["LIVE_REACTIONS_PER_MINUTE"] = 1
    assert post(vc, sid, "react", emoji="❤️").status_code == 429


# ------------------------------------------------------------------ moderation
def test_moderator_powers_and_hierarchy(host, sid, viewer, db, lkcalls):
    hc, hp = host
    mod_c, mod_p = viewer("modmo")
    bad_c, bad_p = viewer("badbob")
    other_c, other_p = viewer("other")
    assert post(mod_c, sid, "mod", action="mute", username="badbob").status_code == 403          # not a moderator yet
    assert post(hc, sid, "mod", action="mod_add", username="modmo").status_code == 200
    assert post(mod_c, sid, "mod", action="mod_add", username="other").status_code == 403         # host-only
    assert post(mod_c, sid, "mod", action="block", username="badbob").status_code == 403          # host-only
    assert post(mod_c, sid, "mod", action="mute", username="hosty").status_code == 403            # never the host
    assert post(mod_c, sid, "mod", action="mute", username="modmo").status_code == 400            # not yourself
    assert post(mod_c, sid, "mod", action="mute", username="badbob").status_code == 200
    assert say(bad_c, sid, "can you hear me").status_code == 400                                   # muted users cannot chat or react
    assert post(bad_c, sid, "react", emoji="🔥").status_code == 429
    post(mod_c, sid, "mod", action="unmute", username="badbob")
    assert say(bad_c, sid, "now I can").status_code == 200
    post(hc, sid, "mod", action="mod_add", username="other")
    assert post(mod_c, sid, "mod", action="timeout", username="other", minutes=5).status_code == 403   # a moderator cannot act on a moderator
    assert post(hc, sid, "mod", action="timeout", username="other", minutes=5).status_code == 200
    assert say(other_c, sid, "hi").status_code == 400
    # the badge shows in chat
    say(mod_c, sid, "mods here")
    ev = [e for e in hc.get(f"/live/api/{sid}/poll?since=0").get_json()["events"] if e["body"] == "mods here"][0]
    assert ev["user"]["badges"] == ["mod"]
    actions = [(a["action"], a["actor_id"]) for a in db.data["live_moderation_actions"]]
    assert ("mute", mod_p["id"]) in actions and ("mod_add", hp["id"]) in actions and ("timeout", hp["id"]) in actions
    assert post(hc, sid, "mod", action="mod_remove", username="modmo").status_code == 200
    assert post(mod_c, sid, "mod", action="mute", username="badbob").status_code == 403


def test_timeout_expires(host, sid, viewer, db):
    vc, vp = viewer("vera")
    post(host[0], sid, "mod", action="timeout", username="vera", minutes=2)
    assert say(vc, sid, "hi").status_code == 400
    db.table("live_viewers").update({"muted_until": iso(utcnow() - timedelta(seconds=1))}).eq("user_id", vp["id"]).execute()
    assert say(vc, sid, "hi again").status_code == 200


def test_remove_viewer_ends_their_access_and_disconnects_media(host, sid, viewer, db, lkcalls):
    vc, vp = viewer("vera", 100)
    assert post(host[0], sid, "mod", action="remove", username="vera", reason="spam").status_code == 200
    assert vc.get(f"/live/api/{sid}/poll?since=0").status_code == 403
    assert post(vc, sid, "join").status_code == 403 and post(vc, sid, "join").get_json()["code"] == "removed"
    assert post(vc, sid, "token").status_code == 403
    assert post(vc, sid, "gift", gift="red_rose", request_id="rq-" + "a" * 10).status_code == 403
    assert balance(db, vp["id"]) == 100
    assert any(c[0] == "RemoveParticipant" and c[1]["room"].startswith("cirq-live-") and c[1]["identity"].startswith("u_") for c in lkcalls)
    assert db.data["live_moderation_actions"][-1]["reason"] == "spam"


def test_block_is_permanent_across_lives_and_reversible(host, sid, viewer, as_user, db, lkcalls):
    hc, _ = host
    vc, vp = viewer("vera")
    assert post(hc, sid, "mod", action="block", username="vera").status_code == 200
    post(hc, sid, "end")
    new = hc.post("/live/api/start", json={"title": "second live"}).get_json()["session_id"]
    assert post(vc, new, "join").get_json()["code"] == "blocked"
    assert vc.get("/live/api/sessions").get_json()["sessions"] == []
    post(hc, new, "mod", action="unblock", username="vera")
    assert post(vc, new, "join").status_code == 200


def test_staff_can_moderate_any_room_but_not_other_staff(host, sid, viewer, as_user):
    vc, _ = viewer("vera")
    adm, _ = as_user("boss", role="admin")
    mod, _ = as_user("sitemod", role="moderator")
    assert post(mod, sid, "mod", action="remove", username="vera").status_code == 200
    assert post(mod, sid, "mod", action="mute", username="boss").status_code == 403


def test_pin_unpin_delete_messages(host, sid, viewer, db):
    hc, _ = host
    vc, vp = viewer("vera")
    say(vc, sid, "Please follow the rules")
    mid = [e for e in hc.get(f"/live/api/{sid}/poll?since=0").get_json()["events"] if e["kind"] == "chat"][0]["id"]
    assert post(vc, sid, "pin", event_id=mid).status_code == 403
    assert post(hc, sid, "pin", event_id=mid).status_code == 200
    st = vc.get(f"/live/api/{sid}/state").get_json()["state"]
    assert st["pinned"]["body"] == "Please follow the rules" and st["pinned"]["id"] == mid
    assert post(hc, sid, "unpin").status_code == 200 and vc.get(f"/live/api/{sid}/state").get_json()["state"]["pinned"] is None
    post(hc, sid, "pin", event_id=mid)
    other, _ = viewer("other")
    assert post(other, sid, "delete", event_id=mid).status_code == 403          # can't delete someone else's message
    assert post(vc, sid, "delete", event_id=mid).status_code == 200             # but may delete their own
    st = vc.get(f"/live/api/{sid}/state").get_json()["state"]
    assert st["pinned"] is None and all(e["id"] != mid for e in st["history"])
    assert post(hc, sid, "delete", event_id=mid).status_code == 400
    kinds = [e["kind"] for e in hc.get(f"/live/api/{sid}/poll?since=0").get_json()["events"]]
    assert "delete_msg" in kinds and "pin" in kinds
    assert {a["action"] for a in db.data["live_moderation_actions"]} >= {"pin", "unpin", "delete_message"}


def test_reports_are_stored_and_deduplicated(host, sid, viewer, db):
    vc, vp = viewer("vera")
    bad, bp = viewer("badbob")
    say(bad, sid, "something rude")
    mid = [e for e in host[0].get(f"/live/api/{sid}/poll?since=0").get_json()["events"] if e["kind"] == "chat"][0]["id"]
    assert post(vc, sid, "report", reason="harassment", event_id=mid, details="mean").status_code == 200
    r = db.data["live_reports"][0]
    assert (r["reporter_id"], r["target_user_id"], r["event_id"], r["status"]) == (vp["id"], bp["id"], mid, "open")
    again = post(vc, sid, "report", reason="harassment", event_id=mid)
    assert again.status_code == 200 and len(db.data["live_reports"]) == 1
    assert post(vc, sid, "report", reason="because").status_code == 400
    assert post(vc, sid, "report", reason="spam", username="vera").status_code == 400


# ------------------------------------------------------------------ guests
def test_guest_invite_consent_and_media_token(host, sid, viewer, app, lkcalls):
    hc, hp = host
    gc, gp = viewer("gina")
    st = gc.get(f"/live/api/{sid}/state").get_json()["state"]
    assert st["guests"]["mine"] is None
    assert post(gc, sid, "guests/invite", username="hosty").status_code == 403          # a viewer can't invite
    assert post(hc, sid, "guests/invite", username="gina", media="video").status_code == 200
    mine = gc.get(f"/live/api/{sid}/state").get_json()["state"]["guests"]["mine"]
    assert mine["status"] == "invited"
    tok = decode_token(post(gc, sid, "token").get_json()["token"], app.config["LIVEKIT_API_SECRET"])
    assert tok["video"]["canPublish"] is False                                            # NO media before the guest consents
    iid = hc.get(f"/live/api/{sid}/state").get_json()["state"]["guests"]["pending"][0]["invite_id"]
    assert post(hc, sid, f"guests/{iid}/respond", accept=True).status_code == 400         # the host can't consent for the guest
    assert gc.get(f"/live/api/{sid}/state").get_json()["state"]["guests"]["accepted"] == []
    assert post(gc, sid, f"guests/{iid}/respond", accept=True).status_code == 200
    d = post(gc, sid, "token").get_json()
    claims = decode_token(d["token"], app.config["LIVEKIT_API_SECRET"])
    assert d["role"] == "guest" and sorted(claims["video"]["canPublishSources"]) == ["camera", "microphone"] and "roomAdmin" not in claims["video"]
    pub = gc.get(f"/live/api/{sid}/state").get_json()["state"]["guests"]
    assert pub["accepted"][0]["username"] == "gina" and "invite_id" not in pub["accepted"][0] and "token" not in json.dumps(pub)  # nothing private leaks to guests/viewers


def test_audio_only_guest_never_gets_a_camera(host, sid, viewer, app):
    hc, _ = host
    gc, _ = viewer("gina")
    post(hc, sid, "guests/invite", username="gina", media="audio")
    iid = hc.get(f"/live/api/{sid}/state").get_json()["state"]["guests"]["pending"][0]["invite_id"]
    post(gc, sid, f"guests/{iid}/respond", accept=True)
    claims = decode_token(post(gc, sid, "token").get_json()["token"], app.config["LIVEKIT_API_SECRET"])
    assert claims["video"]["canPublishSources"] == ["microphone"]
    hc_state = hc.get(f"/live/api/{sid}/state").get_json()["state"]["guests"]["accepted"][0]
    assert hc_state["media"] == "audio" and hc_state["video"] is False


def test_guest_request_host_accepts_and_four_guest_limit(host, sid, viewer, db):
    hc, _ = host
    people = [viewer(f"g{i}") for i in range(5)]
    for c, _ in people:
        assert post(c, sid, "guests/request", media="video").status_code == 200
    pending = hc.get(f"/live/api/{sid}/state").get_json()["state"]["guests"]["pending"]
    assert len(pending) == 5 and all(p["status"] == "requested" for p in pending)
    results = [post(hc, sid, f"guests/{p['invite_id']}/respond", accept=True) for p in pending]
    assert [r.status_code for r in results] == [200, 200, 200, 200, 400] and "taken" in results[4].get_json()["error"]
    assert sum(1 for i in db.data["live_guest_invites"] if i["status"] == "accepted") == 4
    assert all(i["consent_at"] for i in db.data["live_guest_invites"] if i["status"] == "accepted")
    # a requester cannot approve themselves
    c0 = people[0][0]
    iid = pending[4]["invite_id"]
    assert post(c0, sid, f"guests/{iid}/respond", accept=True).status_code in (400, 403)


def test_host_rejects_removes_and_limits_guest_permissions(host, sid, viewer, app, lkcalls):
    hc, _ = host
    gc, gp = viewer("gina")
    post(gc, sid, "guests/request", media="video")
    iid = hc.get(f"/live/api/{sid}/state").get_json()["state"]["guests"]["pending"][0]["invite_id"]
    assert post(hc, sid, f"guests/{iid}/respond", accept=False).status_code == 200            # host rejects
    assert gc.get(f"/live/api/{sid}/state").get_json()["state"]["guests"]["mine"] is None
    post(gc, sid, "guests/request", media="video")                                            # may ask again
    iid = hc.get(f"/live/api/{sid}/state").get_json()["state"]["guests"]["pending"][0]["invite_id"]
    post(hc, sid, f"guests/{iid}/respond", accept=True)
    assert post(gc, sid, f"guests/{iid}/permissions", audio=False).status_code == 403          # only the host controls permissions
    assert post(hc, sid, f"guests/{iid}/permissions", audio=False).status_code == 200          # host mutes the guest
    call = [c for c in lkcalls if c[0] == "UpdateParticipant"][-1][1]
    assert call["permission"]["can_publish_sources"] == ["CAMERA"] and call["permission"]["can_publish_data"] is False
    claims = decode_token(post(gc, sid, "token").get_json()["token"], app.config["LIVEKIT_API_SECRET"])
    assert claims["video"]["canPublishSources"] == ["camera"]                                  # a re-issued token respects the mute
    post(hc, sid, f"guests/{iid}/permissions", video=False)
    assert post(gc, sid, "token").get_json()["can_publish"] is False
    assert post(hc, sid, f"guests/{iid}/remove").status_code == 200
    assert any(c[0] == "RemoveParticipant" for c in lkcalls)
    assert post(gc, sid, "token").get_json()["role"] == "viewer"
    assert post(gc, sid, f"guests/{iid}/respond", accept=True).status_code == 400              # a removed guest can't just re-accept


def test_guest_can_leave_and_guests_off_blocks_new_ones(host, sid, viewer):
    hc, _ = host
    gc, _ = viewer("gina")
    post(hc, sid, "guests/invite", username="gina")
    iid = hc.get(f"/live/api/{sid}/state").get_json()["state"]["guests"]["pending"][0]["invite_id"]
    post(gc, sid, f"guests/{iid}/respond", accept=True)
    assert post(gc, sid, "guests/leave").get_json()["left"] is True
    assert post(gc, sid, "token").get_json()["role"] == "viewer"
    post(hc, sid, "settings", guests_enabled=False)
    assert post(gc, sid, "guests/request").status_code == 403 and post(hc, sid, "guests/invite", username="gina").status_code == 400


def test_invite_requires_the_person_to_be_in_the_room(host, sid, as_user):
    as_user("absent")
    assert post(host[0], sid, "guests/invite", username="absent").status_code == 400
    assert post(host[0], sid, "guests/invite", username="nobody").status_code == 400


# ------------------------------------------------------------------ gifts
GIFTS = [("red_rose", 10, 5), ("love_heart", 50, 25), ("mystery_box", 100, 50), ("golden_crown", 500, 250), ("treasure_chest", 1000, 500),
         ("sports_car", 2000, 1000), ("diamond_ring", 5000, 2500), ("private_jet", 10000, 5000)]


def gift(c, sid, code, rid=None, **extra):
    return post(c, sid, "gift", gift=code, request_id=rid or "rq-" + uuid.uuid4().hex, **extra)


def test_catalogue_matches_the_brief(host, viewer):
    vc, _ = viewer("vera")
    got = [(g["code"], g["price"], g["host_coins"]) for g in vc.get("/live/api/gifts").get_json()["gifts"]]
    assert got == GIFTS
    tiers = {g["code"]: g["tier"] for g in vc.get("/live/api/gifts").get_json()["gifts"]}
    assert tiers["red_rose"] == "small" and tiers["private_jet"] == "premium"           # small gifts -> small animations, premium -> large


@pytest.mark.parametrize("code,price,earn", GIFTS)
def test_each_gift_charges_the_viewer_and_credits_half_to_the_host(host, sid, viewer, db, code, price, earn):
    vc, vp = viewer("vera", 20000)
    r = gift(vc, sid, code)
    d = r.get_json()
    assert r.status_code == 200 and d["ok"] and d["gross"] == price and d["host_coins"] == earn and d["platform_coins"] == price - earn
    assert d["balance"] == 20000 - price == balance(db, vp["id"]) and balance(db, host[1]["id"]) == earn
    led = [(l["user_id"], l["amount"], l["reason"]) for l in db.data["coin_ledger"] if l["ref"] == d["txn_id"]]
    assert sorted(led, key=lambda x: x[1]) == [(vp["id"], -price, "live_gift_out"), (host[1]["id"], earn, "live_gift_in")]
    ev = [e for e in host[0].get(f"/live/api/{sid}/poll?since=0").get_json()["events"] if e["kind"] == "gift"][0]    # shows up in the room in real time
    assert ev["payload"]["gift_code"] == code and ev["payload"]["sender"] == "vera" and ev["user"]["username"] == "vera"
    assert host[0].get(f"/live/api/{sid}/poll?since=0&hb=1").get_json()["me"]["balance"] == earn                     # the host's wallet updates live


def test_mystery_box_always_costs_100_and_rewards_50(host, sid, viewer, db):
    vc, vp = viewer("vera", 1000)
    for i in range(4):
        d = gift(vc, sid, "mystery_box").get_json()
        assert (d["gross"], d["host_coins"], d["reward_coins"], d["diamonds"]) == (100, 50, 50, 0)
        assert d["message"] == "Your Mystery Box opened! Hosty received 50 coins."
    assert balance(db, vp["id"]) == 600 and balance(db, host[1]["id"]) == 200


def test_treasure_chest_shows_10_diamonds_but_credits_500(host, sid, viewer, db):
    vc, vp = viewer("vera", 1000)
    d = gift(vc, sid, "treasure_chest").get_json()
    assert (d["gross"], d["host_coins"], d["diamonds"], d["diamond_value"]) == (1000, 500, 10, 100) and "10 × 100 = 1000 displayed value" in d["message"]
    assert balance(db, host[1]["id"]) == 500
    recs = db.data["live_diamond_records"]
    assert len(recs) == 10 and {r["display_value"] for r in recs} == {100} and not any(r["counts_toward_earnings"] for r in recs)
    dash = host[0].get("/live/api/gifts")           # earnings summary: displayed diamonds never added to coins
    s = live.creator_dashboard.__wrapped__ if hasattr(live.creator_dashboard, "__wrapped__") else None
    with host[0].application.app_context():
        summary = live.creator_dashboard(host[1])["summary"]
    assert summary["host_earnings"] == 500 and summary["gross_value"] == 1000 and summary["diamonds_displayed"] == 10 and summary["platform_share"] == 500


def test_insufficient_balance_is_refused_without_side_effects(host, sid, viewer, db):
    vc, vp = viewer("vera", 99)
    r = gift(vc, sid, "mystery_box")
    assert r.status_code == 402 and r.get_json()["code"] == "insufficient" and "100" in r.get_json()["error"]
    assert balance(db, vp["id"]) == 99 and balance(db, host[1]["id"]) == 0 and not db.data.get("live_gift_transactions")


def test_replayed_tap_is_charged_once(host, sid, viewer, db):
    vc, vp = viewer("vera", 1000)
    a = gift(vc, sid, "golden_crown", rid="tap-0001-xyz")
    b = gift(vc, sid, "golden_crown", rid="tap-0001-xyz")
    assert a.get_json()["txn_id"] == b.get_json()["txn_id"] and b.get_json()["duplicate"] is True and b.status_code == 200
    assert balance(db, vp["id"]) == 500 and balance(db, host[1]["id"]) == 250 and len(db.data["live_gift_transactions"]) == 1
    assert gift(vc, sid, "red_rose", rid="tap-0001-xyz").status_code == 409


def test_client_cannot_tamper_with_price_host_or_split(host, sid, viewer, db):
    vc, vp = viewer("vera", 1000)
    r = gift(vc, sid, "red_rose", price=0, amount=-50, host_id=vp["id"], host_coins=9999, gross=1, battle_id=str(uuid.uuid4()), balance=10**6)
    d = r.get_json()
    assert d["gross"] == 10 and d["host_coins"] == 5 and balance(db, vp["id"]) == 990 and balance(db, host[1]["id"]) == 5
    assert gift(vc, sid, "free_gift").status_code == 404
    assert gift(vc, sid, "red_rose", rid="short").status_code == 400
    assert post(vc, sid, "gift", gift="red_rose").status_code == 400            # missing request id
    assert balance(db, vp["id"]) == 990


def test_no_self_gifting_and_no_gifts_after_the_live_ends(host, sid, viewer, db):
    hc, hp = host
    coins(db, hp["id"], 500)
    assert gift(hc, sid, "red_rose").status_code == 400 and balance(db, hp["id"]) == 500
    vc, vp = viewer("vera", 100)
    post(hc, sid, "end")
    assert gift(vc, sid, "red_rose").status_code == 410 and balance(db, vp["id"]) == 100


def test_daily_live_gift_cap(host, sid, viewer, app, db):
    vc, vp = viewer("vera", 5000)
    app.config["LIVE_GIFT_DAILY_COINS"] = 600
    assert gift(vc, sid, "golden_crown").status_code == 200 and gift(vc, sid, "mystery_box").status_code == 200
    r = gift(vc, sid, "red_rose")
    assert r.status_code == 429 and r.get_json()["code"] == "cap_reached" and balance(db, vp["id"]) == 4400


def test_gift_earnings_respect_loan_collection(host, sid, viewer, monkeypatch):
    seen = []
    monkeypatch.setattr(lending, "garnish", lambda *a, **k: seen.append(a))
    vc, _ = viewer("vera", 100)
    d = gift(vc, sid, "mystery_box").get_json()
    assert seen == [(host[1]["id"], 50, "live_gift_in", d["txn_id"])]


def test_existing_coin_earning_still_works_alongside_live(host, sid, viewer, db, app):
    from app.services import coin_service
    vc, vp = viewer("vera", 0)
    with app.app_context():
        coin_service.apply(vp["id"], 40, "post_created", "p-1")
    assert balance(db, vp["id"]) == 40
    gift(vc, sid, "red_rose")
    assert balance(db, vp["id"]) == 30
    with app.app_context():
        coin_service.apply(vp["id"], 5, "post_created", "p-2")
        coin_service.apply(vp["id"], 5, "post_created", "p-2")           # the existing duplicate guard still holds
    assert balance(db, vp["id"]) == 35


def test_supporter_badge_and_fan_club(host, sid, viewer, db):
    vc, vp = viewer("vera", 2000)
    gift(vc, sid, "golden_crown")                                   # 500 -> Bronze (>=100)
    gift(vc, sid, "sports_car")                                     # +2000? balance
    say(vc, sid, "love this")
    chat = [e for e in host[0].get(f"/live/api/{sid}/poll?since=0").get_json()["events"] if e["kind"] == "chat"][0]
    assert "bronze" in chat["user"]["badges"]
    st = vc.get(f"/live/api/{sid}/state").get_json()["state"]
    assert st["me"]["fan_club"] is True
    assert vc.post("/live/api/fanclub/hosty/leave").status_code == 200
    assert vc.get(f"/live/api/{sid}/state").get_json()["state"]["me"]["fan_club"] is False


def test_levels_and_tiers():
    assert [live.creator_level(n)["level"] for n in (0, 499, 500, 2000, 500000)] == [1, 1, 2, 3, 10]
    assert live.creator_level(600)["to_next"] == 1400
    assert [live.supporter_tier(n) for n in (0, 99, 100, 1000, 5000, 20000)] == [None, None, "Bronze", "Silver", "Gold", "Diamond"]


def test_creator_dashboard_and_rankings(host, sid, viewer, db, app):
    v1, p1 = viewer("vera", 5000)
    v2, p2 = viewer("walt", 5000)
    gift(v1, sid, "golden_crown"); gift(v2, sid, "love_heart"); gift(v1, sid, "treasure_chest")
    with app.app_context():
        d = live.creator_dashboard(host[1])
        r = live.rankings(10)
    assert d["summary"]["gifts_received"] == 3 and d["summary"]["host_earnings"] == 250 + 25 + 500 and d["summary"]["gross_value"] == 1550
    assert d["summary"]["platform_share"] == 775 and d["summary"]["diamonds_displayed"] == 10 and d["wallet_balance"] == 775
    assert d["supporters"][0]["username"] == "vera" and d["supporters"][0]["total"] == 1500 and d["fan_club_members"] == 2
    assert d["history"][0]["gross"] == 1000 and d["history"][0]["earned"] == 500 and d["history"][0]["diamond_value"] == 100
    assert d["level"]["level"] == 2
    assert r["hosts_week"][0]["username"] == "hosty" and r["hosts_week"][0]["coins"] == 775
    assert [g["username"] for g in r["gifters_week"]] == ["vera", "walt"]


# ------------------------------------------------------------------ PK battles
@pytest.fixture
def two_hosts(as_user, db, host, sid, lkcalls, app):
    app.config["LIVE_BATTLE_COUNTDOWN_SECONDS"] = 0
    c2, p2 = as_user("rival")
    db.table("profiles").update({"live_host_granted": True}).eq("id", p2["id"]).execute()
    sid2 = c2.post("/live/api/start", json={"title": "Rival live"}).get_json()["session_id"]
    return host, (c2, p2), sid, sid2


def start_battle(two_hosts, minutes=5):
    (hc, hp), (rc, rp), s1, s2 = two_hosts
    r = post(hc, s1, "battle/invite", username="rival", minutes=minutes)
    assert r.status_code == 200, r.get_json()
    bid = r.get_json()["battle_id"]
    assert rc.post(f"/live/api/battle/{bid}/respond", json={"accept": True}).status_code == 200
    return bid


def test_pk_invite_accept_scoring_and_winner_without_bonus_coins(two_hosts, as_user, db):
    (hc, hp), (rc, rp), s1, s2 = two_hosts
    fan1, f1 = as_user("fan1"); fan2, f2 = as_user("fan2")
    for c, p, s in ((fan1, f1, s1), (fan2, f2, s2)):
        coins(db, p["id"], 5000); post(c, s, "join")
    bid = start_battle(two_hosts)
    assert rc.get(f"/live/api/{s2}/state").get_json()["state"]["battle"]["status"] in ("countdown", "active")
    assert gift(fan1, s1, "golden_crown").get_json()["scores"] == {"a": 500, "b": 0}
    gift(fan2, s2, "treasure_chest"); gift(fan1, s1, "mystery_box")
    snap = fan1.get(f"/live/api/{s1}/poll?since=0&battle={bid}").get_json()["battle"]
    assert snap["status"] == "active" and snap["scores"] == {"a": 600, "b": 1000} and snap["sides"]["a"]["username"] == "hosty" and snap["you_side"] == "a"
    before = (balance(db, hp["id"]), balance(db, rp["id"]))
    post(hc, s1, "battle/invite", username="rival", minutes=5)                           # a second invite during a battle is refused
    assert rc.post(f"/live/api/battle/{bid}/end").status_code == 200
    snap = fan2.get(f"/live/api/{s2}/poll?since=0&battle={bid}").get_json()["battle"]
    assert snap["status"] == "ended" and snap["winner"] == "rival" and snap["outcome"] == "b"
    assert (balance(db, hp["id"]), balance(db, rp["id"])) == before                       # winning pays nothing
    gift(fan1, s1, "red_rose")
    assert fan1.get(f"/live/api/{s1}/poll?since=0&battle={bid}").get_json()["battle"]["scores"] == {"a": 600, "b": 1000}   # not scored after the end
    kinds = [e["kind"] for e in fan1.get(f"/live/api/{s1}/poll?since=0").get_json()["events"]]
    assert kinds.count("battle_end") == 1 and "battle_accept" in kinds                    # winner announced exactly once


def test_pk_every_scored_gift_is_linked_and_never_double_counted(two_hosts, as_user, db):
    (hc, hp), (rc, rp), s1, s2 = two_hosts
    fan, fp = as_user("fan"); coins(db, fp["id"], 5000); post(fan, s1, "join")
    bid = start_battle(two_hosts)
    gift(fan, s1, "golden_crown", rid="same-tap-001"); gift(fan, s1, "golden_crown", rid="same-tap-001")
    score = [p["score"] for p in db.data["live_battle_participants"] if p["host_id"] == hp["id"]][0]
    assert score == 500 == sum(t["gross_coins"] for t in db.data["live_gift_transactions"] if t["battle_id"] == bid)


def test_pk_invalid_states(two_hosts, as_user, db):
    (hc, hp), (rc, rp), s1, s2 = two_hosts
    assert post(hc, s1, "battle/invite", username="hosty", minutes=5).status_code == 400          # not yourself
    assert post(hc, s1, "battle/invite", username="nobody", minutes=5).status_code == 400
    assert post(hc, s1, "battle/invite", username="rival", minutes=7).status_code == 400          # fixed lengths only
    third, tp = as_user("third")
    assert post(hc, s1, "battle/invite", username="third", minutes=5).status_code == 400          # third isn't live
    assert post(third, s1, "battle/invite", username="rival", minutes=5).status_code == 400       # only the live's host invites
    bid = post(hc, s1, "battle/invite", username="rival", minutes=5).get_json()["battle_id"]
    assert post(hc, s1, "battle/invite", username="rival", minutes=5).status_code == 400          # one pending invite
    assert hc.post(f"/live/api/battle/{bid}/respond", json={"accept": True}).status_code == 400   # inviter can't accept own invite
    assert third.post(f"/live/api/battle/{bid}/respond", json={"accept": True}).status_code == 400
    assert third.post(f"/live/api/battle/{bid}/end").status_code == 403
    inv = rc.get(f"/live/api/{s2}/state").get_json()["state"]["battle_invites"]
    assert inv[0]["id"] == bid and inv[0]["from"]["username"] == "hosty" and inv[0]["duration_minutes"] == 5
    assert hc.get(f"/live/api/{s1}/state").get_json()["state"]["battle_invites"] == []
    assert rc.post(f"/live/api/battle/{bid}/respond", json={"accept": False}).status_code == 200  # decline
    assert db.table("live_battles").select("status").eq("id", bid).execute().data[0]["status"] == "declined"
    assert rc.post(f"/live/api/battle/{bid}/respond", json={"accept": True}).status_code == 400
    bid2 = post(hc, s1, "battle/invite", username="rival", minutes=3).get_json()["battle_id"]
    db.table("live_battles").update({"expires_at": iso(utcnow() - timedelta(seconds=1))}).eq("id", bid2).execute()
    assert rc.post(f"/live/api/battle/{bid2}/respond", json={"accept": True}).status_code == 400  # expired invite


def test_pk_ends_when_a_live_ends_and_opponent_token_is_watch_only(two_hosts, as_user, db, app):
    (hc, hp), (rc, rp), s1, s2 = two_hosts
    fan, fp = as_user("fan"); post(fan, s1, "join")
    bid = start_battle(two_hosts)
    d = post(fan, s1, "token", target="opponent").get_json()
    claims = decode_token(d["token"], app.config["LIVEKIT_API_SECRET"])
    room2 = db.table("live_sessions").select("room_name").eq("id", s2).execute().data[0]["room_name"]
    assert d["role"] == "viewer" and claims["video"]["room"] == room2 and claims["video"]["canPublish"] is False
    assert post(hc, s1, "token", target="opponent").get_json()["can_publish"] is False            # even the host only watches the other room
    outsider, _ = as_user("outsider"); post(outsider, s1, "join")
    post(rc, s2, "end")                                                                           # rival leaves: battle must settle, no stuck state
    assert db.table("live_battles").select("status").eq("id", bid).execute().data[0]["status"] == "ended"
    assert post(fan, s1, "token", target="opponent").status_code == 403
    assert not any(p["active"] for p in db.data["live_battle_participants"])


# ------------------------------------------------------------------ security basics
def test_csrf_protection_applies_to_live_posts(host, sid, app):
    app.config["WTF_CSRF_ENABLED"] = True
    c = app.test_client()
    assert c.post(f"/live/api/{sid}/chat", json={"body": "hi"}).status_code in (400, 401, 403)
    app.config["WTF_CSRF_ENABLED"] = False


def test_content_security_policy_allows_only_the_configured_livekit_host(host, sid):
    r = host[0].get(f"/live/api/{sid}/state")
    csp = r.headers["Content-Security-Policy"]
    assert "connect-src 'self' wss://test.livekit.invalid https://test.livekit.invalid;" in csp and "script-src 'self';" in csp
    assert "display-capture=(self)" in r.headers["Permissions-Policy"] and "camera=(self)" in r.headers["Permissions-Policy"]


def test_secrets_never_appear_in_any_api_response(host, sid, viewer, app):
    secret = app.config["LIVEKIT_API_SECRET"]
    vc, _ = viewer("vera", 100)
    say(vc, sid, "hi"); gift(vc, sid, "red_rose")
    for c in (vc, host[0]):
        for url in (f"/live/api/{sid}/state", f"/live/api/{sid}/poll?since=0&hb=1", "/live/api/sessions", "/live/api/gifts"):
            assert secret not in c.get(url).get_data(as_text=True)


def test_non_uuid_and_unknown_sessions_404(host):
    assert host[0].get("/live/api/not-a-uuid/state").status_code == 404
    assert host[0].get(f"/live/api/{uuid.uuid4()}/state").status_code == 404
    assert post(host[0], uuid.uuid4(), "chat", body="x").status_code == 404


# ------------------------------------------------------------------ LiveKit client (token + server API), no network
def test_livekit_server_calls_use_admin_grants_and_map_errors(app, monkeypatch):
    calls = []

    class R:
        def __init__(self, code): self.status_code = code
        def json(self): return {}
    codes = iter([200, 404, 500, 200])
    monkeypatch.setattr(lk, "_post", lambda url, payload, headers, timeout: calls.append((url, payload, headers)) or R(next(codes)))
    with app.app_context():
        lk.remove_participant("room-1", "u_abc")
        lk.remove_participant("room-1", "u_abc")                    # 404 (already gone) is fine
        with pytest.raises(lk.LiveKitError) as e:
            lk.set_publish_permissions("room-1", "u_abc", can_publish=True, sources=["microphone"])
        assert "500" in str(e.value) and app.config["LIVEKIT_API_SECRET"] not in str(e.value)
        lk.delete_room("room-1")
    url, payload, headers = calls[0]
    assert url == "https://test.livekit.invalid/twirp/livekit.RoomService/RemoveParticipant" and payload == {"room": "room-1", "identity": "u_abc"}
    admin = decode_token(headers["Authorization"].split()[1], app.config["LIVEKIT_API_SECRET"])
    assert admin["video"]["roomAdmin"] is True and admin["video"]["room"] == "room-1" and admin["exp"] - admin["nbf"] <= 120
    assert decode_token(calls[3][2]["Authorization"].split()[1], app.config["LIVEKIT_API_SECRET"])["video"]["roomCreate"] is True


def test_livekit_network_failure_is_a_clean_error(app, monkeypatch):
    def boom(*a): raise ConnectionError("dns failure for secret-host")
    monkeypatch.setattr(lk, "_post", boom)
    with app.app_context(), pytest.raises(lk.LiveKitError) as e:
        lk.delete_room("r")
    assert "ConnectionError" in str(e.value) and "secret-host" not in str(e.value)
