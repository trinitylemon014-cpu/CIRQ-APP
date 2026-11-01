import pytest
from tests.conftest import MP4


def post(c, caption="hello world", **files):
    data = {"caption": caption, **files}
    return c.post("/posts", data=data, content_type="multipart/form-data")


def test_create_post_and_see_in_feed(as_user, db):
    c, a = as_user("alice")
    assert post(c, "my first post").status_code == 302
    assert db.data["posts"][0]["caption"] == "my first post"
    assert b"my first post" in c.get("/feed").data
    assert any(r["code"] == "first_post" for r in db.data["user_achievements"])


def test_empty_post_rejected(as_user, db):
    c, _ = as_user("alice")
    post(c, "   ")
    assert not db.data.get("posts")


def test_post_with_image_uses_cloudinary_and_stores_reference(as_user, db, uploads, png):
    import io
    c, _ = as_user("alice")
    post(c, "pic", media=(io.BytesIO(png), "me.png"))
    m = db.data["post_media"][0]
    assert m["public_id"].startswith("cirq/posts/") and m["url"].startswith("https://res.cloudinary.com/")
    assert uploads["uploaded"][0]["resource_type"] == "image"
    assert b"BEGIN" not in str(db.data).encode()                 # no raw bytes in the database


@pytest.mark.parametrize("name,data", [("evil.exe", b"MZ" + b"0" * 50), ("fake.png", b"not an image at all"), ("fake.mp4", b"not a video at all....")])
def test_bad_uploads_rejected(as_user, db, uploads, name, data):
    import io
    c, _ = as_user("alice")
    post(c, "x", media=(io.BytesIO(data), name))
    assert not db.data.get("posts") and not uploads["uploaded"]


def test_upload_size_limits(app, as_user, db, uploads, png):
    import io
    c, _ = as_user("alice")
    app.config["MAX_IMAGE_MB"] = 0
    post(c, "big", media=(io.BytesIO(png), "me.png"))
    assert not db.data.get("posts")


def test_video_post_allowed(as_user, db, uploads):
    import io
    c, _ = as_user("alice")
    post(c, "clip", media=(io.BytesIO(MP4), "clip.mp4"))
    assert db.data["post_media"][0]["media_type"] == "video" and uploads["uploaded"][0]["resource_type"] == "video"


def test_edit_and_delete_only_by_owner(as_user, db, uploads, png):
    import io
    a, _ = as_user("alice"); b, _ = as_user("bob")
    post(a, "original", media=(io.BytesIO(png), "me.png"))
    pid = db.data["posts"][0]["id"]
    assert b.post(f"/posts/{pid}/edit", data={"caption": "hacked"}).status_code == 403
    assert b.post(f"/posts/{pid}/delete").status_code == 403
    a.post(f"/posts/{pid}/edit", data={"caption": "changed"})
    assert db.data["posts"][0]["caption"] == "changed" and db.data["posts"][0]["edited_at"]
    a.post(f"/posts/{pid}/delete")
    assert not db.data["posts"] and uploads["destroyed"]         # Cloudinary file cleaned up


def test_like_unlike_counts_and_notification(as_user, db):
    a, pa = as_user("alice"); b, pb = as_user("bob")
    post(a); pid = db.data["posts"][0]["id"]
    assert b.post(f"/posts/{pid}/like").get_json() == {"liked": True, "count": 1}
    assert b.post(f"/posts/{pid}/like").get_json() == {"liked": False, "count": 0}
    b.post(f"/posts/{pid}/like")
    assert [n for n in db.data["notifications"] if n["kind"] == "like" and n["user_id"] == pa["id"]]
    a.post(f"/posts/{pid}/like")
    assert not [n for n in db.data["notifications"] if n["user_id"] == pa["id"] and n["actor_id"] == pa["id"]]   # no self-notifications


