# Rosapenna Tee Time Booking API

A FastAPI service that checks available Rosapenna tee times and automates the booking form in a Playwright-controlled browser. It can leave the booking open for you to review and pay manually. Payment automation is optional; `auto_pay` is off by default.

## Requirements

- Python 3.13 or newer
- [uv](https://docs.astral.sh/uv/) for installing the project dependencies
- Chromium installed for Playwright

## Set up

Run these commands in PowerShell from this directory:

```powershell
uv sync
if (!(Test-Path .env)) { Copy-Item .example.env .env }
```

Open `.env` and replace the example `BOOKING_API_KEY` with a private random secret. For example, generate one with:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Copy the generated value into `.env`:

```dotenv
BOOKING_API_KEY=paste-your-generated-secret-here
BOOKING_ENVIRONMENT=development
BOOKING_DOCS_ENABLED=true
BOOKING_CORS_ORIGINS=
```

Keep `.env` private and out of version control. Send the exact value after `BOOKING_API_KEY=` as the `X-API-Key` header when calling protected routes. This key is for your backend; it is not a BRS Golf API key.

Set `MONGODB_URI` in the backend `.env` to your MongoDB connection string. Keep
your existing `.env` if it already contains the URI. The database account needs
read/write and index creation access to the chosen application database, and
the backend host must be allowed to connect in MongoDB's network settings.
Check the connection without running BRS:

```powershell
uv run python -m database
```

This prints a safe success/error message without exposing credentials. The API
requires a working MongoDB connection at startup.

Install the Playwright browser once:

```powershell
uv run playwright install chromium
```

## Run the API

Start one worker from this directory so the application loads `.env` and its in-memory booking sessions remain available to the same process:

```powershell
uv run uvicorn app:app --host 127.0.0.1 --port 4040 --workers 1
```

The service is available at `http://127.0.0.1:4040`. Use `--host 0.0.0.0` only when you need access from another device and have set up suitable network protection.

Interactive API documentation:

- Swagger UI: <http://127.0.0.1:4040/docs>
- ReDoc: <http://127.0.0.1:4040/redoc>
- OpenAPI JSON: <http://127.0.0.1:4040/openapi.json>

## API access matrix

Only the health and readiness probes are public API probes. Every other
current API business route is a backend service route protected by `X-API-Key`
and a per-client rate limit.
The API key authenticates the backend service; it does not identify a person
or grant a staff role. Keep it in the backend environment and server-side
frontend configuration, never in browser code.

| Access class | Routes | What access means |
|---|---|---|
| Public probe | `GET /health`, `GET /health/ready` | Liveness and database readiness only. |
| API documentation | `GET /docs`, `GET /redoc`, `GET /openapi.json` when enabled | Public in development when `BOOKING_DOCS_ENABLED=true`; production startup requires this setting to be false. |
| Backend service | `POST /available-times`, `POST /bookings`, `GET /bookings/{session_id}`, `POST /bookings/{session_id}/close`, `GET /teesheets`, `GET /teesheets/status`, `POST /teesheets/revalidate`, and all `/booking-intents` routes | Requires the backend service key and rate limit. Intended for trusted server-side callers; the key itself does not identify or restrict the caller. |
| Authenticated guest | None | Guest accounts or guest sessions are not implemented by this API. |
| Staff role | `GET /sessions`, `POST /teesheets/refresh` | Requires the server-to-server API key, rate limit, and an externally validated OIDC staff role. OIDC is not configured, so these routes currently fail closed with HTTP 503. |

The F06 scope calls for external OIDC and does not allow locally managed staff
accounts. No identity provider is configured yet, so staff-only routes remain
unavailable and fail closed. Public guest booking remains available. Do not send
the backend API key or future identity-provider tokens to browser JavaScript or
store them in local storage.

## API routes

| Method and path | Purpose | Access |
|---|---|---|
| `GET /health` | Check service status and active session count. | None |
| `GET /health/ready` | Ping MongoDB; returns `ready` or HTTP 503. | None |
| `POST /available-times` | Scrape tee times for a course and date. | `X-API-Key` and rate limit |
| `POST /bookings` | Start the booking form automation. | `X-API-Key` and rate limit |
| `GET /bookings/{session_id}` | Check a booking session's status and log. | `X-API-Key` and rate limit |
| `POST /bookings/{session_id}/close` | Ask a booking browser to close. | API key and rate limit |
| `GET /sessions` | List in-memory booking sessions. | API key and rate limit; external OIDC role validation is pending (currently HTTP 503) |
| `GET /teesheets`, `GET /teesheets/status` | Read cached tee times, prices, and refresh status. | API key and rate limit |
| `POST /teesheets/refresh` | Run or retry the daily refresh. | API key and rate limit; external OIDC role validation is pending (currently HTTP 503) |
| `POST /teesheets/revalidate` | Check a selected quote with BRS. | API key and rate limit |

The API key is a shared service credential. It does not identify individual staff or provide staff roles. `/health` and `/health/ready` remain public probes. Browser automation accepts only HTTPS Rosapenna pages on `visitors.brsgolf.com`; attempted main-frame redirects to other destinations are blocked.

For protected endpoints, send the header:

```http
X-API-Key: your-value-from-BOOKING_API_KEY
```

## Check availability

In `/docs`, expand **POST `/available-times`**, click **Try it out**, add your `X-API-Key`, and submit a request such as:

```json
{
  "course_name": "Old Tom Morris Links",
  "holes": "18 Holes",
  "desired_date": "2027-07-15",
  "headless": true,
  "debug": false
}
```

`desired_date` uses `YYYY-MM-DD`. Omit it to use the tee sheet's current selection. The response groups results by rate package in `sections`; each time has a list of player and price options. Check `date_selected` and the `log` to see whether the requested date was selected. The endpoint is intended to read availability and does not submit a booking.

## Start a booking

In `/docs`, use **POST `/bookings`** with your `X-API-Key`. A minimal example is:

```json
{
  "course_name": "Old Tom Morris Links",
  "holes": "18 Holes",
  "desired_date": "2027-07-15",
  "desired_time": "08:00",
  "player_count": 1,
  "personal_info": {
    "first_name": "Example",
    "last_name": "Golfer",
    "country": "Ireland",
    "email": "golfer@example.com",
    "telephone": "+353 87 000 0000",
    "handicap": "14.2",
    "club": "Example Golf Club"
  },
  "auto_pay": false
}
```

Replace the example date and time with a future date and a time returned by `POST /available-times`. Replace all example personal details with accurate details before making a real request. `desired_time` and `personal_info` are required. `url`, course, holes, and several other fields have defaults; see `/docs` for the complete schema.

By default, the automation leaves the browser open for manual review and payment. Card details are optional. Only set `auto_pay: true` if you intentionally want the automation to click Pay after card details are provided. A `filled` status means the form automation completed; it does not confirm that a booking or payment succeeded.

The response includes a `session_id`, `status`, `message`, and step-by-step `log`. If status is `running`, poll `GET /bookings/{session_id}` using the same API key. The booking session is stored only in memory and is lost when the service restarts.

## Configuration

Application settings use the `BOOKING_` prefix; database settings use `MONGODB_`.
Both read `.env` when the service starts. Common application options:

| Variable | Default | Purpose |
|---|---|---|
| `BOOKING_ENVIRONMENT` | `development` | Set to `production` to require a 32+ character API key, explicit HTTPS CORS origins, and disabled docs at startup. |
| `BOOKING_API_KEY` | unset | Shared service secret; missing configuration makes protected routes return HTTP 503. Must be at least 32 characters in production. |
| `BOOKING_HOST` | `0.0.0.0` | Host used when starting with `python app.py`. |
| `BOOKING_PORT` | `4040` | Port used when starting with `python app.py`. |
| `BOOKING_MAX_CONCURRENT_SESSIONS` | `3` | Maximum simultaneous booking browser sessions. |
| `BOOKING_SESSION_TTL_SECONDS` | `1800` | Age used by the cleanup task for closed or failed sessions. |
| `BOOKING_BOOKING_FILL_TIMEOUT_SECONDS` | `90` | How long the booking request waits for browser automation before returning `running`. |
| `BOOKING_RATE_LIMIT_PER_MINUTE` | `20` | Per-process request limit per client IP on protected routes; must be greater than zero. |
| `BOOKING_TRUSTED_PROXY_CIDRS` | empty | Comma-separated proxy IP ranges; forwarded client IPs are honored only when the direct peer is in these ranges. |
| `BOOKING_CORS_ORIGINS` | empty | Comma-separated exact browser origins. Wildcard `*` is allowed only outside production; production origins must be exact HTTPS origins. |
| `BOOKING_DOCS_ENABLED` | `true` | Enables `/docs`, `/redoc`, and `/openapi.json`; set false in production. |
| `BOOKING_DEBUG_SCREENSHOTS` | `false` | Saves automation screenshots in development when explicitly enabled and only before guest details are filled; production screenshots are disabled. Generated screenshots and text snapshots are written to the backend `.temp/` directory, which is ignored by Git. |
| `BOOKING_INTENTS_COLLECTION` | `booking_intents` | MongoDB collection for online checkout drafts. Use a uniquely named collection for isolated verification. |

Request bodies are limited to 1 MiB. Personal detail fields and booking player
counts also have per-field limits; malformed requests receive a validation
response that does not echo submitted values.
| `BOOKING_USE_VIRTUAL_DISPLAY` | `true` | Try to start a virtual display when no display is already configured. |
| `BOOKING_ENABLE_VNC` | `false` | Start a VNC server for the virtual display in development; production startup rejects this setting. |
| `BOOKING_LOG_LEVEL` | `INFO` | Console and file log level. |
| `BOOKING_LOG_FILE` | `booking_api.log` | Rotating log file path. |

See `.example.env` and `Settings` in `rosapenna_backend/config.py` for all options. For production, set `BOOKING_ENVIRONMENT=production`, generate a long private API key, list the exact HTTPS frontend origin(s), set `BOOKING_DOCS_ENABLED=false`, and keep VNC disabled. Production startup fails when those settings are missing or unsafe. Development can keep `BOOKING_CORS_ORIGINS=*`; production cannot. Staff operations remain disabled until an external OIDC provider, callback, audience, and staff-role mapping are selected and securely configured. No local staff account or password reset command is provided. Keep the backend API key and future OIDC tokens server-side. Audit retention still needs an owner-approved policy.

## Troubleshooting

- **401 Unauthorized:** Confirm the service was started from this directory after setting `BOOKING_API_KEY`; send the exact saved value in `X-API-Key`.
- **Playwright says Chromium is missing:** Run `uv run playwright install chromium` from this directory, then restart the API.
- **Date, filter, or time slot selection fails:** Inspect the response `log` and verify the requested future date, holes, and time against current availability. The page automation can fail when a control is hidden or the tee sheet changes.
- **No session after restart:** Sessions are held in process memory, so restarting the API clears them.

## Development

### Backend code layout

The FastAPI application is composed in `rosapenna_backend/main.py`. HTTP routes
and request/response models live under `rosapenna_backend/api/`; booking and
availability workflows live under `rosapenna_backend/services/`; BRS browser
code is under `rosapenna_backend/integrations/brs/`; and MongoDB setup lives in
`rosapenna_backend/database.py`. The small top-level `app.py`, `database.py`,
`teesheets.py`, `booking_intents.py`, and `migrate_cache.py` files keep the
existing launch and maintenance commands working. Unit tests live in `tests/`.

### MongoDB storage

The backend owns database access. The frontend's server routes call the backend
with `BOOKING_BACKEND_URL` and `BOOKING_API_KEY`; they do not connect to MongoDB.
An existing frontend `MONGODB_URI` is unused and can be removed. Never name it
`NEXT_PUBLIC_MONGODB_URI` or place it in browser code.

| Setting | Default | Purpose |
|---|---|---|
| `MONGODB_URI` | required | Private MongoDB URI, including credentials if needed. |
| `MONGODB_DATABASE` | URI database, otherwise `rosapenna` | Optional application database override. |
| `MONGODB_TIMEOUT_MS` | `5000` | Connection/server selection and operation timeout (1000–60000 ms). |

The service creates one pooled PyMongo client per process, pings MongoDB before
startup, ensures the cache indexes, and closes the client after background work
stops on shutdown. It uses acknowledged majority writes. Database failures on
cache requests return HTTP 503 with a safe retry message; `/health` is a liveness
probe and `/health/ready` checks the database connection. Neither exposes the URI.

Current collections:

| Collection | Contents and key |
|---|---|
| `tee_sheets` | One BRS snapshot per `day` + `course_id`, unique compound index, native UTC `fetched_at`, and nested tee-sheet data. Booked-player names are removed. |
| `tee_sheet_refresh_runs` | Daily claim/status keyed by the Dublin date in `_id`; native UTC start/finish times, saved count, and safe errors. |

API responses keep ISO timestamps and omit MongoDB internal IDs. The existing
booking browser sessions remain in process memory: a live Playwright browser
cannot be restored from the database after restart. Linked booking records,
payment ledgers, vendor IDs, and audit history belong to the later integration
tickets and are not created by this cache setup. Backup/restore and business-data
retention policies still need to be selected and verified before deployment.

To import the existing SQLite cache, stop the old backend worker, validate the
source, then apply the import before starting the new backend:

```powershell
uv run python migrate_cache.py
uv run python migrate_cache.py --apply
```

The default source is `state/brs-teesheets.sqlite3`; override it with `--source`.
The first command is a dry run with no MongoDB writes. The second validates all
source rows, removes booked-player names, imports missing snapshots and daily
claims, and preserves existing MongoDB records and timestamps at MongoDB's
millisecond precision. It never changes
or deletes the SQLite file. Reruns insert only missing records. A partially
completed import can be rerun safely. Preserve the SQLite copy until the imported
data is checked. There is no automatic fallback to SQLite after migration.

### Daily BRS browsing cache

The backend keeps BRS tee-sheet responses in MongoDB. The default refresh is
once each day at **05:00 Europe/Dublin**, for today plus the next 29 days and
both courses currently offered on the Rosapenna BRS site: Old Tom Morris
(`course_id=1`) and Sandy Hills (`course_id=3`). It uses one browser session and
60 sequential, paced date/course requests per batch. It does not submit bookings.
St Patrick's uses a separate BRS site and is outside this cache's course list.

| Setting | Default | Purpose |
|---|---|---|
| `BOOKING_TEESHEET_DAYS` | `30` | Rolling date window, including today (1–365). |
| `BOOKING_TEESHEET_REFRESH_HOUR` | `5` | Local Dublin hour (0–23), including daylight-saving changes. |
| `BOOKING_TEESHEET_REFRESH_ENABLED` | `true` | Enable the daily background scheduler. |

Keep the API running with one worker. Starting it after the refresh hour catches
up today's run if it has not been claimed. A MongoDB daily `_id` claim prevents repeat runs
on refresh-button clicks, API restarts, or simultaneous triggers using the same
database. `BOOKING_TEESHEET_CACHE_PATH` is no longer used. A failed attempt is not retried automatically on the same day.
Existing good records survive a failed/partial refresh. Completed runs are
retained for 90 days; out-of-window snapshots are removed after a daily run.

Cache reads and live quote revalidation require the configured `BOOKING_API_KEY` via `X-API-Key`. Manual refresh also requires an external OIDC staff role, which is not configured yet:

| Method/path | Behavior |
|---|---|
| `GET /teesheets?date=2026-10-15&course_id=1` | Read a saved snapshot only; includes fetch time, stale flag, date window and last-run status. A missing snapshot is 404, invalid date/course is 400. |
| `GET /teesheets/status` | See the current window, schedule and most recent batch result. |
| `POST /teesheets/refresh` | Trigger today's batch once; repeated calls return its status. Staff operation, recorded in the audit log. |
| `POST /teesheets/refresh?retry_failed=true` | Explicit staff retry after today's failed/partial run. Successful/running runs remain protected against duplicate triggers. |
| `POST /teesheets/revalidate` | Make an extra live read for a selected booking; compare price, currency, fee, tee time and available player places. |

An example revalidation body (replace IDs and amounts with real saved results):

```json
{
  "date": "2026-10-15", "course_id": 1,
  "tee_time_id": 652762, "green_fee_id": 349,
  "players": 1, "price": "200.00", "currency": "EUR"
}
```

Revalidation returns canonical BRS details when valid, HTTP 409 for changed
prices/availability, 429 when another live check is running, and 502 if BRS cannot
be checked. It also saves the fresh date/course snapshot. These selected-booking
checks are an intentional exception to the daily browsing refresh. A snapshot
or valid check is not a reservation; BRS remains authoritative at checkout.
Guest names from booked slots are removed before saving snapshots. The cache
does not contain API keys or browser cookies.

### Linked booking draft foundation (F05, partial)

The backend also exposes three `X-API-Key`-protected draft routes:

| Method/path | Behavior |
|---|---|
| `POST /booking-intents` | Create a draft using the `Idempotency-Key` UUID header. Repeating the key with identical data returns the same record; reusing it with different data returns 409. |
| `GET /booking-intents/{booking_intent_id}` | Read one draft and its append-only action history. IDs use the internal `bki_` plus random UUID hex format. |
| `POST /booking-intents/{booking_intent_id}/actions` | Append a supported evidence event only when `expected_revision` matches the saved revision; stale or invalid transitions return 409. |

The unique `idempotency_key_hash` index prevents duplicate drafts for one key.
The record includes the `website_checkout` source, selected course/date/time,
holes, green-fee/package and player count, the initial quote, successful live
revalidation quote evidence, lead guest name/email/telephone, timestamps,
revision and actions. The frontend submits
checkout through its server-side backend bridge. Only the lead contact fields
needed by this flow are persisted; other players' personal details, marketing
choices, BRS cookies/tokens, payment credentials and card data are not accepted
by the draft schema. `links` starts with empty Opera, BRS-round and payment
references; no external identifiers are invented.

Stages record draft creation, quote revalidation, checkout preparation request
and its observed response/failure. A successful BRS preparation response is
stored only as an HTTP status and remains unverified. Payment stays
`not_started` and reservation stays `not_confirmed`; this foundation does not
create reservations, payments or callbacks. The existing process-local checkout
cooldown still runs before live revalidation and BRS preparation. Repeated
requests cannot trigger a second preparation while an earlier result is pending,
unknown or already received.

This is partial F05 work. Vendor topology for online and phone/email records,
approved retention/deletion ownership, backup/restore, and cross-vendor
reconciliation are unresolved. The collection has no TTL deletion rule while
retention is unapproved. Shared API-key authorization is not staff identity or
role-based access. Do not treat these endpoints or a draft as a confirmed
booking, payment or full F05 completion.

Verify without creating a booking:

1. Start the backend, open `/docs`, and call `GET /teesheets/status` with the key.
2. After a scheduled run, inspect `last_run`. To trigger a manual refresh, use `POST /teesheets/refresh` with an authorized staff token.
3. Read a saved course/date twice. Its `fetched_at` should stay the same.
4. Repeat the daily trigger; its `started_at` should stay the same.
5. Open the frontend `/tee-times`; check price/player options against BRS.
6. To check the booking guard safely, call `/teesheets/revalidate` with a saved
   selection but an intentionally different price. Expect 409. It never submits a booking.

Run the focused offline cache checks with:

```powershell
uv run python -m unittest tests.test_booking_intents tests.test_teesheets tests.test_database -v
```

Run the API with auto-reload during development:

```powershell
uv run uvicorn app:app --reload --host 127.0.0.1 --port 4040 --workers 1
```

Use `/docs` for request and response schemas and verify API changes with focused local requests.

The offline tests use the development-only `mongomock` dependency; they do not
connect to your MongoDB cluster or BRS. A successful mock test is not evidence of
cluster access, so also check `python -m database` and `/health/ready` locally.

The scheduler remembers a handled Dublin day while the process is running, so it
does not attempt another daily MongoDB claim every minute. Persistent claims
still prevent a duplicate BRS batch after restart. If a MongoDB operation fails,
the scheduler logs a safe warning and retries the daily claim after 60 seconds;
this is separate from the frontend's BRS checkout session configuration. A
recurring timeout needs a database/network check; `MONGODB_TIMEOUT_MS` controls
the operation deadline (5000 ms by default).
