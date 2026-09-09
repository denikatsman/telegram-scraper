"""Private discovery of a running local app for the desktop launcher."""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import build_opener, HTTPRedirectHandler, ProxyHandler, Request

from .storage import StoreError

FILENAME = ".telegram-scraper-ui.json"


def _read(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 8192:
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else None


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, newurl):
        raise URLError("A local app must not redirect instance discovery.")


def discover(root: Path):
    """Only reuse the authenticated loopback server for this exact library."""
    root = root.expanduser().resolve()
    try:
        value = _read(root / FILENAME)
        if not value or value.get("version") != 1:
            return None
        url = urlparse(value.get("url", ""))
        token, pid = value.get("token"), value.get("pid")
        if url.scheme != "http" or url.hostname != "127.0.0.1" or not url.port or not 0 < url.port <= 65535 or url.username or url.password or url.path not in {"", "/"} or url.query or url.fragment:
            return None
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{20,128}", token) or type(pid) is not int or pid <= 0:
            return None
        address = f"http://127.0.0.1:{url.port}"
        request = Request(address + "/api/instance", headers={"Cookie": f"telegram-scraper-session-{url.port}={token}"})
        with build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=2) as response:
            payload = response.read(8193)
            if len(payload) > 8192:
                return None
            identity = json.loads(payload)
        if identity.get("app") != "telegram-scraper" or identity.get("root") != str(root) or identity.get("pid") != pid:
            return None
        return {"url": address, "pid": pid, "version": identity.get("version")}
    except (OSError, ValueError, TypeError, AttributeError):
        return None


@contextmanager
def announce(root, server):
    """Publish only while holding Store.lock; remove only our own descriptor."""
    path = root / FILENAME
    temporary = None
    value = {"version": 1, "url": f"http://127.0.0.1:{server.server_address[1]}", "pid": os.getpid(), "token": server.token}
    try:
        if path.is_symlink():
            raise StoreError("The app's local connection file is a symbolic link. Remove that link before opening the app.")
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=root, prefix=".ui-instance-", delete=False) as file:
            temporary = Path(file.name)
            os.chmod(temporary, 0o600)
            json.dump(value, file)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
        temporary = None
        yield value["url"]
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        try:
            if _read(path) == value:
                path.unlink()
        except (OSError, ValueError):
            pass
