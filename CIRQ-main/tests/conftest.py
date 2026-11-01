import io
import uuid
import pytest
import cloudinary
import cloudinary.uploader
from PIL import Image

from app import create_app
from config import TestConfig
from tests.fakedb import FakeDB, FakeAnon, NOW, seed_from_schema

PASSWORD = "Sup3r-secret-pw"


@pytest.fixture
def db():
    d = FakeDB()
    seed_from_schema(d)
    return d


@pytest.fixture
def uploads(monkeypatch):
    """Replace the Cloudinary network calls. Validation code still runs for real."""
    log = {"uploaded": [], "destroyed": []}

    def fake_upload(fileobj, **opts):
        n = len(log["uploaded"]) + 1
        pid = f"{opts['folder']}/{opts['public_id']}"
        log["uploaded"].append({"public_id": pid, **{k: opts[k] for k in ("resource_type", "type")}})
        return {"secure_url": f"https://res.cloudinary.com/demo/{opts['resource_type']}/upload/{pid}", "public_id": pid,
                "bytes": 1234, "width": 10, "height": 10, "duration": 3 if opts["resource_type"] == "video" else None}

    def fake_destroy(public_id, **opts):
        log["destroyed"].append(public_id)
        return {"result": "ok"}

    cloudinary.config(cloud_name="demo", api_key="k", api_secret="s", secure=True)
    monkeypatch.setattr(cloudinary.uploader, "upload", fake_upload)
    monkeypatch.setattr(cloudinary.uploader, "destroy", fake_destroy)
    return log


@pytest.fixture
def app(db, uploads):
    app = create_app(TestConfig, clients=(db, lambda: FakeAnon(db)))
    return app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def school(db):
    s = db.table("schools").insert({"name": "Kampala Int'l", "slug": "kampala-intl", "country": "Uganda"}).execute().data[0]
    db.table("communities").insert({"school_id": s["id"], "name": "Kampala Int'l Community"}).execute()
    return s


def make_user(db, username, school=None, status="approved", role="student", password=PASSWORD, email=None, verified_email=True):
    email = email or f"{username}@example.com"
    uid = db.auth.admin.create_user({"email": email, "password": password}).user.id
    row = db.table("profiles").insert({"id": uid, "username": username, "display_name": username.title(), "account_status": status,
                                       "role": role, "school_id": school["id"] if school else None,
                                       "email_verified_at": NOW() if verified_email else None}).execute().data[0]
    return row


@pytest.fixture
def mk(db, school):
    return lambda username, **kw: make_user(db, username, school=kw.pop("school", school), **kw)


def login(client, username, password=PASSWORD):
    return client.post("/login", data={"username": username, "password": password})


@pytest.fixture
def as_user(app, db, mk):
    """Return (client, profile) already logged in."""
    def _make(username="alice", **kw):
        p = mk(username, **kw)
        c = app.test_client()
        r = login(c, username)
        assert r.status_code == 302, r.data
        return c, p
    return _make


@pytest.fixture
def png():
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (30, 200, 90)).save(buf, "PNG")
    return buf.getvalue()


MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 64
