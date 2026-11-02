"""Validation and private Supabase Storage operations for chat attachments."""
import io
import unicodedata
import zipfile

from PIL import Image, UnidentifiedImageError
from flask import current_app
from werkzeug.utils import secure_filename

from app.extensions import db

BUCKET = "chat-attachments"
UPLOAD_MIMES = {
    "jpg": {"image/jpeg", "image/pjpeg"},
    "jpeg": {"image/jpeg", "image/pjpeg"},
    "png": {"image/png"},
    "gif": {"image/gif"},
    "webp": {"image/webp"},
    "heic": {"image/heic", "image/heif"},
    "mp4": {"video/mp4"},
    "mov": {"video/quicktime"},
    "webm": {"video/webm"},
    "mkv": {"video/x-matroska", "application/x-matroska"},
    "avi": {"video/x-msvideo", "video/avi"},
    "3gp": {"video/3gpp", "video/3gpp2"},
    "pdf": {"application/pdf"},
    "doc": {"application/msword"},
    "docx": {"application/vnd.openxmlformats-officedocument.wordprocessingml.document"},
    "xls": {"application/vnd.ms-excel"},
    "xlsx": {"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
    "ppt": {"application/vnd.ms-powerpoint"},
    "pptx": {"application/vnd.openxmlformats-officedocument.presentationml.presentation"},
    "txt": {"text/plain"},
    "csv": {"text/csv", "application/csv", "application/vnd.ms-excel"},
    "rtf": {"application/rtf", "text/rtf"},
    "odt": {"application/vnd.oasis.opendocument.text"},
    "zip": {"application/zip", "application/x-zip-compressed"},
    "mp3": {"audio/mpeg", "audio/mp3"},
    "m4a": {"audio/mp4", "audio/x-m4a"},
    "wav": {"audio/wav", "audio/x-wav", "audio/wave"},
    "ogg": {"audio/ogg", "application/ogg"},
}
IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "gif", "webp", "heic"}
VIDEO_EXTENSIONS = {"mp4", "mov", "webm", "mkv", "avi", "3gp"}
AUDIO_EXTENSIONS = {"mp3", "m4a", "wav", "ogg"}
PREFERRED_MIME = {
    "jpg": "image/jpeg", "jpeg": "image/jpeg", "mkv": "video/x-matroska", "avi": "video/x-msvideo",
    "csv": "text/csv", "rtf": "application/rtf", "zip": "application/zip",
    "mp3": "audio/mpeg", "m4a": "audio/mp4", "wav": "audio/wav", "ogg": "audio/ogg",
}


class AttachmentError(ValueError):
    def __init__(self, message, status_code=415):
        super().__init__(message)
        self.status_code = status_code


def _safe_names(filename):
    name = unicodedata.normalize("NFKC", (filename or "").replace("\\", "/").rsplit("/", 1)[-1])
    name = "".join(ch for ch in name if ch.isprintable() and ch not in "\r\n")
    name = name.strip()[:255]
    cleaned = secure_filename(name) or "attachment"
    if "." in cleaned:
        stem, extension = cleaned.rsplit(".", 1)
        safe = f"{stem[:max(1, 179 - len(extension))]}.{extension}"
    else:
        safe = cleaned[:180]
    return name or "attachment", safe


