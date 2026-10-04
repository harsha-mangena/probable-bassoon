"""Front Desk booking engine v1.

Owns the booking write: atomic, idempotent, honest.
Rule: confirmation language is emitted only after a booking id exists.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


# --- errors ----------------------------------------------------------------

class BookingError(Exception):
    """Base for every booking failure the agent handles, never crashes on."""


class SlotConflictError(BookingError):
    """The window overlaps an existing hold or booking."""


class HoldNotFoundError(BookingError):
    pass


class HoldExpiredError(BookingError):
    pass


class BookingNotFoundError(BookingError):
    pass


# --- helpers ---------------------------------------------------------------

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8].upper()}"


def _iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


# --- engine -----------------------------------------------------------------

class Engine:
    """Booking engine over a local SQLite store.

    Every mutating operation runs inside a single IMMEDIATE transaction with
    the overlap check *inside* that transaction. SQLite serializes writers,
    so check-and-insert is atomic: two callers racing for one slot cannot
    both win. The second hold fails; the second commit with the same
    idempotency key returns the first booking's id.
    """

    def __init__(self, config: dict, db_path: str = ":memory:"):
        self.config = config
        self.tz = ZoneInfo(config["business"]["timezone"])
        self.db = sqlite3.connect(db_path)
        self.db.row_factory = sqlite3.Row
        self._init_schema()

    # -- schema ---------------------------------------------------------------

    def _init_schema(self) -> None:
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS bookings (
                id              TEXT PRIMARY KEY,
                idempotency_key TEXT UNIQUE,
                client_hold_id  TEXT UNIQUE,
                service_id      TEXT NOT NULL,
                start_utc       TEXT NOT NULL,
                end_utc         TEXT NOT NULL,
                customer_name   TEXT,
                customer_phone  TEXT,
                status          TEXT NOT NULL,   -- held | confirmed | cancelled | expired
                hold_expires_utc TEXT,
                created_utc     TEXT NOT NULL,
                provider_ref    TEXT             -- real-calendar event id, when bound
            );
            CREATE TABLE IF NOT EXISTS reminders (
                id            TEXT PRIMARY KEY,
                booking_id    TEXT NOT NULL REFERENCES bookings(id),
                remind_at_utc TEXT NOT NULL,
                channel       TEXT NOT NULL,
                sent          INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_bookings_window
                ON bookings (start_utc, end_utc, status);
            """
        )

    # -- config helpers ---------------------------------------------------------

    def _service(self, service_id: str) -> dict:
        for svc in self.config["services"]:
            if svc["id"] == service_id and svc.get("bookable", True):
                return svc
        raise BookingError(f"unknown or unbookable service: {service_id}")

    def _window(self, svc: dict) -> timedelta:
        return timedelta(
            minutes=svc["duration_min"]
            + svc.get("buffer_before_min", 0)
            + svc.get("buffer_after_min", 0)
        )

    def _hours_for(self, day: date) -> list[tuple[time, time]]:
        overrides = self.config.get("hours", {}).get("overrides", {})
        key = day.isoformat()
        if key in overrides:
            raw = overrides[key]
        else:
            raw = self.config.get("hours", {}).get("weekly", {}).get(
                WEEKDAYS[day.weekday()], []
            )
        out = []
        for open_s, close_s in raw:
            o = time.fromisoformat(open_s)
            c = time.fromisoformat(close_s)
            if o < c:
                out.append((o, c))
        return out

    # -- availability -------------------------------------------------------------

    def _overlaps(self, cur, start_utc: str, end_utc: str, exclude_id: str | None) -> bool:
        row = cur.execute(
            """
            SELECT 1 FROM bookings
             WHERE status IN ('held', 'confirmed')
               AND (status = 'confirmed'
                    OR hold_expires_utc IS NULL
                    OR hold_expires_utc > ?)
               AND (? IS NULL OR id != ?)
               AND start_utc < ? AND end_utc > ?
             LIMIT 1
            """,
            (_iso_utc(_utcnow()), exclude_id, exclude_id, end_utc, start_utc),
        ).fetchone()
        return row is not None

    def list_free_slots(
        self, service_id: str, day: date | str, now: datetime | None = None
    ) -> list[tuple[datetime, datetime]]:
        """Pure read, no side effects. Slots are advisory until held."""
        now = now or _utcnow()
        if isinstance(day, str):
            day = date.fromisoformat(day)
        svc = self._service(service_id)
        window = self._window(svc)
        policy = self.config["policy"]
        step = timedelta(minutes=policy["slot_increment_min"])
        earliest = now.astimezone(self.tz) + timedelta(minutes=policy["min_notice_min"])
        latest_day = (now.astimezone(self.tz) + timedelta(
            days=policy["max_advance_days"])).date()
        if day > latest_day or day < now.astimezone(self.tz).date():
            return []
        slots: list[tuple[datetime, datetime]] = []
        cur = self.db.cursor()
        try:
            for open_t, close_t in self._hours_for(day):
                slot_start = datetime.combine(day, open_t, self.tz)
                last_start = datetime.combine(day, close_t, self.tz) - window
                cur_t = slot_start
                while cur_t <= last_start:
                    if cur_t >= earliest:
                        s_utc = _iso_utc(cur_t)
                        e_utc = _iso_utc(cur_t + window)
                        if not self._overlaps(cur, s_utc, e_utc, None):
                            slots.append((cur_t, cur_t + window))
                    cur_t += step
        finally:
            cur.close()
        return slots

    # -- hold ---------------------------------------------------------------------

    def hold_slot(
        self,
        service_id: str,
        start_local: datetime,
        client_hold_id: str,
        now: datetime | None = None,
    ) -> str:
        """Atomic check-and-set. Fails loudly if the window is taken.

        Idempotent on client_hold_id: retrying the same hold request returns
        the same hold id instead of double-holding.
        """
        now = now or _utcnow()
        svc = self._service(service_id)
        window = self._window(svc)
        if start_local.tzinfo is None:
            start_local = start_local.replace(tzinfo=self.tz)
        start_local = start_local.astimezone(self.tz)
        ttl = timedelta(minutes=self.config["policy"].get("hold_ttl_min", 10))
        s_utc, e_utc = _iso_utc(start_local), _iso_utc(start_local + window)

        cur = self.db.cursor()
        try:
            cur.execute("BEGIN IMMEDIATE")
            existing = cur.execute(
                "SELECT id, hold_expires_utc FROM bookings WHERE client_hold_id = ?",
                (client_hold_id,),
            ).fetchone()
            if existing:
                cur.execute("COMMIT")
                return existing["id"]
            if self._overlaps(cur, s_utc, e_utc, None):
                cur.execute("ROLLBACK")
                raise SlotConflictError(
                    f"slot {start_local.isoformat()} overlaps an existing hold/booking"
                )
            hold_id = _new_id("HOLD")
            cur.execute(
                """
                INSERT INTO bookings
                    (id, client_hold_id, service_id, start_utc, end_utc,
                     status, hold_expires_utc, created_utc)
                VALUES (?, ?, ?, ?, ?, 'held', ?, ?)
                """,
                (hold_id, client_hold_id, service_id, s_utc, e_utc,
                 _iso_utc(now + ttl), _iso_utc(now)),
            )
            cur.execute("COMMIT")
            return hold_id
        finally:
            cur.close()

    # -- commit ---------------------------------------------------------------------

    def commit_booking(
        self,
        hold_id: str,
        customer_name: str,
        customer_phone: str,
        idempotency_key: str,
        now: datetime | None = None,
    ) -> tuple[str, bool]:
        """Hold -> confirmed booking. Returns (booking_id, replayed).

        Idempotent on idempotency_key: a retry with the same key returns the
        original booking id and never creates a second booking. The overlap
        is re-checked inside the transaction, so a slot taken after the hold
        fails the commit instead of double-booking.
        """
        now = now or _utcnow()
        cur = self.db.cursor()
        try:
            cur.execute("BEGIN IMMEDIATE")
            prior = cur.execute(
                "SELECT id FROM bookings WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if prior:
                cur.execute("COMMIT")
                return prior["id"], True
            hold = cur.execute(
                "SELECT * FROM bookings WHERE id = ?", (hold_id,)
            ).fetchone()
            if hold is None or hold["status"] not in ("held",):
                cur.execute("ROLLBACK")
                raise HoldNotFoundError(f"no active hold: {hold_id}")
            if hold["hold_expires_utc"] and hold["hold_expires_utc"] <= _iso_utc(now):
                cur.execute("ROLLBACK")
                raise HoldExpiredError(f"hold expired: {hold_id}")
            if self._overlaps(cur, hold["start_utc"], hold["end_utc"], hold_id):
                cur.execute("ROLLBACK")
                raise SlotConflictError("slot was taken after the hold")
            booking_id = _new_id("FD")
            cur.execute(
                """
                UPDATE bookings
                   SET id = ?, idempotency_key = ?, customer_name = ?,
                       customer_phone = ?, status = 'confirmed',
                       hold_expires_utc = NULL
                 WHERE id = ?
                """,
                (booking_id, idempotency_key, customer_name, customer_phone, hold_id),
            )
            cur.execute(
                "UPDATE reminders SET booking_id = ? WHERE booking_id = ?",
                (booking_id, hold_id),
            )
            start_local = datetime.fromisoformat(hold["start_utc"]).astimezone(self.tz)
            for minutes_before in self.config.get("reminders", {}).get(
                "remind_before_min", []
            ):
                remind_at = start_local - timedelta(minutes=minutes_before)
                if remind_at >= now.astimezone(self.tz):
                    cur.execute(
                        """
                        INSERT INTO reminders (id, booking_id, remind_at_utc, channel)
                        VALUES (?, ?, ?, ?)
                        """,
                        (_new_id("RM"),
                         booking_id,
                         _iso_utc(remind_at),
                         self.config["reminders"].get("confirm", "sms")),
                    )
            cur.execute("COMMIT")
            return booking_id, False
        finally:
            cur.close()

    # -- move / cancel / get ----------------------------------------------------------

    def move_booking(
        self, booking_id: str, new_start_local: datetime,
        now: datetime | None = None,
    ) -> str:
        """One atomic update to the same id. Never cancel-plus-rebook.

        If the new window is busy the original booking is left untouched.
        """
        now = now or _utcnow()
        cur = self.db.cursor()
        try:
            cur.execute("BEGIN IMMEDIATE")
            row = cur.execute(
                "SELECT * FROM bookings WHERE id = ?", (booking_id,)
            ).fetchone()
            if row is None or row["status"] != "confirmed":
                cur.execute("ROLLBACK")
                raise BookingNotFoundError(f"no confirmed booking: {booking_id}")
            svc = self._service(row["service_id"])
            window = self._window(svc)
            if new_start_local.tzinfo is None:
                new_start_local = new_start_local.replace(tzinfo=self.tz)
            new_start_local = new_start_local.astimezone(self.tz)
            s_utc = _iso_utc(new_start_local)
            e_utc = _iso_utc(new_start_local + window)
            if self._overlaps(cur, s_utc, e_utc, booking_id):
                cur.execute("ROLLBACK")
                raise SlotConflictError("new slot is taken; booking unchanged")
            old_start = datetime.fromisoformat(row["start_utc"])
            delta = (new_start_local.astimezone(timezone.utc) - old_start)
            cur.execute(
                "UPDATE bookings SET start_utc = ?, end_utc = ? WHERE id = ?",
                (s_utc, e_utc, booking_id),
            )
            for r in cur.execute(
                "SELECT id, remind_at_utc FROM reminders "
                "WHERE booking_id = ? AND sent = 0", (booking_id,)
            ).fetchall():
                shifted = datetime.fromisoformat(r["remind_at_utc"]) + delta
                cur.execute(
                    "UPDATE reminders SET remind_at_utc = ? WHERE id = ?",
                    (_iso_utc(shifted), r["id"]),
                )
            cur.execute("COMMIT")
            return booking_id
        finally:
            cur.close()

    def cancel_booking(self, booking_id: str, reason: str = "") -> None:
        cur = self.db.cursor()
        try:
            cur.execute("BEGIN IMMEDIATE")
            row = cur.execute(
                "SELECT id FROM bookings WHERE id = ? AND status = 'confirmed'",
                (booking_id,),
            ).fetchone()
            if row is None:
                cur.execute("ROLLBACK")
                raise BookingNotFoundError(f"no confirmed booking: {booking_id}")
            cur.execute(
                "UPDATE bookings SET status = 'cancelled' WHERE id = ?",
                (booking_id,),
            )
            cur.execute("COMMIT")
        finally:
            cur.close()

    def get_booking(self, booking_id: str) -> dict | None:
        row = self.db.execute(
            "SELECT * FROM bookings WHERE id = ?", (booking_id,)
        ).fetchone()
        return dict(row) if row else None

    def count_bookings(self, status: str = "confirmed") -> int:
        row = self.db.execute(
            "SELECT COUNT(*) AS n FROM bookings WHERE status = ?", (status,)
        ).fetchone()
        return row["n"]

    # -- housekeeping -------------------------------------------------------------------

    def expire_holds(self, now: datetime | None = None) -> int:
        now = now or _utcnow()
        cur = self.db.cursor()
        try:
            cur.execute("BEGIN IMMEDIATE")
            cur.execute(
                "UPDATE bookings SET status = 'expired' "
                "WHERE status = 'held' AND hold_expires_utc <= ?",
                (_iso_utc(now),),
            )
            n = cur.rowcount
            cur.execute("COMMIT")
            return n
        finally:
            cur.close()

    def reminders_due(self, now: datetime | None = None) -> list[dict]:
        now = now or _utcnow()
        rows = self.db.execute(
            """
            SELECT r.* FROM reminders r
            JOIN bookings b ON b.id = r.booking_id
            WHERE r.sent = 0 AND r.remind_at_utc <= ?
              AND b.status = 'confirmed'
            ORDER BY r.remind_at_utc
            """,
            (_iso_utc(now),),
        ).fetchall()
        return [dict(r) for r in rows]

    def mark_reminder_sent(self, reminder_id: str) -> None:
        self.db.execute(
            "UPDATE reminders SET sent = 1 WHERE id = ?", (reminder_id,)
        )
        self.db.commit()
