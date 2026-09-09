#!/bin/zsh
set -eu
cd -- "${0:A:h}"
if [[ -x .venv/bin/python ]]; then
  exec .venv/bin/python main.py
else
  exec python3 main.py
fi
