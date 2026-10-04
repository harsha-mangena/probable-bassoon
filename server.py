#!/usr/bin/env python3
"""Front Desk server — stdlib only, no dependencies.

Serves the web UI and a JSON API over the real booking engine.
Every booking id the UI shows comes from the engine; retries replay the
same idempotency key against the engine, so the "no duplicate" proof is
real, not a mock.

Run:
    python3 server.py [port]        # default 8080
Then open http://localhost:8080/ in a browser.
"""

import json
import threading
import tomllib
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs
from zoneinfo import ZoneInfo

from engine import (
    BookingError,
    BookingNotFoundError,
    Engine,
    HoldExpiredError,
    HoldNotFoundError,
    SlotConflictError,
    _utcnow,
)

CONFIG_PATH = "salon.toml"
DB_PATH = "frontdesk.db"


def load_config() -> dict:
    with open(CONFIG_PATH, "rb") as f:
        return tomllib.load(f)


CONFIG = load_config()
TZ = ZoneInfo(CONFIG["business"]["timezone"])
ENGINE = Engine(CONFIG, db_path=DB_PATH)

SESSIONS: dict[str, dict] = {}
CALL_LOG: list[dict] = []
LOCK = threading.Lock()


# --------------------------------------------------------------------------
# helpers


def iso_local(dt: datetime) -> str:
    return dt.astimezone(TZ).isoformat()


def slot_label(start: datetime) -> str:
    return start.astimezone(TZ).strftime("%a %b %d, %-I:%M %p")


def booking_view(row: dict) -> dict:
    start = datetime.fromisoformat(row["start_utc"]).astimezone(TZ)
    end = datetime.fromisoformat(row["end_utc"]).astimezone(TZ)
    svc = next((s for s in CONFIG["services"] if s["id"] == row["service_id"]),
               {"name": row["service_id"]})
    return {
        "id": row["id"],
        "service": svc["name"],
        "start": iso_local(start),
        "end": iso_local(end),
        "label": f"{slot_label(start)} – {end.astimezone(TZ).strftime('%-I:%M %p')}",
        "customer_name": row.get("customer_name"),
        "customer_phone": row.get("customer_phone"),
        "status": row["status"],
    }


def match_service(want: str) -> dict | None:
    matches = [s for s in CONFIG["services"]
               if s["id"] in want.lower() or s["name"].lower() in want.lower()]
    return matches[0] if len(matches) == 1 else None


def find_slots(service_id: str, now: datetime):
    """First day (from today) with live slots, up to max_advance_days."""
    day = now.astimezone(TZ).date()
    for _ in range(CONFIG["policy"]["max_advance_days"]):
        slots = ENGINE.list_free_slots(service_id, day, now=now)
        if slots:
            return slots
        day += timedelta(days=1)
    return []


def log_call(transcript, booking_id=None, handoff=False):
    CALL_LOG.append({
        "at": _utcnow().isoformat(),
        "booking_id": booking_id,
        "handoff": handoff,
        "transcript": transcript,
    })


def handoff_message(reason: str) -> str:
    phone = CONFIG["business"]["transfer_phone"]
    return ("I'm having trouble with the booking system on my end, so I don't "
            f"want to guess. Let me connect you with the shop at {phone} — "
            "I've saved everything you've told me so far.")


# --------------------------------------------------------------------------
# API


