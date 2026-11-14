"""CIRQ configuration. All secrets come from environment variables."""
import os
from dotenv import load_dotenv

load_dotenv()


def _bool(name, default=False):
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def _int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


class Config:
    ENV = os.environ.get("FLASK_ENV", "production")
    DEBUG = False
    TESTING = False
    SECRET_KEY = os.environ.get("SECRET_KEY", "")
    APP_BASE_URL = os.environ.get("APP_BASE_URL", "http://localhost:5000").rstrip("/")

    # Supabase (new "publishable/secret" key names are accepted as aliases)
    SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
    SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY") or os.environ.get("SUPABASE_PUBLISHABLE_KEY", "")
    SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_SECRET_KEY", "")

    # Cloudinary
    CLOUDINARY_CLOUD_NAME = os.environ.get("CLOUDINARY_CLOUD_NAME", "")
    CLOUDINARY_API_KEY = os.environ.get("CLOUDINARY_API_KEY", "")
    CLOUDINARY_API_SECRET = os.environ.get("CLOUDINARY_API_SECRET", "")

    # Mail (password recovery, email verification)
    MAIL_SERVER = os.environ.get("MAIL_SERVER", "")
    MAIL_PORT = _int("MAIL_PORT", 587)
    MAIL_USE_TLS = _bool("MAIL_USE_TLS", True)
    MAIL_USERNAME = os.environ.get("MAIL_USERNAME", "")
    MAIL_PASSWORD = os.environ.get("MAIL_PASSWORD", "")
    MAIL_DEFAULT_SENDER = os.environ.get("MAIL_DEFAULT_SENDER", "CIRQ <no-reply@example.com>")

    # Safety / feature switches
    PAID_COMPETITIONS_ENABLED = _bool("PAID_COMPETITIONS_ENABLED", False)
    AUTO_APPROVE_AFTER_MINUTES = _int("AUTO_APPROVE_AFTER_MINUTES", 5)
    ACTIVE_WINDOW_SECONDS = _int("ACTIVE_WINDOW_SECONDS", 120)
    NEW_SCHOOL_SUBMISSIONS_PER_IP_PER_DAY = _int("NEW_SCHOOL_SUBMISSIONS_PER_IP_PER_DAY", 3)
    AUTO_APPROVAL_BLOCKED_EMAIL_DOMAINS = [
        d.strip().lower() for d in os.environ.get("AUTO_APPROVAL_BLOCKED_EMAIL_DOMAINS", "").split(",") if d.strip()
    ]
    AUTO_APPROVAL_BLOCKED_IPS = [
        ip.strip() for ip in os.environ.get("AUTO_APPROVAL_BLOCKED_IPS", "").split(",") if ip.strip()
    ]
    CRON_SECRET = os.environ.get("CRON_SECRET", "")

    # App coins, boosts and trivia (UGX amounts are manual mobile-money payments; no in-app processor)
    DEVELOPER_DISPLAY_NAME = os.environ.get("DEVELOPER_DISPLAY_NAME", "VEX")
    WELCOME_COINS = _int("WELCOME_COINS", 50)
    DEV_CARD_COINS = _int("DEV_CARD_COINS", 500)
    DEV_CARD_REWARDS_PER_DAY = _int("DEV_CARD_REWARDS_PER_DAY", 1)
    COIN_BOOST_COST = _int("COIN_BOOST_COST", 1000)
    COIN_BOOST_DAYS = _int("COIN_BOOST_DAYS", 3)

    # Account recovery code (app/services/recovery_service.py). Set RECOVERY_CODE_KEY to a long random string and NEVER change it:
    # it protects every stored code, and changing it makes existing codes unusable (students would need to make new ones).
    # If unset, a key is derived from SECRET_KEY.
    RECOVERY_CODE_KEY = os.environ.get("RECOVERY_CODE_KEY", "")
    RECOVERY_MAX_ATTEMPTS = _int("RECOVERY_MAX_ATTEMPTS", 5)       # wrong codes in a row before the account is locked for a while
    RECOVERY_LOCK_MINUTES = _int("RECOVERY_LOCK_MINUTES", 30)

    # Coin gifts and loans between friends (mutual followers). See app/services/lending.py.
    LENDING_ENABLED = _bool("LENDING_ENABLED", True)
    GIFT_MAX_COINS = _int("GIFT_MAX_COINS", 1000)
    GIFT_DAILY_COINS = _int("GIFT_DAILY_COINS", 2000)        # most a student can gift in any rolling 24 hours
    GIFT_DAILY_COUNT = _int("GIFT_DAILY_COUNT", 10)
    LOAN_MIN_COINS = _int("LOAN_MIN_COINS", 10)
    LOAN_MAX_COINS = _int("LOAN_MAX_COINS", 1000)
    LOAN_MIN_DAYS = _int("LOAN_MIN_DAYS", 3)                 # due date must be at least this many days away
    LOAN_MAX_DAYS = _int("LOAN_MAX_DAYS", 90)
    LOAN_MAX_FEE_PCT = _int("LOAN_MAX_FEE_PCT", 20)          # the late fee a lender may set is 0..this
    LOAN_DEFAULT_FEE_PCT = _int("LOAN_DEFAULT_FEE_PCT", 20)
    LOAN_OFFER_HOURS = _int("LOAN_OFFER_HOURS", 48)          # an unanswered offer lapses after this long
    LOAN_GARNISH_PCT = _int("LOAN_GARNISH_PCT", 50)          # share of every coin an overdue borrower earns that goes to the lender
    LOAN_OFFERS_PER_DAY = _int("LOAN_OFFERS_PER_DAY", 5)
    TRIVIA_ENTRY_FEE_UGX = _int("TRIVIA_ENTRY_FEE_UGX", 1000)
    TRIVIA_POINTS_PER_COIN = _int("TRIVIA_POINTS_PER_COIN", 10)
    TRIVIA_MAX_COINS = _int("TRIVIA_MAX_COINS", 100)
    # purpose -> (price in UGX, boost days)   (edit via env if the plans change)
    BOOST_PLANS = {
        "boost_3d": (_int("BOOST_3D_PRICE_UGX", 1000), 3),
        "boost_5d": (_int("BOOST_5D_PRICE_UGX", 7000), 5),
    }
    PAYMENT_NUMBER_DISPLAY = os.environ.get("PAYMENT_NUMBER_DISPLAY", "")      # admin's receiving number shown on the pay page
    PAYMENT_ACCOUNT_NAME = os.environ.get("PAYMENT_ACCOUNT_NAME", "")

    # CIRQ LIVE (app/services/live_service.py). Media runs on LiveKit (WebRTC); Flask only issues short-lived tokens and keeps the
    # records. Leave the three LIVEKIT_* values empty and the Live pages still load but nobody can go live (a clear message is shown).
    LIVE_ENABLED = _bool("LIVE_ENABLED", True)
    LIVEKIT_URL = os.environ.get("LIVEKIT_URL", "").rstrip("/")             # wss://your-project.livekit.cloud
    LIVEKIT_API_KEY = os.environ.get("LIVEKIT_API_KEY", "")
    LIVEKIT_API_SECRET = os.environ.get("LIVEKIT_API_SECRET", "")           # server only: never rendered into a page
    # Who may host: "verified" = approved + email-verified + (admin-verified badge OR admin-granted live access);
    #               "approved" = any approved, email-verified student (use only while testing).
    LIVE_ELIGIBILITY = os.environ.get("LIVE_ELIGIBILITY", "verified").strip().lower()
    LIVE_MAX_GUESTS = min(_int("LIVE_MAX_GUESTS", 4), 4)
    LIVE_STARTS_PER_HOUR = _int("LIVE_STARTS_PER_HOUR", 6)
    LIVE_HOST_TIMEOUT_SECONDS = _int("LIVE_HOST_TIMEOUT_SECONDS", 90)        # a host silent this long is treated as gone and the live is ended
    LIVE_VIEWER_TIMEOUT_SECONDS = _int("LIVE_VIEWER_TIMEOUT_SECONDS", 45)
    LIVE_POLL_SECONDS = _int("LIVE_POLL_SECONDS", 3)
    LIVE_CHAT_MAX_CHARS = _int("LIVE_CHAT_MAX_CHARS", 200)
    LIVE_CHAT_PER_MINUTE = _int("LIVE_CHAT_PER_MINUTE", 20)
    LIVE_REACTIONS_PER_MINUTE = _int("LIVE_REACTIONS_PER_MINUTE", 30)
    LIVE_GIFT_DAILY_COINS = _int("LIVE_GIFT_DAILY_COINS", 20000)             # most one student can spend on live gifts in 24h (0 = no cap)
    LIVE_BATTLE_COUNTDOWN_SECONDS = _int("LIVE_BATTLE_COUNTDOWN_SECONDS", 5)
    LIVE_BATTLE_INVITE_SECONDS = _int("LIVE_BATTLE_INVITE_SECONDS", 60)
    LIVE_NOTIFY_MAX = _int("LIVE_NOTIFY_MAX", 500)                           # followers notified when a creator goes live

    # Limits
    MAX_IMAGE_MB = _int("MAX_IMAGE_MB", 8)
    MAX_VIDEO_MB = _int("MAX_VIDEO_MB", 60)
    MAX_AUDIO_MB = _int("MAX_AUDIO_MB", 10)              # music clips for posts and stories
    MAX_AUDIO_SECONDS = _int("MAX_AUDIO_SECONDS", 90)
    CHAT_ATTACHMENT_VIDEO_MAX_MB = _int("CHAT_ATTACHMENT_VIDEO_MAX_MB", 50)
    CHAT_ATTACHMENT_OTHER_MAX_MB = _int("CHAT_ATTACHMENT_OTHER_MAX_MB", 25)
    CHAT_ATTACHMENT_MAX_FILES = _int("CHAT_ATTACHMENT_MAX_FILES", 10)
    CHAT_ATTACHMENT_BATCH_MAX_MB = min(_int("CHAT_ATTACHMENT_BATCH_MAX_MB", 85), 90)
    CHAT_ATTACHMENT_SIGNED_URL_SECONDS = _int("CHAT_ATTACHMENT_SIGNED_URL_SECONDS", 900)
    MAX_CONTENT_LENGTH = min(max(
        _int("MAX_VIDEO_MB", 60) + _int("MAX_AUDIO_MB", 10) + 5,
        CHAT_ATTACHMENT_BATCH_MAX_MB + 5,
    ), 95) * 1024 * 1024  # keep multipart headroom while staying below Render's request-size ceiling
    STORY_TTL_HOURS = _int("STORY_TTL_HOURS", 24)
    RESET_TOKEN_MINUTES = _int("RESET_TOKEN_MINUTES", 30)
    PAGE_SIZE = _int("PAGE_SIZE", 10)

    # Sessions
    SESSION_COOKIE_NAME = "cirq_session"
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = os.environ.get("FLASK_ENV", "production") == "production"
    REMEMBER_DAYS = _int("REMEMBER_DAYS", 14)
    WTF_CSRF_TIME_LIMIT = None

    # Rate limiting storage: use Redis in multi-instance deployments
    RATELIMIT_STORAGE_URI = os.environ.get("LIMITER_STORAGE_URI", "memory://")
    RATELIMIT_HEADERS_ENABLED = True

    # Extra blocked terms for the basic text filter (comma separated)
    BLOCKED_TERMS = [t.strip().lower() for t in os.environ.get("BLOCKED_TERMS", "").split(",") if t.strip()]


