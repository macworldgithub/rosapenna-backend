"""Backend unit tests."""

import os

# Runs before any test module imports the app: keep the developer .env out of tests.
os.environ["BOOKING_IGNORE_ENV_FILE"] = "1"
