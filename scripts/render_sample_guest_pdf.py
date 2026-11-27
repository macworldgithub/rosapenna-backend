"""Render a synthetic guest booking-confirmation PDF for visual review.

Run from ``rosapenna-backend`` with the project virtual environment::

    .venv/Scripts/python.exe scripts/render_sample_guest_pdf.py [--variant typical|stress|real] [--out PATH]

The script uses only synthetic data (the N07 test fixture). It makes no network, email,
payment, OPERA or BRS call. The default output is ``output/guest-confirmation-sample.pdf``
in the repository root.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from rosapenna_backend.api.schemas.communications import BookingConfirmationSnapshot  # noqa: E402
from rosapenna_backend.services.pdf_itinerary import build_n07_confirmation_pdf  # noqa: E402
from tests.test_booking_confirmations import make_test_snapshot  # noqa: E402

DEFAULT_OUT = BACKEND_ROOT.parent / "output" / "guest-confirmation-sample.pdf"
COURSES = ("Old Tom Morris Links", "Sandy Hills Links", "St Patrick's Links")
TEE_TIMES = ("09:10", "10:30", "13:40", "08:20", "11:50", "15:00")


def build_sample(variant: str) -> BookingConfirmationSnapshot:
    """Build a valid synthetic snapshot. ``typical`` is 2 nights and 2 rounds."""
    data = make_test_snapshot(script_injection_name="Seán").model_dump(mode="python")
    data["lead_guest"].update(first_name="Seán", last_name="Ó Briain", email="sean.obriain@example.invalid")
    data["stay"].update(arrival_date="2099-06-01", departure_date="2099-06-03")
    names = ["Seán Ó Briain", "Aoife Ní Bhriain", "Pádraig Murphy", "Niamh Kelly"]
    data["stay"]["rooms"] = [{
        "room_group_id": "RG-1", "room_category": "deluxe_twin", "room_quantity": 2, "occupant_names": names,
    }]

    rounds = 2
    if variant == "stress":
        rounds = 6
        data["stay"]["departure_date"] = "2099-06-07"
        data["lead_guest"].update(
            first_name="Wolfeschlegelsteinhausenbergerdorff-Ó Súilleabháin",
            last_name="Müller-Østergaard Ñandú Þórðarson-Fitzgerald-Montgomery",
        )
        names = [f"Alexandria Konstantinopoulos-Wolfeschlegel {n}" for n in range(1, 5)]
        data["stay"]["rooms"][0]["occupant_names"] = names
    data["golf_rounds"] = [{
        "round_key": f"RND-{n + 1}",
        "course_name": COURSES[n % 3] if variant != "stress" else f"{COURSES[n % 3]} (Championship Tees, Back Nine First)",
        "date": f"2099-06-{2 + n:02d}",
        "tee_time": TEE_TIMES[n],
        "timezone": "Europe/Dublin",
        "players": 4,
        "holes": 18,
        "provider_booking_ids": [f"BRS-SAMPLE-{n + 1}"],
        "golfer_names": names,
    } for n in range(rounds)]

    if variant == "real":
        data.update(environment="production", is_test=False, simulation_run_id=None)
        data["content"]["policy_text"] = None  # exercises the marked terms placeholder
    return BookingConfirmationSnapshot.model_validate(data)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--variant", choices=("typical", "stress", "real"), default="typical")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()

    pdf_bytes = build_n07_confirmation_pdf(build_sample(args.variant))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_bytes(pdf_bytes)
    print(f"Wrote {args.out} ({len(pdf_bytes)} bytes, variant={args.variant})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
