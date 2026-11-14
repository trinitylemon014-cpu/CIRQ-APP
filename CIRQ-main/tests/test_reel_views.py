import uuid

import httpx


def create_reel(db, author):
    post = db.table("posts").insert({
        "id": str(uuid.uuid4()),
        "author_id": author["id"],
        "kind": "reel",
        "caption": "A test reel",
        "visibility": "public",
        "moderation_status": "ok",
    }).execute().data[0]
    db.table("post_media").insert({
        "post_id": post["id"],
        "owner_id": author["id"],
        "media_type": "video",
        "url": "https://example.test/reel.mp4",
        "public_id": "cirq/reels/test",
        "delivery_type": "upload",
    }).execute()
    return post


def record_view(client, reel_id, *, watched_seconds=3):
    return client.post(
        f"/api/reels/{reel_id}/view",
        json={"watched_seconds": watched_seconds},
        headers={"X-Requested-With": "fetch"},
    )


def test_unique_view_increments_once_and_returns_cached_count(as_user, db):
    author, author_profile = as_user("alice")
    viewer, _ = as_user("bob")
    reel = create_reel(db, author_profile)

    first = record_view(viewer, reel["id"])
    assert first.status_code == 200
    assert first.get_json() == {"counted": True, "views_count": 1}
    repeated = record_view(viewer, reel["id"])
    assert repeated.status_code == 200
    assert repeated.get_json() == {"counted": False, "views_count": 1}
    assert len(db.data["reel_views"]) == 1
    assert db.data["posts"][0]["views_count"] == 1


def test_author_and_short_watches_do_not_count(as_user, db):
    author, profile = as_user("alice")
    viewer, _ = as_user("bob")
    reel = create_reel(db, profile)

    own = record_view(author, reel["id"])
    assert own.status_code == 200 and own.get_json() == {"counted": False, "views_count": 0}
    too_short = record_view(viewer, reel["id"], watched_seconds=2.99)
    assert too_short.status_code == 400
    assert not db.data.get("reel_views")


def test_blocked_viewers_and_non_reels_are_not_counted(as_user, db):
    author, profile = as_user("alice")
    viewer, viewer_profile = as_user("bob")
    reel = create_reel(db, profile)

    db.table("blocks").insert({"blocker_id": profile["id"], "blocked_id": viewer_profile["id"]}).execute()
    assert record_view(viewer, reel["id"]).status_code == 404
    db.table("blocks").delete().eq("blocker_id", profile["id"]).eq("blocked_id", viewer_profile["id"]).execute()
    db.table("blocks").insert({"blocker_id": viewer_profile["id"], "blocked_id": profile["id"]}).execute()
    assert record_view(viewer, reel["id"]).status_code == 404
    assert not db.data.get("reel_views")

    post = db.table("posts").insert({"author_id": profile["id"], "kind": "post"}).execute().data[0]
    assert record_view(viewer, post["id"]).status_code == 404


def test_private_reel_requires_visibility_permission(as_user, db):
    author, profile = as_user("alice")
    viewer, _ = as_user("bob")
    db.table("profiles").update({"is_private": True}).eq("id", profile["id"]).execute()
    reel = create_reel(db, profile)

    assert record_view(viewer, reel["id"]).status_code == 404
    assert not db.data.get("reel_views")


def test_reel_counts_render_in_feed_and_profile_without_extra_count_calls(as_user, db):
    author, profile = as_user("alice")
    viewer, _ = as_user("bob")
    reel = create_reel(db, profile)
    db.table("posts").update({"views_count": 1234}).eq("id", reel["id"]).execute()

    feed = viewer.get("/reels")
    assert b'data-reel-view-count>1.2K' in feed.data
    assert b'aria-label="1,234 views"' in feed.data
    profile_reels = viewer.get("/u/alice?tab=reels")
    assert b'aria-label="1,234 views"' in profile_reels.data
    detail = viewer.get(f"/post/{reel['id']}")
    assert detail.status_code == 200 and b'aria-label="1,234 views"' in detail.data


def test_failed_rpc_returns_service_unavailable_without_affecting_reel(as_user, db):
    author, profile = as_user("alice")
    viewer, _ = as_user("bob")
    reel = create_reel(db, profile)

    def failed_rpc(name, params):
        raise httpx.ReadError("simulated network failure")

    db.rpc = failed_rpc
    result = record_view(viewer, reel["id"])
    assert result.status_code == 503
    assert result.get_json()["counted"] is False
    assert not db.data.get("reel_views")


def test_owner_sees_who_viewed_and_others_cannot(as_user, db):
    author, profile = as_user("alice")
    bob, _ = as_user("bob")
    carol, _ = as_user("carol")
    reel = create_reel(db, profile)
    assert record_view(bob, reel["id"]).status_code == 200
    assert record_view(carol, reel["id"]).status_code == 200

    page = author.get(f"/reels/{reel['id']}/viewers", headers={"X-Requested-With": "fetch"})
    html = page.get_data(as_text=True)
    assert page.status_code == 200 and "@bob" in html and "@carol" in html and "@alice" not in html

    assert bob.get(f"/reels/{reel['id']}/viewers").status_code == 403      # viewers cannot see the list
    assert author.get("/reels/not-a-reel/viewers").status_code == 404


def test_viewers_list_hides_blocked_people_and_shows_empty_state(as_user, db):
    author, profile = as_user("alice")
    bob, bob_profile = as_user("bob")
    reel = create_reel(db, profile)
    empty = author.get(f"/reels/{reel['id']}/viewers").get_data(as_text=True)
    assert "No viewers yet" in empty

    assert record_view(bob, reel["id"]).status_code == 200
    db.table("blocks").insert({"blocker_id": profile["id"], "blocked_id": bob_profile["id"]}).execute()
    assert "@bob" not in author.get(f"/reels/{reel['id']}/viewers").get_data(as_text=True)


def test_only_the_owner_gets_the_viewers_button_in_the_feed(as_user, db):
    author, profile = as_user("alice")
    bob, _ = as_user("bob")
    reel = create_reel(db, profile)
    assert f"/reels/{reel['id']}/viewers".encode() in author.get("/reels").data
    assert f"/reels/{reel['id']}/viewers".encode() not in bob.get("/reels").data
