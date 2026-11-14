"""Cloudinary integration. Cloudinary stores the files; Supabase stores references (url, public_id, ...).

Uploads are made server-side with the API secret, which never reaches the browser. Files are validated
(extension, size, and real content signature) before they are sent. Chat attachments use Cloudinary's
`authenticated` delivery type and are served through short-lived signed URLs.
"""
import io
import logging
import uuid
import cloudinary
import cloudinary.uploader
import cloudinary.utils
from flask import current_app
from PIL import Image, UnidentifiedImageError
from app.services import r2_service as r2

log = logging.getLogger(__name__)

IMAGE_EXT = {"jpg", "jpeg", "png", "webp", "gif"}
VIDEO_EXT = {"mp4", "mov", "m4v", "webm"}
AUDIO_EXT = {"mp3", "m4a", "aac", "wav", "ogg", "oga"}
REEL_WIDTH = 540            # delivery width for reels; the eager (pre-built) version uses the same value
UPLOAD_TIMEOUT = 100        # seconds; keep below the gunicorn timeout (120)
IMAGE_FORMATS = {"JPEG", "PNG", "WEBP", "GIF"}
R2_VIDEO_TYPES = {"mp4": "video/mp4", "mov": "video/quicktime", "m4v": "video/x-m4v"}   # formats whose length we can read ourselves


class UploadError(ValueError):
    """Raised with a user-safe message when an upload is rejected."""


def configure(app):
    cfg = app.config
    if cfg.get("CLOUDINARY_CLOUD_NAME"):
        cloudinary.config(cloud_name=cfg["CLOUDINARY_CLOUD_NAME"], api_key=cfg["CLOUDINARY_API_KEY"],
                          api_secret=cfg["CLOUDINARY_API_SECRET"], secure=True)


def is_configured():
    return bool(cloudinary.config().cloud_name)


def _sniff_video(head):
    if len(head) >= 12 and head[4:8] == b"ftyp":
        return True  # mp4 / mov
    return head[:4] == b"\x1a\x45\xdf\xa3"  # webm / matroska


def validate(file_storage, allow_video=True, allow_image=True):
    """Validate a Werkzeug FileStorage. Returns (kind, bytes_data, ext). Raises UploadError."""
    if not file_storage or not file_storage.filename:
        raise UploadError("Choose a file to upload.")
    name = file_storage.filename.lower()
    ext = name.rsplit(".", 1)[-1] if "." in name else ""
    cfg = current_app.config
    data = file_storage.read()
    file_storage.seek(0)
    if not data:
        raise UploadError("That file is empty.")
    if ext in IMAGE_EXT and allow_image:
        if len(data) > cfg["MAX_IMAGE_MB"] * 1024 * 1024:
            raise UploadError(f"Images can be up to {cfg['MAX_IMAGE_MB']} MB.")
        try:
            img = Image.open(io.BytesIO(data))
            img.verify()
            if img.format not in IMAGE_FORMATS:
                raise UploadError("That image format isn't supported.")
        except (UnidentifiedImageError, OSError, SyntaxError):
            raise UploadError("That file isn't a valid image.")
        return "image", data, ext
    if ext in VIDEO_EXT and allow_video:
        if len(data) > cfg["MAX_VIDEO_MB"] * 1024 * 1024:
            raise UploadError(f"Videos can be up to {cfg['MAX_VIDEO_MB']} MB.")
        if not _sniff_video(data[:16]):
            raise UploadError("That file isn't a valid video.")
        return "video", data, ext
    allowed = []
    if allow_image:
        allowed += sorted(IMAGE_EXT)
    if allow_video:
        allowed += sorted(VIDEO_EXT)
    raise UploadError("Unsupported file type. Allowed: " + ", ".join(allowed) + ".")


def _sniff_audio(head):
    """Real content signature, not just the extension: MP3 (ID3 tag or frame sync), AAC/ADTS, M4A, WAV, Ogg."""
    if head[:3] == b"ID3":
        return True
    if len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        return True                                   # MPEG audio / ADTS frame sync
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return True
    if head[:4] == b"OggS":
        return True
    return len(head) >= 12 and head[4:8] == b"ftyp"   # m4a


