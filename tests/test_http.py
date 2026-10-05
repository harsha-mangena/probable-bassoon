import base64
import hashlib
import hmac
import http.client
import json
import tempfile
import threading
import tomllib
import unittest
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from engine import Engine
from server import App, ClinicServer
from telephony import valid_signature


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        with open(Path(__file__).resolve().parents[1] / "clinic.toml", "rb") as f:
            cfg = tomllib.load(f)
        cls.e = Engine(
            cfg,
            cls.temp.name + "/db",
            lambda: datetime(2026, 10, 5, 0, tzinfo=timezone.utc),
        )
        cls.app = App(cls.e)
        cls.app.auth.create_user("admin", "safe-password-123", "admin")
        cls.app.auth.create_user("manager", "safe-password-123", "manager")
        cls.srv = ClinicServer(("127.0.0.1", 0), cls.app)
        cls.port = cls.srv.server_address[1]
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.thread.join()
        cls.temp.cleanup()

    def req(self, path, body=None, headers=None, raw=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Content-Type": "application/json", **(headers or {})}
        c.request(
            "POST" if body is not None or raw is not None else "GET",
            path,
            raw if raw is not None else json.dumps(body) if body is not None else None,
            h,
        )
        r = c.getresponse()
        content = r.read()
        data = (
            json.loads(content)
            if "application/json" in r.getheader("Content-Type", "")
            else content.decode()
        )
        result = r.status, data, dict(r.getheaders())
        c.close()
        return result

    def login(self, username="admin"):
        status, u, h = self.req(
            "/api/auth/login", {"username": username, "password": "safe-password-123"}
        )
        self.assertEqual(status, 200)
        return {"Cookie": h["Set-Cookie"].split(";")[0], "X-CSRF-Token": u["csrf"]}

    def test_01_public_data_no_patient_listing(self):
        self.assertEqual(self.req("/api/doctors")[0], 200)
        self.assertEqual(self.req("/api/bookings")[0], 404)
        self.assertEqual(self.req("/api/admin/overview")[0], 401)
        self.assertEqual(self.req("/api/config")[0], 200)

    def test_02_security_headers(self):
        status, body, h = self.req("/")
        self.assertEqual(status, 200)
        self.assertIn("Care that fits", body)
        self.assertEqual(h["Cache-Control"], "no-store")
        self.assertIn("frame-ancestors 'none'", h["Content-Security-Policy"])

    def test_03_auth_csrf_and_roles(self):
        h = self.login("manager")
        self.assertEqual(self.req("/api/admin/overview", headers=h)[0], 200)
        self.assertEqual(self.req("/api/admin/users", headers=h)[0], 403)
        self.assertEqual(self.req("/api/admin/doctors", {}, h)[0], 403)
        no_csrf = {"Cookie": h["Cookie"]}
        self.assertEqual(
            self.req(
                "/api/admin/availability",
                {"doctor_id": "maya-patel", "weekday": 0, "windows": []},
                no_csrf,
            )[0],
            403,
        )
        self.assertEqual(
            self.req(
                "/api/admin/availability",
                {"doctor_id": "maya-patel", "weekday": 0, "windows": []},
                {**h, "Origin": "https://evil.test"},
            )[0],
            403,
        )

    def test_04_staff_create_user_and_logout(self):
        h = self.login()
        self.assertEqual(
            self.req(
                "/api/admin/users",
                {
                    "username": "new-manager",
                    "password": "safe-password-456",
                    "role": "manager",
                },
                h,
            )[0],
            201,
        )
        self.assertEqual(self.req("/api/auth/logout", {}, h)[0], 200)
        self.assertEqual(self.req("/api/admin/overview", headers=h)[0], 401)

    def test_05_end_to_end_booking_move_cancel(self):
        day = "2026-10-08"
        _, data, _ = self.req("/api/slots?doctor_id=sofia-reyes&day=" + day)
        first = data["slots"][0]["start"]
        second = data["slots"][1]["start"]
        st, h, _ = self.req(
            "/api/holds",
            {
                "doctor_id": "sofia-reyes",
                "start": first,
                "count": 2,
                "idempotency_key": "http-hold-key",
            },
        )
        self.assertEqual(st, 201)
        payload = {
            "hold_id": h["hold_id"],
            "name": "HTTP Patient",
            "phone": "+14155550123",
            "idempotency_key": "http-commit-key",
        }
        st, b, _ = self.req("/api/bookings", payload)
        self.assertEqual(st, 201)
        self.assertTrue(self.req("/api/bookings", payload)[1]["replayed"])
        self.assertEqual(
            self.req(
                "/api/patient/bookings",
                {"reference": b["reference"], "manage_code": "bad"},
            )[0],
            403,
        )
        a = b["appointments"][0]
        st, m, _ = self.req(
            "/api/patient/move",
            {
                "appointment_id": a["id"],
                "start": second,
                "manage_code": b["manage_code"],
            },
        )
        self.assertEqual(st, 200)
        self.assertEqual(a["id"], m["id"])
        st, c, _ = self.req(
            "/api/patient/cancel",
            {"reference": b["reference"], "manage_code": b["manage_code"]},
        )
        self.assertEqual(st, 200)
        self.assertEqual(len(c["cancelled"]), 2)

    def test_06_invalid_json_and_fields(self):
        self.assertEqual(self.req("/api/holds", raw="{")[0], 400)
        self.assertEqual(self.req("/api/holds", raw="[]")[0], 400)
        self.assertEqual(self.req("/api/slots?doctor_id=maya-patel&day=no")[0], 400)
        self.assertEqual(
            self.req(
                "/api/holds",
                {
                    "doctor_id": "maya-patel",
                    "start": "wrong",
                    "idempotency_key": "valid-key",
                },
            )[0],
            400,
        )
        self.assertEqual(
            self.req(
                "/api/auth/login", raw="a=b", headers={"Content-Type": "text/plain"}
            )[0],
            415,
        )
        self.assertEqual(self.req("/api/holds", raw="x" * 33000)[0], 413)
        self.assertEqual(
            self.req(
                "/api/holds",
                {
                    "doctor_id": "missing",
                    "start": "2026-10-09T09:00:00-07:00",
                    "idempotency_key": "unknown-doctor-key",
                },
            )[0],
            404,
        )
        self.assertEqual(
            self.req(
                "/api/bookings",
                {
                    "hold_id": {},
                    "name": "Test Patient",
                    "phone": "+14155550123",
                    "idempotency_key": "invalid-fields-key",
                },
            )[0],
            400,
        )

    def test_07_phone_disabled_fails_closed(self):
        self.assertEqual(
            self.req(
                "/api/voice/twilio",
                raw="CallSid=CA123",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )[0],
            503,
        )

    def test_08_phone_signature_and_call_flow(self):
        token = "fixture-secret"
        self.app.origin = "https://clinic.test"
        self.app.twilio_token = token
        self.app.secure = True
        try:
            params = {"CallSid": "CAintegration123"}
            path = "/api/voice/twilio"

            def phone(path, params):
                url = self.app.origin + path
                payload = url + "".join(k + params[k] for k in sorted(params))
                sig = base64.b64encode(
                    hmac.new(token.encode(), payload.encode(), hashlib.sha1).digest()
                ).decode()
                return self.req(
                    path,
                    raw=urlencode(params),
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded",
                        "X-Twilio-Signature": sig,
                    },
                )

            self.assertEqual(
                self.req(
                    path,
                    raw=urlencode(params),
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )[0],
                403,
            )
            st, xml, _ = phone(path, params)
            self.assertEqual(st, 200)
            self.assertIn("<Gather", xml)
            texts = [
                "book",
                "1",
                "2026-10-09",
                "1",
                "1",
                "Phone Patient",
                "+14155550123",
                "yes",
            ]
            for seq, text in enumerate(texts):
                st, xml, _ = phone(
                    path + "?step=" + str(seq), {**params, "SpeechResult": text}
                )
                self.assertEqual(st, 200)
            self.assertIn("Booked 1 appointment", xml)
            self.assertIn("<Hangup", xml)
            self.assertEqual(
                phone(path + "?step=7", {**params, "SpeechResult": "yes"})[1], xml
            )
        finally:
            self.app.origin = None
            self.app.twilio_token = None
            self.app.secure = False

    def test_09_signature_known_fixture(self):
        url = "https://mycompany.com/myapp.php?foo=1&bar=2"
        # Published fixture from twilio-python/tests/unit/test_request_validator.py.
        params = {
            "CallSid": ["CA1234567890ABCDE"],
            "Caller": ["+14158675309"],
            "Digits": ["1234"],
            "From": ["+14158675309"],
            "To": ["+18005551212"],
        }
        self.assertTrue(
            valid_signature("12345", url, params, "RSOYDt4T1cUTdK1PDd93/VVr8B8=")
        )

    def test_10_rate_limit(self):
        for _ in range(10):
            self.app.rate.check(("fixture", "test"), 10)
        with self.assertRaises(Exception) as cm:
            self.app.rate.check(("fixture", "test"), 10)
        self.assertEqual(cm.exception.status, 429)


if __name__ == "__main__":
    unittest.main()