def _matches_magic(ext, data):
    head = data[:4096]
    if ext in ("jpg", "jpeg"):
        return head.startswith(b"\xff\xd8\xff")
    if ext == "png":
        return head.startswith(b"\x89PNG\r\n\x1a\n")
    if ext == "gif":
        return head.startswith((b"GIF87a", b"GIF89a"))
    if ext == "webp":
        return len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    if ext == "heic":
        return len(head) >= 12 and head[4:8] == b"ftyp" and head[8:12] in {
            b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1",
        }
    if ext in ("mp4", "mov", "3gp", "m4a"):
        return len(head) >= 12 and head[4:8] == b"ftyp"
    if ext in ("webm", "mkv"):
        if not head.startswith(b"\x1a\x45\xdf\xa3"):
            return False
        marker = head.lower()
        return (b"webm" in marker) if ext == "webm" else (b"matroska" in marker or b"webm" not in marker)
    if ext == "avi":
        return len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"AVI "
    if ext == "pdf":
        return b"%PDF-" in head[:1024]
    if ext in ("doc", "xls", "ppt"):
        return head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")
    if ext in ("docx", "xlsx", "pptx", "odt", "zip"):
        if not head.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
            return False
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                names = set(archive.namelist())
        except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile):
            return False
        if ext == "docx":
            return "[Content_Types].xml" in names and any(n.startswith("word/") for n in names)
        if ext == "xlsx":
            return "[Content_Types].xml" in names and any(n.startswith("xl/") for n in names)
        if ext == "pptx":
            return "[Content_Types].xml" in names and any(n.startswith("ppt/") for n in names)
        if ext == "odt":
            return "mimetype" in names and b"application/vnd.oasis.opendocument.text" in data[:4096]
        return True
    if ext == "rtf":
        return head.lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"{\\rtf")
    if ext in ("txt", "csv"):
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            return False
        return not any(ord(ch) < 32 and ch not in "\t\r\n\f" for ch in text)
    if ext == "mp3":
        return head.startswith(b"ID3") or (len(head) >= 2 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0)
    if ext == "wav":
        return len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WAVE"
    if ext == "ogg":
        return head.startswith(b"OggS")
    return False


def _image_dimensions(ext, data):
    if ext == "heic":
        return None, None
    try:
        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size
            image.verify()
        return width, height
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, SyntaxError):
        raise AttachmentError("That file isn't a valid image.")


def validate_upload(file_storage):
    if not file_storage or not file_storage.filename:
        raise AttachmentError("Choose a file to upload.", 400)
    original, safe = _safe_names(file_storage.filename)
    ext = safe.rsplit(".", 1)[-1].lower() if "." in safe else ""
    allowed_mimes = UPLOAD_MIMES.get(ext)
    if not allowed_mimes:
        raise AttachmentError("That file type isn't allowed.")
    client_mime = (file_storage.mimetype or "").split(";", 1)[0].strip().lower()
    if client_mime not in allowed_mimes:
        raise AttachmentError("The file type doesn't match its extension.")

    max_mb = current_app.config["CHAT_ATTACHMENT_VIDEO_MAX_MB"] if ext in VIDEO_EXTENSIONS else current_app.config["CHAT_ATTACHMENT_OTHER_MAX_MB"]
    data = file_storage.stream.read(max_mb * 1024 * 1024 + 1)
    file_storage.stream.seek(0)
    if not data:
        raise AttachmentError("That file is empty.", 400)
    if len(data) > max_mb * 1024 * 1024:
        raise AttachmentError(f"Files of this type can be up to {max_mb} MB.", 413)
    if not _matches_magic(ext, data):
        raise AttachmentError("The file contents don't match an allowed file type.")

    kind = "image" if ext in IMAGE_EXTENSIONS else "video" if ext in VIDEO_EXTENSIONS else "audio" if ext in AUDIO_EXTENSIONS else "document"
    width, height = _image_dimensions(ext, data) if kind == "image" and ext != "heic" else (None, None)
    mime_type = PREFERRED_MIME.get(ext, sorted(allowed_mimes)[0])
    return {
        "data": data,
        "original_filename": original,
        "safe_filename": safe,
        "mime_type": mime_type,
        "size_bytes": len(data),
        "kind": kind,
        "width": width,
        "height": height,
        "duration": None,
    }


def storage_bucket():
    return db().storage.from_(BUCKET)


def upload_to_storage(path, data, mime_type):
    return storage_bucket().upload(
        path, data,
        file_options={"content-type": mime_type, "cache-control": "3600", "upsert": "false"},
    )


def remove_from_storage(paths):
    if paths:
        storage_bucket().remove(paths)


def signed_url(path, filename=None):
    options = {"download": filename} if filename else {}
    result = storage_bucket().create_signed_url(
        path, current_app.config["CHAT_ATTACHMENT_SIGNED_URL_SECONDS"], options=options,
    )
    return result.get("signedURL") or result.get("signedUrl")
