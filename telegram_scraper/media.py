"""Exact Telegram byte representations shared by primary and variant capture."""
from pathlib import Path, PurePosixPath


def descriptor(obj, size=None, *, primary=False):
    kind = "document" if getattr(obj, "mime_type", None) is not None or type(obj).__name__ == "Document" else "photo"
    constructor = type(size).__name__ if size is not None else "original"
    representation = "expanded-jpeg" if primary and constructor == "PhotoStrippedSize" else "embedded" if getattr(size, "bytes", None) is not None else "file"
    return {"kind": kind, "object_id": obj.id, "size_constructor": constructor,
            "size_type": getattr(size, "type", "") if size is not None else "", "representation": representation}


def primary_media(message):
    obj = getattr(message, "document", None)
    if obj is not None:
        return descriptor(obj), getattr(obj, "size", None), None
    obj = getattr(message, "photo", None)
    if obj is None:
        return None, None, None
    from telethon.client.downloads import DownloadMethods
    sizes = list(getattr(obj, "sizes", []) or []) + list(getattr(obj, "video_sizes", []) or [])
    size = DownloadMethods._get_thumb(sizes, None) if sizes else None
    expected = getattr(size, "size", None)
    if getattr(size, "sizes", None):
        expected = max(size.sizes)
    if getattr(size, "bytes", None) is not None:
        if type(size).__name__ == "PhotoStrippedSize":
            from telethon.utils import stripped_photo_to_jpg
            expected = len(stripped_photo_to_jpg(size.bytes))
        else:
            expected = len(size.bytes)
    return descriptor(obj, size, primary=True), expected, size


def safe_primary_path(root, value):
    if not isinstance(value, str) or "\\" in value or "\x00" in value:
        return None
    parts = PurePosixPath(value).parts
    if len(parts) != 3 or parts[:2] != ("telegram_data", "media") or ".." in parts:
        return None
    path = Path(root)
    for part in parts:
        path = path / part
        if path.is_symlink():
            return None
    return path
