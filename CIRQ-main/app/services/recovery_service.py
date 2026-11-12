"""Account recovery code.

At sign-up every student gets a simple code such as  K7M2-9QXD-4TFH  (12 letters and numbers, no look-alikes like 0/O or 1/I/L).
If they forget their password they enter their username (or email) + the code on /recover and choose a new password.

Why it is safe to keep a code the student can look up again:
  * the plaintext is never stored. We keep an HMAC (to check a submitted code) and a Fernet-encrypted copy (so the student can
    view their own code again, only after re-entering their password). Both use keys derived from RECOVERY_CODE_KEY, which never
    leaves the server, and live in a table that the public database key cannot read.
  * a code is useless alone: recovery needs the username/email too, and wrong guesses lock the account for a while.
  * a code is single-use: after it resets a password, a brand-new code replaces it.
"""
import base64
import hashlib
import hmac
import logging
import secrets
from datetime import timedelta

from cryptography.fernet import Fernet, InvalidToken
from flask import current_app

from app.services import supabase_service as sb
from app.utils import utcnow, parse_dt, iso

log = logging.getLogger(__name__)

ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"      # 31 characters: no I, L, O, 0 or 1, so nothing is mistaken when written down
CODE_LEN = 12                                    # 31^12 is about 2^59 possibilities


# ------------------------------------------------------------------ keys
def _master():
    secret = current_app.config.get("RECOVERY_CODE_KEY") or current_app.config.get("SECRET_KEY") or ""
    if not secret:
        raise RuntimeError("Set RECOVERY_CODE_KEY (or SECRET_KEY) before using recovery codes.")
    return secret.encode()


def _fernet():
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(b"cirq-recovery-enc|" + _master()).digest()))


def _mac_key():
    return hashlib.sha256(b"cirq-recovery-mac|" + _master()).digest()


# ------------------------------------------------------------------ the code itself
def generate():
    raw = "".join(secrets.choice(ALPHABET) for _ in range(CODE_LEN))
    return pretty(raw)


def normalize(raw):
    """Forgiving input: any case, with or without dashes/spaces."""
    return "".join(ch for ch in (raw or "").upper() if ch.isalnum())


def pretty(raw):
    n = normalize(raw)
    return "-".join(n[i:i + 4] for i in range(0, len(n), 4))


def looks_valid(raw):
    n = normalize(raw)
    return len(n) == CODE_LEN and all(ch in ALPHABET for ch in n)


def code_hash(raw):
    return hmac.new(_mac_key(), normalize(raw).encode(), hashlib.sha256).hexdigest()


def mask():
    return "•" * 4 + "-" + "•" * 4 + "-" + "•" * 4


# ------------------------------------------------------------------ storing / showing
def has_code(user_id):
    return bool(sb.get("recovery_codes", "user_id", user_id=user_id))


def issue(user_id):
    """Create (or replace) this student's code. The old code stops working immediately. Returns the new code, to be shown once."""
    for _ in range(6):
        code = generate()
        digest = code_hash(code)
        if sb.get("recovery_codes", "user_id", code_hash=digest):          # astronomically unlikely; try again
            continue
        values = {"code_hash": digest, "code_enc": _fernet().encrypt(code.encode()).decode(), "created_at": iso(),
                  "last_used_at": None, "failed_attempts": 0, "locked_until": None}
        if sb.get("recovery_codes", "user_id", user_id=user_id):
            sb.update("recovery_codes", values, user_id=user_id)
        else:
            sb.insert("recovery_codes", {"user_id": user_id, **values})
        sb.update("profiles", {"recovery_saved_at": None}, id=user_id)     # they must confirm they saved the new one
        return code
    raise RuntimeError("could not generate a unique recovery code")


def reveal(user_id):
    """The student's current code, or None if they have none (or the encryption key changed since it was made)."""
    row = sb.get("recovery_codes", "code_enc", user_id=user_id)
    if not row:
        return None
    try:
        return _fernet().decrypt(row["code_enc"].encode()).decode()
    except (InvalidToken, ValueError):
        log.warning("a stored recovery code could not be decrypted (was RECOVERY_CODE_KEY changed?)")
        return None


def mark_saved(user_id):
    sb.update("profiles", {"recovery_saved_at": iso()}, id=user_id)


# ------------------------------------------------------------------ checking a submitted code
def check(profile, submitted):
    """'ok' | 'bad' | 'locked'. Constant-time comparison; counts wrong tries and pauses recovery after too many."""
    digest = code_hash(submitted)                                           # always computed, so unknown users cost the same time
    if not profile:
        return "bad"
    row = sb.get("recovery_codes", user_id=profile["id"])
    now = utcnow()
    if row and row.get("locked_until") and (parse_dt(row["locked_until"]) or now) > now:
        return "locked"
    if row and looks_valid(submitted) and hmac.compare_digest(row["code_hash"], digest):
        sb.update("recovery_codes", {"failed_attempts": 0, "locked_until": None}, user_id=profile["id"])
        return "ok"
    if row:
        tries = int(row.get("failed_attempts") or 0) + 1
        if tries >= current_app.config["RECOVERY_MAX_ATTEMPTS"]:
            sb.update("recovery_codes", {"failed_attempts": 0, "locked_until": iso(now + timedelta(minutes=current_app.config["RECOVERY_LOCK_MINUTES"]))},
                      user_id=profile["id"])
        else:
            sb.update("recovery_codes", {"failed_attempts": tries}, user_id=profile["id"])
    return "bad"
