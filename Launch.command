#!/bin/zsh
set -eu
cd -- "${0:A:h}"
if [[ -x .venv/bin/python ]]; then
  exec .venv/bin/python main.py serve --port 0
else
  exec python3 main.py serve --port 0
fi
