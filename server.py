#!/usr/bin/env python3
"""Clinic HTTP boundary. Persistent data; protected control plane; same-origin UI."""
import argparse
import hmac
import json
import logging
import os
import threading
import time
import tomllib
from collections import defaultdict, deque
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from auth import Auth
from call_flow import Calls
from engine import Engine, Problem, iso
from telephony import handle, valid_signature

ROOT = Path(__file__).resolve().parent


class RateLimit:
    def __init__(self):
        self.values = defaultdict(deque)
        self.lock = threading.Lock()

    def check(self, key, limit=60):
        now = time.monotonic()
        with self.lock:
            if len(self.values) > 10000:
                for k in list(self.values):
                    if not self.values[k] or self.values[k][-1] < now - 60:
                        del self.values[k]
            q = self.values[key]
            while q and q[0] < now - 60:
                q.popleft()
            if len(q) >= limit:
                raise Problem(
                    "Too many requests. Try again in one minute.", 429, "rate_limited"
                )
            q.append(now)


class App:
    def __init__(self, engine, origin=None, twilio_token=None):
        self.engine = engine
        self.auth = Auth(engine)
        self.calls = Calls(engine)
        self.rate = RateLimit()
        self.origin = origin.rstrip("/") if origin else None
        self.twilio_token = twilio_token
        self.secure = bool(self.origin and self.origin.startswith("https://"))


