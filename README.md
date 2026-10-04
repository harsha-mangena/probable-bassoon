# Front Desk

A scheduling agent a small business attaches to the calendar it already uses.
The product is the booking write: atomic, idempotent, and honest — the agent
never says "you're booked" before a booking id exists.

## Run the product

Zero dependencies, Python 3.11+ stdlib only:

```bash
python3 server.py          # serves at http://localhost:8080/
```

Open the page: five tabs — **Today** (call log), **Bookings** (move/cancel),
**Text Call** (the interactive demo), **Setup** (business config), **Admin**
(control plane: engine health, holds, reminders, operator actions).

Every booking id the UI shows comes from the server. The Text Call tab runs
the real flow — hold → commit → server-issued id — and its retry button
replays the identical idempotency key to prove no duplicate is created.

## How it works

One call, in order: greet → intent → service from config → live slots only →
hold → commit → speak (only after the id exists) → enqueue follow-up.
If a tool fails twice, the call hands off to the shop's transfer number with
the state already collected.

The checks are not optional:

- A retry with the same idempotency key returns the same booking id.
- A second hold on the same slot fails.
- A move is one atomic change to the same id, never cancel-plus-rebook.
- Reminders are derived from the booking at commit time, not configured workflows.
- The owner never edits a prompt; rules live in `salon.toml`.

## Layout

| File | What it is |
|---|---|
| `server.py` | Stdlib HTTP server: JSON API + serves the UI |
| `index.html` | The frontend: 5 tabs wired to the API, no mocks |
| `engine.py` | Booking engine: SQLite, holds with TTL, idempotent commits, atomic moves |
| `call_flow.py` | The scripted text-call flow (used by the CLI demos) |
| `google_adapter.py` | The one real calendar: same 6 operations against Google Calendar |
| `salon.toml` | Business config: services, hours, timezone, policy, reminders, escalation |
| `tests.py` | 8 local tests proving the safety checks |
| `demo_call.py` | Scripted haircut call + retry proof (CLI) |
| `e2e_lifecycle.py` | Book → move → cancel → rebook dry run (CLI) |
| `real_calendar_proof.py` | Hold → commit → retry → cancel against Google Calendar (cleans up) |
| `DECISIONS.md` | The product decisions and why |

## API

- `POST /api/call/start` `{want}` → session + live slots
- `POST /api/call/choose` `{session_id, pick}` → server-side hold
- `POST /api/call/commit` `{session_id, name, phone}` → booking id
- `POST /api/call/retry` `{session_id}` → same id, proves no duplicate
- `GET /api/slots?service_id=&day=` · `GET /api/bookings`
- `POST /api/bookings/move` · `POST /api/bookings/cancel`
- `GET /api/calls` · `GET /api/config`
- `GET /api/admin` · `POST /api/admin/expire_holds|sweep|reset`

## CLI checks

```bash
python3 tests.py           # 8 tests, local calendar, no keys needed
python3 demo_call.py       # the haircut call + the retry that creates no second booking
python3 e2e_lifecycle.py   # full lifecycle dry run
```

The Google adapter needs Google Calendar connected; without it every
operation fails closed with the connect URL instead of guessing.

## Decisions

Python + SQLite, stdlib only — the race guard is a real transaction, so
check-and-insert in one transaction *is* the lock. Hold-then-commit, because
a caller needs the slot reserved while giving their name. Text call now;
voice becomes a channel adapter later, since the engine is channel-blind.
