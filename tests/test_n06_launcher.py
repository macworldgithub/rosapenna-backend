"""Verify the simulation child's network guard without making network calls."""

import os
from pathlib import Path
import tempfile
import unittest

from scripts.run_n06_simulation import SITE_CUSTOMIZE, _verify_sitecustomize_guard


class SimulationLauncherSafetyTests(unittest.TestCase):
    def test_registered_guard_denies_synthetic_external_connect(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            (run_dir / "sitecustomize.py").write_text(SITE_CUSTOMIZE, encoding="utf-8")
            env = dict(os.environ, PYTHONPATH=str(run_dir), PYTHONDONTWRITEBYTECODE="1")
            _verify_sitecustomize_guard(run_dir, env)

    def test_missing_guard_stops_before_startup(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            env = dict(os.environ, PYTHONPATH=str(run_dir), PYTHONDONTWRITEBYTECODE="1")
            with self.assertRaisesRegex(RuntimeError, "audit hook self-check failed"):
                _verify_sitecustomize_guard(run_dir, env)


if __name__ == "__main__":
    unittest.main()
