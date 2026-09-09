"""Build a minimal macOS app around the local archive UI. No personal data copied."""

from pathlib import Path
import argparse
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import uuid


def build(output, library):
    source = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(source))
    from telegram_scraper import __version__
    if sys.platform != "darwin":
        raise SystemExit("The macOS app must be built on a Mac with Xcode command-line tools.")
    if sys.version_info < (3, 10):
        raise SystemExit("Python 3.10 or newer is required.")
    # This local wrapper uses the chosen Python installation. Verify that it
    # can provide the login/scraping dependency before presenting a usable app.
    import telethon  # noqa: F401
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    library = library.expanduser().resolve()
    library.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".channel-archive-build-", dir=output.parent) as temporary:
        temporary = Path(temporary)
        bundle = temporary / "Channel Archive.app"
        contents = bundle / "Contents"
        executable = contents / "MacOS" / "ChannelArchive"
        resources = contents / "Resources"
        executable.parent.mkdir(parents=True)
        resources.mkdir()
        subprocess.run(["xcrun", "swiftc", "-swift-version", "5", "-O", "-target",
                        f"{os.uname().machine}-apple-macos12.0", str(source / "macos/ChannelArchive.swift"),
                        "-o", str(executable)], check=True)
        package = resources / "app" / "telegram_scraper"
        for path in (source / "telegram_scraper").rglob("*"):
            if path.is_file() and path.suffix in {".py", ".css", ".js", ".html"} and "__pycache__" not in path.parts:
                target = package / path.relative_to(source / "telegram_scraper")
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
        # Preserve a virtual environment's interpreter path. Resolving its
        # symlink would select the base Python and lose installed dependencies.
        bookmark = subprocess.run([str(executable), "--bookmark-library", str(library)],
                                  check=True, capture_output=True, text=True).stdout.strip()
        (resources / "launcher.json").write_text(json.dumps({"python": os.path.abspath(sys.executable),
            "library": str(library), "libraryBookmark": bookmark}, indent=2))
        shutil.copy2(source / "LICENSE", resources / "LICENSE")
        info = {"CFBundleExecutable": "ChannelArchive", "CFBundleIdentifier": "local.channelarchive.desktop",
                "CFBundleName": "Channel Archive", "CFBundleDisplayName": "Channel Archive",
                "CFBundlePackageType": "APPL", "CFBundleShortVersionString": __version__,
                "CFBundleVersion": __version__, "LSMinimumSystemVersion": "12.0",
                "NSHighResolutionCapable": True, "CFBundleIconFile": "AppIcon",
                "CFBundleURLTypes": [{"CFBundleURLName": "Open Channel Archive", "CFBundleTypeRole": "Viewer",
                                      "CFBundleURLSchemes": ["channel-archive"]}],
                "NSAppTransportSecurity": {"NSAllowsArbitraryLoadsInWebContent": True, "NSAllowsLocalNetworking": True}}
        (contents / "Info.plist").write_bytes(plistlib.dumps(info))
        iconset = temporary / "AppIcon.iconset"
        iconset.mkdir()
        original = iconset / "icon_512x512@2x.png"
        subprocess.run([str(executable), "--render-icon", str(original)], check=True)
        for size in (16, 32, 128, 256, 512):
            for scale in (1, 2):
                target = iconset / f"icon_{size}x{size}{'@2x' if scale == 2 else ''}.png"
                if target == original:
                    continue
                subprocess.run(["sips", "-z", str(size * scale), str(size * scale), str(original), "--out", str(target)], check=True, stdout=subprocess.DEVNULL)
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(resources / "AppIcon.icns")], check=True)
        subprocess.run(["codesign", "--force", "--sign", "-", str(bundle)], check=True)
        subprocess.run(["codesign", "--verify", "--deep", "--strict", str(bundle)], check=True)
        if output.exists():
            backup = output.with_name(f"{output.stem}-previous-{uuid.uuid4().hex[:8]}.app")
            output.rename(backup)
            print(f"Previous app kept at {backup}")
        bundle.rename(output)
    print(f"Built {output}")
    print("This local build uses this Mac's Python installation and the selected library folder.")
    return output


def install_shortcut(bundle):
    """Keep the actual app inside the project; Applications holds a Finder alias."""
    source = Path(__file__).resolve().parents[1]
    shortcut = Path.home() / "Applications" / "Channel Archive.app"
    shortcut.parent.mkdir(parents=True, exist_ok=True)
    if bundle == shortcut:
        raise SystemExit("Build the app inside the project before installing its shortcut.")
    temporary = shortcut.with_name(f".channel-archive-shortcut-{uuid.uuid4().hex}.alias")
    try:
        executable = bundle / "Contents" / "MacOS" / "ChannelArchive"
        subprocess.run([str(executable), "--make-alias", str(bundle), str(temporary)], check=True)
        if shortcut.exists() or shortcut.is_symlink():
            backups = source / "work" / "app-backups"
            backups.mkdir(parents=True, exist_ok=True)
            shortcut.rename(backups / f"Channel Archive-previous-{uuid.uuid4().hex[:8]}.app")
        temporary.rename(shortcut)
    finally:
        temporary.unlink(missing_ok=True)
    subprocess.run(["/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister",
                    "-f", str(bundle)], check=True)
    print(f"Applications shortcut: {shortcut}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[1] / "Channel Archive.app")
    parser.add_argument("--data-dir", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--install-shortcut", action="store_true", help="add or refresh an Applications shortcut to this project’s app")
    args = parser.parse_args()
    bundle = build(args.output, args.data_dir)
    if args.install_shortcut:
        install_shortcut(bundle)
