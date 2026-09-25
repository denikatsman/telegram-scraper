#!/usr/bin/env python3
"""Install a verified utility app without depending on a disposable build folder.

Delivery standard v1. Keep this file identical in all participating repositories.
Previous installed bundles are retained; user data and running apps are never changed.
This helper is kept in each participating utility repository. MY_UTILITIES_HOME is an
optional alternate home for isolated verification; normal builds use Path.home().
"""
import argparse
import ctypes
import datetime
import fcntl
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys
import tempfile
import uuid


class InstallError(RuntimeError):
    pass


def read_record(path):
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def receipt_path(state, bundle_id):
    return state / "Installed" / (re.sub(r"[^A-Za-z0-9._-]", "_", bundle_id) + ".json")


def matching_record(record, destination, bundle_id, signature):
    return bool(record and record.get("bundleIdentifier") == bundle_id and record.get("installed") == str(destination)
                and record.get("signature") == signature)


def incomplete_attempt(state, destination, bundle_id, signature, installed):
    matches = []
    for path in (state / "Updates").glob("*/update.json"):
        record = read_record(path)
        if not matching_record(record, destination, bundle_id, signature) or record.get("status") not in {"preparing", "installed-metadata-incomplete"}:
            continue
        # A later explicitly correlated installation supersedes older attempts.
        if (matching_record(installed, destination, bundle_id, signature) and installed.get("status") == "installed"
                and installed.get("attemptId") and installed.get("attemptId") != record.get("attemptId")
                and installed.get("timestamp", "") > record.get("timestamp", "")):
            continue
        matches.append((path, record))
    return max(matches, key=lambda pair: pair[1].get("timestamp", ""), default=None)


def finish_receipts(state, record):
    """The bundle is already committed. Metadata failure must not imply rollback."""
    record = dict(record)
    record.pop("failedReceiptLocations", None)
    record.pop("metadataErrors", None)
    failed, errors = [], []
    try:
        record["runningPids"] = running_pids(Path(record["installed"]))
        record["sourceRepository"] = source_repository(Path(record["source"]))
    except Exception as exc:
        errors.append(str(exc))
        record.update(status="installed-metadata-incomplete", failedReceiptLocations=[], metadataErrors=list(errors))
    paths = []
    if record.get("recovery"):
        paths.append(Path(record["recovery"]) / "update.json")
    installed = receipt_path(state, record["bundleIdentifier"])
    paths.append(installed)
    for path in paths:
        try:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            write_record(path, record)
        except Exception as exc:
            failed.append(str(path))
            errors.append(str(exc))
            record.update(status="installed-metadata-incomplete", failedReceiptLocations=list(failed), metadataErrors=list(errors))
    if errors:
        record.update(status="installed-metadata-incomplete", failedReceiptLocations=failed, metadataErrors=errors)
    return record


def reconcile_attempt(record, destination, signature, name, bundle_id):
    """Verify retained provenance before repairing receipts without another swap."""
    record = dict(record)
    previous = record.get("previousSignature")
    if previous is not None:
        path = Path(record.get("previousApp") or Path(record["recovery"]) / (name + ".app.previous"))
        if verify_bundle(path, bundle_id) != previous:
            raise InstallError(f"Previous-bundle provenance is uncertain: {path}; all bundles were retained")
        record["previousApp"] = str(path)
    elif record.get("status") == "preparing" and "previousSignature" not in record:
        raise InstallError("This older incomplete attempt does not establish previous-bundle provenance; all bundles were retained")
    if verify_bundle(destination, bundle_id) != signature:
        raise InstallError("The installation changed during receipt repair; all bundles were retained")
    record["status"] = "installed"
    return record


def rename_atomic(source, destination, exchange=False):
    # Darwin's RENAME_SWAP=2 and RENAME_EXCL=4; both paths must share a volume.
    libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    rename = libc.renamex_np
    rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(os.fsencode(source), os.fsencode(destination), 2 if exchange else 4):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(destination))


