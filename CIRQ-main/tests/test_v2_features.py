"""School logos, music on posts/stories, comments on stories, and likes on comments."""
import io
from tests.conftest import MP4, login
from tests.test_stories_privacy import story, FETCH, make_private
from app.utils import iso, hours_from_now

MP3 = b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\x00" * 64          # ID3-tagged MP3 header
WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 32
NOT_AUDIO = b"MZ\x90\x00" + b"\x00" * 64                          # an .exe renamed to .mp3


def adm(app, mk):
    mk("boss", role="admin"); c = app.test_client(); login(c, "boss"); return c


# ------------------------------------------------------------------ school logos
def test_admin_uploads_replaces_and_removes_a_school_logo(app, mk, db, school, png, uploads):
    boss = adm(app, mk)
    r = boss.post(f"/admin/schools/{school['id']}/logo", data={"logo": (io.BytesIO(png), "crest.png")}, content_type="multipart/form-data")
    assert r.status_code == 302
    row = db.data["schools"][0]
    assert row["logo_url"] and row["logo_public_id"].startswith("cirq/schools/")
    first = row["logo_public_id"]
    boss.post(f"/admin/schools/{school['id']}/logo", data={"logo": (io.BytesIO(png), "crest2.png")}, content_type="multipart/form-data")
    assert db.data["schools"][0]["logo_public_id"] != first and first in uploads["destroyed"]    # old file removed from Cloudinary
    boss.post(f"/admin/schools/{school['id']}/logo", data={"remove": "1"})
    assert db.data["schools"][0]["logo_url"] is None and db.data["schools"][0]["logo_public_id"] is None


def test_admin_edits_game_only_developer_card_name_and_image(app, as_user, mk, db, png, uploads):
    boss = adm(app, mk)
    assert boss.get("/admin/developer-card").status_code == 200
    boss.post("/admin/developer-card", data={"display_name": "CIRQ Dev", "image": (io.BytesIO(png), "developer.png")}, content_type="multipart/form-data")
    saved = db.data["developer_card_settings"][0]
    first = saved["image_public_id"]
    assert saved["display_name"] == "CIRQ Dev" and saved["image_url"] and first.startswith("cirq/game-cards/")
    assert not any(p["username"] == "vex" for p in db.data["profiles"])

    for i in range(6):
        mk(f"devcardstudent{i}")
    player, _ = as_user("devcardplayer")
    player.post("/api/games/flip/start")
    answer = db.data["game_rounds"][-1]["answer_index"]
    reveal = player.post("/api/games/flip/guess", json={"card": answer}).get_json()
    assert reveal["developer"]["name"] == "CIRQ Dev" and first in reveal["developer"]["avatar"]
    assert reveal["developer"]["username"] is None

    boss.post("/admin/developer-card", data={"display_name": "CIRQ Dev 2", "image": (io.BytesIO(png), "developer-new.png")}, content_type="multipart/form-data")
    saved = db.data["developer_card_settings"][0]
    assert saved["image_public_id"] != first and first in uploads["destroyed"]
    boss.post("/admin/developer-card", data={"display_name": "CIRQ Dev 2", "remove": "1"})
    assert saved["display_name"] == "CIRQ Dev 2" and saved["image_url"] is None and saved["image_public_id"] is None


def test_only_admins_can_edit_game_developer_card_and_bad_images_are_rejected(app, as_user, mk, db, png):
    student, _ = as_user("devphotostudent")
    assert student.post("/admin/developer-card", data={"display_name": "Hacker", "image": (io.BytesIO(png), "x.png")},
                        content_type="multipart/form-data").status_code == 403
    boss = adm(app, mk)
    boss.post("/admin/developer-card", data={"display_name": "Should not save", "image": (io.BytesIO(b"not an image"), "x.png")}, content_type="multipart/form-data")
    assert db.data["developer_card_settings"][0]["display_name"] is None


