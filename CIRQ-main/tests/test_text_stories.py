"""Text stories: simple written text on a colour background, shared like any other story."""
import io
import re

from app.utils import iso, hours_from_now
from tests.conftest import MP4  # noqa: F401
from tests.test_v2_features import MP3

FETCH = {"X-Requested-With": "fetch"}


def post_text(c, text="Exam week survival mode 📚", **extra):
    return c.post("/stories", data={"story_type": "text", "text": text, **extra})


def api(c, username="alice"):
    return c.get(f"/stories/api/{username}").get_json()


def stories(db, **match):
    return [s for s in db.data.get("stories", []) if all(s.get(k) == v for k, v in match.items())]


# ------------------------------------------------------------------ creating
def test_text_story_is_saved_without_any_upload(as_user, db, uploads):
    c, me = as_user("alice")
    r = post_text(c, "Good luck on exams!", bg="ocean")
    assert r.status_code == 302
    [s] = stories(db, author_id=me["id"])
    assert s["media_type"] == "text" and s["text_body"] == "Good luck on exams!" and s["bg_style"] == "ocean"
    assert s["url"] is None and s["public_id"] is None and s["caption"] is None
    assert uploads["uploaded"] == []                                    # nothing goes to Cloudinary
    assert s["expires_at"]                                              # still disappears after the usual 24 hours


def test_text_story_shows_in_the_feed_tray_and_the_viewer_api(as_user, db):
    c, me = as_user("alice")
    post_text(c, "Hello CIRQ", bg="sunset")
    feed = c.get("/feed").get_data(as_text=True)
    assert "data-story=\"alice\"" in feed or "alice" in feed
    item = api(c)["stories"][0]
    assert item["type"] == "text" and item["text"] == "Hello CIRQ" and item["bg"] == "sunset" and item["src"] is None


def test_bad_or_missing_background_falls_back_to_the_default(as_user, db):
    c, me = as_user("alice")
    post_text(c, "one", bg="<script>"); post_text(c, "two")
    assert [s["bg_style"] for s in stories(db, author_id=me["id"])] == ["violet", "violet"]


def test_blank_and_overlong_text_are_rejected_and_nothing_is_saved(as_user, db):
    c, me = as_user("alice")
    for bad in ("", "   \n  ", "x" * 281):
        r = post_text(c, bad)
        assert r.status_code == 302
    assert stories(db, author_id=me["id"]) == []
    page = c.get("/feed").get_data(as_text=True)
    assert "up to 280 characters" in page or "Write something" in page
    assert len(stories(db, author_id=me["id"])) == 0
    post_text(c, "y" * 280)                                              # exactly the limit is fine
    assert len(stories(db, author_id=me["id"])) == 1


def test_line_breaks_are_kept_but_runaway_blank_lines_are_collapsed(as_user, db):
    c, me = as_user("alice")
    post_text(c, "line one\r\n\r\n\r\n\r\nline two\nline three")
    assert stories(db, author_id=me["id"])[0]["text_body"] == "line one\n\nline two\nline three"


def test_text_goes_through_the_same_moderation_as_captions(as_user, db):
    c, me = as_user("alice")
    post_text(c, "send nudes")
    assert stories(db, author_id=me["id"]) == []
    post_text(c, "call me on 0772 123 456")                              # phone numbers are blocked everywhere on CIRQ
    assert stories(db, author_id=me["id"]) == []


def test_text_is_stored_raw_and_never_trusted_as_html(as_user, db):
    c, me = as_user("alice")
    post_text(c, "<img src=x onerror=alert(1)>")
    assert api(c)["stories"][0]["text"] == "<img src=x onerror=alert(1)>"       # the viewer sets it with textContent, so it can't run
    js = open("app/static/js/app.js").read()
    assert "p.textContent = txt" in js and "innerHTML = txt" not in js


def test_text_story_can_carry_music(as_user, db, uploads):
    c, me = as_user("alice")
    c.post("/stories", data={"story_type": "text", "text": "vibes", "audio": (io.BytesIO(MP3), "s.mp3"), "audio_title": "Tune"}, content_type="multipart/form-data")
    [s] = stories(db, author_id=me["id"])
    assert s["media_type"] == "text" and s["audio_title"] == "Tune" and s["audio_url"]
    assert [u for u in uploads["uploaded"]] and all(u["resource_type"] == "video" for u in uploads["uploaded"])     # only the audio clip was uploaded


