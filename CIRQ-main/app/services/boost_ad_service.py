"""Sponsored profile ads shown only while their owners' account boosts are active."""
import hashlib
import time

from app.services import supabase_service as sb, social, coin_service, cloudinary_service as cs


def for_viewer(viewer_id, cursor=""):
    ads = sb.select("boost_ads", order="updated_at", desc=True, limit=500)
    if not ads:
        return None

    profiles = sb.by_id("profiles", [ad["user_id"] for ad in ads],
                        cols="id,username,display_name,account_status,boosted_until")
    blocked = social.blocked_between(viewer_id)
    eligible = []
    for ad in ads:
        profile = profiles.get(ad["user_id"])
        if (not profile or profile["id"] == viewer_id or profile["id"] in blocked
                or profile["account_status"] != "approved" or not coin_service.is_boosted(profile)):
            continue
        image = {"url": ad["image_url"], "public_id": ad.get("image_public_id"), "media_type": "image"}
        eligible.append({
            "id": ad["id"],
            "username": profile["username"],
            "name": profile["display_name"],
            "headline": ad["headline"],
            "body": ad["body"],
            "image": cs.media_src(image, 900),
        })
    if not eligible:
        return None

    bucket = int(time.time() // 900)
    seed = f"{viewer_id}:{cursor}:{bucket}".encode()
    index = int.from_bytes(hashlib.sha256(seed).digest()[:8], "big") % len(eligible)
    return eligible[index]
