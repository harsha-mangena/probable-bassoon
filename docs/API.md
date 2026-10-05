# Scheduling API

Same-origin JSON API. `POST` requests require `Content-Type: application/json`; malformed/oversized bodies are rejected. Dates use `YYYY-MM-DD`; appointment starts must be ISO datetimes with an explicit offset and whole minutes. Responses use clinic-local offsets and include the clinic timezone.

Errors: `{ "error": "human readable message", "code": "machine_code" }`. Typical statuses: 400 invalid request, 401 staff sign-in required, 403 credentials/role/CSRF denied, 404 resource missing, 409 booking/policy/idempotency conflict, 413 oversized payload, 415 wrong content type, 429 rate limit. Responses are not cached.

## Public retrieval

| Endpoint | Result |
|---|---|
| `GET /api/health` | Readiness status |
| `GET /api/config` | Public clinic identity, policies, local current date, voice-channel configuration |
| `GET /api/doctors` | Active doctors with ID, specialty, duration and buffer |
| `GET /api/slots?doctor_id=maya-patel&day=2026-10-06` | All available starts on that date (no fixed 24-slot truncation) |

## Hold and confirm

`POST /api/holds`

```json
{
  "doctor_id": "maya-patel",
  "start": "2026-10-06T09:00:00-07:00",
  "count": 4,
  "interval_weeks": 1,
  "idempotency_key": "unique-random-key-for-this-hold"
}
```

Response (201): `{ "hold_id": "1234567890", "expires": "UTC timestamp", "appointments": [...] }`. `count` defaults to 1 (maximum 12); interval defaults to 1 (maximum 4). All occurrences must fit the configured advance horizon. Retrying with the same key/payload returns the same live hold. An expired/released/committed hold needs a new key.

`POST /api/bookings`

```json
{
  "hold_id": "1234567890",
  "name": "Patient Name",
  "phone": "+14155550123",
  "idempotency_key": "a-different-unique-key-for-confirmation"
}
```

Response (201): `{ "reference": "1234567890", "manage_code": "012345678901", "replayed": false, "appointments": [...] }`. Identical retries return the original reference/code and `replayed: true`; changed payloads with that key return 409. Do not announce a booking before this succeeds. A prior committed key may replay a now-cancelled appointment; inspect returned appointment statuses.

`POST /api/holds/release`: `{ "hold_id": "...", "idempotency_key": "the-original-hold-key" }`. Releases an unconfirmed hold; confirmed appointments are unaffected.

## Patient management

Keep credentials out of query strings and logs.

| POST endpoint | Body |
|---|---|
| `/api/patient/bookings` | `{reference, manage_code}` → all visits in that series, including cancellations |
| `/api/patient/cancel` | `{reference, manage_code, appointment_id?, reason?}` → cancel one occurrence or all confirmed occurrences |
| `/api/patient/move` | `{appointment_id, manage_code, start}` → move that occurrence and retain its ID |

Series cancellation is atomic. A series cancellation skips past visits and cancels future confirmed occurrences. Cancelling an individual past visit is rejected. If a targeted future visit is inside the cutoff, the entire cancellation fails. An already-cancelled series returns `already_cancelled: true`. Individual moves do not rewrite other occurrences.

## Conversation

`POST /api/call/start` `{}` returns `{call_id, stage, seq, message, choices, result}`. The call ID is a secret bearer capability; do not expose it to another patient. Sessions last 30 minutes.

`POST /api/call/turn`

```json
{
  "call_id": "opaque-server-issued-id",
  "seq": 0,
  "text": "book",
  "request_id": "unique-random-turn-key"
}
```

Use the previous response’s `seq`; each accepted turn advances it. Network retries must reuse the same `request_id` and payload. Stale sequence numbers and key/payload mismatches are rejected. Invalid conversation choices return a new prompt (HTTP 200). Confirmed booking results include the patient credentials; `done` can also mean declined booking or staff referral, so check `result` before treating it as a booking.

Management flow: `manage` → reference → private code → appointment number or `cancel all` → `cancel`/`move` → explicit confirmation. To end an active call and release its unconfirmed hold, send `stop`.

## Staff authentication and control plane

`POST /api/auth/login` `{username,password}` sets a session cookie and returns `{username,role,csrf}`. `GET /api/auth/me` retrieves the current session. Every authenticated POST requires the cookie and `X-CSRF-Token: <csrf>`; cross-origin writes are rejected. `POST /api/auth/logout` `{}` revokes the session.

| Endpoint | Role | Request/result |
|---|---|---|
| `GET /api/admin/overview?day=&doctor_id=&status=` | Manager/admin | Filtered visits (up to 1000), aggregate stats, active/inactive doctors, last 50 audit events |
| `GET /api/admin/availability?doctor_id=` | Manager/admin | Seven weekdays and date overrides |
| `POST /api/admin/availability` | Manager/admin | `{doctor_id, weekday: 0..6, windows: [["09:00","12:00"], ["13:00","17:00"]]}` |
| Same endpoint, date override | Manager/admin | `{doctor_id, day: "2026-10-08", windows: []}` closes that date; `remove: true` removes its override |
| `POST /api/admin/holds` | Manager/admin | Same hold body; staff actor recorded |
| `POST /api/admin/bookings` | Manager/admin | Same confirmation body; staff actor recorded |
| `POST /api/admin/cancel` | Manager/admin | `{reference, appointment_id?, reason?}`; no patient code required |
| `POST /api/admin/move` | Manager/admin | `{appointment_id, start}`; no patient code required |
| `POST /api/admin/doctors` | Admin | `{id,name,specialty,duration,buffer,active}`; creates/updates a doctor |
| `GET /api/admin/users` | Admin | Usernames, roles and active flags; no password hashes |
| `POST /api/admin/users` | Admin | `{username,password,role: "manager"}` creates a new staff account |

Supply exactly one of `weekday` (Monday=0) or `day` for availability. Empty windows close a day. Overlapping/reversed windows are invalid. There is no public database-reset endpoint.

`POST /api/voice/twilio` is the only form-encoded endpoint; it requires valid Twilio request signing and HTTPS configuration. See [phone setup](PHONE_SETUP.md).