def test_photo_stories_still_work_and_ignore_text_fields(as_user, db, uploads, png):
    c, me = as_user("alice")
    c.post("/stories", data={"media": (io.BytesIO(png), "s.png"), "caption": "hi", "text": "ignored", "bg": "rose"}, content_type="multipart/form-data")
    [s] = stories(db, author_id=me["id"])
    assert s["media_type"] == "image" and s["caption"] == "hi" and s["public_id"] and not s.get("text_body")
    assert len(uploads["uploaded"]) == 1
    assert api(c)["stories"][0]["type"] == "image" and api(c)["stories"][0]["text"] is None


def test_text_story_needs_login_and_respects_the_hourly_limit_setting(client):
    assert client.post("/stories", data={"story_type": "text", "text": "hi"}).status_code in (302, 401)


# ------------------------------------------------------------------ viewing, privacy, deleting, cleanup
def test_text_stories_follow_the_same_privacy_rules(as_user, db):
    a, pa = as_user("alice"); b, pb = as_user("bob")
    db.table("profiles").update({"is_private": True}).eq("id", pa["id"]).execute()
    post_text(a, "private thoughts")
    assert b.get("/stories/api/alice").status_code == 404                           # not a follower
    db.table("blocks").insert({"blocker_id": pa["id"], "blocked_id": pb["id"]}).execute()
    assert b.get("/stories/api/alice").status_code == 404
    assert api(a)["stories"][0]["text"] == "private thoughts"


def test_viewers_can_like_view_and_reply_to_a_text_story(as_user, db):
    a, pa = as_user("alice"); b, pb = as_user("bob")
    post_text(a, "tell me your favourite song")
    sid = api(a)["stories"][0]["id"]
    assert b.post(f"/stories/{sid}/view").get_json() == {"ok": True}
    assert b.post(f"/stories/{sid}/like", headers=FETCH).get_json() == {"liked": True}
    r = b.post(f"/stories/{sid}/reply", data={"body": "Blinding Lights"}, headers=FETCH)
    assert r.status_code == 200
    mine = api(a)["stories"][0]
    assert mine["views"] == 1 and mine["likes"] == 1


def test_reply_to_a_text_story_shows_a_coloured_tile_in_chat(as_user, db):
    a, pa = as_user("alice"); b, pb = as_user("bob")
    post_text(a, "hello", bg="gold")
    sid = api(a)["stories"][0]["id"]
    cid = b.post(f"/stories/{sid}/reply", data={"body": "hey!"}, headers=FETCH).get_json()["conversation"]
    thread = a.get(cid).get_data(as_text=True)
    assert "Replied to your story" in thread and 'story-mini bg-gold' in thread and "<img src=\"None\"" not in thread
    db.table("stories").delete().eq("id", sid).execute()                             # after it expires the label remains, no tile
    assert "no longer available" in a.get(cid).get_data(as_text=True)


def test_owner_can_delete_a_text_story_without_touching_cloudinary(as_user, db, uploads):
    c, me = as_user("alice")
    post_text(c, "oops")
    sid = api(c)["stories"][0]["id"]
    assert c.post(f"/stories/{sid}/delete", headers=FETCH).get_json()["ok"] is True
    assert stories(db, author_id=me["id"]) == [] and uploads["destroyed"] == []


def test_expired_text_stories_are_cleaned_up_without_errors(app, as_user, db, uploads):
    c, me = as_user("alice")
    post_text(c, "gone soon")
    db.data["stories"][0]["expires_at"] = iso(hours_from_now(-1))
    from app import run_cleanup
    with app.app_context():
        assert "removed 1 expired stories" in run_cleanup()
    assert stories(db, author_id=me["id"]) == [] and uploads["destroyed"] == []


# ------------------------------------------------------------------ the composer
def test_composer_offers_a_text_mode_with_backgrounds(as_user):
    c, _ = as_user("alice")
    html = c.get("/feed").get_data(as_text=True)
    assert 'data-story-mode="text"' in html and 'data-story-mode="media"' in html
    assert len(re.findall(r'name="bg" value="', html)) == 8 and 'maxlength="280"' in html and 'name="story_type"' in html
    box = html[html.index("data-compose-text"):][:200]
    assert "disabled" in box                                                          # inactive panels never submit stray fields


def test_schema_and_migration_allow_text_stories():
    for path in ("schema.sql", "migrations/2026_11_09_text_stories.sql"):
        sql = open(path).read()
        assert "check (media_type in ('image','video','text'))" in sql and "alter column url drop not null" in sql
        assert "stories_content_check" in sql and "text_body" in sql and "bg_style" in sql