def test_logo_shows_on_school_page_instead_of_cap_icon(app, as_user, mk, db, school):
    c, _ = as_user("alice")
    before = c.get(f"/schools/{school['slug']}").get_data(as_text=True)
    assert "school-logo" in before and "<img" not in before.split('class="school-logo')[1].split("</span>")[0]    # cap icon fallback
    db.table("schools").update({"logo_url": "https://res.cloudinary.com/demo/image/upload/crest.png", "logo_public_id": "cirq/schools/crest"}).eq("id", school["id"]).execute()
    after = c.get(f"/schools/{school['slug']}").get_data(as_text=True)
    assert "cirq/schools/crest" in after and "<img" in after.split('class="school-logo')[1].split("</span>")[0]
    assert "cirq/schools/crest" in c.get("/schools").get_data(as_text=True)                                       # school list too


def test_only_admins_can_set_logos_and_non_images_are_rejected(app, as_user, mk, db, school, png):
    c, _ = as_user("alice")
    assert c.post(f"/admin/schools/{school['id']}/logo", data={"logo": (io.BytesIO(png), "x.png")}, content_type="multipart/form-data").status_code == 403
    boss = adm(app, mk)
    boss.post(f"/admin/schools/{school['id']}/logo", data={"logo": (io.BytesIO(MP4), "x.mp4")}, content_type="multipart/form-data")
    boss.post(f"/admin/schools/{school['id']}/logo", data={"logo": (io.BytesIO(b"not an image"), "x.png")}, content_type="multipart/form-data")
    assert db.data["schools"][0]["logo_url"] is None


def test_admin_can_add_school_with_logo(app, mk, db, png):
    boss = adm(app, mk)
    boss.post("/admin/schools", data={"name": "Gayaza High", "city": "Wakiso", "country": "Uganda", "logo": (io.BytesIO(png), "g.png")}, content_type="multipart/form-data")
    s = [x for x in db.data["schools"] if x["name"] == "Gayaza High"][0]
    assert s["logo_url"] and s["logo_public_id"]


# ------------------------------------------------------------------ music: validation
def test_audio_validation_checks_signature_size_and_extension(app):
    from app.services import cloudinary_service as cs
    from werkzeug.datastructures import FileStorage
    fs = lambda data, name: FileStorage(stream=io.BytesIO(data), filename=name)
    with app.test_request_context():
        assert cs.validate_audio(fs(MP3, "a.mp3"))[1] == "mp3"
        assert cs.validate_audio(fs(WAV, "a.wav"))[1] == "wav"
        for data, name in ((NOT_AUDIO, "a.mp3"), (MP3, "a.exe"), (b"", "a.mp3"), (MP3, "noext")):
            try:
                cs.validate_audio(fs(data, name)); raise AssertionError(f"{name} should be rejected")
            except cs.UploadError:
                pass
        big = MP3 + b"\x00" * (app.config["MAX_AUDIO_MB"] * 1024 * 1024)
        try:
            cs.validate_audio(fs(big, "big.mp3")); raise AssertionError("too big")
        except cs.UploadError:
            pass


# ------------------------------------------------------------------ music on posts
def test_post_with_music_stores_the_clip_and_shows_a_player(as_user, db, uploads):
    c, me = as_user("alice")
    r = c.post("/posts", data={"caption": "vibes", "audio": (io.BytesIO(MP3), "song.mp3"), "audio_title": "My song"}, content_type="multipart/form-data")
    assert r.status_code == 302
    p = db.data["posts"][0]
    assert p["audio_url"] and p["audio_public_id"].startswith("cirq/posts/") and p["audio_title"] == "My song"
    assert uploads["uploaded"][0]["resource_type"] == "video"                    # Cloudinary stores audio under 'video'
    page = c.get("/feed").get_data(as_text=True)
    assert "data-music-toggle" in page and "My song" in page


def test_post_with_bad_music_creates_nothing(as_user, db, uploads, png):
    c, _ = as_user("alice")
    c.post("/posts", data={"caption": "x", "audio": (io.BytesIO(NOT_AUDIO), "song.mp3")}, content_type="multipart/form-data")
    assert not db.data.get("posts") and not uploads["uploaded"]


