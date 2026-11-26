"""Progressive Web App: the web manifest, the service worker, the offline page and (optional) the Android app-link file.

What makes CIRQ installable on any phone or computer:
  * /manifest.webmanifest   name, colours, icons, shortcuts: what the "Install" / "Add to Home Screen" dialogs read
  * /sw.js                  the service worker. Served from the site root so it controls the whole app; versioned per deploy
  * /offline                the page shown when the app can't reach the server
Nothing here needs a login and nothing here ever contains personal data.
"""
import hashlib
import os
import re
from pathlib import Path

from flask import Blueprint, Response, abort, current_app, jsonify, render_template, request, url_for

from app.blueprints.pwa.icon_data import ICONS

bp = Blueprint("pwa", __name__)

DARK, GOLD = "#0b0b0c", "#f7b500"
# Small, always-needed files that are saved at install time so the app shell opens instantly and works on a weak connection.
# Big or rarely used files (the live-stream library, images) are cached the first time they are used instead.
PRECACHE_STATIC = ("css/style.css", "css/style-light.css", "css/pwa.css", "js/app.js", "js/pwa.js", "favicon.ico")
FINGERPRINT = re.compile(r"^([0-9A-F]{2}:){31}[0-9A-F]{2}$")


def _static_urls():
    root = Path(current_app.static_folder)
    return [url_for("static", filename=f) for f in PRECACHE_STATIC if (root / f).is_file()]      # never list a file that doesn't exist: one 404 would fail the whole install


def version():
    """Changes whenever a deploy changes the app's files, so phones know to fetch the new version.
    Render sets RENDER_GIT_COMMIT; anywhere else it is a fingerprint of the precached files."""
    commit = os.getenv("RENDER_GIT_COMMIT") or os.getenv("SOURCE_VERSION")
    if commit:
        return commit[:12]
    digest = hashlib.sha256()
    root = Path(current_app.static_folder)
    for path in [root / f for f in PRECACHE_STATIC] + [Path(current_app.root_path) / "templates" / "pwa" / "sw.js"]:
        try:
            st = path.stat()
            digest.update(f"{path.name}:{st.st_size}:{int(st.st_mtime)}".encode())
        except OSError:
            digest.update(path.name.encode())
    return digest.hexdigest()[:12]


def _icon(name, size, purpose):
    # Served by this app from icon_data.py (not from a file in /static), so an install can never be broken by a missing image file.
    return {"src": url_for("pwa.icon", name=name), "sizes": f"{size}x{size}", "type": "image/png", "purpose": purpose}


def _png(name):
    data = ICONS.get(name)
    if data is None:
        abort(404)
    resp = Response(data, mimetype="image/png")
    resp.headers["Cache-Control"] = "public, max-age=86400"
    resp.add_etag()
    return resp.make_conditional(request)


@bp.get("/pwa-icons/<name>")
def icon(name):
    return _png(name)


@bp.get("/apple-touch-icon-precomposed.png")
@bp.get("/apple-touch-icon.png")                      # registered first, so this is the address url_for() builds
def apple_touch_icon():
    """iPhones and iPads also look for this exact address on their own, so it must exist at the site root."""
    return _png("apple-touch-icon.png")


@bp.get("/manifest.webmanifest")
def manifest():
    shortcuts = [("Reels", "reels.index", "Watch reels"), ("Messages", "messages.inbox", "Open your chats"),
                 ("Discover", "discover.index", "Find people and schools"), ("Live", "live.index", "Watch live streams")]
    data = {
        "id": "/",
        "name": "CIRQ",
        "short_name": "CIRQ",
        "description": "CIRQ is the social app for your school community: share posts, stories and reels, chat, go live and discover people.",
        "start_url": "/?source=pwa",
        "scope": "/",
        "display": "standalone",
        "display_override": ["standalone", "minimal-ui"],
        "background_color": DARK,                  # the splash screen colour while the app opens
        "theme_color": DARK,                       # the title / status bar colour (each page can still set its own for the light theme)
        "lang": "en",
        "dir": "ltr",
        "categories": ["social"],
        "prefer_related_applications": False,
        "launch_handler": {"client_mode": "focus-existing"},
        "icons": [_icon("icon-192.png", 192, "any"), _icon("icon-512.png", 512, "any"),
                  _icon("icon-maskable-192.png", 192, "maskable"), _icon("icon-maskable-512.png", 512, "maskable")],
        "shortcuts": [{"name": name, "short_name": name, "description": desc, "url": url_for(endpoint) + "?source=shortcut",
                       "icons": [_icon("icon-192.png", 192, "any")]} for name, endpoint, desc in shortcuts],
    }
    resp = Response(jsonify(data).get_data(as_text=True), mimetype="application/manifest+json")
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp


@bp.get("/sw.js")
def service_worker():
    body = render_template("pwa/sw.js", version=version(), offline_url=url_for("pwa.offline"),
                           precache=[url_for("pwa.offline")] + _static_urls())
    resp = Response(body, mimetype="text/javascript")
    resp.headers["Cache-Control"] = "no-cache, max-age=0"          # browsers must always be able to find a new service worker
    resp.headers["Service-Worker-Allowed"] = "/"
    return resp


@bp.get("/offline")
def offline():
    resp = Response(render_template("pwa/offline.html"))
    resp.headers["Cache-Control"] = "public, max-age=0, must-revalidate"
    return resp


@bp.get("/.well-known/assetlinks.json")
def assetlinks():
    """Lets a Play Store (Trusted Web Activity) build of CIRQ open the site full-screen without an address bar.
    Only served once ANDROID_PACKAGE_NAME and ANDROID_SHA256_FINGERPRINTS are set. See PWA.md."""
    package = (current_app.config.get("ANDROID_PACKAGE_NAME") or "").strip()
    prints = [p.strip().upper() for p in (current_app.config.get("ANDROID_SHA256_FINGERPRINTS") or "").split(",")]
    prints = [p for p in prints if FINGERPRINT.match(p)]
    if not package or not prints:
        abort(404)
    return jsonify([{"relation": ["delegate_permission/common.handle_all_urls"],
                     "target": {"namespace": "android_app", "package_name": package, "sha256_cert_fingerprints": prints}}])
