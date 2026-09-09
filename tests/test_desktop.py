"""Launch and reuse a real local backend, without Telegram or personal data."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
from urllib.request import Request, urlopen

from telegram_scraper.instance import FILENAME, discover

PROJECT = Path(__file__).resolve().parents[1]


@contextmanager
def desktop_server(root):
    process = subprocess.Popen(
        [sys.executable, "-u", "-m", "telegram_scraper", "--data-dir", str(root),
         "serve", "--port", "0", "--no-browser", "--desktop"],
        cwd=PROJECT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert select.select([process.stdout], [], [], 15)[0], "Desktop backend did not announce readiness"
        line = process.stdout.readline()
        assert line, process.stderr.read()
        ready = json.loads(line)
        assert ready["event"] == "ready"
        yield process, ready
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGINT)
        stdout, stderr = process.communicate(timeout=15)
        assert process.returncode == 0, stderr


def test_desktop_launch_is_offline_and_quit_releases_library(tmp_path):
    root = tmp_path / "My local archive"
    with desktop_server(root) as (process, ready):
        assert ready["owns_server"]
        saved = json.loads((root / FILENAME).read_text())
        assert os.stat(root / FILENAME).st_mode & 0o777 == 0o600
        assert saved["pid"] == process.pid
        assert discover(root)["url"] == ready["url"]
        request = Request(ready["url"] + "/api/state", headers={
            "Cookie": f"telegram-scraper-session-{ready['url'].rsplit(':', 1)[1]}={saved['token']}"})
        state = json.load(urlopen(request, timeout=5))
        assert not state["connection"]["authorized"]
        assert state["library"]["total"] == 0
        assert not (root / "telegram_scraper.session").exists()
        assert not (root / "telegram-scraper.session").exists()
        assert not (root / ".telegram-scraper.json").exists()
    assert not (root / FILENAME).exists()
    with desktop_server(root) as (_, ready):
        assert ready["owns_server"]


def test_second_gui_reuses_same_server_without_owning_or_stopping_it(tmp_path):
    with desktop_server(tmp_path) as (first, ready):
        with desktop_server(tmp_path) as (second, reused):
            assert reused["url"] == ready["url"]
            assert not reused["owns_server"]
            second.wait(timeout=5)
        assert first.poll() is None
        assert discover(tmp_path)["pid"] == first.pid


def test_discovery_rejects_wrong_library_stale_token_and_symlinks(tmp_path):
    first_root, second_root = tmp_path / "one", tmp_path / "two"
    second_root.mkdir()
    with desktop_server(first_root) as (_, ready):
        descriptor = first_root / FILENAME
        saved = descriptor.read_bytes()
        copy = second_root / FILENAME
        copy.write_bytes(saved)
        assert discover(second_root) is None
        value = json.loads(saved)
        value["token"] = "x" * 43
        descriptor.write_text(json.dumps(value))
        assert discover(first_root) is None
        descriptor.write_bytes(saved)
        copy.unlink()
        copy.symlink_to(descriptor)
        assert discover(second_root) is None
        assert discover(first_root)["url"] == ready["url"]


def test_discovery_does_not_follow_untrusted_urls_or_create_files(tmp_path):
    assert discover(tmp_path) is None
    assert list(tmp_path.iterdir()) == []
    descriptor = tmp_path / FILENAME
    for address in ("https://example.com", "http://localhost:80", "http://127.0.0.1:80/elsewhere",
                    "http://127.0.0.1:80?query=1", "http://name:secret@127.0.0.1:80",
                    "http://127.0.0.1:999999"):
        descriptor.write_text(json.dumps({"version": 1, "pid": 10, "token": "x" * 43, "url": address}))
        assert discover(tmp_path) is None


def test_bad_settings_are_reported_without_overwriting_them(tmp_path):
    path = tmp_path / ".telegram-scraper.json"
    path.write_text("{ broken")
    result = subprocess.run([sys.executable, "-m", "telegram_scraper", "--data-dir", str(tmp_path),
                             "serve", "--port", "0", "--no-browser", "--desktop"],
                            cwd=PROJECT, capture_output=True, text=True, timeout=10)
    assert result.returncode == 1
    assert "Could not continue" in result.stderr
    assert path.read_text() == "{ broken"
    assert not (tmp_path / FILENAME).exists()