def test_post_music_with_photo_and_deleting_the_post_removes_both_files(as_user, db, uploads, png):
    c, me = as_user("alice")
    c.post("/posts", data={"caption": "x", "media": (io.BytesIO(png), "a.png"), "audio": (io.BytesIO(MP3), "s.mp3")}, content_type="multipart/form-data")
    p = db.data["posts"][0]
    assert len(uploads["uploaded"]) == 2
    c.post(f"/posts/{p['id']}/delete")
    assert p["audio_public_id"] in uploads["destroyed"] and len(uploads["destroyed"]) == 2


def test_overlong_music_is_rejected_and_cleaned_up(app, as_user, db, uploads, monkeypatch):
    import cloudinary.uploader
    real = cloudinary.uploader.upload
    monkeypatch.setattr(cloudinary.uploader, "upload", lambda f, **o: {**real(f, **o), "duration": app.config["MAX_AUDIO_SECONDS"] + 30})
    c, _ = as_user("alice")
    c.post("/posts", data={"caption": "x", "audio": (io.BytesIO(MP3), "s.mp3")}, content_type="multipart/form-data")
    assert not db.data.get("posts") and uploads["destroyed"]


# ------------------------------------------------------------------ music on stories
def test_story_with_music_exposes_it_in_the_api(as_user, db, uploads, png):
    a, pa = as_user("alice"); b, _ = as_user("bob")
    a.post("/stories", data={"media": (io.BytesIO(png), "a.png"), "audio": (io.BytesIO(MP3), "s.mp3"), "audio_title": "Tune"}, content_type="multipart/form-data")
    s = db.data["stories"][0]
    assert s["audio_url"] and s["audio_title"] == "Tune"
    item = b.get("/stories/api/alice").get_json()["stories"][0]
    assert item["audio"] and item["audio_title"] == "Tune" and item["comments"] == 0


def test_story_with_bad_music_uploads_nothing_and_failed_music_removes_the_photo(as_user, db, uploads, png, monkeypatch):
    a, _ = as_user("alice")
    a.post("/stories", data={"media": (io.BytesIO(png), "a.png"), "audio": (io.BytesIO(NOT_AUDIO), "s.mp3")}, content_type="multipart/form-data")
    assert not db.data.get("stories") and not uploads["uploaded"]
    from app.services import cloudinary_service as cs
    def boom(*a, **k): raise cs.UploadError("Music upload failed.")
    monkeypatch.setattr(cs, "upload_audio", boom)
    a.post("/stories", data={"media": (io.BytesIO(png), "a.png"), "audio": (io.BytesIO(MP3), "s.mp3")}, content_type="multipart/form-data")
    assert not db.data.get("stories") and len(uploads["destroyed"]) == 1          # the photo was removed again


def test_deleting_a_story_removes_its_music(as_user, db, uploads, png):
    a, _ = as_user("alice")
    a.post("/stories", data={"media": (io.BytesIO(png), "a.png"), "audio": (io.BytesIO(MP3), "s.mp3")}, content_type="multipart/form-data")
    s = db.data["stories"][0]
    a.post(f"/stories/{s['id']}/delete", headers=FETCH)
    assert s["audio_public_id"] in uploads["destroyed"]


# ------------------------------------------------------------------ story comments
def test_story_comments_add_list_count_and_notify_owner(as_user, db):
    a, pa = as_user("alice"); b, pb = as_user("bob")
    s = story(db, pa)
    r = b.post(f"/stories/{s['id']}/comments", data={"body": "love this"}, headers=FETCH)
    assert r.status_code == 200 and r.get_json()["comment"]["body"] == "love this" and r.get_json()["comment"]["mine"] is True
    listing = a.get(f"/stories/{s['id']}/comments", headers=FETCH).get_json()
    assert listing["count"] == 1 and listing["comments"][0]["user"]["username"] == "bob" and listing["comments"][0]["can_delete"] is True   # owner may delete
    assert b.get(f"/stories/{s['id']}/comments", headers=FETCH).get_json()["comments"][0]["can_delete"] is True                              # author may delete
    note = [n for n in db.data["notifications"] if n["kind"] == "story_comment"]
    assert len(note) == 1 and note[0]["user_id"] == pa["id"] and note[0]["body"] == "commented on your story"
    assert b.get("/stories/api/alice").get_json()["stories"][0]["comments"] == 1
    assert b.post(f"/stories/{s['id']}/comments", data={"body": "   "}, headers=FETCH).status_code == 400


