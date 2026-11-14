from flask import Blueprint, render_template, request, redirect, url_for, flash, g, abort, jsonify, session, current_app
from app.decorators import login_required
from app.extensions import limiter, db
from app.services import lending as lending_service,  supabase_service as sb, cloudinary_service as cs, social, auth_service
from app.services.cloudinary_service import UploadError
from app.services.notification_service import notify
from app.utils import normalize_username, clean_text, iso, parse_dt, utcnow, password_problems, TIERS, tier_for

bp = Blueprint("profiles", __name__)


def _profile_or_404(username):
    p = sb.get("profiles", username=normalize_username(username))
    if not p or (p["account_status"] != "approved" and g.user["id"] != p["id"] and g.user.get("role") != "admin"):
        abort(404)
    return p


@bp.get("/saved")
@login_required
def saved_shortcut():
    """Short link to 'Saved posts and reels' on your own profile."""
    return redirect(url_for("profiles.view", username=g.user["username"], tab="saved"))


@bp.get("/u/<username>")
@login_required
def view(username):
    prof = _profile_or_404(username)
    me = g.user
    sb.parallel(lambda: social.blocked_between(me["id"]), lambda: social.following_ids(me["id"]))   # looked up once for the whole page
    if social.is_blocked(me["id"], prof["id"]) and prof["id"] != me["id"]:
        abort(404)
    mine = prof["id"] == me["id"]
    can_see = social.can_see_profile_content(me, prof)
    tab = request.args.get("tab", "posts")
    if tab not in ("posts", "reels", "saved") or (tab == "saved" and not mine):
        tab = "posts"                      # bookmarks are private: only the owner ever sees the Saved tab
    saved_type = request.args.get("type", "all") if tab == "saved" else "all"
    if saved_type not in ("all", "posts", "reels"):
        saved_type = "all"
    before = request.args.get("before")
    if before and not parse_dt(before):
        abort(400, description="Invalid cursor.")

    def load_items():
        if can_see and tab == "saved":
            return social.saved_page(me, {"posts": "post", "reels": "reel"}.get(saved_type), before)
        if not can_see:
            return [], None
        n = current_app.config["PAGE_SIZE"]
        q = db().table("posts").select("*").eq("author_id", prof["id"]).eq("moderation_status", "ok") \
            .eq("kind", "reel" if tab == "reels" else "post").is_("community_id", "null").order("created_at", desc=True).limit(n + 1)
        if before:
            q = q.lt("created_at", before)
        rows = q.execute().data or []
        if not mine and not social.is_following(me["id"], prof["id"]):
            rows = [r for r in rows if r["visibility"] == "public"]
        return social.hydrate_posts(rows[:n], me["id"]), (rows[n - 1]["created_at"] if len(rows) > n else None)

    if request.args.get("partial"):
        items, cursor = load_items()
        if tab == "saved":
            return render_template("partials/saved_tiles.html", posts=items, cursor=cursor)
        return render_template("partials/post_list.html", posts=items, cursor=cursor)
    # everything the profile shows is independent of the rest, so it loads at the same time
    (items, cursor), stats, school, follow_state, can_message, can_send_coins, pending_requests, achievements = sb.parallel(
        load_items,
        lambda: social.profile_stats(prof),
        lambda: sb.get("schools", id=prof["school_id"]) if prof.get("school_id") else None,
        lambda: "none" if mine else social.follow_state(me["id"], prof["id"]),
        lambda: (not mine) and social.can_message(me, prof),
        lambda: (not mine) and lending_service.can_send_to(me, prof),
        lambda: social.pending_request_count(me["id"]) if mine and prof.get("is_private") else 0,
        lambda: [a for a in social.user_achievements(prof["id"]) if a["earned"]])
    return render_template("profiles/view.html", prof=prof, stats=stats, school=school, can_see=can_see, items=items, posts=items, cursor=cursor,
                           tab=tab, saved_type=saved_type, follow_state=follow_state, can_message=can_message,
                           can_send_coins=can_send_coins, pending_requests=pending_requests, achievements=achievements)


