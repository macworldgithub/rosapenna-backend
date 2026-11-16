"""Run the audited N06 offline test suite under a local-only network policy.

This runner must be launched from ``rosapenna-backend`` (or any directory) with
the project virtual environment. It refuses any outbound connection to a host
other than loopback before application modules are imported.
"""

from __future__ import annotations

import os
import secrets
import socket
import sys
import tempfile
import unittest
from pathlib import Path

_ALLOWED_HOSTS = {"", "localhost", "127.0.0.1", "::1"}
_DENIALS: list[str] = []


def _host_allowed(host: object) -> bool:
    if host is None:
        return True
    return str(host).strip().lower() in _ALLOWED_HOSTS


def _guard(event: str, args: tuple) -> None:
    host: object = None
    if event == "socket.connect":
        address = args[1] if len(args) > 1 else None
        host = address[0] if isinstance(address, tuple) and address else address
    elif event in {"socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyname_ex"}:
        host = args[0] if args else None
        if host is None:
            return
    elif event == "http.client.connect":
        host = args[1] if len(args) > 1 else None
    else:
        return

    if not _host_allowed(host):
        _DENIALS.append(f"{event} -> {host}")
        raise PermissionError(
            f"N06 offline policy blocked {event} to '{host}'. Only loopback is allowed."
        )


def _prepare_environment(backend_root: Path, run_dir: Path) -> str:
    run_id = f"n06-tests-{secrets.token_hex(8)}"
    signing_secret = secrets.token_urlsafe(48)
    safe_keys = {
        "SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "COMSPEC", "TEMP", "TMP",
        "USERPROFILE", "LOCALAPPDATA", "APPDATA", "HOMEDRIVE", "HOMEPATH",
        "PYTHONPATH", "VIRTUAL_ENV",
    }
    clean = {key: value for key, value in os.environ.items() if key.upper() in safe_keys}
    clean.update({
        "BOOKING_ENVIRONMENT": "test",
        "BOOKING_PAYMENT_MODE": "simulation",
        "BOOKING_SIMULATION_ENABLED": "true",
        "BOOKING_SIMULATION_SIGNING_SECRET": signing_secret,
        "BOOKING_SIMULATION_RUN_ID": run_id,
        "BOOKING_SECURITY_BYPASS_IN_DEV": "true",
        "BOOKING_TEESHEET_REFRESH_ENABLED": "false",
        "BOOKING_USE_VIRTUAL_DISPLAY": "false",
        "BOOKING_ENABLE_VNC": "false",
        "PROMMT_ALLOW_LIVE_CALLS": "false",
        "OPERA_ALLOW_LIVE_WRITES": "false",
        "BRS_ALLOW_LIVE_WRITES": "false",
        "EMAIL_ALLOW_LIVE_SEND": "false",
        "PYTHONPATH": str(backend_root),
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    os.environ.clear()
    os.environ.update(clean)
    os.chdir(run_dir)
    if str(backend_root) not in sys.path:
        sys.path.insert(0, str(backend_root))
    return run_id


def main() -> int:
    backend_root = Path(__file__).resolve().parents[1]
    run_dir = Path(tempfile.mkdtemp(prefix="rosapenna-n06-tests-"))
    run_id = _prepare_environment(backend_root, run_dir)

    # Install the policy before importing any application or test module.
    sys.addaudithook(_guard)

    # Prove denial via synthetic audit event before running tests
    probe_denied = False
    try:
        sys.audit("socket.connect", None, ("blocked.invalid.domain", 80))
    except PermissionError:
        probe_denied = True
    if not probe_denied:
        raise RuntimeError("Safety failure: audit hook did not block synthetic external connection probe.")
    _DENIALS.clear()

    print("N06 OFFLINE TEST RUN")
    print(f"  run id       : {run_id}")
    print(f"  isolated cwd : {run_dir}")
    print("  payment mode : simulation (no real payment)")
    print("  network      : loopback only; any external attempt fails the run")
    print()

    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for module in (
        "tests.test_n06_launcher",
        "tests.test_n06_simulation",
        "tests.test_n06_commit",
        "tests.test_n06_recovery",
        "tests.test_packages",
        "tests.test_booking_operations",
        "tests.test_mmb_m08",
    ):
        suite.addTests(loader.loadTestsFromName(module))

    result = unittest.TextTestRunner(verbosity=2).run(suite)

    print()
    print(f"Tests run: {result.testsRun}")
    print(f"Failures : {len(result.failures)}")
    print(f"Errors   : {len(result.errors)}")
    print(f"Skipped  : {len(result.skipped)}")
    print(f"Blocked external network attempts: {len(_DENIALS)}")
    for denial in _DENIALS:
        print(f"  DENIED {denial}")

    return 1 if (result.failures or result.errors or _DENIALS) else 0


if __name__ == "__main__":
    raise SystemExit(main())
