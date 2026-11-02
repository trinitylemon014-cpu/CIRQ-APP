import io
import uuid
from types import SimpleNamespace

from storage3.exceptions import StorageException

from tests.conftest import MP4


def install_storage(monkeypatch, db, *, fail_upload=False):
    log = {"bucket": None, "uploaded": [], "removed": [], "signed": []}

    class Bucket:
        def upload(self, path, data, file_options):
            if fail_upload:
                raise StorageException("storage unavailable")
            log["uploaded"].append((path, data, file_options))

        def remove(self, paths):
            log["removed"].extend(paths)

        def create_signed_url(self, path, expires_in, options=None):
            log["signed"].append((path, expires_in, options or {}))
            return {"signedURL": f"https://storage.example.test/signed/{path}?token=temporary"}

    bucket = Bucket()
    monkeypatch.setattr(db, "storage", SimpleNamespace(from_=lambda name: log.update(bucket=name) or bucket), raising=False)
    return log


def send_file(client, cid, filename, data, mime, *, body="", message_id=None):
    return client.post(
        f"/api/attachments/conversations/{cid}/messages",
        data={
            "message_id": message_id or str(uuid.uuid4()),
            "body": body,
            "attachments": (io.BytesIO(data), filename, mime),
        },
        content_type="multipart/form-data",
        headers={"X-Requested-With": "fetch"},
    )


def test_uploads_multiple_private_attachments_and_serves_them_to_members(as_user, db, monkeypatch, png):
    storage = install_storage(monkeypatch, db)
    alice, _ = as_user("alice")
    bob, _ = as_user("bob")
    cid = alice.post("/messages/start/bob").headers["Location"].rsplit("/", 1)[1]
    request_id = str(uuid.uuid4())
    response = alice.post(
        f"/api/attachments/conversations/{cid}/messages",
        data={
            "message_id": request_id,
            "body": "See these files",
            "attachments": [
                (io.BytesIO(png), "photo.png", "image/png"),
                (io.BytesIO(b"meeting notes\n"), "notes.txt", "text/plain"),
            ],
        },
        content_type="multipart/form-data",
        headers={"X-Requested-With": "fetch"},
    )
    assert response.status_code == 200
    assert b"photo.png" in response.data and b"notes.txt" in response.data
    message = next(row for row in db.data["messages"] if row["id"] == request_id)
    attachments = [row for row in db.data["message_attachments"] if row["message_id"] == request_id]
    assert message["attachment_count"] == 2 and message["body"] == "See these files"
    assert {row["kind"] for row in attachments} == {"image", "document"}
    assert all(row["storage_path"].startswith(f"{cid}/{request_id}/") for row in attachments)
    assert storage["bucket"] == "chat-attachments" and len(storage["uploaded"]) == 2
    assert "data-lightbox-src" in bob.get(f"/messages/{cid}").get_data(as_text=True)

    image = next(row for row in attachments if row["kind"] == "image")
    viewed = bob.get(f"/api/attachments/{image['id']}/view")
    downloaded = bob.get(f"/api/attachments/{image['id']}/download")
    assert viewed.status_code == 302 and "token=temporary" in viewed.location
    assert downloaded.status_code == 302
    assert storage["signed"][0][1] == 900
    assert storage["signed"][1][2] == {"download": "photo.png"}


def test_attachment_only_message_and_idempotent_retry(as_user, db, monkeypatch, png):
    install_storage(monkeypatch, db)
    alice, _ = as_user("alice")
    bob, _ = as_user("bob")
    cid = alice.post("/messages/start/bob").headers["Location"].rsplit("/", 1)[1]
    request_id = str(uuid.uuid4())
    response = send_file(alice, cid, "image.png", png, "image/png", message_id=request_id)
    assert response.status_code == 200
    assert db.data["messages"][0]["body"] is None
    assert db.data["messages"][0]["attachment_count"] == 1
    retry = send_file(alice, cid, "image.png", png, "image/png", message_id=request_id)
    assert retry.status_code == 200 and len(db.data["messages"]) == 1
    assert b"image.png" in bob.get(f"/messages/{cid}").data