def test_story_comments_respect_expiry_blocks_and_private_accounts(as_user, db):
    a, pa = as_user("alice"); b, pb = as_user("bob"); c, pc = as_user("carol")
    expired = story(db, pa, hours=-1)
    assert b.post(f"/stories/{expired['id']}/comments", data={"body": "hi"}, headers=FETCH).status_code == 404
    live = story(db, pa)
    c.post("/u/alice/follow"); c.post("/u/alice/block")
    assert c.post(f"/stories/{live['id']}/comments", data={"body": "hi"}, headers=FETCH).status_code == 404
    make_private(a)
    assert b.post(f"/stories/{live['id']}/comments", data={"body": "hi"}, headers=FETCH).status_code == 404
    assert b.get(f"/stories/{live['id']}/comments", headers=FETCH).status_code == 404
    assert not db.data.get("story_comments")


def test_blocked_people_are_hidden_from_each_others_story_comment_view(as_user, db):
    a, pa = as_user("alice"); b, pb = as_user("bob"); c, pc = as_user("carol")
    s = story(db, pa)
    b.post(f"/stories/{s['id']}/comments", data={"body": "from bob"}, headers=FETCH)
    c.post("/u/bob/block")
    assert c.get(f"/stories/{s['id']}/comments", headers=FETCH).get_json()["count"] == 0


def test_only_author_owner_or_admin_can_delete_a_story_comment(app, as_user, mk, db):
    a, pa = as_user("alice"); b, pb = as_user("bob"); c, pc = as_user("carol")
    s = story(db, pa)
    cid = b.post(f"/stories/{s['id']}/comments", data={"body": "hi"}, headers=FETCH).get_json()["comment"]["id"]
    assert c.post(f"/stories/comments/{cid}/delete", headers=FETCH).status_code == 403
    assert a.post(f"/stories/comments/{cid}/delete", headers=FETCH).status_code == 200                # the story's owner
    cid2 = b.post(f"/stories/{s['id']}/comments", data={"body": "again"}, headers=FETCH).get_json()["comment"]["id"]
    assert b.post(f"/stories/comments/{cid2}/delete", headers=FETCH).status_code == 200               # the author
    assert not db.data["story_comments"]


def test_story_comment_likes_toggle_count_and_notify_once(as_user, db):
    a, pa = as_user("alice"); b, pb = as_user("bob"); c, pc = as_user("carol")
    s = story(db, pa)
    cid = b.post(f"/stories/{s['id']}/comments", data={"body": "hi"}, headers=FETCH).get_json()["comment"]["id"]
    assert a.post(f"/stories/comments/{cid}/like", headers=FETCH).get_json() == {"liked": True, "count": 1}
    assert c.post(f"/stories/comments/{cid}/like", headers=FETCH).get_json() == {"liked": True, "count": 2}
    row = [x for x in a.get(f"/stories/{s['id']}/comments", headers=FETCH).get_json()["comments"]][0]
    assert row["likes"] == 2 and row["liked"] is True
    assert a.post(f"/stories/comments/{cid}/like", headers=FETCH).get_json() == {"liked": False, "count": 1}
    assert len([n for n in db.data["notifications"] if n["kind"] == "comment_like" and n["user_id"] == pb["id"]]) == 2


