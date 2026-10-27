#!/usr/bin/env python3
"""Compatibility entrypoint for `magpie_accounts.py inspect`; see README.md."""

import sys
from magpie_accounts import main


if __name__ == "__main__":
    sys.exit(main(["inspect", *sys.argv[1:]]))