@bp.get("/u/<username>/<kind>")
@login_required
def connections(username, kind):
    if kind not in ("followers", "following"):
        abort(404)
    prof = _profile_or_404(username)
    if not social.can_see_profile_content(g.user, prof):
        abort(404)

    column = "follower_id" if kind == "followers" else "followee_id"
    filter_column = "followee_id" if kind == "followers" else "follower_id"
    rows = sb.select("follows", f"{column},created_at", order="created_at", desc=True, **{filter_column: prof["id"]})
    ids = [row[column] for row in rows]
    people = sb.by_id("profiles", ids)
    blocked = social.blocked_between(g.user["id"])
    items = [people[user_id] for user_id in ids if user_id in people and user_id not in blocked
             and people[user_id]["account_status"] == "approved"]
    return render_template("profiles/connections.html", prof=prof, kind=kind, items=items)


@bp.post("/u/<username>/follow")
@login_required
@limiter.limit("120 per hour")
def follow(username):
    """One button, four outcomes: follow a public account, send a request to a private one,
    withdraw a pending request, or unfollow. `state` tells the browser what the button should now say."""
    prof = _profile_or_404(username)
    me = g.user["id"]
    if prof["id"] == me or social.is_blocked(me, prof["id"]):
        abort(400)
    current = social.follow_state(me, prof["id"])
    if current == "following":
        sb.delete("follows", follower_id=me, followee_id=prof["id"])
        state = "none"
    elif current == "requested":
        social.remove_follow_request(me, prof["id"])
        state = "none"
    elif prof.get("is_private"):
        sb.insert("follow_requests", {"requester_id": me, "target_id": prof["id"]})
        notify(prof["id"], "follow_request", me, "profile", me)
        state = "requested"
    else:
        sb.insert("follows", {"follower_id": me, "followee_id": prof["id"]})
        notify(prof["id"], "follow", me, "profile", me)
        social.grant_achievement(me, "first_follow")
        social.grant_achievement(prof["id"], "first_follower")
        state = "following"
    count = social.follower_count(prof["id"])
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify(state=state, following=state == "following", requested=state == "requested", followers=count)
    return redirect(url_for("profiles.view", username=prof["username"]))


@bp.get("/follow-requests")
@login_required
def follow_requests():
    """People waiting for this (private) account to approve them."""
    me = g.user["id"]
    hidden = social.blocked_between(me)
    rows = [r for r in sb.select("follow_requests", order="created_at", target_id=me) if r["requester_id"] not in hidden]
    people = sb.by_id("profiles", [r["requester_id"] for r in rows])
    items = [{"profile": people[r["requester_id"]], "at": r["created_at"]} for r in rows
             if r["requester_id"] in people and people[r["requester_id"]]["account_status"] == "approved"]
    return render_template("profiles/requests.html", items=items)


def _resolve_request(username, approve):
    me = g.user["id"]
    prof = sb.get("profiles", username=normalize_username(username))
    # Scoped to *my* pending requests: nobody can approve or decline a request addressed to someone else.
    if not prof or not social.has_requested(prof["id"], me):
        abort(404)
    if approve:
        social.approve_follow_request(prof["id"], me)
    else:
        social.remove_follow_request(prof["id"], me)
    remaining = social.pending_request_count(me)
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify(ok=True, approved=approve, remaining=remaining, followers=social.follower_count(me))
    flash(f"@{prof['username']} can now follow you and chat with you." if approve else "Request deleted.", "success" if approve else "info")
    return redirect(url_for("profiles.follow_requests"))


@bp.post("/follow-requests/<username>/approve")
@login_required
@limiter.limit("240 per hour")
def approve_request(username):
    return _resolve_request(username, True)


@bp.post("/follow-requests/<username>/decline")
@login_required
@limiter.limit("240 per hour")
def decline_request(username):
    return _resolve_request(username, False)


@bp.post("/u/<username>/block")
@login_required
def block(username):
    prof = _profile_or_404(username)
    me = g.user["id"]
    if prof["id"] == me:
        abort(400)
    if sb.get("blocks", blocker_id=me, blocked_id=prof["id"]):
        sb.delete("blocks", blocker_id=me, blocked_id=prof["id"])
        flash(f"Unblocked @{prof['username']}.", "info")
    else:
        social.block_user(me, prof["id"])
        flash(f"Blocked @{prof['username']}. They can't see your profile or message you.", "success")
    return redirect(url_for("feed.home"))


