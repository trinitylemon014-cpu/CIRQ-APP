"""CIRQ as an installable app: manifest, service worker, offline page, icons, and the safety rules of the worker."""
import json
import os
import re
from pathlib import Path

import pytest
from PIL import Image

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"
SW_CALLS = re.compile(r"const PRECACHE = (\[.*?\]);", re.S)


def manifest(client):
    r = client.get("/manifest.webmanifest")
    assert r.status_code == 200
    return r, json.loads(r.get_data(as_text=True))


def sw_text(client):
    return client.get("/sw.js").get_data(as_text=True)


def local_path(url):
    return STATIC / url.split("?")[0].removeprefix("/static/")


# ------------------------------------------------------------------ manifest
def test_manifest_is_served_publicly_with_the_right_type(client):
    r, data = manifest(client)
    assert r.mimetype == "application/manifest+json" and "max-age" in r.headers["Cache-Control"]
    assert data["name"] == "CIRQ" and data["short_name"] == "CIRQ" and len(data["short_name"]) <= 12


def test_manifest_meets_the_installability_rules(client):
    _, m = manifest(client)
    assert m["display"] == "standalone" and m["scope"] == "/" and m["id"] == "/"
    assert m["start_url"].startswith("/") and m["start_url"].startswith(m["scope"])                  # the start page is inside the scope
    for key in ("background_color", "theme_color"):
        assert re.fullmatch(r"#[0-9a-f]{6}", m[key])
    assert m["prefer_related_applications"] is False and m["description"] and m["categories"] == ["social"]
    by_purpose = {}
    for icon in m["icons"]:
        by_purpose.setdefault(icon["purpose"], set()).add(icon["sizes"])
    assert {"192x192", "512x512"} <= by_purpose["any"] and {"192x192", "512x512"} <= by_purpose["maskable"]   # Chrome needs 192 and 512


def test_every_manifest_icon_exists_and_has_the_size_it_claims(client):
    _, m = manifest(client)
    for icon in m["icons"] + [i for s in m["shortcuts"] for i in s["icons"]]:
        path = local_path(icon["src"])
        assert path.is_file(), icon["src"]
        w, h = Image.open(path).size
        assert icon["sizes"] == f"{w}x{h}" and icon["type"] == "image/png"
        assert client.get(icon["src"]).status_code == 200


def test_manifest_shortcuts_open_real_pages(client):
    _, m = manifest(client)
    assert [s["name"] for s in m["shortcuts"]] == ["Reels", "Messages", "Discover", "Live"]
    for s in m["shortcuts"]:
        assert s["url"].startswith("/") and client.get(s["url"]).status_code in (200, 302)           # a login redirect is fine, a 404 is not


def test_manifest_needs_no_login_and_is_identical_for_everyone(app, as_user, client):
    _, public = manifest(client)
    c, _ = as_user("alice")
    assert json.loads(c.get("/manifest.webmanifest").get_data(as_text=True)) == public


# ------------------------------------------------------------------ the icons themselves
def test_maskable_icons_are_full_bleed_and_keep_the_logo_in_the_safe_zone():
    for size in (192, 512):
        im = Image.open(STATIC / "icons" / f"icon-maskable-{size}.png").convert("RGBA")
        assert im.getpixel((0, 0))[3] == 255 and im.getpixel((size - 1, size - 1))[3] == 255          # no transparent corners: the OS cuts the shape
        cx = cy = size / 2
        r_safe = size * 0.4                                                                            # the 80% safe circle that no mask may crop
        dark = [(x, y) for x in range(0, size, 2) for y in range(0, size, 2) if sum(im.getpixel((x, y))[:3]) < 150]
        assert dark and all(((x - cx) ** 2 + (y - cy) ** 2) ** .5 <= r_safe for x, y in dark)