class Handler(BaseHTTPRequestHandler):
    server_version = "Northstar/2"

    def log_message(self, *args):
        pass

    @property
    def app(self):
        return self.server.app

    def cookie(self):
        c = SimpleCookie()
        try:
            c.load(self.headers.get("Cookie", ""))
        except Exception:
            return ""
        return c["clinic_session"].value if "clinic_session" in c else ""

    def staff(self, admin=False, write=False):
        u = self.app.auth.session(self.cookie())
        if admin and u["role"] != "admin":
            raise Problem("Administrator role required", 403, "forbidden")
        if write and not hmac.compare_digest(
            u["csrf"], self.headers.get("X-CSRF-Token", "")
        ):
            raise Problem("Invalid CSRF token", 403, "forbidden")
        return u

    def send(
        self, status, payload, ctype="application/json; charset=utf-8", cookie=None
    ):
        raw = (
            json.dumps(payload).encode()
            if ctype.startswith("application/json")
            else payload
        )
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        )
        self.send_header("Permissions-Policy", "microphone=(self), camera=()")
        if self.app.secure:
            self.send_header("Strict-Transport-Security", "max-age=31536000")
        if status == 429:
            self.send_header("Retry-After", "60")
        if cookie is not None:
            self.send_header(
                "Set-Cookie",
                "clinic_session="
                + cookie
                + "; Path=/; HttpOnly; SameSite=Strict; Max-Age="
                + ("28800" if cookie else "0")
                + ("; Secure" if self.app.secure else ""),
            )
        self.end_headers()
        self.wfile.write(raw)

    def read(self, form=False):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise Problem("Invalid content length")
        if not 0 < length <= 32768:
            raise Problem("Request body must be 1–32768 bytes", 413)
        expected = "application/x-www-form-urlencoded" if form else "application/json"
        if self.headers.get("Content-Type", "").split(";")[0] != expected:
            raise Problem("Unsupported content type", 415)
        try:
            raw = self.rfile.read(length).decode()
            body = (
                parse_qs(raw, keep_blank_values=True, max_num_fields=100)
                if form
                else json.loads(raw)
            )
        except (ValueError, UnicodeError):
            raise Problem("Invalid request body")
        if not isinstance(body, dict):
            raise Problem("Request body must be an object")
        return body

    def do_GET(self):
        self.dispatch(False)

    def do_POST(self):
        self.dispatch(True)

    def dispatch(self, post):
        try:
            path = urlparse(self.path).path
            q = parse_qs(urlparse(self.path).query)
            one = lambda k: q.get(k, [None])[0]
            e = self.app.engine
            if path == "/api/voice/twilio" and post:
                if (
                    not self.app.twilio_token
                    or not self.app.origin
                    or not self.app.secure
                ):
                    raise Problem("Phone channel is not configured", 503)
                params = self.read(True)
                url = self.app.origin + self.path
                if not valid_signature(
                    self.app.twilio_token,
                    url,
                    params,
                    self.headers.get("X-Twilio-Signature"),
                ):
                    raise Problem("Invalid phone webhook signature", 403)
                raw = handle(self.app.calls, params, one("step"))
                return self.send(200, raw, "application/xml; charset=utf-8")
            self.app.rate.check((self.client_address[0], "general"), 600)
            if post:
                expected = self.app.origin or "http://" + self.headers.get("Host", "")
                origin = self.headers.get("Origin")
                if origin and origin != expected:
                    raise Problem("Cross-origin request rejected", 403)
                if self.headers.get("Sec-Fetch-Site") == "cross-site":
                    raise Problem("Cross-site request rejected", 403)
                body = self.read()
            else:
                body = {}
            if path.startswith("/api/admin") or path == "/api/auth/logout":
                user = self.staff(
                    admin=path in ("/api/admin/users", "/api/admin/doctors"), write=post
                )
            if not post:
                if path in ("/", "/index.html"):
                    return self.send(
                        200,
                        (ROOT / "index.html").read_bytes(),
                        "text/html; charset=utf-8",
                    )
                if path in ("/web/app.js", "/web/style.css"):
                    return self.send(
                        200,
                        (ROOT / path.lstrip("/")).read_bytes(),
                        (
                            "application/javascript; charset=utf-8"
                            if path.endswith(".js")
                            else "text/css; charset=utf-8"
                        ),
                    )
                if path == "/api/health":
                    with e.db() as db:
                        db.execute("SELECT 1").fetchone()
                    return self.send(200, {"ok": True, "service": "clinic-scheduler"})
                if path == "/api/config":
                    return self.send(
                        200,
                        {
                            "clinic": e.config["clinic"],
                            "policy": e.policy,
                            "today": e.clock().astimezone(e.tz).date().isoformat(),
                            "voice": {
                                "browser": "Web Speech API",
                                "phone_enabled": bool(
                                    self.app.twilio_token and self.app.secure
                                ),
                            },
                        },
                    )
                if path == "/api/doctors":
                    return self.send(200, {"doctors": e.doctors()})
                if path == "/api/slots":
                    return self.send(
                        200, {"slots": e.slots(one("doctor_id"), one("day"))}
                    )
                if path == "/api/auth/me":
                    return self.send(200, self.staff())
                if path == "/api/admin/overview":
                    return self.send(
                        200, e.overview(one("day"), one("status"), one("doctor_id"))
                    )
                if path == "/api/admin/availability":
                    return self.send(200, e.schedule(one("doctor_id")))
                if path == "/api/admin/users":
                    with e.db() as db:
                        return self.send(
                            200,
                            {
                                "users": [
                                    dict(x)
                                    for x in db.execute(
                                        "SELECT username,role,active FROM users"
                                    )
                                ]
                            },
                        )
            else:
                if path == "/api/auth/login":
                    self.app.rate.check((self.client_address[0], "login"), 10)
                    token, result = self.app.auth.login(
                        body.get("username"), body.get("password")
                    )
                    return self.send(200, result, cookie=token)
                if path == "/api/auth/logout":
                    self.app.auth.logout(self.cookie())
                    return self.send(200, {"ok": True}, cookie="")
                if path.startswith("/api/patient/"):
                    self.app.rate.check((self.client_address[0], "patient"), 20)
                if path in ("/api/holds", "/api/admin/holds"):
                    self.app.rate.check((self.client_address[0], "holds"), 30)
                    return self.send(
                        201,
                        e.hold(
                            body.get("doctor_id"),
                            body.get("start"),
                            body.get("idempotency_key"),
                            body.get("count", 1),
                            body.get("interval_weeks", 1),
                            actor=(
                                user["username"]
                                if path.startswith("/api/admin")
                                else "patient"
                            ),
                        ),
                    )
                if path == "/api/holds/release":
                    return self.send(
                        200, e.release(body.get("hold_id"), body.get("idempotency_key"))
                    )
                if path in ("/api/bookings", "/api/admin/bookings"):
                    self.app.rate.check((self.client_address[0], "commits"), 30)
                    return self.send(
                        201,
                        e.commit(
                            body.get("hold_id"),
                            body.get("name"),
                            body.get("phone"),
                            body.get("idempotency_key"),
                            actor=(
                                user["username"]
                                if path.startswith("/api/admin")
                                else "patient"
                            ),
                        ),
                    )
                if path == "/api/patient/bookings":
                    return self.send(
                        200,
                        e.patient_bookings(
                            body.get("reference"), body.get("manage_code")
                        ),
                    )
                if path == "/api/patient/cancel":
                    return self.send(
                        200,
                        e.cancel(
                            body.get("reference"),
                            body.get("manage_code"),
                            body.get("appointment_id"),
                            body.get("reason", ""),
                        ),
                    )
                if path == "/api/patient/move":
                    return self.send(
                        200,
                        e.move(
                            body.get("appointment_id"),
                            body.get("start"),
                            body.get("manage_code"),
                        ),
                    )
                if path == "/api/call/start":
                    self.app.rate.check((self.client_address[0], "calls"), 30)
                    return self.send(201, self.app.calls.start())
                if path == "/api/call/turn":
                    self.app.rate.check((self.client_address[0], "turns"), 60)
                    return self.send(
                        200,
                        self.app.calls.turn(
                            body.get("call_id"),
                            body.get("text"),
                            body.get("request_id"),
                            body.get("seq"),
                        ),
                    )
                if path == "/api/admin/availability":
                    return self.send(
                        200,
                        e.set_availability(
                            body.get("doctor_id"),
                            body.get("windows", []),
                            body.get("weekday"),
                            body.get("day"),
                            body.get("remove", False),
                            actor=user["username"],
                        ),
                    )
                if path == "/api/admin/doctors":
                    return self.send(200, e.save_doctor(body, user["username"]))
                if path == "/api/admin/cancel":
                    return self.send(
                        200,
                        e.cancel(
                            body.get("reference"),
                            appointment_id=body.get("appointment_id"),
                            reason=body.get("reason", "Staff cancellation"),
                            actor=user["username"],
                            staff=True,
                        ),
                    )
                if path == "/api/admin/move":
                    return self.send(
                        200,
                        e.move(
                            body.get("appointment_id"),
                            body.get("start"),
                            actor=user["username"],
                            staff=True,
                        ),
                    )
                if path == "/api/admin/users":
                    return self.send(
                        201,
                        self.app.auth.create_user(
                            body.get("username"),
                            body.get("password"),
                            body.get("role", "manager"),
                            actor=user["username"],
                        ),
                    )
            raise Problem("Endpoint not found", 404, "not_found")
        except Problem as exc:
            self.send(exc.status, {"error": str(exc), "code": exc.code})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            logging.exception("Request failed: %s", urlparse(self.path).path)
            self.send(500, {"error": "Internal server error", "code": "internal_error"})


class ClinicServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, app):
        self.app = app
        super().__init__(address, Handler)


def make_engine(db=None, config=None):
    with open(config or ROOT / "clinic.toml", "rb") as f:
        cfg = tomllib.load(f)
    return Engine(cfg, db or os.environ.get("CLINIC_DB", str(ROOT / "clinic.db")))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--db")
    parser.add_argument("--config")
    args = parser.parse_args()
    engine = make_engine(args.db, args.config)
    app = App(engine, os.environ.get("APP_ORIGIN"), os.environ.get("TWILIO_AUTH_TOKEN"))
    with engine.db() as db:
        ready = db.execute(
            'SELECT 1 FROM users WHERE role="admin" AND active=1'
        ).fetchone()
    if not ready:
        print(
            "Create staff access with: python manage.py create-user admin --role admin"
        )
    srv = ClinicServer((args.host, args.port), app)
    stop = threading.Event()

    def maintain():
        while not stop.is_set():
            try:
                engine.maintenance()
            except Exception:
                logging.exception("Housekeeping failed")
            stop.wait(60)

    worker = threading.Thread(target=maintain, daemon=True)
    worker.start()
    print(f"Clinic scheduling available at http://{args.host}:{args.port}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        worker.join(timeout=2)
        srv.server_close()


if __name__ == "__main__":
    main()