@bp.post("/theme")
@limiter.limit("60 per hour")
def set_theme():
    """Switch between the dark look (new) and the light look (the original). Stored in a cookie for a year, so it needs no database
    change, applies before the page paints (no flash) and also works on the landing and login pages."""
    choice = request.form.get("theme")
    target = request.form.get("next") or ""
    safe = target.startswith("/") and not target.startswith("//") and "\\" not in target      # same-site paths only: never an open redirect
    resp = redirect(target if safe else url_for("feed.home"))
    if choice in ("dark", "light"):
        resp.set_cookie("cirq_theme", choice, max_age=60 * 60 * 24 * 365, samesite="Lax", secure=request.is_secure, httponly=True, path="/")
    return resp


@bp.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    me = g.user
    if request.method == "POST":
        action = request.form.get("action", "profile")
        if action == "profile":
            display = clean_text(request.form.get("display_name"), 40)
            bio = clean_text(request.form.get("bio"), 200)
            if len(display) < 2:
                flash("Display name must be at least 2 characters.", "error")
                return redirect(url_for("profiles.settings"))
            values = {"display_name": display, "bio": bio or None}
            f = request.files.get("avatar")
            if f and f.filename:
                try:
                    info = cs.upload(f, "avatars", me["id"], allow_video=False)
                    cs.destroy(me.get("avatar_public_id"))
                    values.update(avatar_url=info["url"], avatar_public_id=info["public_id"])
                except UploadError as e:
                    flash(str(e), "error")
                    return redirect(url_for("profiles.settings"))
            sb.update("profiles", values, id=me["id"])   # only whitelisted columns can ever be written here
            flash("Profile saved.", "success")
        elif action == "privacy":
            private = bool(request.form.get("is_private"))
            _set_privacy(me, private)
            sb.update("profiles", {"show_active_status": bool(request.form.get("show_active_status"))}, id=me["id"])
            flash("Your account is now private. New followers need your approval." if private
                  else "Your account is now public. Pending requests were approved.", "success")
        elif action == "school":
            school = sb.get("schools", id=request.form.get("school_id"))
            if not school:
                flash("Choose a school from the list.", "error")
            else:
                sb.update("profiles", {
                    "school_id": school["id"], "school_verified": True,
                    "school_submission_id": None, "submitted_school_name": None,
                }, id=me["id"])
                flash("Your school was updated.", "success")
        elif action == "password":
            cur, new, new2 = request.form.get("current", ""), request.form.get("new", ""), request.form.get("new2", "")
            errs = password_problems(new) + (["Passwords don't match."] if new != new2 else [])
            if not auth_service.verify_password(me["id"], cur):
                errs.append("Your current password isn't right.")
            if errs:
                for e in errs:
                    flash(e, "error")
            else:
                auth_service.set_password(me["id"], new)
                epoch = me.get("session_epoch", 0) + 1
                sb.update("profiles", {"session_epoch": epoch}, id=me["id"])   # signs out other devices
                session["epoch"] = epoch
                flash("Password changed. Other devices were signed out.", "success")
        elif action == "recs":
            from app.services import recommender
            if request.form.get("reset"):
                recommender.reset(me["id"])
                flash("Done. Your suggestions have been reset and will start fresh.", "success")
            else:
                on = bool(request.form.get("personalized_recs"))
                sb.update("profiles", {"personalized_recs": on}, id=me["id"])
                if not on:
                    recommender.reset(me["id"])        # switching off also forgets what we learned
                flash("Personalised suggestions are on." if on else "Personalised suggestions are off. We cleared what we'd learned.", "success")
        elif action == "delete":
            if not auth_service.verify_password(me["id"], request.form.get("confirm_password", "")) or request.form.get("confirm_text") != "DELETE":
                flash("Type DELETE and your password to confirm.", "error")
            else:
                _erase_account(me)
                session.clear()
                flash("Your account and its content were deleted.", "info")
                return redirect(url_for("auth.landing"))
        return redirect(url_for("profiles.settings"))
    blocked = sb.by_id("profiles", [b["blocked_id"] for b in sb.select("blocks", blocker_id=me["id"])])
    return render_template("profiles/settings.html", blocked=list(blocked.values()),
                           schools=sb.select("schools", order="name", desc=False),
                           pending_requests=social.pending_request_count(me["id"]))


