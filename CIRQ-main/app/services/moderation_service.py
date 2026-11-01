"""Basic, transparent content safety checks. This is a first line of defence, not a replacement for
human moderation: reports and the admin queue are the main safety mechanism."""
import re
from flask import current_app

# Intentionally small and conservative. Extend with BLOCKED_TERMS in the environment.
_SEVERE = ["kill yourself", "kys", "send nudes", "nudes", "i will rape", "suicide method"]
_PHONE = re.compile(r"(?:\+?\d[\s-]?){9,14}\d")


def check_text(text):
    """Return (allowed, reason). Disallows severe terms and phone numbers (privacy for minors)."""
    t = (text or "").lower()
    for term in _SEVERE + list(current_app.config.get("BLOCKED_TERMS", [])):
        if re.search(r"\b" + re.escape(term) + r"\b", t):
            return False, "That message breaks the CIRQ community guidelines."
    if _PHONE.search(text or ""):
        return False, "Don't share phone numbers publicly. Use CIRQ messages instead."
    return True, ""


def media_status_for_upload():
    """Uploads are visible immediately but remain reportable; admins can set 'removed'."""
    return "ok"
