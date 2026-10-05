# Deployment and data operations

## Environment

| Variable | Default | Purpose |
|---|---|---|
| `HOST` | `127.0.0.1` | Listen interface; containers use `0.0.0.0` |
| `PORT` | `8080` | HTTP port |
| `CLINIC_DB` | `clinic.db` beside server.py | Durable SQLite file; not committed |
| `APP_ORIGIN` | Unset | Exact public browser/webhook origin; HTTPS enables Secure cookies/HSTS |
| `TWILIO_AUTH_TOKEN` | Unset | Optional phone-signature secret; phone channel otherwise disabled |

`.env.example` lists these settings; Python does not automatically load `.env`. Export the variables in your service manager, or configure your container platform. Docker Compose consumes an adjacent `.env` for interpolation.

Place an HTTPS reverse proxy before the app for external access. Forward requests to port 8080, preserve path/query, enforce body-size and connection limits, and add edge abuse protection. Proxy only the clinic origin; no CORS access is enabled. The app does not trust `X-Forwarded-For`; rate limits use the socket peer, so a proxy shares that limit until a trusted edge limiter is configured. Rate-limit state and call transition locking are process-local; run a single worker.

## SQLite backup

Use SQLite’s backup API for a consistent snapshot of the running WAL database:

```bash
python3 - <<'PY'
import sqlite3
source = sqlite3.connect('clinic.db')
target = sqlite3.connect('/private/backups/clinic-backup.db')
source.backup(target)
target.close()
source.close()
PY
```

Restrict access to the database directory and backups; use encrypted storage as appropriate. Do not copy only `clinic.db` while WAL writes are active. Test restoring to a separate path and start with `--db` pointing to that restored copy. Never run two independent production apps against copies of the same clinic schedule.

The database contains appointments, contact details, password hashes, capability derivation secret, active staff sessions and short-lived call state/receipts. Audit entries contain actor/action/reference, not private management codes or medical details. Do not rotate/delete the capability derivation secret without planning patient management access. App startup maintains existing doctor availability; seed configuration is not reapplied.

Expired holds stop blocking slots immediately, even before housekeeping updates their status. Old calls/receipts are cleaned when new calls start; staff sessions are cleaned on login. The main server also performs periodic housekeeping. Appointment/audit retention and deletion/export policies should be chosen for the clinic rather than silently erasing bookings.

## Operational limits

This version schedules one doctor per visit with a fixed doctor-specific duration and buffer. It does not coordinate rooms, equipment or multiple providers; synchronize external calendars; send reminders; bill patients; or record clinical information. Live phone setup needs provider testing. Browser speech is progressive enhancement with a text fallback, and may rely on a browser speech service.

Passwords can be reset locally with `manage.py reset-password`, which revokes that user’s sessions. Admin UI creates additional accounts; arbitrary role changes/user disabling are deliberately not exposed. Backups and local administrative access are privileged operations. No bulk reset is exposed in the web application.
