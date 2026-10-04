"""Google Calendar adapter: the one real calendar.

Implements the same 6-operation interface as the local engine, backed by
the business's Google Calendar via `hatch_gws_cli calendar`.

Design notes (from the integration research):
- The calendar is the source of truth; the local ledger mirrors for
  idempotency keys, holds, and reminders.
- Holds are tentative Google events (opaque, "[HOLD]" summary) with a
  deterministic event id derived from the client_hold_id, so a retried
  hold that meets a 409 adopts the existing event instead of doubling.
- Commit patches the hold event in place: one object, same id, never
  cancel-plus-rebook.
- Move is a single events.patch; cancel marks cancelled (patch), not delete.

Requires the user to connect Google Calendar once (OAuth). Without it,
every operation raises NotConnectedError carrying the connect URL.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from engine import (
    BookingError,
    Engine,
    SlotConflictError,
    _iso_utc,
    _utcnow,
)

CONNECT_URL = "https://agent.meta.ai/connectors/connect/google_calendar"


class NotConnectedError(BookingError):
    def __init__(self):
        super().__init__(
            "Google Calendar is not connected. Connect it here: " + CONNECT_URL
        )
        self.connect_url = CONNECT_URL


def derive_event_id(prefix: str, key: str) -> str:
    """Deterministic Google event id from a client key.

    Google event ids must be base32hex (0-9, a-v), 5-1024 chars.
    A sha1 hex digest only uses 0-9a-f, which is a subset, so it qualifies.
    """
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
    return f"{prefix}{digest}"[:64]


def _rfc3339(dt: datetime) -> str:
    return dt.isoformat()


class GoogleCalendarAdapter:
    """The 6-op calendar interface, backed by Google Calendar."""

    def __init__(self, config: dict, ledger: Engine | None = None,
                 account: str | None = None, db_path: str = "ledger.db"):
        self.config = config
        self.tz = ZoneInfo(config["business"]["timezone"])
        self.calendar_id = config["calendar"].get("calendar_id", "primary")
        self.account = account
        # Local ledger: holds (the atomic lock), idempotency keys, reminders.
        # The calendar holds the truth; the ledger holds the receipts.
        self.ledger = ledger or Engine(config, db_path=db_path)

    # -- raw CLI ---------------------------------------------------------

    def _gws(self, *args: str, params: dict | None = None,
             body: dict | None = None) -> dict:
        cmd = ["hatch_gws_cli", "calendar"]
        if self.account:
            cmd += ["--account", self.account]
        cmd += list(args)
        if params is not None:
            cmd += ["--params", json.dumps(params)]
        if body is not None:
            cmd += ["--json", json.dumps(body)]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        try:
            data = json.loads(proc.stdout or "{}")
        except json.JSONDecodeError:
            data = {}
        text = (proc.stdout + proc.stderr).lower()
        not_connected_signals = (
            "not_connected", "not connected", "missing connector",
            "missing access_token", "access_token surrogate",
            "unauthorized", "invalid_grant", "auth error",
        )
        if any(sig in text for sig in not_connected_signals):
            raise NotConnectedError()
        if proc.returncode != 0:
            raise BookingError(f"calendar call failed: {proc.stderr[:300]}")
        if isinstance(data, dict) and data.get("status") == "not_connected":
            raise NotConnectedError()
        return data

    def check_connected(self) -> bool:
        try:
            self._gws("status")
            return True
        except NotConnectedError:
            return False

    # -- 1. list_free_slots ----------------------------------------------

    def list_free_slots(self, service_id: str, day: date | str,
                        now: datetime | None = None):
        now = now or _utcnow()
        if isinstance(day, str):
            day = date.fromisoformat(day)
        # Pure candidates from config math (empty engine => no local overlap).
        candidates = Engine(self.config).list_free_slots(service_id, day, now=now)
        if not candidates:
            return []
        day_start = datetime.combine(day, datetime.min.time()).replace(tzinfo=self.tz)
        day_end = day_start + timedelta(days=1)
        busy = self._busy_intervals(day_start, day_end)
        free = []
        for s, e in candidates:
            s_u, e_u = s.astimezone(timezone.utc), e.astimezone(timezone.utc)
            if not any(s_u < b_e and e_u > b_s for b_s, b_e in busy):
                free.append((s, e))
        return free

    def _busy_intervals(self, start, end) -> list[tuple[datetime, datetime]]:
        data = self._gws(
            "freebusy", "query",
            body={"timeMin": _rfc3339(start), "timeMax": _rfc3339(end),
                  "items": [{"id": self.calendar_id}]},
        )
        intervals = []
        cals = (data.get("calendars") or {})
        for cal in cals.values():
            for b in cal.get("busy", []):
                intervals.append(
                    (datetime.fromisoformat(b["start"]),
                     datetime.fromisoformat(b["end"])))
        return intervals

    # -- 2. hold_slot -----------------------------------------------------

    def hold_slot(self, service_id: str, start_local: datetime,
                  client_hold_id: str, now: datetime | None = None) -> str:
        now = now or _utcnow()
        # The atomic lock lives in the ledger transaction.
        hold_id = self.ledger.hold_slot(service_id, start_local,
                                        client_hold_id, now=now)
        hold = self.ledger.get_booking(hold_id)
        event_id = derive_event_id("fdh", client_hold_id)
        svc = next(s for s in self.config["services"] if s["id"] == service_id)
        start = datetime.fromisoformat(hold["start_utc"])
        end = datetime.fromisoformat(hold["end_utc"])
        body = {
            "id": event_id,
            "summary": f"[HOLD] {svc['name']}",
            "description": f"Front Desk tentative hold {hold_id}; "
                           f"expires {hold['hold_expires_utc']}",
            "start": {"dateTime": _rfc3339(start), "timeZone": str(self.tz)},
            "end": {"dateTime": _rfc3339(end), "timeZone": str(self.tz)},
            "transparency": "opaque",
            "extendedProperties": {"private": {
                "frontdesk": "hold",
                "client_hold_id": client_hold_id,
                "hold_expires": hold["hold_expires_utc"] or "",
            }},
        }
        try:
            self._insert_event(event_id, body)
        except Exception:
            # Mirror failed: release the lock so the slot is not stuck.
            self.ledger.db.execute(
                "UPDATE bookings SET status='expired' WHERE id=?", (hold_id,))
            self.ledger.db.commit()
            raise
        self.ledger.db.execute(
            "UPDATE bookings SET provider_ref=? WHERE id=?", (event_id, hold_id))
        self.ledger.db.commit()
        return hold_id

    def _insert_event(self, event_id: str, body: dict) -> dict:
        try:
            return self._gws(
                "events", "insert",
                params={"calendarId": self.calendar_id}, body=body)
        except BookingError as exc:
            if "409" in str(exc) or "already exists" in str(exc).lower():
                # A retried hold meets its own event: adopt it.
                return self._gws(
                    "events", "get",
                    params={"calendarId": self.calendar_id, "eventId": event_id})
            raise

    # -- 3. commit_booking -------------------------------------------------

    def commit_booking(self, hold_id: str, customer_name: str,
                       customer_phone: str, idempotency_key: str,
                       now: datetime | None = None):
        now = now or _utcnow()
        booking_id, replayed = self.ledger.commit_booking(
            hold_id, customer_name, customer_phone, idempotency_key, now=now)
        if replayed:
            return booking_id, True
        booking = self.ledger.get_booking(booking_id)
        event_id = booking["provider_ref"]
        svc = next(s for s in self.config["services"]
                   if s["id"] == booking["service_id"])
        start = datetime.fromisoformat(booking["start_utc"])
        when = start.astimezone(self.tz).strftime("%A, %B %-d at %-I:%M %p")
        self._gws(
            "events", "patch",
            params={"calendarId": self.calendar_id, "eventId": event_id},
            body={
                "summary": f"{svc['name']} — {customer_name}",
                "description": f"Booked by Front Desk.\nBooking {booking_id}\n"
                               f"{when}\n{customer_phone}",
                "extendedProperties": {"private": {
                    "frontdesk": "booking",
                    "booking_id": booking_id,
                    "idempotency_key": idempotency_key,
                }},
            },
        )
        return booking_id, False

    # -- 4. move_booking ----------------------------------------------------

    def move_booking(self, booking_id: str, new_start_local: datetime,
                     now: datetime | None = None) -> str:
        now = now or _utcnow()
        booking = self.ledger.get_booking(booking_id)
        if not booking or not booking.get("provider_ref"):
            raise BookingError(f"no mirrored booking: {booking_id}")
        # Google first, then the ledger. Patch is idempotent on (start,end),
        # so a crash between the two converges on retry.
        svc = next(s for s in self.config["services"]
                   if s["id"] == booking["service_id"])
        from engine import Engine as _E  # local import: keep module import-light
        window = _E(self.config)._window(svc)
        if new_start_local.tzinfo is None:
            new_start_local = new_start_local.replace(tzinfo=self.tz)
        new_start_local = new_start_local.astimezone(self.tz)
        self._gws(
            "events", "patch",
            params={"calendarId": self.calendar_id,
                    "eventId": booking["provider_ref"]},
            body={
                "start": {"dateTime": _rfc3339(new_start_local),
                          "timeZone": str(self.tz)},
                "end": {"dateTime": _rfc3339(new_start_local + window),
                        "timeZone": str(self.tz)},
            },
        )
        return self.ledger.move_booking(booking_id, new_start_local, now=now)

    # -- 5. cancel_booking --------------------------------------------------

    def cancel_booking(self, booking_id: str, reason: str = "") -> None:
        booking = self.ledger.get_booking(booking_id)
        if booking and booking.get("provider_ref"):
            # Mark cancelled, don't delete: the audit trail stays.
            self._gws(
                "events", "patch",
                params={"calendarId": self.calendar_id,
                        "eventId": booking["provider_ref"]},
                body={"status": "cancelled",
                      "description": f"Cancelled via Front Desk. {reason}"},
            )
        self.ledger.cancel_booking(booking_id, reason)

    # -- 6. get_booking ------------------------------------------------------

    def get_booking(self, booking_id: str) -> dict | None:
        return self.ledger.get_booking(booking_id)
