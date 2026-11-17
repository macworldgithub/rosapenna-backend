# Rosapenna Booking API (Backend)

FastAPI service that manages package pricing, golf tee-sheet caching, payment workflows, and reservation synchronization across Opera Cloud and BRS Golf.

---

## Features

- **Stay & Play Quotes**: Server-verified package pricing and quote calculations.
- **Tee-Sheet Automation**: Daily background caching and live availability checks via BRS Golf.
- **Deposit & Payment Pipeline**: Deposit link generation and webhook verification (Prommt & simulation).
- **Vendor Synchronization**: Coordinated writes between Opera PMS (hotel) and BRS Golf (tee times).
- **Data Persistence**: MongoDB-backed records for quotes, booking intents, operations, and audit trails.

---

## Prerequisites

- **Python**: 3.13 or newer
- **uv**: Package installer and environment manager (`https://docs.astral.sh/uv/`)
- **MongoDB**: Running instance (local or MongoDB Atlas)
- **Chromium**: Required for Playwright automation

---

## Installation

Do not overwrite an existing `.env` file. It may contain live credentials. The N06 website simulation is not ready for browser testing.

Run the following commands from the `rosapenna-backend` directory:

```bash
# 1. Install project dependencies
uv sync

# 2. Configure environment variables
test -e .env || cp .example.env .env

# 3. Install Playwright browser dependencies
uv run playwright install chromium

# 4. Test MongoDB connection
uv run python -m database
```

---

## Environment Variables

Edit `.env` to configure your local settings:

| Variable | Description | Default / Example |
|---|---|---|
| `BOOKING_ENVIRONMENT` | Environment mode (`development` or `production`) | `development` |
| `BOOKING_API_KEY` | Secret key for protected API endpoints | Random string (12+ chars) |
| `BOOKING_HOST` | Host address | `0.0.0.0` |
| `BOOKING_PORT` | Port number | `4040` |
| `MONGODB_URI` | MongoDB connection string | `mongodb://localhost:27017` |
| `MONGODB_DATABASE` | Target database name | `rosapenna` |

---

## Running the API

### Development Mode (with hot-reload)

```bash
uv run uvicorn app:app --reload --host 127.0.0.1 --port 4040 --workers 1
```

### Standard Run

```bash
uv run uvicorn app:app --host 127.0.0.1 --port 4040 --workers 1
```

Once running, access the service:
- **API Base:** `http://127.0.0.1:4040`
- **Swagger UI:** `http://127.0.0.1:4040/docs`
- **Readiness Probe:** `http://127.0.0.1:4040/ready`

---

## Running Tests

```bash
# Run the isolated N06 offline tests
uv run --offline --frozen --no-sync python scripts/run_n06_tests.py

# Run the isolated N07 booking confirmation offline tests and preview generator
uv run python scripts/run_n07_tests.py
uv run python scripts/run_n07_tests.py --preview

# Run the isolated N08 pre-arrival scheduling offline tests
uv run python scripts/run_n08_tests.py

# Run the N08 pre-arrival batch worker (dry-run inspection)
uv run python scripts/run_n08_worker.py --once --dry-run
```