def test_comments_add_delete_permissions(as_user, db):
    a, _ = as_user("alice"); b, _ = as_user("bob"); c, _ = as_user("carol")
    post(a); pid = db.data["posts"][0]["id"]
    r = b.post(f"/posts/{pid}/comments", data={"body": "nice one"}, headers={"X-Requested-With": "fetch"})
    assert r.status_code == 200 and "nice one" in r.get_json()["html"] and r.get_json()["count"] == 1
    cid = db.data["comments"][0]["id"]
    assert c.post(f"/comments/{cid}/delete").status_code == 403          # stranger
    assert a.post(f"/comments/{cid}/delete").status_code == 302          # post owner may remove
    assert not db.data["comments"]


def test_blocked_words_and_phone_numbers_rejected(as_user, db):
    a, _ = as_user("alice")
    post(a, "text me on +256 700 123 456")
    post(a, "go and kys")
    assert not db.data.get("posts")


def test_follow_unfollow_and_counts(as_user, db):
    a, pa = as_user("alice"); b, pb = as_user("bob")
    r = a.post("/u/bob/follow", headers={"X-Requested-With": "fetch"}).get_json()
    assert r == {"following": True, "requested": False, "state": "following", "followers": 1}
    assert a.post("/u/bob/follow", headers={"X-Requested-With": "fetch"}).get_json()["following"] is False
    assert a.post("/u/alice/follow").status_code == 400                  # can't follow yourself
    a.post("/u/bob/follow")
    assert [n for n in db.data["notifications"] if n["kind"] == "follow" and n["user_id"] == pb["id"]]


def test_followers_only_post_visibility(as_user, db):
    a, pa = as_user("alice"); b, pb = as_user("bob")
    a.post("/posts", data={"caption": "secret thoughts", "visibility": "followers"})
    pid = db.data["posts"][0]["id"]
    assert b"secret thoughts" not in b.get("/feed").data
    assert b.get(f"/post/{pid}").status_code == 404
    b.post("/u/alice/follow")
    assert b"secret thoughts" in b.get("/feed").data and b.get(f"/post/{pid}").status_code == 200


def test_private_profile_hides_content(as_user, db):
    a, pa = as_user("alice"); b, _ = as_user("bob")
    post(a, "visible?")
    a.post("/settings", data={"action": "privacy", "is_private": "on"})
    assert db.data["posts"][0]["visibility"] == "followers"
    assert b"private" in b.get("/u/alice").data.lower() and b"visible?" not in b.get("/u/alice").data


def test_block_prevents_visibility_and_messaging(as_user, db):
    a, pa = as_user("alice"); b, pb = as_user("bob")
    post(a, "from alice"); b.post("/u/alice/follow")
    b.post("/u/alice/block")
    assert b.get("/u/alice").status_code == 404 and a.get("/u/bob").status_code == 404
    assert b"from alice" not in b.get("/feed").data
    assert a.post("/messages/start/bob").status_code == 404
    assert not db.data.get("follows")


def test_report_post_creates_one_report(as_user, db):
    a, _ = as_user("alice"); b, _ = as_user("bob")
    post(a); pid = db.data["posts"][0]["id"]
    b.post("/report", data={"target_type": "post", "target_id": pid, "reason": "spam"})
    b.post("/report", data={"target_type": "post", "target_id": pid, "reason": "spam"})
    assert len(db.data["reports"]) == 1
    assert b.post("/report", data={"target_type": "post", "target_id": "nope"}).status_code == 404


def test_settings_cannot_escalate_privileges(as_user, db):
    c, p = as_user("alice")
    c.post("/settings", data={"action": "profile", "display_name": "Alice", "role": "admin", "verified": "1", "account_status": "approved", "coin_balance": "99999"})
    row = db.data["profiles"][0]
    assert row["role"] == "student" and row["verified"] is False and row["coin_balance"] == 0


def test_delete_account_removes_everything(as_user, db, uploads, png):
    import io
    c, p = as_user("alice")
    post(c, "bye", media=(io.BytesIO(png), "me.png"))
    r = c.post("/settings", data={"action": "delete", "confirm_text": "DELETE", "confirm_password": "Sup3r-secret-pw"})
    assert r.status_code == 302
    assert not db.users and uploads["destroyed"]