class DevConfig(Config):
    ENV = "development"
    DEBUG = True
    SESSION_COOKIE_SECURE = False


class TestConfig(Config):
    ENV = "testing"
    TESTING = True
    SECRET_KEY = "test-secret-key-not-for-production"
    SESSION_COOKIE_SECURE = False
    WTF_CSRF_ENABLED = False
    RATELIMIT_ENABLED = False
    APP_BASE_URL = "http://localhost"
    PAID_COMPETITIONS_ENABLED = False
    LIVEKIT_URL = "wss://test.livekit.invalid"
    LIVEKIT_API_KEY = "APItestkey"
    LIVEKIT_API_SECRET = "test-livekit-secret-not-real-0123456789"


REQUIRED_IN_PRODUCTION = [
    "SECRET_KEY", "SUPABASE_URL", "SUPABASE_ANON_KEY", "SUPABASE_SERVICE_ROLE_KEY",
    "CLOUDINARY_CLOUD_NAME", "CLOUDINARY_API_KEY", "CLOUDINARY_API_SECRET", "APP_BASE_URL",
]


def validate_config(cfg):
    """Fail fast with a readable message when production configuration is missing."""
    missing = [k for k in REQUIRED_IN_PRODUCTION if not cfg.get(k)]
    if missing:
        raise RuntimeError(
            "CIRQ cannot start. Missing environment variables: " + ", ".join(missing)
            + ". Copy .env.example to .env (local) or set them in Render (production)."
        )
    if len(cfg["SECRET_KEY"]) < 32:
        raise RuntimeError("SECRET_KEY must be at least 32 characters. Generate one with: python -c \"import secrets;print(secrets.token_urlsafe(48))\"")
