import re
import hashlib
import secrets
from datetime import datetime, timezone, timedelta

USERNAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9_.]{1,22})[a-z0-9]$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")

# CIRQ STATUS tiers. Ranges are contiguous and non-overlapping.
TIERS = [
    ("Rookie", 0, 99),
    ("Elite", 100, 500),
    ("Legendary", 501, 1000),
    ("Icon", 1001, 2500),
    ("Superstar", 2501, 5000),
    ("Royalty", 5001, 10000),
    ("Hall of Fame", 10001, None),
]


def utcnow():
    return datetime.now(timezone.utc)


def iso(dt=None):
    return (dt or utcnow()).isoformat()


def parse_dt(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    s = str(value).replace("Z", "+00:00")
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def normalize_username(raw):
    return (raw or "").strip().lower().lstrip("@")


def valid_username(name):
    return bool(USERNAME_RE.match(name)) and ".." not in name and "__" not in name


def valid_email(email):
    return bool(EMAIL_RE.match(email or "")) and len(email) <= 254


def password_problems(pw, username=""):
    problems = []
    if len(pw or "") < 10:
        problems.append("Use at least 10 characters.")
    if not re.search(r"[A-Za-z]", pw or "") or not re.search(r"\d", pw or ""):
        problems.append("Include at least one letter and one number.")
    if username and username.lower() in (pw or "").lower():
        problems.append("Your password can't contain your username.")
    if (pw or "").lower() in {"password123", "1234567890", "qwerty12345", "password1234"}:
        problems.append("That password is too common.")
    return problems


def tier_for(followers):
    n = max(0, int(followers or 0))
    for name, lo, hi in TIERS:
        if n >= lo and (hi is None or n <= hi):
            idx = [t[0] for t in TIERS].index(name)
            nxt = TIERS[idx + 1] if idx + 1 < len(TIERS) else None
            return {"name": name, "min": lo, "max": hi, "index": idx,
                    "next_name": nxt[0] if nxt else None,
                    "to_next": (nxt[1] - n) if nxt else 0,
                    "progress": 100 if not nxt else int(100 * (n - lo) / (nxt[1] - lo))}
    return {"name": "Rookie", "min": 0, "max": 99, "index": 0, "next_name": "Elite", "to_next": 100, "progress": 0}


def time_ago(value):
    d = parse_dt(value)
    if not d:
        return ""
    secs = int((utcnow() - d).total_seconds())
    if secs < 45:
        return "just now"
    if secs < 3600:
        return f"{max(1, secs // 60)}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    if secs < 7 * 86400:
        return f"{secs // 86400}d"
    return d.strftime("%d %b %Y")


EAT = timezone(timedelta(hours=3))


def fmt_event_dt(value):
    """Events are stored in UTC and shown in East Africa Time."""
    d = parse_dt(value)
    return d.astimezone(EAT).strftime("%a %d %b, %H:%M EAT") if d else ""


def parse_local_eat(value):
    """Parse a <input type=datetime-local> value as East Africa Time and return a UTC datetime."""
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M").replace(tzinfo=EAT).astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def new_token():
    """Return (token_for_user, sha256_hash_for_database)."""
    token = secrets.token_urlsafe(32)
    return token, hash_token(token)


def hash_token(token):
    return hashlib.sha256((token or "").encode()).hexdigest()


def clean_text(value, limit):
    return (value or "").replace("\r", "").strip()[:limit]


def short_count(n):
    n = int(n or 0)
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 10_000:
        return f"{n/1000:.0f}K"
    if n >= 1000:
        return f"{n/1000:.1f}K"
    return str(n)


def hours_from_now(h):
    return utcnow() + timedelta(hours=h)
