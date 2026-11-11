import logging
import os
import click
import httpx
from urllib.parse import urlparse
from flask import Flask, g, session, render_template, request, jsonify, url_for
from werkzeug.middleware.proxy_fix import ProxyFix
from config import Config, DevConfig, TestConfig, validate_config
from app.extensions import csrf, limiter
from app.services import supabase_service as sb, cloudinary_service as cs
from app.services.coin_service import is_boosted
from app.services.notification_service import unread_count
from app.navigation import admin_navigation, main_navigation
from app.utils import time_ago, fmt_event_dt, short_count, utcnow, iso, tier_for


def create_app(config_object=None, clients=None):
    """Application factory. `clients` = (service_client, anon_factory) lets tests inject a fake database."""
    app = Flask(__name__)
    if config_object is None:
        config_object = DevConfig if os.environ.get("FLASK_ENV") == "development" else Config
    app.config.from_object(config_object)
    if app.config["ENV"] == "production":
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if not app.config["TESTING"]:
        validate_config(app.config) if app.config["ENV"] == "production" else None
        if app.config["ENV"] != "production" and not app.config["SECRET_KEY"]:
            app.config["SECRET_KEY"] = "dev-only-secret-change-me-" + "x" * 16

    if clients:
        app.extensions["cirq_db"], app.extensions["cirq_anon_factory"] = clients
    else:
        url, svc, anon = app.config["SUPABASE_URL"], app.config["SUPABASE_SERVICE_ROLE_KEY"], app.config["SUPABASE_ANON_KEY"]
        if url and svc:
            app.extensions["cirq_db"] = sb.make_client(url, svc)
            app.extensions["cirq_anon_factory"] = lambda: sb.make_client(url, anon)
        else:
            app.logger.warning("Supabase is not configured; database features will fail until it is.")
    cs.configure(app)
    csrf.init_app(app)
    limiter.init_app(app)

    from app.blueprints.auth.routes import bp as auth_bp
    from app.blueprints.feed.routes import bp as feed_bp
    from app.blueprints.profiles.routes import bp as profiles_bp
    from app.blueprints.discover.routes import bp as discover_bp
    from app.blueprints.reels.routes import bp as reels_bp
    from app.blueprints.stories.routes import bp as stories_bp
    from app.blueprints.linkup.routes import bp as linkup_bp
    from app.blueprints.messages.routes import bp as messages_bp
    from app.blueprints.messages.routes import attachments_bp
    from app.blueprints.notifications.routes import bp as notifications_bp
    from app.blueprints.games.routes import bp as games_bp
    from app.blueprints.schools.routes import bp as schools_bp
    from app.blueprints.admin.routes import bp as admin_bp
    from app.blueprints.wallet.routes import bp as wallet_bp
    from app.blueprints.lending.routes import bp as lending_bp
    from app.blueprints.legal.routes import bp as legal_bp
    from app.blueprints.trivia.routes import bp as trivia_bp
    from app.blueprints.recs.routes import bp as recs_bp
    for b in (auth_bp, feed_bp, profiles_bp, discover_bp, reels_bp, stories_bp, linkup_bp, messages_bp,
              notifications_bp, attachments_bp, games_bp, schools_bp, admin_bp, wallet_bp, lending_bp, trivia_bp, recs_bp, legal_bp):
        app.register_blueprint(b)

    @app.before_request
    def load_user():
        g.user = None
        if request.endpoint in (None, "static", "health"):
            return
        uid = session.get("uid")
        if not uid or "cirq_db" not in app.extensions:
            return
        p = sb.get("profiles", id=uid)
        if not p or p.get("session_epoch", 0) != session.get("epoch") or p["account_status"] == "suspended":
            session.clear()
            return
        g.user = p

    @app.after_request
    def security_headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        resp.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(self), camera=(self)")
        parsed_storage_url = urlparse(app.config.get("SUPABASE_URL", ""))
        storage_origin = f"{parsed_storage_url.scheme}://{parsed_storage_url.netloc}" if parsed_storage_url.netloc else ""
        resp.headers.setdefault("Content-Security-Policy",
            f"default-src 'self'; img-src 'self' data: blob: https://res.cloudinary.com {storage_origin}; "
            f"media-src 'self' blob: https://res.cloudinary.com {storage_origin}; frame-src 'self' {storage_origin}; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src https://fonts.gstatic.com; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
        if g.get("user"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.context_processor
    def inject():
        def unread_notifs():
            if not g.get("user"):
                return 0
            if "_unread" not in g.__dict__:
                g._unread = unread_count(g.user["id"])
            return g._unread

        def unread_msgs():
            if not g.get("user"):
                return 0
            from app.blueprints.messages.routes import unread_total
            return unread_total(g.user["id"])
        def nav_items():
            if not g.get("user"):
                return []
            return main_navigation(g.user, request.endpoint, unread_msgs(), unread_notifs())
        def admin_nav_items():
            if not g.get("user"):
                return []
            return admin_navigation(g.user, request.endpoint)
        return {"cur": g.get("user"), "unread_notifs": unread_notifs, "unread_msgs": unread_msgs,
                "avatar_src": cs.avatar_src, "media_src": cs.media_src, "audio_src": cs.audio_src,
                "school_logo_src": cs.school_logo_src, "is_boosted": is_boosted,
                "paid_enabled": app.config["PAID_COMPETITIONS_ENABLED"], "tier_for": tier_for,
                "nav_items": nav_items, "admin_nav_items": admin_nav_items}

    app.jinja_env.globals.update(avatar_src=cs.avatar_src, media_src=cs.media_src, audio_src=cs.audio_src,
                                 school_logo_src=cs.school_logo_src, is_boosted=is_boosted, tier_for=tier_for)
    app.jinja_env.filters["ago"] = time_ago
    app.jinja_env.filters["event_dt"] = fmt_event_dt
    app.jinja_env.filters["short"] = short_count

    @app.get("/health")
    def health():
        return jsonify(status="ok", time=iso())

    def _err(code, title, msg):
        if request.path.startswith("/api/") or request.headers.get("X-Requested-With") == "fetch" or request.is_json:
            return jsonify(error=msg), code
        return render_template("errors/error.html", code=code, title=title, msg=msg), code

    @app.errorhandler(403)
    def e403(e): return _err(403, "No access", "You don't have permission to open this page.")

    @app.errorhandler(404)
    def e404(e): return _err(404, "Lost in orbit", "We couldn't find that page.")

    @app.errorhandler(413)
    def e413(e): return _err(413, "File too large", "That upload is bigger than CIRQ allows.")

    @app.errorhandler(429)
    def e429(e): return _err(429, "Slow down", "Too many attempts. Please wait a moment and try again.")

    @app.errorhandler(httpx.TransportError)
    def e_database_unavailable(e):
        app.logger.warning("database transport failed after retries: %s", type(e).__name__)
        return _err(503, "Temporarily unavailable", "The service is temporarily unavailable. Please try again.")

    @app.errorhandler(500)
    def e500(e):
        app.logger.exception("unhandled error")
        return _err(500, "Something broke", "Something went wrong on our side. Please try again.")

    from app.csrf_errors import register_csrf_handler
    register_csrf_handler(app)
    register_cli(app)
    return app


def register_cli(app):
    @app.cli.command("create-admin")
    @click.option("--username", prompt=True)
    @click.option("--email", prompt=True)
    @click.option("--display-name", prompt=True)
    @click.password_option()
    def create_admin(username, email, display_name, password):
        """Create the first administrator securely (password is typed, never stored in files)."""
        from app.services import auth_service
        from app.utils import normalize_username, valid_username, password_problems
        username = normalize_username(username)
        if not valid_username(username):
            raise click.ClickException("Invalid username.")
        probs = password_problems(password)
        if probs:
            raise click.ClickException(" ".join(probs))
        uid = auth_service.create_auth_user(email.strip().lower(), password)
        sb.insert("profiles", {"id": uid, "username": username, "display_name": display_name, "role": "admin",
                               "account_status": "approved", "email_verified_at": iso(),
                               "approval_source": "admin", "approved_at": iso()})
        from app.services.audit_service import audit
        audit(None, "bootstrap_admin_approved", "profile", uid, username=username)
        click.echo(f"Administrator '{username}' created.")

    @app.cli.command("cleanup-expired")
    def cleanup_expired():
        """Delete expired stories (rows + Cloudinary files) and expire old reset tokens."""
        click.echo(run_cleanup())


def run_cleanup():
    from app.extensions import db
    now = iso()
    expired = db().table("stories").select("*").lt("expires_at", now).limit(500).execute().data or []
    for s in expired:
        cs.destroy(s["public_id"], s["media_type"], s.get("delivery_type", "upload"))
        cs.destroy(s.get("audio_public_id"), "audio")          # the story's music clip, if it had one
        sb.delete("stories", id=s["id"])
    stale = db().table("password_reset_requests").select("id").eq("status", "approved").lt("token_expires_at", now).execute().data or []
    for r in stale:
        sb.update("password_reset_requests", {"status": "expired"}, id=r["id"])
    try:
        from app.services import recommender
        old_signals = recommender.cleanup_old_signals()
    except Exception:
        old_signals = 0
    return f"removed {len(expired)} expired stories; expired {len(stale)} reset tokens; pruned {old_signals} old viewing signals"
