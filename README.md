# Northstar — doctor appointment scheduling

An end-to-end clinic scheduling application: a patient booking page, a browser voice/text front desk, and a protected manager/admin control plane. Python 3.11+ and SQLite power the application; **running the app requires no npm packages, paid APIs, API keys, or cloud accounts**.

This replaces the earlier salon prototype, its unauthenticated reset endpoint, and the separate Next.js/ElevenLabs interface. Existing salon data in `frontdesk.db` is not imported; clinic data uses a separate `clinic.db`. The old implementation remains in Git history.

## Start locally

```bash
git clone https://github.com/harsha-mangena/probable-bassoon.git
cd probable-bassoon
python3 manage.py create-user admin --role admin
# Enter a unique password of at least 12 characters at the prompt.
python3 server.py
```

Open **http://localhost:8080**. Patients can book immediately. Use **Staff portal** to sign in with the account you created. The seeded doctor names, clinic name, phone number and hours are demonstration values; replace them before use.

Python 3.11+ is required (`tomllib` is included). On Windows, use `python` instead of `python3`. No `pip install` is needed. Browser speech requires HTTPS or localhost and a supported browser; text input is always available.

## Patient experience

1. Choose a doctor and a clinic-local date; retrieve live available times.
2. Select a single visit or 2/4/8/12 recurring visits (the API supports every count from 1–12), repeated every 1–4 weeks.
3. Enter a name and phone number. Review the full series; the times are held for five minutes.
4. Explicitly confirm. Save the ten-digit booking reference and twelve-digit private management code.
5. Use **Manage my appointments** to retrieve the series, move an individual visit, cancel one occurrence, or cancel the series. Changes require two hours’ notice by default; staff can override that cutoff for future appointments.

The **Call the front desk** tab runs the same transactions through a deterministic conversation. Book one or multiple weekly visits, or choose `manage` with your reference/code to cancel or move appointments. Enable voice to hear prompts and speak responses; supported browsers provide speech recognition and synthesis. The interface also accepts typed responses and clickable choices. Dates support `today`, `tomorrow`, `next available`, `YYYY-MM-DD`, or keypad `YYYYMMDD`. Doctor and slot choices accept their menu numbers. This is a guided scheduling flow, not an LLM or medical assistant.

## Control plane

| Role | Access |
|---|---|
| Patient | Public doctor/slot retrieval; own bookings with reference + private code |
| Manager | Appointment filters, booking on a patient's behalf, reschedule/cancel, weekly doctor hours, date-specific overrides/time off, audit log |
| Admin | All manager functions plus create/edit/deactivate doctors and create staff accounts |

Create staff users in **Administration**, or locally:

```bash
python3 manage.py create-user reception --role manager
python3 manage.py reset-password reception
```

A password reset revokes existing sessions. Passwords use salted scrypt hashes; sessions expire after eight hours and use HttpOnly/SameSite cookies. Staff mutations require CSRF tokens. No default passwords are shipped. Staff permissions are enforced on the server, independently of UI visibility.

Weekly hours and date overrides live in SQLite and survive restarts. New doctors need availability configured in the control plane. A doctor with future appointments/holds cannot be deactivated or have visit duration/buffer changed until those visits are moved or cancelled. Schedule edits that would invalidate an existing appointment/hold are rejected atomically.

## Free APIs and phone calls

| Channel | Included | Cost / setup |
|---|---|---|
| Browser voice | Web Speech API: speech recognition + speech synthesis | No app API key or usage bill. Browser support and speech-service availability vary. Recognition may use the browser provider’s servers. Explicit opt-in; no audio stored by this app. |
| Browser text | Same server-side call state machine | Free; no third-party API |
| Scheduling REST API | Doctors, availability, holds, bookings, recurring visits, cancellations and rescheduling | Free; self-hosted |
| Optional telephone adapter | Signed Twilio Voice webhook, speech/DTMF menus, booking and management | Requires a Twilio account/number and a public HTTPS endpoint. Trials have limits; regular PSTN usage is paid. Disabled by default. |

