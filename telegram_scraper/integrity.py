"""Read-only integrity checks for source evidence and additional media variants.

An intact file is different from a complete Telegram capture. API limitations,
disabled downloads and unfinished runs are reported separately from damaged or
missing files that a saved receipt says were downloaded.
"""

from collections import Counter
import hashlib
import json
from pathlib import Path, PurePosixPath
import re

from .evidence import EvidenceStore, EvidenceError
from .storage import OperationCancelled, StoreError


_HEX = r"[0-9a-f]{64}"
_BINARY_NAME = re.compile(rf"({_HEX})(?:-recovered-[0-9a-f]{{32}})?\.bin")
_MANIFEST_NAME = re.compile(rf"({_HEX})-({_HEX})(?:-[0-9a-f]{{32}})?\.json")
_NO_FILE_STATES = {"primary_archive", "disabled", "metadata_only", "failed"}


def _check_cancel(cancel):
    if cancel and cancel():
        raise OperationCancelled("Source and variant verification was stopped before all files were checked.")


def _json(path):
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError("duplicate JSON field")
            result[key] = value
        return result
    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=pairs,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))


def verify_extended(root, cancel=None, report=None) -> dict:
    root = Path(root).expanduser().resolve()
    data = root / "telegram_data"
    variants = data / "media" / "variants"
    index = variants / "index"
    result = {"ok": False, "checked_evidence_observations": 0, "checked_evidence_occurrences": 0,
              "checked_evidence_runs": 0, "checked_variant_files": 0, "checked_variant_manifests": 0,
              "issues": [], "limitations": [], "cancelled": False}
    issues, limitations = result["issues"], result["limitations"]
    checked_files = {}
    manifest_paths = set()
    checked_receipts = set()
    states = Counter()

    def report_progress(message):
        if report:
            report(message)

    def safe_variant_path(value):
        if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
            return None
        parts = PurePosixPath(value).parts
        if parts[:3] != ("telegram_data", "media", "variants") or len(parts) != 4 or not _BINARY_NAME.fullmatch(parts[3]):
            return None
        path = root / value
        if path.is_symlink():
            return None
        return path

    def check_file(path, label):
        key = path.relative_to(root).as_posix()
        if key in checked_files:
            return checked_files[key]
        _check_cancel(cancel)
        if path.is_symlink() or not path.is_file():
            issues.append(f"{label}: recorded media variant is missing or is not a safe regular file ({path.name}).")
            checked_files[key] = None
            return None
        digest, size = hashlib.sha256(), 0
        report_progress(f"Checking media variant: {path.name}")
        try:
            before = path.stat()
            with path.open("rb") as file:
                while chunk := file.read(1024 * 1024):
                    _check_cancel(cancel)
                    digest.update(chunk)
                    size += len(chunk)
            after = path.stat()
            if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
                issues.append(f"{label}: media variant changed while it was being checked ({path.name}).")
        except OSError:
            issues.append(f"{label}: media variant could not be read ({path.name}).")
            checked_files[key] = None
            return None
        value = digest.hexdigest(), size
        result["checked_variant_files"] += 1
        checked_files[key] = value
        return value

    def check_receipt(receipt, label, *, manifest_name=None):
        if not isinstance(receipt, dict):
            issues.append(f"{label}: media variant receipt is not an object.")
            return
        if "schema_version" in receipt and (type(receipt["schema_version"]) is not int or receipt["schema_version"] != 1):
            issues.append(f"{label}: media variant receipt uses an unsupported schema version.")
            return
        identity, digest, size = receipt.get("identity"), receipt.get("sha256"), receipt.get("size")
        path_value = receipt.get("path") or receipt.get("media_file")
        if not isinstance(identity, str) or not identity or not isinstance(digest, str) or not re.fullmatch(_HEX, digest) or type(size) is not int or size <= 0:
            issues.append(f"{label}: media variant identity, checksum or byte count is invalid.")
            return
        if receipt.get("path") and receipt.get("media_file") and receipt["path"] != receipt["media_file"]:
            issues.append(f"{label}: media variant paths disagree.")
        expected = receipt.get("expected_bytes")
        if expected is not None and (type(expected) is not int or expected < 0 or expected != size):
            issues.append(f"{label}: archived variant size does not match the size Telegram advertised.")
        path = safe_variant_path(path_value)
        if path is None:
            issues.append(f"{label}: media variant path is unsafe or unsupported.")
            return
        filename = _BINARY_NAME.fullmatch(path.name)
        if filename[1] != digest:
            issues.append(f"{label}: the media variant filename does not match its recorded checksum.")
        if manifest_name:
            match = _MANIFEST_NAME.fullmatch(manifest_name)
            expected_key = hashlib.sha256(identity.encode("utf-8")).hexdigest()
            if not match or match[1] != expected_key or match[2] != digest:
                issues.append(f"{label}: manifest filename does not match its recorded variant identity and checksum.")
        if "recovered_from" in receipt:
            old = receipt["recovered_from"]
            # The original may intentionally remain damaged. Check its lexical
            # path here; its own file and original receipt are checked separately.
            parts = PurePosixPath(old).parts if isinstance(old, str) else ()
            if len(parts) != 4 or parts[:3] != ("telegram_data", "media", "variants") or not _BINARY_NAME.fullmatch(parts[3]) or old == path_value:
                issues.append(f"{label}: recovery provenance has an unsafe or invalid original path.")
            else:
                limitations.append(f"A recovered media variant is preserved alongside its original: {path.name}.")
        signature = identity, path_value, digest, size
        if signature in checked_receipts:
            return
        checked_receipts.add(signature)
        actual = check_file(path, label)
        if actual is not None and actual != (digest, size):
            issues.append(f"{label}: media variant bytes do not match its saved checksum and size ({path.name}).")

    try:
        _check_cancel(cancel)
        # Parent-directory symlinks can turn a safe-looking manifest path into a
        # read outside this archive. Reject them before any variant reads.
        for directory in (data, data / "media", variants, index):
            if directory.is_symlink():
                raise StoreError(f"Source verification cannot follow a symbolic-link folder: {directory.name}.")
            if directory.exists() and not directory.is_dir():
                raise StoreError(f"Source verification expected a folder at {directory.name}.")

        evidence = EvidenceStore(root)
        evidence_valid = False
        try:
            report_progress("Checking immutable source observations and run receipts…")
            validated = evidence.validate(cancel=cancel)
            result["checked_evidence_observations"] = validated["observations"]
            result["checked_evidence_occurrences"] = validated["occurrences"]
            result["checked_evidence_runs"] = validated["runs"]
            evidence_valid = validated["exists"]
        except EvidenceError as exc:
            issues.append(str(exc))

        if index.exists():
            for manifest in sorted(index.iterdir()):
                _check_cancel(cancel)
                if manifest.name == ".DS_Store":
                    continue
                if manifest.is_symlink() or not manifest.is_file():
                    issues.append(f"Unsafe media variant index entry: {manifest.name}.")
                    continue
                if manifest.suffix == ".partial":
                    issues.append(f"An incomplete media variant manifest remains: {manifest.name}.")
                    continue
                if manifest.suffix != ".json":
                    limitations.append(f"Unrecognized variant index file was not treated as a manifest: {manifest.name}.")
                    continue
                result["checked_variant_manifests"] += 1
                try:
                    receipt = _json(manifest)
                except (OSError, ValueError, UnicodeError):
                    issues.append(f"Media variant manifest is unreadable or invalid JSON: {manifest.name}.")
                    continue
                if isinstance(receipt, dict) and isinstance(receipt.get("path"), str):
                    manifest_paths.add(receipt["path"])
                check_receipt(receipt, f"Variant manifest {manifest.name}", manifest_name=manifest.name)

        if variants.exists():
            for path in sorted(variants.iterdir()):
                _check_cancel(cancel)
                if path == index or path.name == ".DS_Store":
                    continue
                if path.is_symlink() or not path.is_file():
                    issues.append(f"Unsafe or unexpected media variant entry: {path.name}.")
                    continue
                if path.suffix == ".partial":
                    issues.append(f"An incomplete media variant download remains: {path.name}.")
                    continue
                match = _BINARY_NAME.fullmatch(path.name)
                if not match:
                    limitations.append(f"Unrecognized file was not treated as a downloaded variant: {path.name}.")
                    continue
                relative = path.relative_to(root).as_posix()
                actual = check_file(path, "Variant archive")
                if actual and (actual[0] != match[1] or actual[1] <= 0):
                    issues.append(f"Variant archive: bytes do not match the content-addressed filename ({path.name}).")
                if relative not in manifest_paths:
                    issues.append(f"Downloaded media variant has no immutable manifest: {path.name}.")

        if evidence_valid:
            # Use the evidence reader's short, read-only transaction. This runs
            # during an exclusive verification job; it never creates source data.
            with evidence._read() as connection:
                for row in connection.execute("SELECT observation_id,payload_json FROM observations WHERE kind='media_variant_file' ORDER BY observation_id"):
                    _check_cancel(cancel)
                    receipt = json.loads(row["payload_json"])
                    check_receipt(receipt, f"Source observation {row['observation_id']}")
                latest = connection.execute("SELECT * FROM runs ORDER BY started_at DESC,run_id DESC LIMIT 1").fetchone()
                if latest:
                    if latest["status"] in {"running", "cancelled", "interrupted", "error", "failed", "partial", "stopped"}:
                        limitations.append(f"The latest source capture ended with status '{latest['status']}'; full capture coverage is unconfirmed.")
                    coverage = json.loads(latest["coverage_json"])
                    if coverage.get("capture_complete") is False or coverage.get("history_scan_complete") is False:
                        limitations.append("The latest run does not claim complete source capture. Intact saved files do not establish complete Telegram coverage.")
                    for note in coverage.get("limitations", []):
                        limitations.append(str(note))
                    for row in connection.execute("""SELECT DISTINCT o.payload_json FROM observations o JOIN occurrences c USING(observation_id)
                        WHERE c.run_id=? AND o.kind='context_limitation'""", (latest["run_id"],)):
                        _check_cancel(cancel)
                        note = json.loads(row["payload_json"])
                        if isinstance(note, dict):
                            limitations.append(str(note.get("message") or note.get("code") or "Telegram context access was limited."))

        messages_path = data / "messages_all.json"
        if messages_path.is_symlink():
            issues.append("The message index is a symbolic link; variant capture receipts were not followed.")
        elif messages_path.exists():
            try:
                messages = _json(messages_path)
                if not isinstance(messages, list):
                    raise ValueError()
                for record in messages:
                    _check_cancel(cancel)
                    if not isinstance(record, dict):
                        raise ValueError()
                    context = record.get("context_capture")
                    if context is None:
                        continue
                    if not isinstance(context, dict) or not isinstance(context.get("media_variants", []), list):
                        issues.append(f"Post {record.get('id')}: variant capture receipts have an invalid format.")
                        continue
                    for receipt in context.get("media_variants", []):
                        if not isinstance(receipt, dict):
                            issues.append(f"Post {record.get('id')}: a variant capture receipt is invalid.")
                            continue
                        state = receipt.get("state")
                        states[state] += 1
                        if state in {"downloaded", "reused"}:
                            check_receipt(receipt, f"Post {record.get('id')}")
                        elif state not in _NO_FILE_STATES:
                            limitations.append(f"Post {record.get('id')}: a variant has an unrecognized capture state; no download was assumed.")
            except (OSError, ValueError, UnicodeError):
                issues.append("The message index could not be read for variant capture receipts.")
        for state, explanation in (("disabled", "were inventoried with downloads disabled"),
                                   ("metadata_only", "contain metadata without downloadable bytes"),
                                   ("failed", "have unsuccessful downloads")):
            if states[state]:
                limitations.append(f"{states[state]} media variant(s) {explanation}; their bytes are not claimed as archived.")
    except OperationCancelled:
        result["cancelled"] = True
        issues.append("Source and variant verification was stopped before all checks finished.")
    except (StoreError, OSError, ValueError, TypeError) as exc:
        issues.append(f"Source and variant verification could not finish: {exc}")
    result["issues"] = list(dict.fromkeys(issues))
    result["limitations"] = list(dict.fromkeys(limitations))
    result["ok"] = not result["issues"]
    return result