def test_standard_icons_keep_the_rounded_brand_tile_and_apple_icon_is_opaque():
    for size in (192, 512):
        im = Image.open(STATIC / "icons" / f"icon-{size}.png").convert("RGBA")
        assert im.getpixel((0, 0))[3] == 0 and im.getpixel((size // 2, 8))[:3] == (247, 181, 0)       # transparent corners, brand gold inside
    apple = Image.open(STATIC / "apple-touch-icon.png")
    assert apple.size == (180, 180) and apple.mode == "RGB" and apple.getpixel((2, 2)) == (247, 181, 0)   # iOS turns transparency black


# ------------------------------------------------------------------ service worker
def test_service_worker_headers_let_it_control_the_whole_site_and_update_quickly(client):
    r = client.get("/sw.js")
    assert r.status_code == 200 and r.mimetype == "text/javascript"
    assert r.headers["Service-Worker-Allowed"] == "/" and "no-cache" in r.headers["Cache-Control"]
    assert "default-src 'self'" in r.headers["Content-Security-Policy"]                                # still covered by the app's strict policy


def test_service_worker_precaches_only_files_that_exist(client):
    text = sw_text(client)
    urls = json.loads(SW_CALLS.search(text).group(1))
    assert "/offline" in urls and any(u.endswith("css/style.css") for u in urls) and any(u.endswith("js/pwa.js") for u in urls)
    for u in urls:
        assert client.get(u).status_code == 200, u                                                     # one 404 would break the whole install


def test_service_worker_version_changes_with_each_deploy(client, monkeypatch):
    monkeypatch.setenv("RENDER_GIT_COMMIT", "aaaaaaaaaaaa1111")
    first = sw_text(client)
    monkeypatch.setenv("RENDER_GIT_COMMIT", "bbbbbbbbbbbb2222")
    second = sw_text(client)
    assert 'const VERSION = "aaaaaaaaaaaa"' in first and 'const VERSION = "bbbbbbbbbbbb"' in second and first != second   # new bytes = phones update


def test_without_a_commit_id_the_version_follows_the_files(client, monkeypatch):
    monkeypatch.delenv("RENDER_GIT_COMMIT", raising=False)
    monkeypatch.delenv("SOURCE_VERSION", raising=False)
    target = STATIC / "css" / "pwa.css"
    before_time = target.stat().st_mtime
    v1 = re.search(r'const VERSION = "(\w+)"', sw_text(client)).group(1)
    try:
        os.utime(target, (before_time + 1000, before_time + 1000))
        v2 = re.search(r'const VERSION = "(\w+)"', sw_text(client)).group(1)
    finally:
        os.utime(target, (before_time, before_time))
    assert v1 != v2


def test_service_worker_never_caches_private_pages_or_other_sites(client):
    text = sw_text(client)
    assert 'req.method !== "GET"' in text and "url.origin !== self.location.origin" in text            # posts, uploads and other sites pass straight through
    assert 'req.headers.has("range")' in text                                                          # video streaming is left alone
    assert 'startsWith("/static/")' in text and 'req.mode === "navigate"' in text
    assert not re.search(r"https?://", re.sub(r"/\*.*?\*/", "", text, flags=re.S))                    # no hard-coded outside addresses
    nav = text[text.index("async function page"):text.index("async function offlinePage")]
    assert "cache.put" not in nav and ".put(" not in nav                                               # a page the person opened is never stored


def test_service_worker_is_identical_for_logged_in_people_so_it_holds_no_personal_data(as_user, client):
    c, _ = as_user("alice")
    assert c.get("/sw.js").get_data(as_text=True) == sw_text(client)


# ------------------------------------------------------------------ offline page
def test_offline_page_is_public_generic_and_self_recovering(client, as_user):
    r = client.get("/offline")
    html = r.get_data(as_text=True)
    assert r.status_code == 200 and "You're offline" in html and "data-offline-retry" in html and "js/pwa.js" in html
    assert "csrf" not in html.lower() and "<script>" not in html                                     # nothing personal, no inline script (the CSP forbids it)
    c, _ = as_user("alice")
    assert c.get("/offline").get_data(as_text=True) == html                                          # identical for everyone, so it is safe to cache


# ------------------------------------------------------------------ pages link everything up
@pytest.mark.parametrize("path", ["/login", "/offline"])
def test_public_pages_are_installable(client, path):
    html = client.get(path).get_data(as_text=True)
    if path == "/login":
        assert 'rel="manifest"' in html and "apple-mobile-web-app-capable" in html and "apple-touch-icon" in html
        assert "css/pwa.css" in html and "js/pwa.js" in html and 'data-sw="/sw.js"' in html and "data-pwa-banner" in html
    assert 'name="theme-color"' in html


def test_status_bar_follows_the_theme(client):
    client.set_cookie("cirq_theme", "dark")
    dark = client.get("/login").get_data(as_text=True)
    assert 'apple-mobile-web-app-status-bar-style" content="black"' in dark and 'theme-color" content="#0b0b0c"' in dark
    client.set_cookie("cirq_theme", "light")
    light = client.get("/login").get_data(as_text=True)
    assert 'apple-mobile-web-app-status-bar-style" content="default"' in light and 'theme-color" content="#f6f6f1"' in light


def test_logged_in_pages_get_the_install_banner_and_no_inline_script(as_user):
    c, _ = as_user("alice")
    html = c.get("/feed").get_data(as_text=True)
    assert "data-pwa-banner" in html and 'rel="manifest"' in html
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html)                                      # every script is an external file (strict CSP)


def test_settings_has_an_install_card_with_steps_for_every_platform(as_user):
    c, _ = as_user("alice")
    html = c.get("/settings").get_data(as_text=True)
    for needle in ("Install the CIRQ app", "data-pwa-install", "Add to Home Screen", "Install app", "Add to Dock", "data-pwa-installed"):
        assert needle in html


def test_pwa_urls_do_not_touch_the_session_or_database(app, client, monkeypatch):
    from app.services import supabase_service as sb
    monkeypatch.setattr(sb, "get", lambda *a, **k: pytest.fail("the PWA endpoints must not load a user"))
    client.set_cookie("session", "junk")
    for path in ("/manifest.webmanifest", "/sw.js", "/offline"):
        assert client.get(path).status_code == 200


# ------------------------------------------------------------------ optional Play Store link file
def test_assetlinks_is_off_until_configured(client):
    assert client.get("/.well-known/assetlinks.json").status_code == 404


def test_assetlinks_serves_valid_json_once_configured(app, client):
    fp = ":".join(["AB"] * 32)
    app.config.update(ANDROID_PACKAGE_NAME="com.cirqapp.twa", ANDROID_SHA256_FINGERPRINTS=f"{fp}, not-a-fingerprint")
    r = client.get("/.well-known/assetlinks.json")
    assert r.status_code == 200
    target = r.get_json()[0]["target"]
    assert target == {"namespace": "android_app", "package_name": "com.cirqapp.twa", "sha256_cert_fingerprints": [fp]}   # the malformed one is dropped
    app.config.update(ANDROID_PACKAGE_NAME="", ANDROID_SHA256_FINGERPRINTS="")


def test_client_script_registers_the_worker_and_handles_install_and_updates():
    js = (STATIC / "js" / "pwa.js").read_text()
    for needle in ("serviceWorker.register", "beforeinstallprompt", "appinstalled", "SKIP_WAITING", "controllerchange", "updatefound", "display-mode: standalone"):
        assert needle in js
    assert "if (!hadController || reloading) return" in js                                             # the very first install must not trigger a surprise reload
