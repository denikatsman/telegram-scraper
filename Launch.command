#!/bin/zsh
set -eu
cd -- "${0:A:h}"
if [[ -d 'telegram-scraper.app' ]]; then
  exec /usr/bin/open -a "$PWD/telegram-scraper.app"
fi
if [[ -x .venv/bin/python ]]; then
  exec .venv/bin/python main.py --data-dir "$PWD" serve --port 0
else
  exec python3 main.py --data-dir "$PWD" serve --port 0
fi
