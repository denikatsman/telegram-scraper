"""One entry point for the browser app and original command-line workflows."""

from __future__ import annotations

import argparse
from contextlib import ExitStack
from getpass import getpass
import json
from pathlib import Path
import sys
import time
import webbrowser

from . import __version__
from .config import ConfigError, default_root
from .storage import StoreError


def parser():
    result = argparse.ArgumentParser(prog="telegram-scraper", description="Save and search Telegram messages, videos and files on this computer.")
    result.add_argument("--version", action="version", version=f"Telegram Scraper {__version__}")
    result.add_argument("--data-dir", type=Path, default=default_root(), help="folder for settings, session, archive and backups")
    sub = result.add_subparsers(dest="command")
    serve = sub.add_parser("serve", help="open the local archive interface (default)")
    serve.add_argument("--port", type=int, default=8765, help="local port; use 0 for an available port")
    serve.add_argument("--no-browser", action="store_true", help="print the local address without opening a browser")
    serve.add_argument("--desktop", action="store_true", help=argparse.SUPPRESS)
    sub.add_parser("login", help="connect a saved session or sign in interactively")
    sub.add_parser("sync", help="sync all accessible history and repair missing media")
    dates = sub.add_parser("range", help="sync an inclusive range of UTC calendar dates")
    dates.add_argument("--start", help="first day, YYYY-MM-DD")
    dates.add_argument("--end", help="last day, YYYY-MM-DD (defaults to first day)")
    sub.add_parser("watch", help="sync history, then capture new and edited posts")
    sub.add_parser("verify", help="check local media and cumulative backup integrity")
    sub.add_parser("check", help="read the first five saved posts without contacting Telegram")
    return result


def print_job(job):
    message = job.get("message", "")
    counters = []
    for key, label in (("processed", "checked"), ("added", "new"), ("updated", "updated"), ("media_downloaded", "downloaded"), ("media_failed", "media failed")):
        if job.get(key):
            counters.append(f"{job[key]} {label}")
    print(message + ("  ·  " + ", ".join(counters) if counters else ""), flush=True)


def main(argv=None):
    args = parser().parse_args(argv)
    command = args.command or "serve"
    root = args.data_dir.expanduser().resolve()
    runtime = None
    server = None
    stack = ExitStack()
    try:
        from .instance import announce, discover
        if command == "serve" and getattr(args, "desktop", False):
            existing = discover(root)
            if existing:
                print(json.dumps({"event": "ready", "url": existing["url"], "owns_server": False}), flush=True)
                return 0
        from .server import LocalServer, Runtime, date_bounds
        runtime = Runtime(root)
        # One process owns the session/library; a second app exits with a clear message.
        stack.enter_context(runtime.lock())
        # Read-only checks never repair data. App/capture startup can recover a
        # committed SQLite transaction now that this process owns the writer lock.
        if command not in {"verify", "check"}:
            runtime.recover_evidence()
        if command == "serve":
            port = getattr(args, "port", 8765)
            if not 0 <= port <= 65535:
                raise ValueError("Choose a port between 0 and 65535.")
            try:
                server = LocalServer(("127.0.0.1", port), runtime)
            except OSError as exc:
                raise ValueError(f"Port {port} is already in use. Try: python3 main.py serve --port 0") from exc
            with announce(root, server) as address:
                if getattr(args, "desktop", False):
                    print(json.dumps({"event": "ready", "url": address, "owns_server": True}), flush=True)
                else:
                    print(f"Telegram Scraper {__version__}\nOpen {address}\nData folder: {root}\nPress Ctrl+C here to stop the app.", flush=True)
                    if not getattr(args, "no_browser", False):
                        webbrowser.open(address)
                server.serve_forever(poll_interval=0.2)
            return 0
        if command == "check":
            if runtime.library_error:
                raise StoreError(runtime.library_error)
            first = sorted(runtime.store.records.values(), key=lambda r: r["id"])[:5]
            print(f"{len(runtime.store.records)} saved posts. Showing the first {len(first)}:")
            for record in first:
                print(f"\n#{record['id']} · {record.get('date', 'Date unavailable')}\n{record.get('text') or '(No text)'}")
            return 0
        if command == "login":
            result = runtime.call(runtime.authenticate("connect", {}))
            if result.get("authorized"):
                print("Connected with your saved Telegram session.")
                return 0
            phone = input("Telegram phone number, including country code: ").strip()
            runtime.call(runtime.authenticate("code", {"phone": phone}))
            code = getpass("Code from Telegram: ").strip()
            result = runtime.call(runtime.authenticate("verify", {"code": code}))
            if result.get("step") == "password":
                runtime.call(runtime.authenticate("verify", {"password": getpass("Telegram two-step verification password: ")}))
            print("Connected. Your session is saved locally.")
            return 0
        payload = {"mode": command}
        if command == "range":
            start = args.start or input("First day (YYYY-MM-DD, UTC): ").strip()
            end = args.end or (input("Last day (Enter for the same day): ").strip() if not args.start else "") or start
            date_bounds(start, end)
            payload.update(start=start, end=end)
        if command != "verify":
            result = runtime.call(runtime.authenticate("connect", {}))
            if not result.get("authorized"):
                raise ValueError("Sign in first with python3 main.py login, or use Settings in the browser app.")
        admission = runtime.call(runtime.start_job(payload))
        if admission.get("capture_started") is False:
            print("Capture did not start. This channel already belongs to archive(s): " + ", ".join(admission["existing_archive_ids"]) + ". Open the chosen archive explicitly before syncing.")
            return 1
        previous = None
        try:
            while True:
                job = runtime.call(runtime.state())["job"]
                signature = (job.get("message"), job.get("processed"), job.get("status"))
                if signature != previous:
                    print_job(job)
                    previous = signature
                if not job["running"]:
                    if job.get("result"):
                        print(json.dumps(job["result"], indent=2, ensure_ascii=False))
                    return 0 if job["status"] == "completed" else 1
                time.sleep(0.5)
        except KeyboardInterrupt:
            runtime.call(runtime.stop_job())
            print("\nStopping safely…", flush=True)
            return 130
    except KeyboardInterrupt:
        print("\nClosing Telegram Scraper.")
        return 0
    except (ConfigError, StoreError, ValueError) as exc:
        print(f"Could not continue: {exc}", file=sys.stderr)
        return 1
    except OSError:
        print("Could not continue: check that the data folder is writable and the disk has free space.", file=sys.stderr)
        return 1
    finally:
        if server:
            server.server_close()
        if runtime:
            runtime.close()
        stack.close()


if __name__ == "__main__":
    raise SystemExit(main())
