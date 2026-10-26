"""Paths for local automation diagnostics."""

from pathlib import Path

TEMP_DIR = Path(__file__).resolve().parents[2] / ".temp"


def temp_output_path(filename: str) -> Path:
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    return TEMP_DIR / filename
