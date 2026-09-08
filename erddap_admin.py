#!/usr/bin/env python3
"""Compatibility entry point for the reviewed metadata administration workflow.

Run --help for scan, review, apply, rollback and reload commands.
Legacy SSH/database commands are intentionally no longer accepted.
"""
from erddap_admin_rename import main

if __name__ == "__main__":
    raise SystemExit(main())
