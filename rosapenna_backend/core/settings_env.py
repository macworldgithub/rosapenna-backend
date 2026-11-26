"""Shared dotenv location for all settings classes."""

import os

# The test package sets BOOKING_IGNORE_ENV_FILE=1 so local live credentials never reach test runs.
ENV_FILE = None if os.getenv("BOOKING_IGNORE_ENV_FILE") == "1" else ".env"
