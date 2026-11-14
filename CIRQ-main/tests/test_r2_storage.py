"""Cloudflare R2 holds public videos; Cloudinary keeps images/audio; chat files stay private. No network is used."""
import io
import struct
import types

import pytest
from werkzeug.datastructures import FileStorage

from app.services import cloudinary_service as cs
from app.services import r2_service
from tests.conftest import MP4

BASE = "https://media.example.com"


def box(kind, payload):
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


def mp4(seconds, version=0, moov_first=True):
    """A minimal but structurally real MP4 whose movie header says it lasts `seconds`."""
    ftyp = box(b"ftyp", b"mp42\x00\x00\x00\x00mp42isom")
    if version == 1:
        mvhd = box(b"mvhd", b"\x01\x00\x00\x00" + struct.pack(">QQIQ", 0, 0, 1000, int(seconds * 1000)) + b"\x00" * 80)
    else:
        mvhd = box(b"mvhd", b"\x00\x00\x00\x00" + struct.pack(">IIII", 0, 0, 1000, int(seconds * 1000)) + b"\x00" * 80)
    moov, mdat = box(b"moov", mvhd), box(b"mdat", b"\x00" * 500)
    return ftyp + (moov + mdat if moov_first else mdat + moov)


@pytest.fixture
def r2(app, monkeypatch):
    app.config.update(R2_ACCOUNT_ID="acct", R2_ACCESS_KEY_ID="k", R2_SECRET_ACCESS_KEY="s",
                      R2_BUCKET="cirq-media", R2_PUBLIC_BASE_URL=BASE)
    log = {"put": [], "deleted": [], "fail": False}

    def fake_put(data, key, content_type):
        if log["fail"]:
            raise RuntimeError("r2 is down")
        log["put"].append({"key": key, "type": content_type, "size": len(data)})
        return f"{BASE}/{key}"

    monkeypatch.setattr(r2_service, "upload_bytes", fake_put)
    monkeypatch.setattr(r2_service, "_client", lambda: types.SimpleNamespace(
        delete_object=lambda **kw: log["deleted"].append(kw["Key"])))
    return log


def post_reel(client, data, name="a.mp4"):
    return client.post("/reels", data={"video": (io.BytesIO(data), name), "caption": "hi"}, content_type="multipart/form-data",
                       headers={"X-Requested-With": "fetch"})


# ---------------- reading the length of a video
def test_mp4_duration_is_read_from_the_movie_header():
    assert r2_service.mp4_duration(mp4(12.5)) == pytest.approx(12.5)
    assert r2_service.mp4_duration(mp4(33, version=1)) == pytest.approx(33)
    assert r2_service.mp4_duration(mp4(7, moov_first=False)) == pytest.approx(7)      # moov after the media data


@pytest.mark.parametrize("data", [b"", b"not a video at all", MP4, mp4(5)[:30], mp4(0)])
def test_unreadable_or_zero_length_videos_have_no_duration(data):
    assert r2_service.mp4_duration(data) is None


# ---------------- uploads
def test_reel_video_goes_to_r2_not_cloudinary(as_user, db, uploads, r2):
    c, _ = as_user("alice")
    res = post_reel(c, mp4(12.5))
    assert res.status_code == 200 and res.get_json()["ok"] is True
    assert uploads["uploaded"] == []                                                   # Cloudinary was not used
    assert len(r2["put"]) == 1 and r2["put"][0]["type"] == "video/mp4" and r2["put"][0]["key"].startswith("cirq/reels/")
    row = db.data["post_media"][0]
    assert row["delivery_type"] == "r2" and row["url"] == f"{BASE}/{row['public_id']}" and row["media_type"] == "video"
    assert float(row["duration"]) == pytest.approx(12.5)


def test_reels_page_plays_the_r2_url_and_the_csp_allows_it(as_user, db, uploads, r2):
    c, _ = as_user("alice")
    post_reel(c, mp4(5))
    page = c.get("/reels")
    html = page.get_data(as_text=True)
    assert f'data-src="{BASE}/cirq/reels/' in html
    assert BASE in page.headers["Content-Security-Policy"].split("media-src")[1].split(";")[0]


def test_a_reel_longer_than_the_limit_is_rejected_and_removed_from_r2(as_user, db, uploads, r2):
    c, _ = as_user("alice")
    res = post_reel(c, mp4(120))
    assert res.status_code == 400 and "90 seconds" in res.get_json()["error"]
    assert len(r2["deleted"]) == 1 and r2["deleted"][0] == r2["put"][0]["key"]
    assert db.data["posts"] == []


def test_webm_and_unreadable_videos_fall_back_to_cloudinary(as_user, db, uploads, r2):
    c, _ = as_user("alice")
    webm = b"\x1a\x45\xdf\xa3" + b"\x00" * 64
    assert post_reel(c, webm, "a.webm").status_code == 200
    assert post_reel(c, MP4, "b.mp4").status_code == 200                               # no readable length
    assert r2["put"] == [] and len(uploads["uploaded"]) == 2
    assert {row["delivery_type"] for row in db.data["post_media"]} == {"upload"}


def test_an_r2_outage_never_blocks_an_upload(as_user, db, uploads, r2):
    c, _ = as_user("alice")
    r2["fail"] = True
    assert post_reel(c, mp4(8)).status_code == 200
    assert len(uploads["uploaded"]) == 1 and db.data["post_media"][0]["delivery_type"] == "upload"


def test_images_private_chat_files_and_unconfigured_r2_stay_on_cloudinary(app, uploads, r2, png):
    def fs(data, name):
        return FileStorage(io.BytesIO(data), filename=name)
    with app.app_context():
        assert cs.upload(fs(png, "a.png"), "posts", "u" * 8)["delivery_type"] == "upload"           # image
        assert cs.upload(fs(mp4(5), "a.mp4"), "chat", "u" * 8, private=True)["delivery_type"] == "authenticated"
        assert r2["put"] == []
        app.config["R2_PUBLIC_BASE_URL"] = ""                                                         # R2 switched off
        assert cs.upload(fs(mp4(5), "a.mp4"), "reels", "u" * 8)["delivery_type"] == "upload"
        assert r2["put"] == []


# ---------------- deleting and displaying
def test_destroy_removes_r2_videos_from_r2_and_cloudinary_ones_from_cloudinary(app, uploads, r2):
    with app.app_context():
        assert cs.destroy("cirq/reels/abc.mp4", "video", "r2") is True
        assert cs.destroy("cirq/reels/old_clip", "video", "upload") is True
    assert r2["deleted"] == ["cirq/reels/abc.mp4"] and uploads["destroyed"] == ["cirq/reels/old_clip"]


def test_media_src_serves_r2_videos_directly_with_no_generated_poster(app, r2):
    row = {"url": f"{BASE}/cirq/reels/a.mp4", "public_id": "cirq/reels/a.mp4", "media_type": "video", "delivery_type": "r2"}
    with app.app_context():
        assert cs.media_src(row, 540) == row["url"]
        assert cs.media_src(row, 540, thumb=True) == ""


def test_deleting_a_reel_removes_its_file_from_r2(as_user, db, uploads, r2):
    c, _ = as_user("alice")
    post_reel(c, mp4(6))
    post_id = db.data["posts"][0]["id"]
    key = db.data["post_media"][0]["public_id"]
    assert c.post(f"/posts/{post_id}/delete", headers={"X-Requested-With": "fetch"}).status_code in (200, 204, 302)
    assert key in r2["deleted"]
