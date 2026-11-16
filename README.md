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

---

## Ticket N08: Pre-Arrival Scheduling and Staff Recovery

Ticket N08 implements the offline pre-arrival scheduling, persistence, safe dispatch, and staff recovery interface without executing live guest emails or live Prommt transactions:
- **Pure Milestone Calculator**: Schedules `balance_due_first` and `rooming_and_caddy` using strict policies (`eight_weeks`, `six_weeks`, or `blocked_dependency` if unresolved).
- **Channel-Agnostic Inputs**: Normalizes confirmed online and phone/email booking records (via F05/M02 adapter) to current arrival dates and revisions; unconfirmed and cancelled bookings are strictly suppressed.
- **Durable Persistence & Atomic Locking**: Stores jobs in `scheduled_booking_reminders` collection with atomic `find_one_and_update` claims to prevent duplicate dispatch under concurrency.
- **Safe Dispatch Mode**: Default `recording_test` transport records synthetic communications to disk (`var/test_pre_arrival_recordings/`); live external email is disabled until explicit written activation.
- **Staff Recovery Endpoints**: Protected under `/staff/pre-arrival` (requires `BOOKING_PRE_ARRIVAL_STAFF_API_ENABLED=true`):
  - `GET /staff/pre-arrival/bookings/{booking_reference}/jobs`: inspect scheduled reminders.
  - `POST /staff/pre-arrival/bookings/{booking_reference}/reconcile`: manually reconcile schedule with current booking revision.
  - `POST /staff/pre-arrival/jobs/{reminder_id}/retry`: reset failed/stale job with required staff reason.
- **Guest Protection**: Mutation of reminders via guest endpoints (`POST /api/mmb/confirmations/{ref}/reminders/{reminder_id}/dispatch`) is strictly denied (HTTP 403 Forbidden).

---

## Ticket N07: Guest and Hotel Booking Confirmations (Structure Only)

Ticket N07 implements the offline confirmation structure without connecting live mail transport or external services:
- **Snapshot Contract**: Strict `BookingConfirmationSnapshot` validating exact financials, dates, rooms, and rounds.
- **Transports**: `RecordingConfirmationTransport` saves synthetic `.eml` files for test domains (`.invalid`); `DisabledConfirmationTransport` fails closed.
- **Rendering**: Strict PDF rendering (`build_n07_confirmation_pdf`) with XML-escaping, 2 MiB ceiling, and explicit `TEST — NOT A REAL BOOKING CONFIRMATION` banner.
- **Route Gating**: Protected staff endpoints (`/staff/confirmations/{operation_id}`) require `BOOKING_CONFIRMATIONS_STAFF_API_ENABLED=true` and authenticated staff roles; legacy confirmation templates and unscoped itinerary downloads are strictly denied (HTTP 403).

