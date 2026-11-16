"""Run the audited N08 pre-arrival test suite and regression suites under a local-only network policy.

This runner enforces loopback-only network isolation, sets up a clean test environment,
and executes all N08 and affected regression test suites with zero external network leakage.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
from pathlib import Path
import secrets
import sys
import tempfile
import unittest

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
    elif event == "socket.sendto":
        address = args[-1] if args else None
        host = address[0] if isinstance(address, tuple) and address else address
    elif event in {"socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyname_ex"}:
        host = args[0] if args else None
        if host is None:
            return
    elif event in {"socket.gethostbyaddr", "socket.getnameinfo"}:
        address = args[0] if args else None
        host = address[0] if event == "socket.getnameinfo" and isinstance(address, tuple) else address
    elif event == "http.client.connect":
        host = args[1] if len(args) > 1 else None
    elif event in {"subprocess.Popen", "os.system", "os.exec", "os.spawn"}:
        _DENIALS.append(f"{event} -> process launch")
        raise PermissionError("N08 offline policy blocked process launch")
    else:
        return

    if not _host_allowed(host):
        _DENIALS.append(f"{event} -> {host}")
        raise PermissionError(
            f"N08 offline policy blocked {event} to '{host}'. Only loopback is allowed."
        )


def _prepare_environment(backend_root: Path, run_dir: Path, recording_dir: Path) -> str:
    run_id = f"n08-tests-{secrets.token_hex(8)}"
    api_key = secrets.token_hex(16)
    staff_api_key = secrets.token_hex(16)

    safe_keys = {
        "SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "COMSPEC", "TEMP", "TMP",
        "USERPROFILE", "LOCALAPPDATA", "APPDATA", "HOMEDRIVE", "HOMEPATH",
        "PYTHONPATH", "VIRTUAL_ENV",
    }
    clean = {key: value for key, value in os.environ.items() if key.upper() in safe_keys}
    clean.update({
        "BOOKING_ENVIRONMENT": "test",
        "BOOKING_API_KEY": api_key,
        "BOOKING_STAFF_API_KEY": staff_api_key,
        "BOOKING_PRE_ARRIVAL_ENABLED": "true",
        "BOOKING_PRE_ARRIVAL_MODE": "recording",
        "BOOKING_PRE_ARRIVAL_TEST_RUN_ID": run_id,
        "BOOKING_PRE_ARRIVAL_RECORDING_DIR": str(recording_dir),
        "BOOKING_PRE_ARRIVAL_STAFF_API_ENABLED": "true",
        "BOOKING_CONFIRMATIONS_MODE": "recording",
        "BOOKING_CONFIRMATIONS_TEST_RUN_ID": run_id,
        "BOOKING_CONFIRMATIONS_RECORDING_DIR": str(recording_dir),
        "BOOKING_CONFIRMATIONS_STAFF_API_ENABLED": "true",
        "BOOKING_SECURITY_BYPASS_IN_DEV": "false",
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
    run_dir = Path(tempfile.mkdtemp(prefix="rosapenna-n08-tests-"))
    recording_dir = run_dir / "recordings"
    run_id = _prepare_environment(backend_root, run_dir, recording_dir)

    # Install loopback-only network guard
    sys.addaudithook(_guard)

    # Probe each denial path using synthetic audit events
    denial_probes = (
        ("socket.connect", (None, ("blocked.invalid.domain", 80))),
        ("socket.getaddrinfo", ("blocked.invalid.domain", 80)),
        ("socket.sendto", (None, b"synthetic", ("blocked.invalid.domain", 53))),
        ("subprocess.Popen", ("synthetic-command",)),
    )
    for event, probe_args in denial_probes:
        try:
            sys.audit(event, *probe_args)
        except PermissionError:
            continue
        raise RuntimeError(f"Safety failure: audit hook did not block synthetic {event} probe.")
    _DENIALS.clear()

    print("=" * 70)
    print("N08 OFFLINE TEST RUN — PRE-ARRIVAL SCHEDULING")
    print(f"  Run ID       : {run_id}")
    print(f"  Isolated CWD : {run_dir}")
    print("  Mode         : recording (no real mail sent)")
    print("  Network      : loopback only; any external attempt fails the run")
    print("=" * 70)
    print()

    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    modules = [
        "tests.test_pre_arrival",
        "tests.test_pre_arrival_storage",
        "tests.test_pre_arrival_api",
        "tests.test_mmb_m15",
        "tests.test_mmb_m16",
        "tests.test_booking_confirmations",
        "tests.test_booking_confirmations_api",
        "tests.test_communications",
        "tests.test_booking_operations",
        "tests.test_n06_commit",
    ]
    for module in modules:
        suite.addTests(loader.loadTestsFromName(module))

    result = unittest.TextTestRunner(verbosity=2).run(suite)

    print()
    print("-" * 70)
    print(f"Tests run: {result.testsRun}")
    print(f"Failures : {len(result.failures)}")
    print(f"Errors   : {len(result.errors)}")
    print(f"Skipped  : {len(result.skipped)}")
    print(f"Blocked external network attempts: {len(_DENIALS)}")
    for denial in _DENIALS:
        print(f"  DENIED {denial}")
    print("-" * 70)

    return 1 if (result.failures or result.errors or _DENIALS) else 0


if __name__ == "__main__":
    raise SystemExit(main())
