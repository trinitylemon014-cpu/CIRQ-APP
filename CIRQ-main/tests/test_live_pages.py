"""Every live page renders for the right people with the right wiring (no inline scripts, no secrets, vendored SDK referenced)."""
import re

import pytest

from tests.test_live import coins, host, lkcalls, post, sid, viewer  # noqa: F401  (shared fixtures)


def html(r):
    assert r.status_code == 200, r.data[:300]
    return r.get_data(as_text=True)


def test_index_go_dashboard_rankings_settings_render(host, sid):
    c, _ = host
    page = html(c.get("/live/"))
    assert "live-index.js" in page and "lv-grid" in page and "Go live" in page
    go = html(c.get("/live/go"))
    assert "lv-start" in go and "live-go.js" in go and "Followers only" in go
    assert "Display only" in html(c.get("/live/dashboard"))
    assert "Top creators" in html(c.get("/live/rankings"))
    assert "viewer lists" in html(c.get("/live/settings"))


def test_go_page_for_ineligible_student_explains_why(as_user, lkcalls):
    c, _ = as_user("newbie")
    page = html(c.get("/live/go"))
    assert "lv-start" not in page and "lv-note" in page


def test_room_page_has_wiring_and_no_inline_script_or_secret(host, sid, app):
    c, _ = host
    page = html(c.get(f"/live/{sid}"))
    for needle in ("live.js", "live-gifts.js", "livekit-client.umd.min.js", 'id="lr-chat"', 'id="lr-ctl"', 'id="lr-end"'):
        assert needle in page, needle
    assert 'id="lr-giftbtn"' not in page                          # a host can't gift themselves
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", page), "inline script would be blocked by the CSP"
    assert app.config["LIVEKIT_API_SECRET"] not in page


def test_viewer_room_page_has_gift_and_report_buttons(host, sid, viewer):
    v, _ = viewer()
    page = html(v.get(f"/live/{sid}"))
    assert 'id="lr-giftbtn"' in page and 'id="lr-report"' in page and 'id="lr-ctl"' not in page


def test_admin_live_page_renders_for_admin_only(as_user, host, sid, db):
    c, _ = as_user("boss", role="admin")
    assert "Live hosting access" in html(c.get("/admin/live"))
    u, _ = as_user("plain")
    assert u.get("/admin/live").status_code in (302, 403)


def test_static_assets_exist_and_the_room_script_uses_only_real_endpoints(app):
    import pathlib
    root = pathlib.Path(app.root_path) / "static"
    js = (root / "js" / "live.js").read_text()
    for f in ("css/live.css", "js/live.js", "js/live-gifts.js", "js/live-go.js", "js/live-index.js", "js/live-clubs.js"):
        assert (root / f).is_file(), f
    assert "innerHTML" not in js and "eval(" not in js and "LIVEKIT_API_SECRET" not in js
    # every endpoint the room script calls is a registered route
    rules = {r.rule for r in app.url_map.iter_rules()}
    for path in ("/live/api/<uuid:sid>/poll", "/live/api/<uuid:sid>/gift", "/live/api/<uuid:sid>/token", "/live/api/<uuid:sid>/guests/request",
                 "/live/api/<uuid:sid>/guests/<uuid:iid>/permissions", "/live/api/battle/<uuid:bid>/respond", "/u/<username>/follow"):
        assert path in rules, path


def test_open_mode_lets_any_approved_account_go_live_without_email_or_grant(as_user, app, lkcalls):
    app.config["LIVE_ELIGIBILITY"] = "anyone"
    c, p = as_user("fresh")
    r = c.post("/live/api/start", json={"title": "Hello world", "category": "music", "mode": "video"})
    assert r.status_code == 200, r.get_json()


def test_open_mode_still_blocks_pending_accounts(as_user, db, app, lkcalls):
    app.config["LIVE_ELIGIBILITY"] = "anyone"
    from app.services import live_service as live
    with app.app_context():
        ok, _ = live.can_host({"account_status": "pending"})
    assert not ok


def test_room_script_builds_room_api_urls_not_site_root_urls(app):
    """Regression: api('/state') must resolve to /live/api/<sid>/state, not /state (that 404 made every live show 'ended')."""
    import pathlib
    js = (pathlib.Path(app.root_path) / "static" / "js" / "live.js").read_text()
    assert 'path.startsWith("/") ? path : API + path' not in js
    assert r'/^\/(live|u)\//.test(path) ? path : API + path' in js
