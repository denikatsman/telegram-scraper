# Working on telegram-scraper

## Completed app updates

Follow [APP_DELIVERY.md](APP_DELIVERY.md). After every completed app change,
run `/usr/local/opt/python@3.14/bin/python3.14 tools/build_macos_app.py` and verify the installed build before reporting completion.
The user reviews updates through `~/Applications/telegram-scraper.app`.
Preserve previous bundles and user data; report any required restart or
signing/installation blocker. A compile alone is not a delivered update.
