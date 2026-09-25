# Local app delivery

The daily entry is `~/Applications/telegram-scraper.app`. After completing and validating an
app change, run the command below from this repository. Do not report the update
as delivered until installation and the exact-build check succeed.

```sh
/usr/local/opt/python@3.14/bin/python3.14 tools/build_macos_app.py
python3 tools/install-app.py "telegram-scraper.app" --name "telegram-scraper" --bundle-id local.telegram-scraper.desktop --check
```

Use the actual new output path for the check when the build takes an explicit
destination. A successful signed build installs automatically. Build failures
leave the installed app intact. `MY_UTILITIES_AUTO_INSTALL=0` is only for isolated
verification or an intentionally unfinished preview; it does not deliver an update.

All participating repositories carry an identical `install-app.py`. It verifies
bundle identity and signing team, stages and verifies a complete copy, swaps it
atomically, and retains the previous bundle under
`~/Library/Application Support/My Utilities/Updates`. Receipts under `Installed`
record the source build, signature, repository commit and installation time.
Receipts establish build-copy equality; they do not certify tests or unbuilt code.
Do not add extra app copies or shortcuts to disposable build folders.

Never stop a running app automatically during installation. If the installer
reports running processes, tell the user to save, quit and reopen from Applications.
Rebuilding does not replace code already loaded in a running process.
Never change app data, bookmarks, bundle identity, entitlements or signing team
as part of a convenience-launcher change.

The standard source and cross-repository comparison are in
`/Users/xxx/Documents/ChatGPT/Merging and Cleaning Up/launcher-audit`.