def test_story_comment_reports_reach_moderators(app, as_user, mk, db):
    a, pa = as_user("alice"); b, pb = as_user("bob")
    s = story(db, pa)
    cid = b.post(f"/stories/{s['id']}/comments", data={"body": "rude"}, headers=FETCH).get_json()["comment"]["id"]
    assert a.post("/report", data={"target_type": "story_comment", "target_id": cid, "reason": "harassment"}, headers=FETCH).status_code == 200
    boss = adm(app, mk)
    assert b"rude" in boss.get("/admin/reports").data
    boss.post(f"/admin/reports/{db.data['reports'][0]['id']}/remove")
    assert db.data["story_comments"][0]["moderation_status"] == "removed"
    assert a.get(f"/stories/{s['id']}/comments", headers=FETCH).get_json()["count"] == 0


# ------------------------------------------------------------------ likes on post comments
def _post_with_comment(a, b, db):
    a.post("/posts", data={"caption": "hello"})
    pid = db.data["posts"][0]["id"]
    b.post(f"/posts/{pid}/comments", data={"body": "nice"}, headers=FETCH)
    return pid, db.data["comments"][0]["id"]


def test_like_a_comment_toggles_counts_and_notifies_its_author(as_user, db):
    a, pa = as_user("alice"); b, pb = as_user("bob")
    pid, cid = _post_with_comment(a, b, db)
    assert a.post(f"/comments/{cid}/like", headers=FETCH).get_json() == {"liked": True, "count": 1}
    n = [x for x in db.data["notifications"] if x["kind"] == "comment_like"]
    assert len(n) == 1 and n[0]["user_id"] == pb["id"] and n[0]["body"] == "liked your comment" and n[0]["ref_id"] == pid
    page = a.get(f"/post/{pid}").get_data(as_text=True)
    assert f'data-clike="{cid}"' in page and "liked" in page
    assert a.post(f"/comments/{cid}/like", headers=FETCH).get_json() == {"liked": False, "count": 0}


def test_comment_like_respects_post_visibility_and_blocks(as_user, db):
    a, pa = as_user("alice"); b, pb = as_user("bob"); c, pc = as_user("carol")
    pid, cid = _post_with_comment(a, b, db)
    c.post("/u/bob/block")
    assert c.post(f"/comments/{cid}/like", headers=FETCH).status_code == 404            # blocked from the comment's author
    make_private(a)
    d, _ = as_user("dave")
    assert d.post(f"/comments/{cid}/like", headers=FETCH).status_code == 404            # can't see a private account's post
    assert not db.data.get("comment_likes")


# ------------------------------------------------------------------ camera needs the browser permission
def test_permissions_policy_allows_camera_and_microphone_for_the_app(client):
    pp = client.get("/health").headers["Permissions-Policy"]
    assert "camera=(self)" in pp and "microphone=(self)" in pp


def test_camera_buttons_are_on_the_composer_story_sheet_and_reel_page(as_user):
    c, _ = as_user("alice")
    home = c.get("/feed").get_data(as_text=True)
    assert 'data-camera="#composer input[name=media]"' in home and 'data-camera="#story-form input[name=media]"' in home
    assert "js/camera.js" in home and 'name="audio"' in home
    assert 'data-camera="#rc-file"' in c.get("/reels/new").get_data(as_text=True)


# ------------------------------------------------------------------ pages render
def test_admin_schools_page_shows_logo_upload_for_each_school(app, mk, db, school, png):
    boss = adm(app, mk)
    page = boss.get("/admin/schools").get_data(as_text=True)
    assert f"/admin/schools/{school['id']}/logo" in page and 'name="logo"' in page and "Remove logo" not in page
    boss.post(f"/admin/schools/{school['id']}/logo", data={"logo": (io.BytesIO(png), "crest.png")}, content_type="multipart/form-data")
    assert "Remove logo" in boss.get("/admin/schools").get_data(as_text=True)


def test_trivia_pages_say_free_play_earns_coins(app, as_user, mk, db):
    from tests.test_games_coins_trivia_admin import make_set
    boss = adm(app, mk); sid = make_set(boss, db, 2)
    c, _ = as_user("alice")
    assert "earn app coins" in c.get("/trivia").get_data(as_text=True)
    expected_reward = f"Earn {500 // app.config['TRIVIA_POINTS_PER_COIN']} app coins per 500 points"
    assert expected_reward in c.get(f"/trivia/{sid}").get_data(as_text=True)
