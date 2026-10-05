"""Transactional clinic scheduling. Python stdlib; no external API required."""

from __future__ import annotations
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc


def utcnow():
    return datetime.now(UTC)


def iso(dt):
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class Problem(Exception):
    def __init__(self, message, status=400, code="invalid_request"):
        super().__init__(message)
        self.status, self.code = status, code


SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS doctors (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, specialty TEXT NOT NULL,
 duration INTEGER NOT NULL, buffer INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS weekly (
 doctor_id TEXT REFERENCES doctors(id), weekday INTEGER, start INTEGER, end INTEGER,
 PRIMARY KEY(doctor_id,weekday,start));
CREATE TABLE IF NOT EXISTS overrides (
 doctor_id TEXT REFERENCES doctors(id), day TEXT, windows TEXT NOT NULL,
 PRIMARY KEY(doctor_id,day));
CREATE TABLE IF NOT EXISTS groups (
 id TEXT PRIMARY KEY, hold_key TEXT UNIQUE NOT NULL, fingerprint TEXT NOT NULL,
 doctor_id TEXT REFERENCES doctors(id), state TEXT NOT NULL, expires TEXT NOT NULL,
 name TEXT, phone TEXT, token_hash TEXT, interval_weeks INTEGER NOT NULL,
 created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS appointments (
 id TEXT PRIMARY KEY, group_id TEXT REFERENCES groups(id), doctor_id TEXT REFERENCES doctors(id),
 start TEXT NOT NULL, end TEXT NOT NULL, status TEXT NOT NULL,
 cancel_reason TEXT NOT NULL DEFAULT '', created TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS appointments_overlap ON appointments(doctor_id,status,start,end);
CREATE TABLE IF NOT EXISTS commits (
 key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, group_id TEXT REFERENCES groups(id));
CREATE TABLE IF NOT EXISTS audit (
 id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, actor TEXT NOT NULL,
 action TEXT NOT NULL, target TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS users (
 username TEXT PRIMARY KEY, password TEXT NOT NULL, role TEXT NOT NULL,
 active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS sessions (
 token_hash TEXT PRIMARY KEY, username TEXT REFERENCES users(username), expires TEXT NOT NULL,
 csrf TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS calls (
 id TEXT PRIMARY KEY, state TEXT NOT NULL, expires TEXT NOT NULL,
 updated TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS call_turns (
 call_id TEXT, request_id TEXT, fingerprint TEXT, response TEXT,
 PRIMARY KEY(call_id,request_id));
"""


class Engine:
    def __init__(self, config, db_path="clinic.db", clock=utcnow):
        self.config, self.db_path, self.clock = config, str(db_path), clock
        self.tz = ZoneInfo(config["clinic"]["timezone"])
        self.policy = config["policy"]
        with self.db() as db:
            db.executescript(SCHEMA)
        with self.db(True) as db:
            db.execute(
                "INSERT OR IGNORE INTO settings VALUES ('capability_secret',?)",
                (secrets.token_hex(32),),
            )
            existing_tz = db.execute(
                "SELECT value FROM settings WHERE key='timezone'"
            ).fetchone()
            if existing_tz and existing_tz[0] != str(self.tz):
                raise Problem(
                    "Timezone cannot change on an existing database; migrate schedules explicitly."
                )
            db.execute(
                "INSERT OR IGNORE INTO settings VALUES ('timezone',?)", (str(self.tz),)
            )
            seeded = db.execute(
                "SELECT value FROM settings WHERE key='seeded'"
            ).fetchone()
            if not seeded:
                for d in config["doctors"]:
                    db.execute(
                        "INSERT INTO doctors VALUES (?,?,?,?,?,1)",
                        (
                            d["id"],
                            d["name"],
                            d["specialty"],
                            d["duration_minutes"],
                            d.get("buffer_minutes", 0),
                        ),
                    )
                    for weekday in range(5):
                        for start, end in [(540, 720), (780, 1020)]:
                            db.execute(
                                "INSERT INTO weekly VALUES (?,?,?,?)",
                                (d["id"], weekday, start, end),
                            )
                db.execute("INSERT INTO settings VALUES ('seeded','1')")

    @contextmanager
    def db(self, write=False):
        db = sqlite3.connect(self.db_path, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA journal_mode=WAL")
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            if write:
                db.execute("COMMIT")
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        finally:
            db.close()

    def audit(self, db, actor, action, target):
        db.execute(
            "INSERT INTO audit(at,actor,action,target) VALUES (?,?,?,?)",
            (iso(self.clock()), actor, action, target),
        )

    def doctor(self, db, doctor_id):
        if not isinstance(doctor_id, str):
            raise Problem("Doctor id must be a string")
        d = db.execute(
            "SELECT * FROM doctors WHERE id=? AND active=1", (doctor_id,)
        ).fetchone()
        if not d:
            raise Problem("Doctor not available", 404, "doctor_not_found")
        return dict(d)

    def doctors(self):
        with self.db() as db:
            return [
                dict(x)
                for x in db.execute(
                    "SELECT * FROM doctors WHERE active=1 ORDER BY name"
                )
            ]

    def windows(self, db, doctor_id, day):
        row = db.execute(
            "SELECT windows FROM overrides WHERE doctor_id=? AND day=?",
            (doctor_id, day.isoformat()),
        ).fetchone()
        if row:
            return json.loads(row[0])
        return [
            [r[0], r[1]]
            for r in db.execute(
                "SELECT start,end FROM weekly WHERE doctor_id=? AND weekday=? ORDER BY start",
                (doctor_id, day.weekday()),
            )
        ]

    def wall(self, day, minute):
        naive = datetime.combine(day, time()) + timedelta(minutes=minute)
        dt = naive.replace(tzinfo=self.tz)
        # Omit nonexistent and ambiguous wall times; never silently shift a booking.
        if dt.astimezone(UTC).astimezone(self.tz).replace(tzinfo=None) != naive:
            return None
        if dt.utcoffset() != naive.replace(tzinfo=self.tz, fold=1).utcoffset():
            return None
        return dt

    def expire(self, db, now):
        db.execute(
            "UPDATE appointments SET status='expired' WHERE status='held' AND group_id IN (SELECT id FROM groups WHERE expires<=? AND state='held')",
            (iso(now),),
        )
        db.execute(
            "UPDATE groups SET state='expired' WHERE state='held' AND expires<=?",
            (iso(now),),
        )

    def overlap(self, db, doctor_id, start, end, now, exclude=None):
        return db.execute(
            """SELECT a.id FROM appointments a JOIN groups g ON g.id=a.group_id
          WHERE a.doctor_id=? AND a.start<? AND a.end>? AND a.id!=?
          AND (a.status='confirmed' OR (a.status='held' AND g.expires>?)) LIMIT 1""",
            (doctor_id, iso(end), iso(start), exclude or "", iso(now)),
        ).fetchone()

    def candidates(self, db, doctor_id, day, now, policy=True):
        d = self.doctor(db, doctor_id)
        if policy and not now.astimezone(self.tz).date() <= day <= now.astimezone(
            self.tz
        ).date() + timedelta(days=self.policy["max_advance_days"]):
            return []
        result = []
        width = d["duration"] + d["buffer"]
        for start, end in self.windows(db, doctor_id, day):
            for minute in range(start, end - width + 1, width):
                s = self.wall(day, minute)
                if not s or (
                    policy
                    and s < now + timedelta(minutes=self.policy["min_notice_minutes"])
                ):
                    continue
                e = s.astimezone(UTC) + timedelta(minutes=width)
                if (
                    e.astimezone(self.tz).date() != day
                    or e.astimezone(self.tz).hour * 60 + e.astimezone(self.tz).minute
                    > end
                ):
                    continue
                result.append((s, e))
        return result

    def slots(self, doctor_id, day):
        day = self.parse_day(day)
        now = self.clock()
        with self.db() as db:
            return [
                self.slot_view(s, e)
                for s, e in self.candidates(db, doctor_id, day, now)
                if not self.overlap(db, doctor_id, s, e, now)
            ]

    def slot_view(self, s, e):
        return {
            "start": s.astimezone(self.tz).isoformat(),
            "end": e.astimezone(self.tz).isoformat(),
            "label": s.astimezone(self.tz).strftime("%a, %b %d · %I:%M %p"),
            "timezone": str(self.tz),
        }

    @staticmethod
    def parse_day(value):
        try:
            return date.fromisoformat(value)
        except (ValueError, TypeError):
            raise Problem("Date must use YYYY-MM-DD")

    def parse_start(self, value):
        try:
            dt = datetime.fromisoformat(value)
            if dt.tzinfo is None or dt.second or dt.microsecond:
                raise ValueError()
            return dt.astimezone(self.tz)
        except (ValueError, TypeError):
            raise Problem(
                "Start must be an ISO datetime with timezone offset and whole minutes"
            )

    def validate_slot(self, db, doctor_id, s, now, exclude=None):
        candidate = next(
            (
                (a, b)
                for a, b in self.candidates(db, doctor_id, s.date(), now)
                if iso(a) == iso(s)
            ),
            None,
        )
        if not candidate:
            raise Problem(
                "Time is outside doctor availability or booking policy",
                409,
                "unavailable",
            )
        if self.overlap(db, doctor_id, *candidate, now, exclude):
            raise Problem(
                "Time was just taken. Choose another slot.", 409, "slot_conflict"
            )
        return candidate

    @staticmethod
    def key(value):
        if not isinstance(value, str) or not 8 <= len(value) <= 160:
            raise Problem("An idempotency key of 8–160 characters is required")
        return value

    def hold(self, doctor_id, start, key, count=1, interval_weeks=1, actor="patient"):
        key = self.key(key)
        if (
            type(count) is not int
            or not 1 <= count <= self.policy["max_occurrences"]
            or type(interval_weeks) is not int
            or not 1 <= interval_weeks <= 4
        ):
            raise Problem("Use 1–12 occurrences and a 1–4 week interval")
        s = self.parse_start(start)
        fingerprint = digest(json.dumps([doctor_id, iso(s), count, interval_weeks]))
        now = self.clock()
        with self.db(True) as db:
            self.expire(db, now)
            self.doctor(db, doctor_id)
            prior = db.execute(
                "SELECT * FROM groups WHERE hold_key=?", (key,)
            ).fetchone()
            if prior:
                if prior["fingerprint"] != fingerprint:
                    raise Problem(
                        "Idempotency key was used for a different request",
                        409,
                        "idempotency_conflict",
                    )
                if prior["state"] != "held":
                    raise Problem(
                        "Hold is no longer active. Start a new booking.",
                        409,
                        "hold_expired",
                    )
                return self.hold_view(db, prior["id"])
            gid = str(secrets.randbelow(9000000000) + 1000000000)
            expires = iso(now + timedelta(minutes=self.policy["hold_minutes"]))
            db.execute(
                "INSERT INTO groups(id,hold_key,fingerprint,doctor_id,state,expires,interval_weeks,created) VALUES (?,?,?,?,'held',?,?,?)",
                (gid, key, fingerprint, doctor_id, expires, interval_weeks, iso(now)),
            )
            for i in range(count):
                local = (
                    s.replace(tzinfo=None) + timedelta(weeks=i * interval_weeks)
                ).replace(tzinfo=self.tz)
                a, b = self.validate_slot(db, doctor_id, local, now)
                db.execute(
                    "INSERT INTO appointments(id,group_id,doctor_id,start,end,status,created) VALUES (?,?,?,?,?,'held',?)",
                    (secrets.token_hex(8), gid, doctor_id, iso(a), iso(b), iso(now)),
                )
            self.audit(db, actor, "hold.create", gid)
            return self.hold_view(db, gid)

    def hold_view(self, db, gid):
        g = db.execute("SELECT * FROM groups WHERE id=?", (gid,)).fetchone()
        rows = db.execute(
            "SELECT * FROM appointments WHERE group_id=? ORDER BY start", (gid,)
        ).fetchall()
        return {
            "hold_id": gid,
            "expires": g["expires"],
            "appointments": [self.appointment_view(db, x) for x in rows],
        }

    def manage_code(self, db, gid):
        secret = db.execute(
            "SELECT value FROM settings WHERE key='capability_secret'"
        ).fetchone()[0]
        value = hmac.new(secret.encode(), gid.encode(), hashlib.sha256).digest()
        return str(int.from_bytes(value[:8], "big") % 10**12).zfill(12)

    def commit(self, hold_id, name, phone, key, actor="patient"):
        key = self.key(key)
        if not isinstance(hold_id, str):
            raise Problem("Hold id must be a string")
        if not isinstance(name, str) or not 2 <= len(name.strip()) <= 100:
            raise Problem("Enter a patient name of 2–100 characters")
        if (
            not isinstance(phone, str)
            or not re.fullmatch(r"\+?[0-9 ()-]{7,25}", phone)
            or not 7 <= len(re.sub(r"\D", "", phone)) <= 15
        ):
            raise Problem("Enter a valid phone number with country code")
        name, phone = name.strip(), phone.strip()
        fingerprint = digest(json.dumps([hold_id, name, phone]))
        now = self.clock()
        with self.db(True) as db:
            prior = db.execute("SELECT * FROM commits WHERE key=?", (key,)).fetchone()
            if prior:
                if prior["fingerprint"] != fingerprint:
                    raise Problem(
                        "Idempotency key was used for a different request",
                        409,
                        "idempotency_conflict",
                    )
                return self.confirmation(db, prior["group_id"], True)
            g = db.execute("SELECT * FROM groups WHERE id=?", (hold_id,)).fetchone()
            if not g or g["state"] != "held" or g["expires"] <= iso(now):
                raise Problem(
                    "Hold expired or already used. Choose a time again.",
                    409,
                    "hold_expired",
                )
            for a in db.execute(
                "SELECT * FROM appointments WHERE group_id=?", (hold_id,)
            ).fetchall():
                self.validate_slot(
                    db, a["doctor_id"], self.parse_start(a["start"]), now, a["id"]
                )
            token = self.manage_code(db, hold_id)
            db.execute(
                "UPDATE groups SET state='confirmed',name=?,phone=?,token_hash=? WHERE id=?",
                (name, phone, digest(token), hold_id),
            )
            db.execute(
                "UPDATE appointments SET status='confirmed' WHERE group_id=?",
                (hold_id,),
            )
            db.execute(
                "INSERT INTO commits VALUES (?,?,?)", (key, fingerprint, hold_id)
            )
            self.audit(db, actor, "booking.confirm", hold_id)
            return self.confirmation(db, hold_id)

    def confirmation(self, db, gid, replayed=False):
        return {
            "reference": gid,
            "manage_code": self.manage_code(db, gid),
            "replayed": replayed,
            "appointments": [
                self.appointment_view(db, x)
                for x in db.execute(
                    "SELECT * FROM appointments WHERE group_id=? ORDER BY start", (gid,)
                )
            ],
        }

    def appointment_view(self, db, row):
        a = dict(row)
        doctor = db.execute(
            "SELECT name,specialty,duration FROM doctors WHERE id=?", (a["doctor_id"],)
        ).fetchone()
        g = db.execute(
            "SELECT name,phone,interval_weeks FROM groups WHERE id=?", (a["group_id"],)
        ).fetchone()
        s = datetime.fromisoformat(a["start"])
        a["future"] = s > self.clock()
        a.update(self.slot_view(s, s + timedelta(minutes=doctor["duration"])))
        a.update(
            doctor_name=doctor["name"],
            specialty=doctor["specialty"],
            patient_name=g["name"],
            phone=g["phone"],
            interval_weeks=g["interval_weeks"],
        )
        return a

    def authorize_group(self, db, gid, code):
        if not isinstance(gid, str) or not re.fullmatch(r"\d{10}", gid):
            raise Problem("Reference or management code is incorrect", 403, "forbidden")
        row = db.execute(
            "SELECT token_hash FROM groups WHERE id=? AND state='confirmed'", (gid,)
        ).fetchone()
        if (
            not row
            or not row[0]
            or not isinstance(code, str)
            or not hmac.compare_digest(row[0], digest(code))
        ):
            raise Problem("Reference or management code is incorrect", 403, "forbidden")

    def patient_bookings(self, gid, code):
        with self.db() as db:
            self.authorize_group(db, gid, code)
            return {
                "reference": gid,
                "appointments": [
                    self.appointment_view(db, x)
                    for x in db.execute(
                        "SELECT * FROM appointments WHERE group_id=? ORDER BY start",
                        (gid,),
                    )
                ],
            }

    def cancel(
        self,
        gid,
        code=None,
        appointment_id=None,
        reason="",
        actor="patient",
        staff=False,
    ):
        now = self.clock()
        if not isinstance(gid, str) or (
            appointment_id is not None and not isinstance(appointment_id, str)
        ):
            raise Problem("Invalid booking reference or appointment id")
        if not isinstance(reason, str) or len(reason) > 300:
            raise Problem("Cancellation reason must be at most 300 characters")
        with self.db(True) as db:
            if not staff:
                self.authorize_group(db, gid, code)
            rows = db.execute(
                "SELECT * FROM appointments WHERE group_id=? AND (? IS NULL OR id=?)",
                (gid, appointment_id, appointment_id),
            ).fetchall()
            if not rows:
                raise Problem("Appointment not found", 404, "not_found")
            confirmed = [x for x in rows if x["status"] == "confirmed"]
            if appointment_id is None:
                confirmed = [
                    x for x in confirmed if datetime.fromisoformat(x["start"]) > now
                ]
            elif any(datetime.fromisoformat(x["start"]) <= now for x in confirmed):
                raise Problem(
                    "Past appointments cannot be cancelled", 409, "past_appointment"
                )
            if not staff and any(
                datetime.fromisoformat(x["start"])
                < now + timedelta(minutes=self.policy["cancellation_notice_minutes"])
                for x in confirmed
            ):
                raise Problem(
                    "Too close to appointment time. Please call clinic staff.",
                    409,
                    "cancellation_cutoff",
                )
            for a in confirmed:
                db.execute(
                    "UPDATE appointments SET status='cancelled',cancel_reason=? WHERE id=?",
                    (reason, a["id"]),
                )
                self.audit(db, actor, "booking.cancel", a["id"])
            return {
                "cancelled": [x["id"] for x in confirmed],
                "already_cancelled": not confirmed,
            }

    def move(self, appointment_id, start, code=None, actor="patient", staff=False):
        now = self.clock()
        if not isinstance(appointment_id, str):
            raise Problem("Appointment id must be a string")
        s = self.parse_start(start)
        with self.db(True) as db:
            a = db.execute(
                "SELECT * FROM appointments WHERE id=? AND status='confirmed'",
                (appointment_id,),
            ).fetchone()
            if not a:
                raise Problem("Confirmed appointment not found", 404, "not_found")
            if not staff:
                self.authorize_group(db, a["group_id"], code)
            if datetime.fromisoformat(a["start"]) <= now or (
                not staff
                and datetime.fromisoformat(a["start"])
                < now + timedelta(minutes=self.policy["cancellation_notice_minutes"])
            ):
                raise Problem(
                    "Too close to appointment time. Please contact clinic staff.",
                    409,
                    "cancellation_cutoff",
                )
            s, e = self.validate_slot(db, a["doctor_id"], s, now, a["id"])
            db.execute(
                "UPDATE appointments SET start=?,end=? WHERE id=?",
                (iso(s), iso(e), appointment_id),
            )
            self.audit(db, actor, "booking.move", appointment_id)
            return self.appointment_view(
                db,
                db.execute(
                    "SELECT * FROM appointments WHERE id=?", (appointment_id,)
                ).fetchone(),
            )

    def release(self, hold_id, key):
        self.key(key)
        if not isinstance(hold_id, str):
            raise Problem("Hold id must be a string")
        with self.db(True) as db:
            g = db.execute(
                "SELECT * FROM groups WHERE id=? AND hold_key=?", (hold_id, key)
            ).fetchone()
            if not g:
                raise Problem("Hold not found", 404)
            if g["state"] == "held":
                db.execute("UPDATE groups SET state='expired' WHERE id=?", (hold_id,))
                db.execute(
                    "UPDATE appointments SET status='expired' WHERE group_id=? AND status='held'",
                    (hold_id,),
                )
            return {"released": hold_id}

    @staticmethod
    def clean_windows(windows):
        if not isinstance(windows, list) or len(windows) > 8:
            raise Problem("Supply up to eight availability windows")
        result = []
        for w in windows:
            if not isinstance(w, list) or len(w) != 2:
                raise Problem("Windows use [HH:MM, HH:MM]")
            try:
                values = []
                for v in w:
                    if not isinstance(v, str) or not re.fullmatch(r"\d{2}:\d{2}", v):
                        raise ValueError()
                    t = time.fromisoformat(v)
                    values.append(t.hour * 60 + t.minute)
                if values[0] >= values[1]:
                    raise ValueError()
            except (ValueError, TypeError):
                raise Problem(
                    "Use valid same-day availability windows, e.g. 09:00–17:00"
                )
            result.append(values)
        result.sort()
        if any(a[1] > b[0] for a, b in zip(result, result[1:])):
            raise Problem("Availability windows must not overlap")
        return result

    def schedule(self, doctor_id):
        with self.db() as db:
            self.doctor(db, doctor_id)

            def fmt(w):
                return [
                    [f"{s//60:02}:{s%60:02}", f"{e//60:02}:{e%60:02}"] for s, e in w
                ]

            return {
                "weekly": {
                    str(i): fmt(
                        [
                            [r[0], r[1]]
                            for r in db.execute(
                                "SELECT start,end FROM weekly WHERE doctor_id=? AND weekday=? ORDER BY start",
                                (doctor_id, i),
                            )
                        ]
                    )
                    for i in range(7)
                },
                "overrides": {
                    r[0]: fmt(json.loads(r[1]))
                    for r in db.execute(
                        "SELECT day,windows FROM overrides WHERE doctor_id=? ORDER BY day",
                        (doctor_id,),
                    )
                },
            }

    def set_availability(
        self, doctor_id, windows, weekday=None, day=None, remove=False, actor="manager"
    ):
        now = self.clock()
        if type(remove) is not bool:
            raise Problem("Remove must be a boolean")
        if (weekday is None) == (day is None):
            raise Problem("Specify exactly one weekday or date")
        if weekday is not None and (type(weekday) is not int or not 0 <= weekday <= 6):
            raise Problem("Weekday must be 0 (Monday) through 6 (Sunday)")
        if day is not None:
            self.parse_day(day)
        values = self.clean_windows(windows)
        with self.db(True) as db:
            self.doctor(db, doctor_id)
            if day is not None:
                if remove:
                    db.execute(
                        "DELETE FROM overrides WHERE doctor_id=? AND day=?",
                        (doctor_id, day),
                    )
                else:
                    db.execute(
                        "INSERT OR REPLACE INTO overrides VALUES (?,?,?)",
                        (doctor_id, day, json.dumps(values)),
                    )
            else:
                db.execute(
                    "DELETE FROM weekly WHERE doctor_id=? AND weekday=?",
                    (doctor_id, weekday),
                )
                db.executemany(
                    "INSERT INTO weekly VALUES (?,?,?,?)",
                    [(doctor_id, weekday, s, e) for s, e in values],
                )
            live = db.execute(
                """SELECT a.* FROM appointments a JOIN groups g ON g.id=a.group_id WHERE a.doctor_id=? AND a.start>?
              AND (a.status='confirmed' OR (a.status='held' AND g.expires>?))""",
                (doctor_id, iso(now), iso(now)),
            ).fetchall()
            for a in live:
                s = self.parse_start(a["start"])
                if not any(
                    iso(x) == a["start"] and iso(y) == a["end"]
                    for x, y in self.candidates(
                        db, doctor_id, s.date(), now, policy=False
                    )
                ):
                    raise Problem(
                        "Schedule change conflicts with an existing appointment or hold. Move/cancel it first.",
                        409,
                        "schedule_conflict",
                    )
            self.audit(db, actor, "availability.update", doctor_id)
        return self.schedule(doctor_id)

    def save_doctor(self, body, actor):
        did, name, specialty = body.get("id"), body.get("name"), body.get("specialty")
        if (
            not isinstance(did, str)
            or not re.fullmatch(r"[a-z0-9-]{3,60}", did)
            or not isinstance(name, str)
            or not 2 <= len(name) <= 100
            or not isinstance(specialty, str)
            or not 2 <= len(specialty) <= 100
        ):
            raise Problem("Doctor needs an id, name and specialty")
        duration, buffer = body.get("duration", 30), body.get("buffer", 0)
        if (
            type(duration) is not int
            or not 5 <= duration <= 180
            or type(buffer) is not int
            or not 0 <= buffer <= 60
        ):
            raise Problem("Duration must be 5–180 minutes; buffer 0–60 minutes")
        if type(body.get("active", True)) is not bool:
            raise Problem("Active must be boolean")
        active = int(body.get("active", True))
        with self.db(True) as db:
            old = db.execute("SELECT * FROM doctors WHERE id=?", (did,)).fetchone()
            if old and (
                old["duration"] != duration or old["buffer"] != buffer or not active
            ):
                live = db.execute(
                    """SELECT 1 FROM appointments a JOIN groups g ON g.id=a.group_id WHERE a.doctor_id=? AND a.end>?
                 AND (a.status='confirmed' OR (a.status='held' AND g.expires>?)) LIMIT 1""",
                    (did, iso(self.clock()), iso(self.clock())),
                ).fetchone()
                if live:
                    raise Problem(
                        "Move/cancel future bookings and holds before changing duration or deactivating.",
                        409,
                    )
            db.execute(
                "INSERT INTO doctors VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,specialty=excluded.specialty,duration=excluded.duration,buffer=excluded.buffer,active=excluded.active",
                (did, name, specialty, duration, buffer, active),
            )
            self.audit(db, actor, "doctor.update", did)
        return {"id": did}

    def overview(self, day=None, status=None, doctor_id=None):
        now = self.clock()
        with self.db(True) as db:
            self.expire(db, now)
            conditions, args = ["a.status IN ('confirmed','cancelled')"], []
            if day:
                d = self.parse_day(day)
                lo = datetime.combine(d, time(), self.tz)
                hi = datetime.combine(d + timedelta(days=1), time(), self.tz)
                conditions.append("a.start>=? AND a.start<?")
                args += [iso(lo), iso(hi)]
            if status:
                conditions.append("a.status=?")
                args.append(status)
            if doctor_id:
                conditions.append("a.doctor_id=?")
                args.append(doctor_id)
            rows = db.execute(
                "SELECT a.* FROM appointments a WHERE "
                + " AND ".join(conditions)
                + " ORDER BY a.start LIMIT 1000",
                args,
            ).fetchall()
            stats = {
                r[0]: r[1]
                for r in db.execute(
                    "SELECT status,count(*) FROM appointments GROUP BY status"
                )
            }
            calls = db.execute("SELECT count(*) FROM calls").fetchone()[0]
            today = now.astimezone(self.tz).date()
            today_rows = db.execute(
                "SELECT start FROM appointments WHERE status='confirmed'"
            ).fetchall()
            return {
                "appointments": [self.appointment_view(db, x) for x in rows],
                "stats": stats,
                "today": sum(
                    datetime.fromisoformat(x[0]).astimezone(self.tz).date() == today
                    for x in today_rows
                ),
                "calls": calls,
                "audit": [
                    dict(r)
                    for r in db.execute("SELECT * FROM audit ORDER BY id DESC LIMIT 50")
                ],
                "doctors": [
                    dict(r) for r in db.execute("SELECT * FROM doctors ORDER BY name")
                ],
            }

    def maintenance(self):
        """Expire holds and remove sensitive call receipts/sessions after their TTL."""
        now = iso(self.clock())
        with self.db(True) as db:
            self.expire(db, self.clock())
            db.execute(
                "DELETE FROM call_turns WHERE call_id IN (SELECT id FROM calls WHERE expires<=?)",
                (now,),
            )
            db.execute("DELETE FROM calls WHERE expires<=?", (now,))
            db.execute("DELETE FROM sessions WHERE expires<=?", (now,))
