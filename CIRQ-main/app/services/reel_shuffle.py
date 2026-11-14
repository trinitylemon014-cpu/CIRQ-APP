"""Shuffled reels feed.

Instead of showing reels newest-first (the same order every day), every visit to /reels gets its own random order.

How it works
  * A visit ("session") picks a random SEED and remembers the moment it started (T0).
  * Every reel gets a stable sort key = sha256(seed + reel id). Same seed -> same order, so scrolling through pages never
    repeats or skips a reel, and a reel uploaded mid-session cannot shift the pages (the position of every other reel
    is unchanged). A new visit = a new seed = a new order.
  * Reels the viewer already watched BEFORE this visit started (a counted view in `reel_views`) and the viewer's own
    reels go to a second tier that is only reached after every unseen reel. So yesterday's reels are not served first.
    Views recorded during the visit do not change the order, which is what keeps paging stable.
  * The page cursor carries seed, T0 and the sort position of the last reel shown: "v2|seed|t0|tier|key|reel_id".

This module has no framework or database imports on purpose, so the ordering logic can be tested on its own.
"""
import base64
import binascii
import hashlib
import re
import secrets
import uuid
from datetime import datetime

VERSION = "v2"
POOL_SIZE = 500          # newest eligible reels that are shuffled together on each visit
SEEN_CHUNK = 80          # ids per "which of these did I already watch" lookup (keeps URLs short)

_SEED_RE = re.compile(r"^[0-9a-f]{8}$")
_KEY_RE = re.compile(r"^[0-9a-f]{16}$")


class BadCursor(ValueError):
    """The cursor is not a valid shuffled-feed cursor."""


def new_seed():
    return secrets.token_hex(4)


def shuffle_key(seed, reel_id):
    return hashlib.sha256(f"{seed}:{reel_id}".encode()).hexdigest()[:16]


def sort_tuple(seed, reel_id, deprioritised):
    """Unseen reels (tier 0) always come before seen / own reels (tier 1); inside a tier the order is the seeded shuffle."""
    return (1 if deprioritised else 0, shuffle_key(seed, reel_id), str(reel_id))


def order(reel_ids, seed, deprioritised):
    """All reel ids in this visit's order."""
    deprioritised = set(deprioritised)
    return sorted(reel_ids, key=lambda rid: sort_tuple(seed, rid, rid in deprioritised))


def encode_cursor(seed, t0, reel_id, deprioritised):
    tier, key, rid = sort_tuple(seed, reel_id, deprioritised)
    raw = "|".join((VERSION, seed, t0, str(tier), key, rid))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor):
    """-> dict(seed, t0, position) where position is the (tier, key, id) of the last reel already shown."""
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        version, seed, t0, tier, key, rid = raw.split("|")
        if version != VERSION or not _SEED_RE.match(seed) or tier not in ("0", "1") or not _KEY_RE.match(key):
            raise ValueError
        datetime.fromisoformat(t0)
        uuid.UUID(rid)
        return {"seed": seed, "t0": t0, "position": (int(tier), key, rid)}
    except (binascii.Error, UnicodeDecodeError, ValueError, TypeError):
        raise BadCursor("Invalid reel cursor.")


def remaining(ordered_ids, seed, deprioritised, position):
    """The part of `ordered_ids` that comes after `position` (everything when position is None)."""
    if position is None:
        return list(ordered_ids)
    deprioritised = set(deprioritised)
    return [rid for rid in ordered_ids if sort_tuple(seed, rid, rid in deprioritised) > position]