def test_rejects_bad_extensions_mime_and_oversize_uploads(as_user, db, monkeypatch, png, app):
    install_storage(monkeypatch, db)
    alice, _ = as_user("alice")
    bob, _ = as_user("bob")
    cid = alice.post("/messages/start/bob").headers["Location"].rsplit("/", 1)[1]

    disallowed = send_file(alice, cid, "bad.exe", b"MZ" + b"\0" * 8, "application/octet-stream")
    assert disallowed.status_code == 415 and "error" in disallowed.get_json()
    mime_mismatch = send_file(alice, cid, "photo.png", png, "application/octet-stream")
    assert mime_mismatch.status_code == 415

    app.config["CHAT_ATTACHMENT_OTHER_MAX_MB"] = 0
    too_large = send_file(alice, cid, "notes.txt", b"oversize", "text/plain")
    assert too_large.status_code == 413 and "error" in too_large.get_json()
    assert not db.data.get("messages")


def test_rejects_nonmembers_and_blocked_users(as_user, db, monkeypatch, png):
    install_storage(monkeypatch, db)
    alice, _ = as_user("alice")
    bob, _ = as_user("bob")
    carol, _ = as_user("carol")
    cid = alice.post("/messages/start/bob").headers["Location"].rsplit("/", 1)[1]
    sent = send_file(alice, cid, "photo.png", png, "image/png")
    attachment_id = db.data["message_attachments"][0]["id"]

    assert carol.get(f"/api/attachments/{attachment_id}/view").status_code == 404
    assert bob.get(f"/api/attachments/{attachment_id}/view").status_code == 302
    bob.post("/u/alice/block")
    assert bob.get(f"/api/attachments/{attachment_id}/view").status_code == 404
    assert alice.get(f"/api/attachments/{attachment_id}/download").status_code == 404
    assert sent.status_code == 200


def test_rolls_back_message_when_storage_upload_fails(as_user, db, monkeypatch, png):
    storage = install_storage(monkeypatch, db, fail_upload=True)
    alice, _ = as_user("alice")
    bob, _ = as_user("bob")
    cid = alice.post("/messages/start/bob").headers["Location"].rsplit("/", 1)[1]
    response = send_file(alice, cid, "photo.png", png, "image/png")
    assert response.status_code == 502 and "error" in response.get_json()
    assert not db.data.get("messages") and not db.data.get("message_attachments")
    assert storage["uploaded"] == []


def test_escapes_uploaded_filenames_in_chat(as_user, db, monkeypatch):
    install_storage(monkeypatch, db)
    alice, _ = as_user("alice")
    bob, _ = as_user("bob")
    cid = alice.post("/messages/start/bob").headers["Location"].rsplit("/", 1)[1]
    response = send_file(alice, cid, "<img src=x onerror=alert(1)>.txt", b"plain text", "text/plain")
    assert response.status_code == 200
    html = bob.get(f"/messages/{cid}").get_data(as_text=True)
    assert "&lt;img" in html and "<img src=x onerror=alert(1)>" not in html


def test_video_audio_and_pdf_preview_controls(as_user, db, monkeypatch):
    storage = install_storage(monkeypatch, db)
    alice, _ = as_user("alice")
    bob, _ = as_user("bob")
    cid = alice.post("/messages/start/bob").headers["Location"].rsplit("/", 1)[1]
    video = send_file(alice, cid, "clip.mp4", MP4, "video/mp4")
    audio = send_file(alice, cid, "voice.mp3", b"ID3" + b"\0" * 20, "audio/mpeg")
    pdf = send_file(alice, cid, "guide.pdf", b"%PDF-1.4\nsample\n%%EOF", "application/pdf")
    assert video.status_code == audio.status_code == pdf.status_code == 200
    assert '<video controls preload="metadata"' in video.get_json()["html"] and "Download video" in video.get_json()["html"]
    assert '<audio class="chat-audio-attachment"' in audio.get_json()["html"] and "Download audio" in audio.get_json()["html"]
    assert "data-pdf-preview-url" in pdf.get_json()["html"]

    pdf_id = next(item["id"] for item in db.data["message_attachments"] if item["mime_type"] == "application/pdf")
    preview = bob.get(f"/api/attachments/{pdf_id}/preview-url")
    assert preview.status_code == 200 and preview.get_json()["url"].startswith("https://storage.example.test/")
    assert storage["signed"][-1][2] == {}
