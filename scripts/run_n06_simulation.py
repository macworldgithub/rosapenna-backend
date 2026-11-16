"""Safe local launcher for the N06 simulation journey.

Starts an isolated backend process on 127.0.0.1 with a fresh run identity, a
fresh signing secret, a dedicated local MongoDB database, and no inherited live
provider credentials. This launcher starts the backend only. The website
process has not yet been isolated and must not be used for an N06 journey.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

LOCAL_MONGO_URI = "mongodb://127.0.0.1:27017"
SAFE_ENV_KEYS = {
    "SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "COMSPEC", "TEMP", "TMP",
    "USERPROFILE", "LOCALAPPDATA", "APPDATA", "HOMEDRIVE", "HOMEPATH",
    "VIRTUAL_ENV",
}

SITE_CUSTOMIZE = '''"""Loopback-only network policy for the N06 simulation child process."""
import os
import sys

ALLOWED = {"", "localhost", "127.0.0.1", "::1"}


def _allowed(host):
    return host is None or str(host).strip().lower() in ALLOWED


def _guard(event, args):
    host = None
    if event == "socket.connect":
        address = args[1] if len(args) > 1 else None
        host = address[0] if isinstance(address, tuple) and address else address
    elif event in {"socket.sendto", "socket.sendmsg"}:
        address = args[1] if len(args) > 1 else None
        host = address[0] if isinstance(address, tuple) and address else address
    elif event in {"subprocess.Popen", "os.system", "os.spawn", "os.exec"}:
        raise PermissionError("N06 simulation backend cannot start child processes")
    elif event in {"socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyname_ex"}:
        host = args[0] if args else None
    elif event == "http.client.connect":
        host = args[1] if len(args) > 1 else None
    else:
        return
    if not _allowed(host):
        sys.stderr.write(f"N06 network policy denied {event} to {host}\\n")
        raise PermissionError(f"N06 network policy denied {event} to {host}")

sys.addaudithook(_guard)
'''


def _verify_sitecustomize_guard(run_dir: Path, env: dict[str, str]) -> None:
    probe_code = (
        "import sys\n"
        "for event, args in (\n"
        "    ('socket.connect', (None, ('blocked.invalid.domain', 80))),\n"
        "    ('socket.sendto', (None, ('blocked.invalid.domain', 80))),\n"
        "    ('socket.sendmsg', (None, ('blocked.invalid.domain', 80))),\n"
        "    ('subprocess.Popen', ('blocked-process', [], None, None)),\n"
        "    ('os.system', (b'blocked-process',)),\n"
        "):\n"
        "    try:\n"
        "        sys.audit(event, *args)\n"
        "    except PermissionError:\n"
        "        continue\n"
        "    sys.exit(2)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe_code],
        cwd=str(run_dir),
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Safety failure: sitecustomize audit hook self-check failed (returncode {result.returncode}). "
            f"stdout: {result.stdout.strip()} stderr: {result.stderr.strip()}"
        )


def _port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _choose_port(preferred: int) -> int:
    for candidate in range(preferred, preferred + 11):
        if _port_is_free(candidate):
            return candidate
    raise RuntimeError(f"No free local port between {preferred} and {preferred + 10}.")


def _mongo_reachable(host: str = "127.0.0.1", port: int = 27017, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _prepare_database(run_id: str, database_name: str) -> None:
    from pymongo import MongoClient

    client = MongoClient(LOCAL_MONGO_URI, serverSelectionTimeoutMS=3000, connectTimeoutMS=3000)
    try:
        client.admin.command("ping")
        db = client[database_name]
        existing = [name for name in db.list_collection_names() if not name.startswith("system.")]
        if existing:
            raise RuntimeError(
                f"Isolated database '{database_name}' is not empty. Refusing to reuse it."
            )
        db["n06_run_ownership"].insert_one({
            "run_id": run_id,
            "environment": "development",
            "is_test": True,
            "evidence_source": "simulation",
            "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        })
    finally:
        client.close()


def _drop_database(database_name: str, run_id: str) -> None:
    from pymongo import MongoClient

    client = MongoClient(LOCAL_MONGO_URI, serverSelectionTimeoutMS=3000, connectTimeoutMS=3000)
    try:
        db = client[database_name]
        marker = db["n06_run_ownership"].find_one({"run_id": run_id})
        if not marker:
            print(f"Run ownership marker missing; keeping database '{database_name}'.")
            return
        client.drop_database(database_name)
        print(f"Removed isolated database '{database_name}'.")
    finally:
        client.close()


def _wait_for_health(backend_url: str, api_key: str, timeout: float = 45.0) -> bool:
    deadline = time.monotonic() + timeout
    request = urllib.request.Request(
        f"{backend_url}/health/ready",
        headers={"X-API-Key": api_key},
    )
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                if response.status == 200:
                    return True
        except (urllib.error.URLError, TimeoutError, OSError):
            pass
        time.sleep(0.5)
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Start the isolated N06 simulation backend.")
    parser.add_argument("--port", type=int, default=4040, help="Preferred local backend port (default 4040).")
    parser.add_argument("--frontend-port", type=int, default=3030, help="Frontend dev port to print (default 3030).")
    parser.add_argument("--check", action="store_true", help="Start, verify health, then stop and clean up.")
    parser.add_argument("--keep-data", action="store_true", help="Keep the isolated database after shutdown.")
    args = parser.parse_args()

    if not _mongo_reachable():
        print("ERROR: local MongoDB is not reachable at 127.0.0.1:27017.")
        print("Start a local MongoDB service first. No shared or cloud database is used.")
        return 1

    backend_root = Path(__file__).resolve().parents[1]
    run_id = f"n06-{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(4)}"
    database_name = f"rosapenna_n06_{run_id.lower().replace('-', '_')}"
    signing_secret = secrets.token_urlsafe(48)
    api_key = secrets.token_urlsafe(32)
    staff_key = secrets.token_urlsafe(32)
    port = _choose_port(args.port)
    backend_url = f"http://127.0.0.1:{port}"

    run_dir = Path(tempfile.mkdtemp(prefix="rosapenna-n06-sim-"))
    (run_dir / "sitecustomize.py").write_text(SITE_CUSTOMIZE, encoding="utf-8")
    env = {key: value for key, value in os.environ.items() if key.upper() in SAFE_ENV_KEYS}
    env.update({
        "BOOKING_ENVIRONMENT": "development",
        "BOOKING_PAYMENT_MODE": "simulation",
        "BOOKING_SIMULATION_ENABLED": "true",
        "BOOKING_SIMULATION_SIGNING_SECRET": signing_secret,
        "BOOKING_SIMULATION_RUN_ID": run_id,
        "BOOKING_HOST": "127.0.0.1",
        "BOOKING_PORT": str(port),
        "BOOKING_API_KEY": api_key,
        "STAFF_API_KEY": staff_key,
        "BOOKING_SECURITY_BYPASS_IN_DEV": "false",
        "BOOKING_CORS_ORIGINS": f"http://127.0.0.1:{args.frontend_port}",
        "BOOKING_DOCS_ENABLED": "true",
        "BOOKING_TEESHEET_REFRESH_ENABLED": "false",
        "BOOKING_USE_VIRTUAL_DISPLAY": "false",
        "BOOKING_ENABLE_VNC": "false",
        "BOOKING_LOG_LEVEL": "INFO",
        "PROMMT_ALLOW_LIVE_CALLS": "false",
        "OPERA_ALLOW_LIVE_WRITES": "false",
        "BRS_ALLOW_LIVE_WRITES": "false",
        "EMAIL_ALLOW_LIVE_SEND": "false",
        "MONGODB_URI": LOCAL_MONGO_URI,
        "MONGODB_DATABASE": database_name,
        "MONGODB_TIMEOUT_MS": "5000",
        "PYTHONPATH": f"{run_dir}{os.pathsep}{backend_root}",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
    })

    _verify_sitecustomize_guard(run_dir, env)
    _prepare_database(run_id, database_name)

    child_code = (
        "import uvicorn;"
        "from rosapenna_backend.main import app;"
        f"uvicorn.run(app, host='127.0.0.1', port={port}, log_level='info')"
    )

    print("=" * 72)
    print("  SIMULATION - NO REAL PAYMENT OR BOOKING")
    print("=" * 72)
    print(f"Run ID        : {run_id}")
    print(f"Backend       : {backend_url}")
    print(f"API docs      : {backend_url}/docs")
    print("Frontend      : not started; process isolation is not yet verified")
    print(f"Local MongoDB : {LOCAL_MONGO_URI} / {database_name} (isolated, owned by this run)")
    print("The backend does not inherit live provider or cloud database settings.")
    print()

    process = subprocess.Popen(
        [sys.executable, "-c", child_code],
        cwd=str(run_dir),
        env=env,
        stdout=None,
        stderr=None,
    )

    try:
        if not _wait_for_health(backend_url, api_key):
            print("ERROR: backend did not report healthy within 45 seconds.")
            return 1

        print("Backend health check: OK")
        print()
        print("Website isolation is incomplete. Do not use this launcher for a browser journey yet.")
        print("Press Ctrl+C to stop the backend and remove the isolated database.")
        print()

        if args.check:
            print("Check mode: startup verified; stopping now.")
            return 0

        while process.poll() is None:
            time.sleep(1)
        print("Backend process exited unexpectedly.")
        return 1
    except KeyboardInterrupt:
        print("Stopping simulation backend...")
        return 0
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if not args.keep_data:
            _drop_database(database_name, run_id)


if __name__ == "__main__":
    raise SystemExit(main())
