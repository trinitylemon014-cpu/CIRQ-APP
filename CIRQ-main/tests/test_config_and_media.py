import pytest
import cloudinary
import httpx
from config import validate_config, Config, REQUIRED_IN_PRODUCTION
from app.services import cloudinary_service as cs
from app.services.supabase_service import RetryingHttpxClient


def test_production_requires_all_credentials():
    def test_supabase_http_client_retries_transient_failures(monkeypatch):
        attempts = []

        def handler(request):
            attempts.append(request)
            if len(attempts) < 3:
                raise httpx.ConnectError("temporary failure")
            return httpx.Response(200, json={"ok": True})

        monkeypatch.setattr("app.services.supabase_service.time.sleep", lambda _: None)
        with RetryingHttpxClient(http2=False, transport=httpx.MockTransport(handler)) as client:
            response = client.get("https://supabase.example/rest/v1/profiles")

        assert response.status_code == 200 and len(attempts) == 3
    with pytest.raises(RuntimeError) as e:
        validate_config({k: "" for k in REQUIRED_IN_PRODUCTION})
    assert "SUPABASE_SERVICE_ROLE_KEY" in str(e.value) and "CLOUDINARY_API_SECRET" in str(e.value)


def test_short_secret_key_rejected():
    cfg = {k: "x" * 40 for k in REQUIRED_IN_PRODUCTION}; cfg["SECRET_KEY"] = "short"
    with pytest.raises(RuntimeError):
        validate_config(cfg)
    cfg["SECRET_KEY"] = "s" * 40
    validate_config(cfg)


def test_paid_competitions_default_off_and_secrets_not_in_templates(monkeypatch, app, as_user):
    assert Config.PAID_COMPETITIONS_ENABLED is False
    cloudinary.config(api_secret="topsecretvalue-123-xyz")
    app.config["SUPABASE_SERVICE_ROLE_KEY"] = "service-key-value-456-abc"
    c, _ = as_user("alice")
    html = "".join(c.get(p).get_data(as_text=True) for p in ("/feed", "/games", "/coins", "/settings", "/discover", "/linkup"))
    assert "topsecretvalue-123-xyz" not in html and "service-key-value-456-abc" not in html and "service_role" not in html.lower()


def test_cloudinary_urls_are_optimised_and_private_ones_signed(uploads):
    pub = cs.media_src({"public_id": "cirq/posts/a", "media_type": "image", "delivery_type": "upload", "url": "u"}, 600)
    assert "w_600" in pub and "f_auto" in pub and "q_auto" in pub
    priv = cs.media_src({"public_id": "cirq/chat/a", "media_type": "image", "delivery_type": "authenticated", "url": "u"}, 400)
    assert "/authenticated/" in priv and "s--" in priv
    vid = cs.media_src({"public_id": "cirq/reels/a", "media_type": "video", "delivery_type": "upload", "url": "u"}, 540, thumb=True)
    assert vid.endswith(".jpg") and "/video/upload/" in vid


def test_secret_never_in_static_files():
    import pathlib
    for f in pathlib.Path("app/static").rglob("*.js"):
        t = f.read_text()
        assert "API_SECRET" not in t.upper() and "SERVICE_ROLE" not in t.upper()


def test_security_headers_present(client):
    h = client.get("/").headers
    assert h["X-Frame-Options"] == "DENY" and "script-src 'self'" in h["Content-Security-Policy"] and h["X-Content-Type-Options"] == "nosniff"


def test_no_inline_scripts_in_templates():
    import pathlib, re
    for f in pathlib.Path("app/templates").rglob("*.html"):
        for m in re.finditer(r"<script(?![^>]*\bsrc=)[^>]*>", f.read_text()):
            pytest.fail(f"inline script in {f}")
        assert not re.search(r"\son(click|change|submit|load)=", f.read_text().replace("onchange=\"this.form.submit()\"", "")), f
