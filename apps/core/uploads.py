"""Checks shared by file uploads: accepted types, size, and content that
matches the type, so a renamed file is refused."""

import hashlib
from pathlib import Path

from django.core.exceptions import ValidationError

PHOTO_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".webp", ".heic"})
VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".3gp", ".webm"})

# Leading bytes for formats we can recognise cheaply. A file claiming one of
# these extensions must start with a matching signature.
_SIGNATURES = {
    ".jpg": (b"\xff\xd8\xff",),
    ".jpeg": (b"\xff\xd8\xff",),
    ".png": (b"\x89PNG\r\n\x1a\n",),
    ".pdf": (b"%PDF-",),
}
_ISO_MEDIA = {".mp4", ".mov", ".3gp", ".heic", ".m4a"}  # "ftyp" box at offset 4


def matches_signature(extension: str, head: bytes) -> bool:
    if extension in _SIGNATURES:
        return head.startswith(_SIGNATURES[extension])
    if extension == ".webp":
        return head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    if extension in _ISO_MEDIA:
        return head[4:8] == b"ftyp"
    if extension == ".webm":
        return head.startswith(b"\x1a\x45\xdf\xa3")  # EBML header
    return True


def inspect_upload(upload, *, max_bytes: int) -> str:
    """Refuse an empty, oversized or mislabelled file; return its SHA-256.

    Whether the extension is accepted at all is the caller's to decide.
    """
    if upload.size == 0:
        raise ValidationError({"file": "The file is empty."})
    if upload.size > max_bytes:
        raise ValidationError(
            {"file": f"The file exceeds the {max_bytes // (1024 * 1024)} MB limit."}
        )
    digest = hashlib.sha256()
    head = b""
    for chunk in upload.chunks():
        if len(head) < 16:
            head += chunk[: 16 - len(head)]
        digest.update(chunk)
    upload.seek(0)
    if not matches_signature(Path(upload.name).suffix.lower(), head):
        raise ValidationError({"file": "The file content does not match its type."})
    return digest.hexdigest()
