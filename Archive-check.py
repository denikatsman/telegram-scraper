"""Compatibility entry point. See main.py --help for the shared application."""

import sys
from telegram_scraper.cli import main

if __name__ == "__main__":
    raise SystemExit(main(["verify", *sys.argv[1:]]))