There is no claim of unlimited free phone-number calling. The browser channel is the default no-cost implementation. The phone adapter does not need the Twilio SDK or an LLM key. See [phone setup](docs/PHONE_SETUP.md) for configuration and keypad instructions.

Sources: [Web Speech API](https://developer.mozilla.org/en-US/docs/Web/API/Web_Speech_API), [speech recognition](https://developer.mozilla.org/en-US/docs/Web/API/SpeechRecognition), [Twilio trial](https://www.twilio.com/docs/usage/trials), [TwiML Gather](https://www.twilio.com/docs/voice/twiml/gather), [request signing](https://www.twilio.com/docs/usage/security).

## Booking guarantees

- Separate resource calendar for each doctor; visit duration and buffer both block availability.
- `BEGIN IMMEDIATE` serializes writers; overlapping confirmed visits or unexpired holds block a new hold.
- Recurring holds/commits are all-or-nothing, with every occurrence revalidated. A conflict produces no partial series.
- Holds and confirmation requests accept idempotency keys. Reusing a key for a different payload is rejected. Retrying the same commit returns the original reference and code.
- Rescheduling updates the same appointment ID in one transaction. A failed move leaves the original visit intact.
- Patient capabilities never appear in URLs. Only staff can list all appointments; patients need both reference and private code.
- Slots respect notice, advance-booking horizon, working hours, holidays/overrides and cancellations. All stored instants are UTC; recurring visits keep clinic-local wall time across DST changes. Ambiguous/nonexistent wall-time starts are omitted.
- Confirmation is spoken/displayed only after the database commit succeeds. Declining confirmation releases the hold.
- Call sessions and turn retry receipts persist across restarts; the same turn key cannot advance a call twice.

## Configure and deploy

Edit `clinic.toml` for the clinic identity, timezone and policy **before the first run**. Seed doctors are inserted once; later changes use the control plane. Changing timezone for an existing database is rejected to avoid reinterpreting schedules. Use a deliberate migration for that change.

```bash
# Alternate database and port
python3 server.py --db /path/to/clinic.db --port 8081
# For containers/reverse proxy:
HOST=0.0.0.0 PORT=8080 APP_ORIGIN=https://clinic.example.com python3 server.py
```

`APP_ORIGIN` must match the browser/public webhook origin exactly (no path or trailing slash). HTTPS origins enable Secure cookies and HSTS. Terminate HTTPS at a reverse proxy and expose only that proxy. Keep the database and backup directory private. The app stores contact information and management capability material; backups require the same protection. It does not collect medical history or diagnoses, and does not assert certification for clinical data handling.

Use one application process on one host with durable SQLite storage. The call-transition lock and rate limits are process-local; scaling to multiple processes requires a shared rate limiter and database-backed call concurrency control. The stdlib HTTP server is suitable for local/small controlled deployments behind a proxy, not a high-volume edge service. Add monitoring, abuse controls, backup/restore procedures and a privacy/retention review before a public clinical launch. Optional real phone calls still need credentialed validation with your chosen provider; no live number was provisioned by this change.

A Docker setup is included:

```bash
docker compose run --rm clinic python manage.py create-user admin --role admin
docker compose up --build
```

Set `APP_ORIGIN` in the environment for HTTPS deployments. For local Docker access, leave it unset; access the app at localhost:8080. [Deployment details](docs/DEPLOYMENT.md).

## Tests

```bash
python3 -m unittest discover -s tests -v
node --check web/app.js
# Optional browser suite (development dependencies only):
npm ci
npx playwright install chromium
npm run test:browser
```

The browser suite launches an isolated server and disposable SQLite database. It exercises recurring booking, individual and series cancellation, rescheduling, the conversational front desk, staff sign-in, time off, doctor/staff creation and manager permissions. It checks mobile layout and JavaScript errors. Microphone recognition and live PSTN audio need a real supported browser/provider; the automated suite tests the shared call flow and signed webhook protocol.

GitHub Actions runs backend tests, JavaScript checks and the browser suite. See [API reference](docs/API.md), [architecture decisions](DECISIONS.md), and [validation record](docs/VALIDATION.md).
