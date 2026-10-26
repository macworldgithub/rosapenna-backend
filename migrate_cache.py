"""Compatibility command for importing the legacy SQLite cache."""

from rosapenna_backend.scripts.migrate_cache import main

if __name__ == "__main__":
    raise SystemExit(main())