def api_call_start(body: dict) -> tuple[int, dict]:
    now = _utcnow()
    want = (body.get("want") or "").strip()
    transcript = [
        {"speaker": "agent",
         "text": f"Hi, thanks for calling {CONFIG['business']['name']}. "
                 "What can I book for you today?"},
        {"speaker": "caller", "text": want},
    ]
    svc = match_service(want)
    if svc is None:
        msg = handoff_message("ambiguous")
        transcript.append({"speaker": "agent", "text": msg})
        log_call(transcript, handoff=True)
        return 200, {"handoff": True, "transcript": transcript,
                     "transfer_phone": CONFIG["business"]["transfer_phone"]}
    slots = find_slots(svc["id"], now)
    if not slots:
        msg = handoff_message("no_availability")
        transcript.append({"speaker": "agent", "text": msg})
        log_call(transcript, handoff=True)
        return 200, {"handoff": True, "transcript": transcript,
                     "transfer_phone": CONFIG["business"]["transfer_phone"]}
    sid = uuid.uuid4().hex[:12]
    shown = slots[:6]
    transcript.append(
        {"speaker": "agent",
         "text": f"Got it — {svc['name'].lower()}. Here are the next open times:"})
    with LOCK:
        SESSIONS[sid] = {
            "stage": "slots", "service_id": svc["id"],
            "slots": [(iso_local(s), iso_local(e)) for s, e in shown],
            "transcript": transcript, "created": now,
        }
    return 200, {
        "session_id": sid,
        "service": svc["name"],
        "slots": [{"index": i, "start": iso_local(s), "end": iso_local(e),
                   "label": slot_label(s)} for i, (s, e) in enumerate(shown)],
        "transcript": transcript,
    }


def api_call_choose(body: dict) -> tuple[int, dict]:
    now = _utcnow()
    sid = body.get("session_id")
    pick = body.get("pick", 0)
    with LOCK:
        sess = SESSIONS.get(sid)
    if not sess or sess["stage"] != "slots":
        return 400, {"error": "unknown or expired session; start a new call"}
    try:
        start_local = datetime.fromisoformat(sess["slots"][pick][0])
    except (IndexError, ValueError):
        return 400, {"error": "invalid slot choice"}
    sess["transcript"].append(
        {"speaker": "caller",
         "text": datetime.fromisoformat(sess["slots"][pick][0])
                 .strftime("%A at %-I:%M %p")})
    try:
        hold_id = ENGINE.hold_slot(
            sess["service_id"], start_local,
            client_hold_id=f"sess-{sid}", now=now)
    except SlotConflictError:
        msg = ("That time just got taken — let me check what's open now instead.")
        sess["transcript"].append({"speaker": "agent", "text": msg})
        slots = find_slots(sess["service_id"], now)
        with LOCK:
            sess["stage"] = "slots"
            sess["slots"] = [(iso_local(s), iso_local(e)) for s, e in slots[:6]]
        log_call(sess["transcript"], handoff=True)
        return 409, {"conflict": True,
                     "slots": [{"index": i, "start": iso_local(s),
                                "end": iso_local(e), "label": slot_label(s)}
                               for i, (s, e) in enumerate(slots[:6])],
                     "transcript": sess["transcript"]}
    except BookingError as exc:
        msg = handoff_message("hold_failed")
        sess["transcript"].append({"speaker": "agent", "text": msg})
        log_call(sess["transcript"], handoff=True)
        return 200, {"handoff": True, "transcript": sess["transcript"],
                     "error": str(exc)}
    with LOCK:
        sess["stage"] = "held"
        sess["hold_id"] = hold_id
        sess["slot_label"] = slot_label(start_local)
    sess["transcript"].append(
        {"speaker": "agent",
         "text": "I've held that slot for you. What's your name and phone number?"})
    return 200, {"session_id": sid, "hold_id": hold_id,
                 "slot": sess["slot_label"], "transcript": sess["transcript"]}


def api_call_commit(body: dict) -> tuple[int, dict]:
    now = _utcnow()
    sid = body.get("session_id")
    name = (body.get("name") or "").strip()
    phone = (body.get("phone") or "").strip()
    with LOCK:
        sess = SESSIONS.get(sid)
    if not sess or sess["stage"] != "held" or not name:
        return 400, {"error": "session not ready to commit (need slot + name)"}
    idem_key = f"sess-{sid}-commit"
    try:
        booking_id, replayed = ENGINE.commit_booking(
            sess["hold_id"], name, phone, idem_key, now=now)
    except (HoldExpiredError, HoldNotFoundError, SlotConflictError,
            BookingError) as exc:
        msg = handoff_message("commit_failed")
        sess["transcript"].append({"speaker": "agent", "text": msg})
        log_call(sess["transcript"], handoff=True)
        return 200, {"handoff": True, "transcript": sess["transcript"],
                     "error": str(exc)}
    # Confirmation language ONLY after the booking id exists.
    svc = next(s for s in CONFIG["services"] if s["id"] == sess["service_id"])
    sess["transcript"].append(
        {"speaker": "agent",
         "text": f"You're booked, {name} — {svc['name'].lower()} "
                 f"{sess['slot_label']}. Your booking reference is {booking_id}."})
    with LOCK:
        sess["stage"] = "committed"
        sess["booking_id"] = booking_id
        sess["idem_key"] = idem_key
    log_call(sess["transcript"], booking_id=booking_id)
    return 200, {"session_id": sid, "booking_id": booking_id,
                 "replayed": replayed, "transcript": sess["transcript"]}