def verify_bundle(app, expected_id):
    app = Path(app)
    if app.is_symlink() or not app.is_dir():
        raise InstallError(f"Expected an actual app bundle: {app}")
    try:
        info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    except (OSError, plistlib.InvalidFileException) as error:
        raise InstallError(f"Cannot read app metadata: {app}") from error
    if info.get("CFBundleIdentifier") != expected_id:
        raise InstallError(f"App identity mismatch at {app}; expected {expected_id}")
    executable = info.get("CFBundleExecutable", "")
    if not executable or "/" in executable or executable in [".", ".."]:
        raise InstallError(f"Invalid app executable metadata: {app}")
    if not (app / "Contents/MacOS" / executable).is_file():
        raise InstallError(f"Missing app executable: {app}")
    result = subprocess.run(
        ["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)],
        capture_output=True, text=True,
    )
    if result.returncode:
        raise InstallError(f"Signature verification failed for {app}: {result.stderr.strip()}")
    details = subprocess.run(
        ["/usr/bin/codesign", "-d", "--verbose=4", str(app)],
        capture_output=True, text=True, check=True,
    ).stderr
    fields = dict(re.findall(r"^(Identifier|TeamIdentifier|CDHash)=(.*)$", details, re.M))
    if fields.get("Identifier") != expected_id or not fields.get("CDHash"):
        raise InstallError(f"Missing or inconsistent code identity: {app}")
    return fields


def write_record(path, record):
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(record, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def install_app(source, name, bundle_id, home=None, replace_source_link=False, link_only=False):
    source = Path(os.path.abspath(source))
    home = Path(os.path.abspath(home or Path.home()))
    if not name or "/" in name or name in [".", ".."] or name.endswith(".app"):
        raise InstallError("Pass a plain app name without a path or .app suffix")
    applications = home / "Applications"
    state = home / "Library/Application Support/My Utilities"
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_fd = os.open(state / "install.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        signature = verify_bundle(source, bundle_id)
        destination = applications / (name + ".app")
        if source == destination:
            raise InstallError("The build output and installed app must be different paths")
        if link_only:
            if destination.exists() or destination.is_symlink():
                if not destination.is_symlink() or destination.resolve() != source.resolve():
                    raise InstallError(f"Refusing to replace an unrelated item: {destination}")
            else:
                applications.mkdir(parents=True, exist_ok=True)
                destination.symlink_to(source)
            record = {"app": name, "bundleIdentifier": bundle_id,
                      "source": str(source), "installed": str(destination),
                      "signature": signature, "status": "linked-external",
                      "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat()}
            installed_records = state / "Installed"
            installed_records.mkdir(exist_ok=True, mode=0o700)
            safe_id = re.sub(r"[^A-Za-z0-9._-]", "_", bundle_id)
            write_record(installed_records / (safe_id + ".json"), record)
            return record
        replacing_link = destination.is_symlink()
        if replacing_link and (not replace_source_link or destination.resolve() != source):
            raise InstallError(f"Refusing to replace an unrelated or unapproved app symlink: {destination}")
        if destination in source.parents or source in destination.parents:
            raise InstallError("The build output and installed app cannot contain each other")
        applications.mkdir(parents=True, exist_ok=True)
        previous_signature = None
        if destination.exists():
            previous_signature = verify_bundle(source if replacing_link else destination, bundle_id)
            if previous_signature.get("TeamIdentifier") != signature.get("TeamIdentifier"):
                raise InstallError("Refusing to change the installed app's signing team")

        record = {
            "app": name, "bundleIdentifier": bundle_id, "source": str(source),
            "installed": str(destination),
            "signature": signature,
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        changed = replacing_link or previous_signature != signature
        installed = read_record(receipt_path(state, bundle_id))
        if not changed:
            pending = incomplete_attempt(state, destination, bundle_id, signature, installed)
            if pending:
                record = reconcile_attempt(pending[1], destination, signature, name, bundle_id)
                return finish_receipts(state, record)
            if matching_record(installed, destination, bundle_id, signature) and installed.get("recovery"):
                # Also repair an absent installed/update receipt after the other
                # receipt reached disk, retaining unknown fields and provenance.
                record = reconcile_attempt(installed, destination, signature, name, bundle_id)
                return finish_receipts(state, record)
            for path in (state / "Updates").glob("*/update.json"):
                attempt = read_record(path)
                if not matching_record(installed, destination, bundle_id, signature) and matching_record(attempt, destination, bundle_id, signature) and attempt.get("status") == "installed":
                    return finish_receipts(state, reconcile_attempt(attempt, destination, signature, name, bundle_id))
        if changed:
            identifier = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:12]
            recovery = state / "Updates" / identifier
            recovery.mkdir(parents=True, mode=0o700)
            # Keep staged and installed directories on one volume for atomic exchange.
            if applications.stat().st_dev != recovery.stat().st_dev:
                raise InstallError("Applications and update recovery must be on the same volume")
            staging = Path(tempfile.mkdtemp(prefix=".my-utilities-", dir=applications))
            candidate = staging / (name + ".app")
            backup = recovery / (name + ".app.previous")
            record.update({"recovery": str(recovery), "staging": str(staging), "status": "preparing",
                           "attemptId": identifier, "formatVersion": 2, "candidate": str(candidate),
                           "previousSignature": previous_signature,
                           "previousApp": str(backup) if previous_signature is not None else None})
            write_record(recovery / "update.json", record)
            committed = False
            exchanged = previous_signature is not None
            previous_location = candidate
            try:
                subprocess.run(["/usr/bin/ditto", str(source), str(candidate)], check=True)
                if verify_bundle(candidate, bundle_id) != signature:
                    raise InstallError("The build changed while it was being copied; keeping the installed version")
                rename_atomic(candidate, destination, exchange=exchanged)
                committed = True
                if verify_bundle(destination, bundle_id) != signature:
                    raise InstallError("Installed signature differs from the verified candidate")
                if exchanged:
                    rename_atomic(candidate, backup)
                    previous_location = backup
                    record["previousApp"] = str(backup)
                    if not replacing_link and verify_bundle(backup, bundle_id) != previous_signature:
                        raise InstallError("The retained previous app differs from its preparation signature")
                try:
                    staging.rmdir()
                except OSError:
                    # Finder may add metadata to this staging directory. Keep
                    # unexpected files; they must not undo a verified update.
                    residue = recovery / "staging-residue"
                    try:
                        rename_atomic(staging, residue)
                        record["stagingRetained"] = str(residue)
                    except OSError as error:
                        record["stagingRetained"] = str(staging)
                        record["stagingWarning"] = str(error)
                record["status"] = "installed"
            except Exception as error:
                if committed:
                    # The previous app remains in candidate until final verification succeeds.
                    rename_atomic(previous_location if exchanged else destination,
                                  destination if exchanged else candidate,
                                  exchange=exchanged)
                    record["status"] = "rolled-back"
                    record.pop("previousApp", None)
                    record["failedCandidate"] = str(previous_location if exchanged else candidate)
                else:
                    record["status"] = "not-installed"
                record["error"] = str(error)
                write_record(recovery / "update.json", record)
                raise
        else:
            record = {**(installed or {}), **record, "status": "unchanged"}
        return finish_receipts(state, record)


def running_pids(app):
    result = subprocess.run(["/bin/ps", "-axo", "pid=,comm="],
                            capture_output=True, text=True, check=True)
    prefix = str(app) + "/Contents/"
    return [int(parts[0]) for line in result.stdout.splitlines()
            if len(parts := line.strip().split(None, 1)) == 2 and parts[1].startswith(prefix)]


def source_repository(source):
    result = subprocess.run(["git", "-C", str(source.parent), "rev-parse", "--show-toplevel"],
                            capture_output=True, text=True)
    if result.returncode:
        return None
    root = result.stdout.strip()
    head = subprocess.run(["git", "-C", root, "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()
    return {"root": root, "head": head}


def check_install(source, name, bundle_id, home=None):
    destination = Path(home or Path.home()) / "Applications" / (name + ".app")
    source_signature = verify_bundle(source, bundle_id)
    installed_signature = verify_bundle(destination.resolve(), bundle_id)
    if source_signature != installed_signature:
        raise InstallError(f"Installed app differs from this build: {destination}")
    state = Path(home or Path.home()) / "Library/Application Support/My Utilities"
    path = receipt_path(state, bundle_id)
    installed = read_record(path)
    issues = []
    if not matching_record(installed, destination, bundle_id, installed_signature) or installed.get("status") not in {"installed", "unchanged", "linked-external"}:
        issues.append(str(path))
    pending = incomplete_attempt(state, destination, bundle_id, installed_signature, installed)
    if pending:
        issues.append(str(pending[0]))
    if matching_record(installed, destination, bundle_id, installed_signature) and installed.get("recovery"):
        update_path = Path(installed["recovery"]) / "update.json"
        update = read_record(update_path)
        if (not matching_record(update, destination, bundle_id, installed_signature) or update.get("status") != "installed"
                or installed.get("attemptId") and installed["attemptId"] != update.get("attemptId")):
            issues.append(str(update_path))
    return {"status": "installed-metadata-incomplete" if issues else "verified", "installed": str(destination),
            "bundleMatches": True, "receiptsComplete": not issues, "failedReceiptLocations": list(dict.fromkeys(issues)),
            "previousApp": (pending[1] if pending else installed or {}).get("previousApp"),
            "runningPids": running_pids(destination), "signature": installed_signature}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--name", required=True)
    parser.add_argument("--bundle-id", required=True)
    parser.add_argument("--replace-source-link", action="store_true",
                        help="Replace an existing Applications link to this exact source with a real installation")
    parser.add_argument("--link-only", action="store_true",
                        help="Expose an existing signed external installation without replacing it")
    parser.add_argument("--check", action="store_true",
                        help="Read-only: verify that Applications contains this exact signed build")
    args = parser.parse_args()
    try:
        if args.check:
            record = check_install(args.source, args.name, args.bundle_id,
                                   home=os.environ.get("MY_UTILITIES_HOME"))
        else:
            record = install_app(args.source, args.name, args.bundle_id,
                                 home=os.environ.get("MY_UTILITIES_HOME"),
                                 replace_source_link=args.replace_source_link, link_only=args.link_only)
    except Exception as error:
        print(f"My Utilities: {error}", file=sys.stderr)
        return 1
    print(f"My Utilities: {record['status']} — {record['installed']}")
    print(f"Open from: {record['installed']}")
    if record.get("previousApp"):
        print(f"Previous version retained: {record['previousApp']}")
    if record.get("runningPids"):
        print("An existing process is still running. Save your work, quit and reopen "
              "the app to review this installed build. PIDs: " +
              ", ".join(map(str, record["runningPids"])), file=sys.stderr)
    if record["status"] == "installed-metadata-incomplete":
        print("The app is installed, but delivery metadata needs repair. Retry the installer with the same build. Incomplete locations: " + ", ".join(record.get("failedReceiptLocations", [])), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
