"""Backend-owned MongoDB connection; credentials never enter API responses."""

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from pymongo import MongoClient
from pymongo.errors import PyMongoError
from pymongo.write_concern import WriteConcern


class MongoSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MONGODB_", env_file=".env", extra="ignore", hide_input_in_errors=True,
    )

    uri: SecretStr = SecretStr("")
    database: str | None = None
    timeout_ms: int = Field(5000, ge=1000, le=60000)


class DatabaseUnavailable(RuntimeError):
    pass


def connect_database(settings: MongoSettings | None = None):
    """One pooled, thread-safe client per backend process, created at startup."""
    settings = settings or MongoSettings()
    uri = settings.uri.get_secret_value().strip()
    if not uri.startswith(("mongodb://", "mongodb+srv://")):
        raise DatabaseUnavailable("Set a valid MONGODB_URI in the backend environment.")
    client = None
    try:
        client = MongoClient(
            uri, appname="rosapenna-backend", tz_aware=True, maxPoolSize=20,
            serverSelectionTimeoutMS=settings.timeout_ms,
            connectTimeoutMS=settings.timeout_ms, timeoutMS=settings.timeout_ms,
        )
        name = settings.database or client.get_default_database(default="rosapenna").name
        db = client.get_database(name, write_concern=WriteConcern(w="majority"))
        db.command("ping")
        return client, db
    except (PyMongoError, ValueError):
        if client is not None:
            client.close()
        # Driver diagnostics can contain connection details. Keep public errors safe.
        raise DatabaseUnavailable(
            "MongoDB connection failed. Check the backend URI, database access, and network allowlist."
        ) from None


def main():
    try:
        client, _ = connect_database()
    except DatabaseUnavailable as exc:
        print(str(exc))
        return 1
    client.close()
    print("MongoDB connection is ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