def api_call_retry(body: dict) -> tuple[int, dict]:
    """Replay the commit with the SAME idempotency key. Must return the
    same booking id and must not create a second booking."""
    now = _utcnow()
    sid = body.get("session_id")
    with LOCK:
        sess = SESSIONS.get(sid)
    if not sess or sess["stage"] != "committed":
        return 400, {"error": "nothing to retry yet — complete a booking first"}
    before = ENGINE.count_bookings()
    booking_id, replayed = ENGINE.commit_booking(
        sess["hold_id"], "", "", sess["idem_key"], now=now)
    after = ENGINE.count_bookings()
    return 200, {
        "booking_id": booking_id,
        "replayed": replayed,
        "same_id": booking_id == sess["booking_id"],
        "bookings_before": before,
        "bookings_after": after,
        "no_duplicate": before == after,
    }


def api_slots(query: dict) -> tuple[int, dict]:
    service_id = query.get("service_id", ["haircut"])[0]
    day = query.get("day", [None])[0]
    now = _utcnow()
    if not day:
        day = now.astimezone(TZ).date().isoformat()
    try:
        slots = ENGINE.list_free_slots(service_id, day, now=now)
    except BookingError as exc:
        return 400, {"error": str(exc)}
    return 200, {"day": day, "slots": [
        {"start": iso_local(s), "end": iso_local(e), "label": slot_label(s)}
        for s, e in slots[:24]]}


def api_bookings() -> tuple[int, dict]:
    rows = ENGINE.db.execute(
        "SELECT * FROM bookings WHERE status='confirmed' ORDER BY start_utc"
    ).fetchall()
    return 200, {"bookings": [booking_view(dict(r)) for r in rows]}


def api_booking_move(body: dict) -> tuple[int, dict]:
    try:
        new_start = datetime.fromisoformat(body["new_start"])
    except (KeyError, ValueError):
        return 400, {"error": "new_start must be an ISO datetime"}
    try:
        bid = ENGINE.move_booking(body["booking_id"], new_start)
    except (BookingNotFoundError, SlotConflictError) as exc:
        return 409, {"error": str(exc)}
    return 200, {"booking_id": bid, "booking": booking_view(ENGINE.get_booking(bid))}


def api_booking_cancel(body: dict) -> tuple[int, dict]:
    try:
        ENGINE.cancel_booking(body["booking_id"], body.get("reason", ""))
    except BookingNotFoundError as exc:
        return 404, {"error": str(exc)}
    return 200, {"cancelled": body["booking_id"]}


def api_calls() -> tuple[int, dict]:
    return 200, {"calls": list(reversed(CALL_LOG))}


def api_config() -> tuple[int, dict]:
    return 200, {
        "business": CONFIG["business"],
        "calendar": {"provider": CONFIG["calendar"].get("provider", "local"),
                     "note": "server-side store (SQLite); Google adapter binds later"},
        "services": CONFIG["services"],
        "hours_weekly": CONFIG["hours"]["weekly"],
        "hours_overrides": CONFIG["hours"].get("overrides", {}),
        "policy": CONFIG["policy"],
        "reminders": CONFIG["reminders"],
        "escalation": CONFIG["escalation"],
    }