@bp.post("/api/presence/ping")
@login_required
@limiter.limit("30 per minute", key_func=lambda: g.user["id"])
def presence_ping():
    if not g.user.get("show_active_status", True):
        return jsonify(ok=True, updated=False)
    try:
        updated = db().rpc("cirq_ping_presence", {"p_user_id": g.user["id"]}).execute().data
    except Exception:
        current_app.logger.exception("presence heartbeat failed for user %s", g.user["id"])
        return jsonify(error="Presence is temporarily unavailable."), 503
    return jsonify(ok=True, updated=updated is True or updated == [True])


@bp.get("/api/presence/status")
@login_required
@limiter.limit("10 per minute", key_func=lambda: g.user["id"])
def presence_status():
    ids = list(dict.fromkeys(request.args.getlist("user_id")))[:100]
    if not ids:
        return jsonify(statuses={}, last_seen_at={})
    if not g.user.get("show_active_status", True):
        return jsonify(statuses={user_id: "hidden" for user_id in ids}, last_seen_at={})

    people = sb.by_id("profiles", ids, cols="id,last_seen_at,show_active_status")
    blocked = {row["blocked_id"] for row in sb.select("blocks", blocker_id=g.user["id"])}
    blocked.update(row["blocker_id"] for row in sb.select("blocks", blocked_id=g.user["id"]))
    cutoff = utcnow().timestamp() - current_app.config["ACTIVE_WINDOW_SECONDS"]
    statuses, last_seen_at = {}, {}
    for user_id in ids:
        person = people.get(user_id)
        if not person or not person.get("show_active_status", True) or user_id in blocked:
            statuses[user_id] = "hidden"
            continue
        seen = parse_dt(person.get("last_seen_at"))
        statuses[user_id] = "active" if seen and seen.timestamp() >= cutoff else "offline"
        last_seen_at[user_id] = person.get("last_seen_at")
    return jsonify(statuses=statuses, last_seen_at=last_seen_at)


def _set_privacy(me, private):
    """Switch between a public and a private account.
    Going private: existing followers stay; every public post becomes followers-only; from now on new followers
    must be approved. Going public: waiting requests are approved automatically (nobody is left in limbo)."""
    if bool(me.get("is_private")) == private:
        return
    sb.update("profiles", {"is_private": private}, id=me["id"])
    if private:
        sb.update("posts", {"visibility": "followers"}, author_id=me["id"], visibility="public")
    else:
        for r in sb.select("follow_requests", target_id=me["id"]):
            social.approve_follow_request(r["requester_id"], me["id"])


def _erase_account(me):
    """Delete the person's Cloudinary files, then their auth user (database rows cascade)."""
    for m in sb.select("post_media", owner_id=me["id"]):
        cs.destroy(m["public_id"], m["media_type"], m.get("delivery_type", "upload"))
    for s in sb.select("stories", author_id=me["id"]):
        cs.destroy(s["public_id"], s["media_type"], s.get("delivery_type", "upload"))
        cs.destroy(s.get("audio_public_id"), "audio")
    for p in sb.select("posts", author_id=me["id"]):
        cs.destroy(p.get("audio_public_id"), "audio")      # music clips on posts
    ad = sb.get("boost_ads", user_id=me["id"])
    if ad:
        cs.destroy(ad.get("image_public_id"))
    cs.destroy(me.get("avatar_public_id"))
    auth_service.delete_auth_user(me["id"])


@bp.get("/status")
@login_required
def status():
    me = g.user
    stats = social.profile_stats(me)
    ach = social.user_achievements(me["id"])
    return render_template("profiles/status.html", stats=stats, tiers=TIERS, achievements=ach)
