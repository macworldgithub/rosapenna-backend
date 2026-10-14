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
Copy-Item .example.env .env
```

Open `.env` and replace the example `BOOKING_API_KEY` with a private random secret. For example, generate one with:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Copy the generated value into `.env`:

```dotenv
BOOKING_API_KEY=paste-your-generated-secret-here
```

Keep `.env` private and out of version control. Send the exact value after `BOOKING_API_KEY=` as the `X-API-Key` header when calling protected routes. This key is for your backend; it is not a BRS Golf API key.

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

## API routes

| Method and path | Purpose | Authentication in current code |
|---|---|---|
| `GET /health` | Check service status and active session count. | None |
| `POST /available-times` | Scrape tee times for a course and date. | `X-API-Key` and rate limit |
| `POST /bookings` | Start the booking form automation. | `X-API-Key` and rate limit |
| `GET /bookings/{session_id}` | Check a booking session's status and log. | `X-API-Key` and rate limit |
| `POST /bookings/{session_id}/close` | Ask a booking browser to close. | None in current code |
| `GET /sessions` | List in-memory booking sessions. | None in current code |

**Important:** session listing and browser closing routes currently have no API-key or rate-limit dependency, despite the `401` responses mentioned in their route documentation. Avoid exposing this service to an untrusted network until those routes are protected.

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

Settings use the `BOOKING_` environment prefix and are read from `.env` when the service starts. Common options:

| Variable | Default | Purpose |
|---|---|---|
| `BOOKING_API_KEY` | unset | Shared secret for routes that enforce API-key authentication. Set this before use. |
| `BOOKING_HOST` | `0.0.0.0` | Host used when starting with `python app.py`. |
| `BOOKING_PORT` | `4040` | Port used when starting with `python app.py`. |
| `BOOKING_MAX_CONCURRENT_SESSIONS` | `3` | Maximum simultaneous booking browser sessions. |
| `BOOKING_SESSION_TTL_SECONDS` | `1800` | Age used by the cleanup task for closed or failed sessions. |
| `BOOKING_BOOKING_FILL_TIMEOUT_SECONDS` | `90` | How long the booking request waits for browser automation before returning `running`. |
| `BOOKING_RATE_LIMIT_PER_MINUTE` | `20` | Per-process request limit per client IP on routes that use the common dependencies; `0` disables it. |
| `BOOKING_CORS_ORIGINS` | `*` | Allowed browser origins; use a comma-separated list to restrict them. |
| `BOOKING_USE_VIRTUAL_DISPLAY` | `true` | Try to start a virtual display when no display is already configured. |
| `BOOKING_ENABLE_VNC` | `false` | Start a VNC server for the virtual display when enabled and available. |
| `BOOKING_LOG_LEVEL` | `INFO` | Console and file log level. |
| `BOOKING_LOG_FILE` | `booking_api.log` | Rotating log file path. |

See `.example.env` and `Settings` in `app.py` for all options. Avoid enabling VNC on an untrusted network; the code may start it without a password if `BOOKING_VNC_PASSWORD` is empty.

## Troubleshooting

- **401 Unauthorized:** Confirm the service was started from this directory after setting `BOOKING_API_KEY`; send the exact saved value in `X-API-Key`.
- **Playwright says Chromium is missing:** Run `uv run playwright install chromium` from this directory, then restart the API.
- **Date, filter, or time slot selection fails:** Inspect the response `log` and verify the requested future date, holes, and time against current availability. The page automation can fail when a control is hidden or the tee sheet changes.
- **No session after restart:** Sessions are held in process memory, so restarting the API clears them.

## Development

Run the API with auto-reload during development:

```powershell
uv run uvicorn app:app --reload --host 127.0.0.1 --port 4040 --workers 1
```

There is currently no automated test suite in this backend. Use `/docs` for request and response schemas and verify changes with focused local API requests.