def validate_audio(file_storage):
    """Validate a music clip. Returns (bytes_data, ext). Raises UploadError."""
    if not file_storage or not file_storage.filename:
        raise UploadError("Choose an audio file.")
    name = file_storage.filename.lower()
    ext = name.rsplit(".", 1)[-1] if "." in name else ""
    if ext not in AUDIO_EXT:
        raise UploadError("Unsupported music file. Allowed: " + ", ".join(sorted(AUDIO_EXT)) + ".")
    cfg = current_app.config
    data = file_storage.read()
    file_storage.seek(0)
    if not data:
        raise UploadError("That audio file is empty.")
    if len(data) > cfg["MAX_AUDIO_MB"] * 1024 * 1024:
        raise UploadError(f"Music can be up to {cfg['MAX_AUDIO_MB']} MB.")
    if not _sniff_audio(data[:16]):
        raise UploadError("That file isn't a valid audio file.")
    return data, ext


def upload_audio(file_storage, folder, owner_id):
    """Validate and upload a music clip (Cloudinary stores audio under resource_type 'video').
    Returns {url, public_id, duration}. Clips longer than MAX_AUDIO_SECONDS are rejected and removed again."""
    data, _ext = validate_audio(file_storage)
    if not is_configured():
        raise UploadError("Media uploads aren't configured on this server yet.")
    try:
        res = cloudinary.uploader.upload(
            io.BytesIO(data), resource_type="video", type="upload", folder=f"cirq/{folder}",
            public_id=f"{owner_id[:8]}_{uuid.uuid4().hex[:12]}", overwrite=False,
            context={"owner": owner_id}, invalidate=True, timeout=UPLOAD_TIMEOUT)
    except Exception as exc:
        log.error("cloudinary audio upload failed: %s: %s", type(exc).__name__, str(exc)[:300])
        raise UploadError("Music upload failed. Please try again.")
    duration = res.get("duration")
    limit = current_app.config["MAX_AUDIO_SECONDS"]
    if duration and float(duration) > limit + 1:
        destroy(res.get("public_id"), "audio")
        raise UploadError(f"Music clips can be up to {limit} seconds long.")
    return {"url": res.get("secure_url"), "public_id": res.get("public_id"), "duration": duration}


def audio_fields(info, title):
    """Columns to store on a post or story row for an uploaded clip (or all-None when there is no music)."""
    if not info:
        return {"audio_url": None, "audio_public_id": None, "audio_title": None, "audio_duration": None}
    return {"audio_url": info["url"], "audio_public_id": info["public_id"],
            "audio_title": (title or "").strip()[:80] or None, "audio_duration": info.get("duration")}


def _reel_transform(width=REEL_WIDTH):
    """One definition shared by the pre-built (eager) copy and the delivery URL, so they always match.
    H.264 + AAC in an MP4 plays on every phone and browser, even when the original is an iPhone HEVC .mov."""
    return {"width": width, "crop": "limit", "quality": "auto", "video_codec": "h264", "audio_codec": "aac", "format": "mp4"}


def _upload_video_to_r2(data, ext, folder, owner_id):
    """Store a public video in Cloudflare R2. Returns the media dict, or None to let Cloudinary take the file instead
    (not an MP4/MOV/M4V, a length that can't be read, or R2 being unavailable), so an upload never fails just because of R2."""
    content_type = R2_VIDEO_TYPES.get(ext)
    duration = r2.mp4_duration(data) if content_type else None
    if not duration:
        return None
    key = f"cirq/{folder}/{owner_id[:8]}_{uuid.uuid4().hex[:12]}.{ext}"
    try:
        url = r2.upload_bytes(data, key, content_type)
    except Exception as exc:
        log.error("r2 upload failed, falling back to cloudinary: %s: %s", type(exc).__name__, str(exc)[:300])
        return None
    return {"media_type": "video", "url": url, "public_id": key, "delivery_type": "r2", "bytes": len(data),
            "width": None, "height": None, "duration": round(duration, 2)}


