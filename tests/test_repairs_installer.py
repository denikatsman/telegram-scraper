import errno
import importlib.util
import json
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import pytest

SPEC = importlib.util.spec_from_file_location("delivery", Path(__file__).resolve().parents[1] / "tools/install-app.py")
delivery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(delivery)
ID = "test.telegram.scraper.fixture"


@pytest.fixture(scope="module")
def bundles(tmp_path_factory):
    root = tmp_path_factory.mktemp("signed-installer-fixtures")
    result = []
    for version in ("old", "new"):
        app = root / (version + ".app")
        (app / "Contents/MacOS").mkdir(parents=True)
        (app / "Contents/Resources").mkdir()
        shutil.copy("/usr/bin/true", app / "Contents/MacOS/fixture")
        (app / "Contents/Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": ID, "CFBundleExecutable": "fixture", "CFBundlePackageType": "APPL"}))
        (app / "Contents/Resources/version").write_text(version)
        subprocess.run(["/usr/bin/codesign", "--force", "--sign", "-", str(app)], check=True, capture_output=True)
        result.append(app)
    return result


@pytest.mark.parametrize("boundary", ["update", "installed"])
def test_committed_receipt_failure_exit2_readonly_check_and_repair(tmp_path, bundles, monkeypatch, capsys, boundary):
    old, new = bundles
    first = delivery.install_app(old, "Fixture", ID, home=tmp_path)
    original = delivery.write_record
    def fail(path, record):
        if record["status"] == "installed" and ((boundary == "update" and path.name == "update.json") or (boundary == "installed" and path.parent.name == "Installed")):
            raise OSError(errno.ENOSPC, "synthetic full receipt volume")
        return original(path, record)
    monkeypatch.setattr(delivery, "write_record", fail)
    monkeypatch.setenv("MY_UTILITIES_HOME", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["install-app.py", str(new), "--name", "Fixture", "--bundle-id", ID])
    assert delivery.main() == 2
    assert "installed-metadata-incomplete" in capsys.readouterr().out
    installed = tmp_path / "Applications/Fixture.app"
    assert delivery.verify_bundle(installed, ID) == delivery.verify_bundle(new, ID)
    state = tmp_path / "Library/Application Support/My Utilities"
    previous = list((state / "Updates").glob("*/Fixture.app.previous"))
    assert len(previous) == 1 and delivery.verify_bundle(previous[0], ID) == first["signature"]
    before = {p: p.read_bytes() for p in state.rglob("*.json")}
    check = delivery.check_install(new, "Fixture", ID, home=tmp_path)
    assert check["bundleMatches"] and not check["receiptsComplete"]
    assert all(p.read_bytes() == data for p, data in before.items())
    monkeypatch.setattr(delivery, "write_record", original)
    monkeypatch.setattr(delivery, "rename_atomic", lambda *args, **kwargs: pytest.fail("Receipt retry must not swap again"))
    repaired = delivery.install_app(new, "Fixture", ID, home=tmp_path)
    assert repaired["status"] == "installed" and repaired["previousApp"] == str(previous[0])
    assert delivery.check_install(new, "Fixture", ID, home=tmp_path)["receiptsComplete"]


@pytest.mark.parametrize("status", ["installed", "unchanged", "linked-external"])
def test_legacy_successful_receipt_does_not_require_new_format_or_update(tmp_path, bundles, status):
    source = bundles[0]
    destination = tmp_path / "Applications/Fixture.app"
    destination.parent.mkdir()
    if status == "linked-external":
        destination.symlink_to(source)
    else:
        shutil.copytree(source, destination)
    state = tmp_path / "Library/Application Support/My Utilities"
    path = delivery.receipt_path(state, ID)
    path.parent.mkdir(parents=True)
    record = {"status": status, "installed": str(destination), "source": str(source), "bundleIdentifier": ID,
              "signature": delivery.verify_bundle(source, ID), "olderUnknownField": "preserve"}
    delivery.write_record(path, record)
    before = path.read_bytes()
    assert delivery.check_install(source, "Fixture", ID, home=tmp_path)["status"] == "verified"
    assert path.read_bytes() == before and not (state / "Updates").exists()


def test_unrelated_and_superseded_attempts_do_not_invalidate_current_build(tmp_path, bundles):
    record = delivery.install_app(bundles[1], "Fixture", ID, home=tmp_path)
    state = tmp_path / "Library/Application Support/My Utilities"
    for name, signature in [("unrelated", delivery.verify_bundle(bundles[0], ID)), ("superseded", record["signature"])]:
        path = state / "Updates" / name / "update.json"
        path.parent.mkdir()
        delivery.write_record(path, {**record, "attemptId": name, "timestamp": "2000-01-01", "signature": signature, "status": "preparing"})
    assert delivery.check_install(bundles[1], "Fixture", ID, home=tmp_path)["status"] == "verified"


def test_candidate_verification_failure_rolls_back_and_identity_mismatch_rejected(tmp_path, bundles, monkeypatch):
    old, new = bundles
    delivery.install_app(old, "Fixture", ID, home=tmp_path)
    destination = tmp_path / "Applications/Fixture.app"
    verify = delivery.verify_bundle
    def failed_candidate(path, expected):
        signature = verify(path, expected)
        if Path(path) == destination and signature == verify(new, ID):
            raise delivery.InstallError("synthetic post-swap verification failure")
        return signature
    monkeypatch.setattr(delivery, "verify_bundle", failed_candidate)
    with pytest.raises(delivery.InstallError, match="post-swap"):
        delivery.install_app(new, "Fixture", ID, home=tmp_path)
    assert verify(destination, ID) == verify(old, ID)
    with pytest.raises(delivery.InstallError, match="identity mismatch"):
        delivery.install_app(new, "Fixture", "wrong.identifier", home=tmp_path)


def test_signing_team_change_is_rejected_before_swap(tmp_path, bundles, monkeypatch):
    old, new = bundles
    delivery.install_app(old, "Fixture", ID, home=tmp_path)
    verify = delivery.verify_bundle
    def different_team(path, expected):
        value = verify(path, expected)
        return {**value, "TeamIdentifier": "different-team"} if Path(path) == new else value
    monkeypatch.setattr(delivery, "verify_bundle", different_team)
    with pytest.raises(delivery.InstallError, match="signing team"):
        delivery.install_app(new, "Fixture", ID, home=tmp_path)
    assert verify(tmp_path / "Applications/Fixture.app", ID) == verify(old, ID)


def test_post_swap_metadata_collection_failure_is_saved_and_repairable(tmp_path, bundles, monkeypatch):
    delivery.install_app(bundles[0], "Fixture", ID, home=tmp_path)
    original = delivery.running_pids
    monkeypatch.setattr(delivery, "running_pids", lambda app: (_ for _ in ()).throw(OSError("synthetic process lookup failure")))
    result = delivery.install_app(bundles[1], "Fixture", ID, home=tmp_path)
    assert result["status"] == "installed-metadata-incomplete"
    monkeypatch.setattr(delivery, "running_pids", original)
    assert not delivery.check_install(bundles[1], "Fixture", ID, home=tmp_path)["receiptsComplete"]
    monkeypatch.setattr(delivery, "rename_atomic", lambda *args, **kwargs: pytest.fail("Receipt retry must not swap again"))
    assert delivery.install_app(bundles[1], "Fixture", ID, home=tmp_path)["status"] == "installed"
    assert delivery.check_install(bundles[1], "Fixture", ID, home=tmp_path)["receiptsComplete"]
