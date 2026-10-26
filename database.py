"""Compatibility module for existing database commands and imports."""

from rosapenna_backend.database import DatabaseUnavailable, MongoSettings, connect_database, main

__all__ = ["DatabaseUnavailable", "MongoSettings", "connect_database", "main"]

if __name__ == "__main__":
    raise SystemExit(main())
