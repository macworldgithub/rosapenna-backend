"""Run the audited N07 and affected offline regression suites under a local-only network policy.

This runner must be launched from ``rosapenna-backend`` (or any directory) with
the project virtual environment. It enforces loopback-only network isolation,
cleans environment settings, runs N07 and affected regression suites, and
optionally generates synthetic preview artifacts (--preview).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import secrets
import shutil
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
        raise PermissionError("N07 offline policy blocked process launch")
    else:
        return

    if not _host_allowed(host):
        _DENIALS.append(f"{event} -> {host}")
        raise PermissionError(
            f"N07 offline policy blocked {event} to '{host}'. Only loopback is allowed."
        )


def _prepare_environment(backend_root: Path, run_dir: Path, recording_dir: Path) -> str:
    run_id = f"n07-tests-{secrets.token_hex(8)}"
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


def generate_preview_artifacts(recording_dir: Path, run_id: str) -> None:
    """Generate synthetic guest.eml, hotel.eml, itinerary.pdf, and redacted status.json."""
    from rosapenna_backend.services.booking_confirmations import (
        BookingConfirmationCoordinator,
        ConfirmationRuntimeContext,
    )
    from rosapenna_backend.services.email_delivery import RecordingConfirmationTransport
    from tests.test_booking_confirmations import make_test_confirmed_event, make_test_operation_doc

    preview_dir = recording_dir / "preview"
    preview_dir.mkdir(parents=True, exist_ok=True)

    transport = RecordingConfirmationTransport(recording_dir=preview_dir)
    coord = BookingConfirmationCoordinator(
        transport=transport,
        allow_in_memory_fixtures=True,
        runtime_context=ConfirmationRuntimeContext(
            mode="recording", environment="test", run_id=run_id, recording_dir=str(preview_dir)
        ),
    )

    op_id = f"OP-PREVIEW-{run_id[:8]}"
    event = make_test_confirmed_event(op_id=op_id)
    coord._in_memory_records[op_id] = make_test_operation_doc(event)

    # 1. Prepare
    rec = coord.prepare_confirmation(op_id)

    # 2. Save itinerary.pdf
    pdf_path = preview_dir / "itinerary.pdf"
    if rec.pdf_bytes:
        pdf_path.write_bytes(rec.pdf_bytes)

    # 3. Dispatch guest and hotel through the same entry point a later runner will use
    coord.process_confirmed_operation(op_id)

    # Rename emitted .eml files to stable guest.eml and hotel.eml for preview inspection
    guest_address = event.snapshot.lead_guest.email.strip().lower()
    for item in transport.recorded_submissions:
        name = "guest.eml" if item["recipient"] == guest_address else "hotel.eml"
        shutil.copyfile(item["file_path"], preview_dir / name)

    # 5. Save redacted status.json
    status = coord.get_status(op_id)
    status_path = preview_dir / "status.json"
    status_path.write_text(json.dumps(status.model_dump(mode="json"), indent=2))

    print("PREVIEW ARTIFACTS CREATED:")
    print(f"  Directory: {preview_dir}")
    print(f"  - itinerary.pdf : {pdf_path.stat().st_size} bytes")
    print(f"  - guest.eml     : {(preview_dir / 'guest.eml').stat().st_size} bytes")
    print(f"  - hotel.eml     : {(preview_dir / 'hotel.eml').stat().st_size} bytes")
    print(f"  - status.json   : {status_path.stat().st_size} bytes")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run audited N07 offline test suite and preview generator.")
    parser.add_argument("--preview", action="store_true", help="Generate synthetic preview artifacts for inspection.")
    args = parser.parse_args()

    backend_root = Path(__file__).resolve().parents[1]
    run_dir = Path(tempfile.mkdtemp(prefix="rosapenna-n07-tests-"))
    recording_dir = run_dir / "recordings"
    run_id = _prepare_environment(backend_root, run_dir, recording_dir)

    # Install loopback-only network guard
    sys.addaudithook(_guard)

    # Probe each denial path using synthetic audit events only.
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

    print("N07 OFFLINE TEST RUN")
    print(f"  run id       : {run_id}")
    print(f"  isolated cwd : {run_dir}")
    print("  mode         : recording (no real mail sent)")
    print("  network      : loopback only; any external attempt fails the run")
    print()

    if args.preview:
        generate_preview_artifacts(recording_dir, run_id)

    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for module in (
        "tests.test_booking_confirmations",
        "tests.test_booking_confirmations_api",
        "tests.test_communications",
        "tests.test_booking_operations",
        "tests.test_n06_commit",
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