def upload(file_storage, folder, owner_id, allow_video=True, allow_image=True, private=False):
    """Validate and upload. Returns a dict ready to store in Supabase."""
    kind, data, ext = validate(file_storage, allow_video, allow_image)
    if kind == "video" and not private and r2.is_configured():
        stored = _upload_video_to_r2(data, ext, folder, owner_id)
        if stored:
            return stored
    if not is_configured():
        raise UploadError("Media uploads aren't configured on this server yet.")
    delivery = "authenticated" if private else "upload"
    opts = dict(
        resource_type=kind, type=delivery, folder=f"cirq/{folder}",
        public_id=f"{owner_id[:8]}_{uuid.uuid4().hex[:12]}", overwrite=False,
        context={"owner": owner_id}, invalidate=True, timeout=UPLOAD_TIMEOUT,
    )
    if kind == "video" and folder == "reels" and not private:
        # Build the phone-friendly MP4 in the background right after upload, so the first viewer
        # doesn't wait for (or hit the size limit of) an on-the-fly conversion.
        opts.update(eager=[_reel_transform()], eager_async=True)
    try:
        res = cloudinary.uploader.upload(io.BytesIO(data), **opts)
    except Exception as exc:
        log.error("cloudinary upload failed: %s: %s", type(exc).__name__, str(exc)[:300])
        raise UploadError("Upload failed. Please try again.")
    return {
        "media_type": kind, "url": res.get("secure_url"), "public_id": res.get("public_id"),
        "delivery_type": delivery, "bytes": res.get("bytes"), "width": res.get("width"),
        "height": res.get("height"), "duration": res.get("duration"),
    }


def destroy(public_id, media_type="image", delivery_type="upload"):
    """Delete an asset. Safe to call when Cloudinary isn't configured (returns False). Videos stored in R2 are removed from R2."""
    if delivery_type == "r2":
        return r2.delete(public_id)
    if not public_id or not is_configured():
        return False
    try:
        cloudinary.uploader.destroy(public_id, resource_type="video" if media_type in ("video", "audio") else "image",
                                    type=delivery_type, invalidate=True)
        return True
    except Exception as exc:  # pragma: no cover
        log.error("cloudinary delete failed: %s", type(exc).__name__)
        return False


def media_src(row, width=None, thumb=False):
    """Responsive delivery URL for a media row (dict with url/public_id/media_type/delivery_type)."""
    if not row:
        return ""
    if row.get("delivery_type") == "r2":
        return "" if thumb else row.get("url", "")       # served as-is from R2; there is no generated still frame
    if not (is_configured() and row.get("public_id")):
        return row.get("url", "")
    kind = row.get("media_type", "image")
    delivery = row.get("delivery_type", "upload")
    tr = {"fetch_format": "auto", "quality": "auto"}
    if width:
        tr.update(width=width, crop="limit")
    opts = {"resource_type": "video" if kind == "video" else "image", "type": delivery, "secure": True}
    if delivery == "authenticated":
        opts.update(sign_url=True)
    if kind == "video" and thumb:
        tr.pop("fetch_format", None)
        tr.update(width=width or 480, crop="fill", start_offset="0")
        opts.update(transformation=[tr], format="jpg")   # the .jpg extension makes Cloudinary return a still frame
        return cloudinary.utils.cloudinary_url(row["public_id"], **opts)[0]
    if kind == "video":
        # Always deliver H.264/AAC MP4. Without an explicit format Cloudinary returns the original file,
        # which for phone uploads (HEVC .mov etc.) will not play in most browsers.
        tr = _reel_transform(width) if width else _reel_transform(REEL_WIDTH)
        opts["format"] = tr.pop("format")
    opts["transformation"] = [tr]
    return cloudinary.utils.cloudinary_url(row["public_id"], **opts)[0]


def avatar_src(profile, size=96):
    if profile and profile.get("avatar_url"):
        row = {"url": profile["avatar_url"], "public_id": profile.get("avatar_public_id"), "media_type": "image"}
        if is_configured() and row["public_id"]:
            return cloudinary.utils.cloudinary_url(row["public_id"], secure=True, transformation=[
                {"width": size, "height": size, "crop": "fill", "gravity": "face", "fetch_format": "auto", "quality": "auto"}])[0]
        return row["url"]
    return ""


def audio_src(item):
    """Playable URL for a post/story row that has music. Always delivered as MP3 so m4a/wav/ogg uploads play everywhere."""
    if not item or not item.get("audio_url"):
        return ""
    pid = item.get("audio_public_id")
    if is_configured() and pid:
        return cloudinary.utils.cloudinary_url(pid, resource_type="video", type="upload", format="mp3", secure=True)[0]
    return item["audio_url"]


def school_logo_src(school, size=96):
    """Delivery URL for a school's logo, or '' when the school has none (templates then show the cap icon).
    Logos are fitted, never cropped, so a crest keeps its shape."""
    if not school or not school.get("logo_url"):
        return ""
    pid = school.get("logo_public_id")
    if is_configured() and pid:
        return cloudinary.utils.cloudinary_url(pid, secure=True, transformation=[
            {"width": size, "height": size, "crop": "fit", "fetch_format": "auto", "quality": "auto"}])[0]
    return school["logo_url"]
