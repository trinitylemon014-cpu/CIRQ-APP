"""Cloudflare R2 storage for videos (reels, post videos, story videos).

R2 speaks the S3 API, so this uses boto3. Files are uploaded server-side with the secret key (never sent to the browser) and
served straight from the bucket's public address, which R2 does not charge egress for. Supabase keeps only the reference
(url, public_id = the object key, delivery_type = 'r2'), exactly like it does for Cloudinary files.

boto3 is imported lazily, so the app starts and everything else works when R2 is not configured.
"""
import logging
import struct
import threading
from flask import current_app

log = logging.getLogger(__name__)
CACHE_CONTROL = "public, max-age=31536000, immutable"      # object keys are random and never overwritten
_clients = {}
_lock = threading.Lock()


def is_configured():
    cfg = current_app.config
    return bool((cfg.get("R2_ACCOUNT_ID") or cfg.get("R2_ENDPOINT_URL")) and cfg.get("R2_ACCESS_KEY_ID")
                and cfg.get("R2_SECRET_ACCESS_KEY") and cfg.get("R2_BUCKET") and cfg.get("R2_PUBLIC_BASE_URL"))


def public_origin():
    """scheme://host of the public bucket address (used in the Content-Security-Policy), or ''."""
    from urllib.parse import urlsplit
    parts = urlsplit(current_app.config.get("R2_PUBLIC_BASE_URL") or "")
    return f"{parts.scheme}://{parts.netloc}" if parts.scheme in ("https", "http") and parts.netloc else ""


def public_url(key):
    return f"{current_app.config['R2_PUBLIC_BASE_URL'].rstrip('/')}/{key}"


def _client():
    cfg = current_app.config
    endpoint = cfg.get("R2_ENDPOINT_URL") or f"https://{cfg['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com"
    ident = (endpoint, cfg["R2_ACCESS_KEY_ID"], cfg["R2_SECRET_ACCESS_KEY"])
    with _lock:
        if ident not in _clients:
            import boto3
            from botocore.config import Config
            opts = dict(signature_version="s3v4", retries={"max_attempts": 3, "mode": "standard"},
                        connect_timeout=10, read_timeout=90, s3={"addressing_style": "path"})
            try:   # newer botocore adds checksum headers by default; R2 wants them only when required
                conf = Config(request_checksum_calculation="when_required", response_checksum_validation="when_required", **opts)
            except TypeError:
                conf = Config(**opts)
            _clients[ident] = boto3.client("s3", endpoint_url=endpoint, aws_access_key_id=cfg["R2_ACCESS_KEY_ID"],
                                           aws_secret_access_key=cfg["R2_SECRET_ACCESS_KEY"], region_name="auto", config=conf)
        return _clients[ident]


def upload_bytes(data, key, content_type):
    """Store `data` under `key` and return its public URL. Raises on any failure (the caller decides what to do)."""
    _client().put_object(Bucket=current_app.config["R2_BUCKET"], Key=key, Body=data,
                         ContentType=content_type, CacheControl=CACHE_CONTROL)
    return public_url(key)


def delete(key):
    """Remove an object. Safe to call when R2 isn't configured (returns False)."""
    if not key or not is_configured():
        return False
    try:
        _client().delete_object(Bucket=current_app.config["R2_BUCKET"], Key=key)
        return True
    except Exception as exc:  # pragma: no cover
        log.error("r2 delete failed: %s: %s", type(exc).__name__, str(exc)[:300])
        return False


def mp4_duration(data):
    """Length in seconds of an MP4 / MOV / M4V, read from its 'mvhd' header. None when it can't be read (then the caller
    lets Cloudinary handle the file, because the reel length limit needs a trustworthy duration)."""
    def boxes(start, end):
        pos = start
        while pos + 8 <= end:
            size, kind = struct.unpack(">I4s", data[pos:pos + 8])
            header = 8
            if size == 1:
                if pos + 16 > end:
                    return
                size, header = struct.unpack(">Q", data[pos + 8:pos + 16])[0], 16
            elif size == 0:
                size = end - pos
            if size < header or pos + size > end:
                return
            yield kind, pos + header, pos + size
            pos += size
    try:
        for kind, start, end in boxes(0, len(data)):
            if kind != b"moov":
                continue
            for kind2, start2, _end2 in boxes(start, end):
                if kind2 != b"mvhd":
                    continue
                if data[start2] == 1:
                    timescale, duration = struct.unpack(">IQ", data[start2 + 20:start2 + 32])
                else:
                    timescale, duration = struct.unpack(">II", data[start2 + 12:start2 + 20])
                return duration / timescale if timescale and duration else None
    except (struct.error, IndexError):
        return None
    return None
