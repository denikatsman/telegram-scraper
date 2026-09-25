import errno
from http.client import HTTPConnection, IncompleteRead
import json
import os
from pathlib import Path
import subprocess
import pytest
from test_app import application, request


@pytest.mark.parametrize("child", [False, True])
def test_actual_rendered_download_link_returns_selected_archive_bytes(application, child):
    runtime, _ = application
    if child:
        runtime.settings.update({"channel": "@first"})
        runtime.call(runtime.add_channel({"channel": "@second"}))
        runtime.store.media_dir.mkdir(parents=True)
        (runtime.store.media_dir / "child.bin").write_bytes(b"child attachment")
        runtime.store.upsert({"id": 1, "media_file": "media/child.bin"})
        runtime.store.save()
    script = "const {harness,state}=require('./tests/app_harness.cjs'); const h=harness(); h.app.setState(state('1',process.argv[1])); h.app.renderPostDetail({id:1,media_kind:'file',media_url:'/api/media/1',media_name:'fixture.bin',revisions:[]}); console.log(h.get('post-detail-body').querySelectorAll('a').find(n=>n.download==='fixture.bin').href);"
    url = subprocess.check_output(["node", "-e", script, runtime.channels.active], text=True).strip()
    status, headers, body = request(application, url)
    assert status == 200 and body == (b"child attachment" if child else b"0123456789abcdef")
    assert headers["Content-Disposition"].startswith("attachment;")
    assert request(application, url, "HEAD")[2] == b""


@pytest.mark.parametrize("route", ["media", "range", "export", "evidence", "static"])
@pytest.mark.parametrize("fault", ["eio", "eof"])
def test_stream_failure_never_appends_second_http_response(application, monkeypatch, route, fault):
    runtime, server = application
    if route in {"media", "range"}:
        file = runtime.store.media_dir / "lesson.mp4"
        file.write_bytes(b"x" * (1024 * 1024 + 100))
        url = "/api/media/1"
    elif route == "export":
        runtime.store.upsert({"id": 6, "text": "x" * (1024 * 1024)})
        runtime.store.save()
        file, url = runtime.store.messages_file, "/api/export"
    elif route == "evidence":
        run = runtime.evidence.begin_run({"mode": "fixture"})
        runtime.evidence.append_observation(run, "fixture", "large", {"bytes": "x" * (1024 * 1024)})
        runtime.evidence.finish_run(run)
        file, url = None, "/api/evidence/export"
    else:
        file = Path(__file__).resolve().parents[1] / "telegram_scraper/static/app.js"
        url = "/app.js"
    real_open = Path.open
    class FailingRead:
        def __init__(self, handle):
            self.handle, self.reads = handle, 0
        def __enter__(self): return self
        def __exit__(self, *args): self.handle.close()
        def fileno(self): return self.handle.fileno()
        def seek(self, *args): return self.handle.seek(*args)
        def read(self, size):
            self.reads += 1
            if self.reads == 1:
                return self.handle.read(min(size, os.fstat(self.fileno()).st_size - 100))
            if fault == "eof": return b""
            raise OSError(errno.EIO, "synthetic second-read failure")
    def open_file(path, *args, **kwargs):
        handle = real_open(path, *args, **kwargs)
        if args and args[0] == "rb" and (path == file or route == "evidence" and path.name == "source-observations.json"):
            return FailingRead(handle)
        return handle
    monkeypatch.setattr(Path, "open", open_file)
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    headers = {"Cookie": f"{server.cookie_name}={server.token}"}
    if route == "range": headers["Range"] = "bytes=0-1048675"
    connection.request("GET", url, headers=headers)
    response = connection.getresponse()
    assert response.status == (206 if route == "range" else 200)
    with pytest.raises(IncompleteRead) as failure:
        response.read()
    assert b"HTTP/" not in failure.value.partial and b"archive could not be read" not in failure.value.partial
    assert len(failure.value.partial) < int(response.getheader("Content-Length"))
    connection.close()