def api_admin() -> tuple[int, dict]:
    now = _utcnow()
    holds = [dict(r) for r in ENGINE.db.execute(
        "SELECT * FROM bookings WHERE status='held' ORDER BY hold_expires_utc"
    ).fetchall()]
    for h in holds:
        exp = datetime.fromisoformat(h["hold_expires_utc"]) if h.get("hold_expires_utc") else None
        h["expires_in_min"] = max(0, int((exp - now).total_seconds() // 60)) if exp else None
        h["slot_label"] = slot_label(datetime.fromisoformat(h["start_utc"]))
    reminders = [dict(r) for r in ENGINE.db.execute(
        """SELECT r.*, b.status AS booking_status FROM reminders r
           JOIN bookings b ON b.id = r.booking_id ORDER BY r.remind_at_utc"""
    ).fetchall()]
    return 200, {
        "engine": "ok",
        "business": CONFIG["business"]["name"],
        "timezone": str(TZ),
        "calendar_provider": "local (SQLite store)",
        "counts": {
            "bookings_confirmed": ENGINE.count_bookings("confirmed"),
            "holds_active": len(holds),
            "reminders_pending": sum(1 for r in reminders if not r["sent"]),
            "calls_today": len(CALL_LOG),
        },
        "holds": holds,
        "reminders": reminders,
    }


def api_admin_expire_holds() -> tuple[int, dict]:
    return 200, {"expired": ENGINE.expire_holds()}


def api_admin_sweep() -> tuple[int, dict]:
    due = ENGINE.reminders_due()
    for r in due:
        ENGINE.mark_reminder_sent(r["id"])
    return 200, {"sent": len(due),
                 "reminders": [{"booking_id": r["booking_id"],
                                "channel": r["channel"]} for r in due]}


def api_admin_reset() -> tuple[int, dict]:
    with LOCK:
        SESSIONS.clear()
        CALL_LOG.clear()
    ENGINE.db.execute("DELETE FROM reminders")
    ENGINE.db.execute("DELETE FROM bookings")
    ENGINE.db.commit()
    return 200, {"reset": True}


# --------------------------------------------------------------------------
# HTTP


class Handler(BaseHTTPRequestHandler):
    server_version = "FrontDesk/1.0"

    def log_message(self, fmt, *args):  # quieter logs
        pass

    def _send(self, status: int, payload: dict):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return {}

    def do_GET(self):
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)
        try:
            if path == "/api/slots":
                self._send(*api_slots(query))
            elif path == "/api/bookings":
                self._send(*api_bookings())
            elif path == "/api/calls":
                self._send(*api_calls())
            elif path == "/api/config":
                self._send(*api_config())
            elif path == "/api/admin":
                self._send(*api_admin())
            elif path == "/" or path == "/index.html":
                self._serve_file("index.html", "text/html")
            else:
                self._send(404, {"error": "not found"})
        except BrokenPipeError:
            pass
        except Exception as exc:  # never crash the demo on a bad request
            self._send(500, {"error": f"server error: {exc}"})

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        body = self._read_body()
        try:
            if path == "/api/call/start":
                self._send(*api_call_start(body))
            elif path == "/api/call/choose":
                self._send(*api_call_choose(body))
            elif path == "/api/call/commit":
                self._send(*api_call_commit(body))
            elif path == "/api/call/retry":
                self._send(*api_call_retry(body))
            elif path == "/api/bookings/move":
                self._send(*api_booking_move(body))
            elif path == "/api/bookings/cancel":
                self._send(*api_booking_cancel(body))
            elif path == "/api/admin/expire_holds":
                self._send(*api_admin_expire_holds())
            elif path == "/api/admin/sweep":
                self._send(*api_admin_sweep())
            elif path == "/api/admin/reset":
                self._send(*api_admin_reset())
            else:
                self._send(404, {"error": "not found"})
        except BrokenPipeError:
            pass
        except Exception as exc:
            self._send(500, {"error": f"server error: {exc}"})

    def _serve_file(self, rel: str, content_type: str | None):
        import os
        full = os.path.join(os.path.dirname(os.path.abspath(__file__)), rel)
        if not os.path.isfile(full) or ".." in rel:
            self._send(404, {"error": "not found"})
            return
        ctype = content_type or {
            ".html": "text/html", ".js": "application/javascript",
            ".css": "text/css", ".json": "application/json",
        }.get(os.path.splitext(full)[1], "application/octet-stream")
        with open(full, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main():
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    server = HTTPServer(("127.0.0.1", port), Handler)
    print(f"Front Desk running at http://localhost:{port}/  (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye.")


if __name__ == "__main__":
    main()
