import concurrent.futures
import copy
import tempfile
import tomllib
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from auth import Auth
from call_flow import Calls
from engine import Engine, Problem

ROOT = Path(__file__).resolve().parents[1]


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        with open(ROOT / "clinic.toml", "rb") as f:
            self.cfg = tomllib.load(f)
        self.now = datetime(2026, 10, 5, 0, tzinfo=timezone.utc)
        self.e = Engine(self.cfg, self.temp.name + "/db", lambda: self.now)

    def tearDown(self):
        self.temp.cleanup()

    def slot(self, doctor="maya-patel", day="2026-10-06", index=0):
        return self.e.slots(doctor, day)[index]["start"]

    def book(
        self,
        doctor="maya-patel",
        day="2026-10-06",
        index=0,
        count=1,
        key="test-key-123",
    ):
        h = self.e.hold(doctor, self.slot(doctor, day, index), key, count)
        return self.e.commit(
            h["hold_id"], "Test Patient", "+14155550123", key + "-commit"
        )

    def assert_problem(self, code, fn, *args, **kwargs):
        with self.assertRaises(Problem) as cm:
            fn(*args, **kwargs)
        self.assertEqual(cm.exception.code, code)

    def test_slots_per_doctor_and_buffers(self):
        self.assertEqual(len(self.e.slots("maya-patel", "2026-10-06")), 14)
        self.assertEqual(len(self.e.slots("james-chen", "2026-10-06")), 7)
        self.book()
        self.assertEqual(len(self.e.slots("maya-patel", "2026-10-06")), 13)
        self.assertEqual(len(self.e.slots("sofia-reyes", "2026-10-06")), 14)

    def test_hold_and_commit_idempotency(self):
        s = self.slot()
        h = self.e.hold("maya-patel", s, "hold-idem-key")
        self.assertEqual(h, self.e.hold("maya-patel", s, "hold-idem-key"))
        b = self.e.commit(
            h["hold_id"], "Test Patient", "+14155550123", "commit-idem-key"
        )
        replay = self.e.commit(
            h["hold_id"], "Test Patient", "+14155550123", "commit-idem-key"
        )
        self.assertTrue(replay["replayed"])
        self.assertEqual(b["reference"], replay["reference"])
        self.assertEqual(b["manage_code"], replay["manage_code"])
        self.assert_problem(
            "idempotency_conflict",
            self.e.commit,
            h["hold_id"],
            "Another Patient",
            "+14155550123",
            "commit-idem-key",
        )

    def test_hold_key_payload_mismatch(self):
        s = self.slot()
        self.e.hold("maya-patel", s, "hold-key-001")
        self.assert_problem(
            "idempotency_conflict",
            self.e.hold,
            "maya-patel",
            self.slot(index=1),
            "hold-key-001",
        )

    def test_concurrent_callers_one_winner(self):
        s = self.slot()

        def worker(i):
            try:
                return self.e.hold("maya-patel", s, f"parallel-{i}")
            except Problem as ex:
                return ex.code

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(worker, range(8)))
        self.assertEqual(sum(isinstance(x, dict) for x in results), 1)
        self.assertEqual(results.count("slot_conflict"), 7)

    def test_atomic_recurring_conflict(self):
        s = self.slot()
        self.book(day="2026-10-13")
        self.assert_problem(
            "slot_conflict", self.e.hold, "maya-patel", s, "series-bad-key", 4
        )
        self.assertEqual(len(self.e.slots("maya-patel", "2026-10-06")), 14)
        with self.e.db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM groups").fetchone()[0], 1)

    def test_recurring_series_and_partial_cancel(self):
        b = self.book(count=4)
        self.assertEqual(len(b["appointments"]), 4)
        self.e.cancel(b["reference"], b["manage_code"], b["appointments"][1]["id"])
        rows = self.e.patient_bookings(b["reference"], b["manage_code"])["appointments"]
        self.assertEqual(
            [x["status"] for x in rows],
            ["confirmed", "cancelled", "confirmed", "confirmed"],
        )
        self.e.cancel(b["reference"], b["manage_code"])
        self.assertTrue(
            self.e.cancel(b["reference"], b["manage_code"])["already_cancelled"]
        )

    def test_series_cancellation_skips_past_visits(self):
        b = self.book(count=3)
        self.now = datetime.fromisoformat(b["appointments"][0]["start"]) + timedelta(
            days=1
        )
        result = self.e.cancel(b["reference"], b["manage_code"])
        self.assertEqual(len(result["cancelled"]), 2)
        self.assertEqual(
            self.e.patient_bookings(b["reference"], b["manage_code"])["appointments"][
                0
            ]["status"],
            "confirmed",
        )
        self.assert_problem(
            "past_appointment",
            self.e.cancel,
            b["reference"],
            b["manage_code"],
            b["appointments"][0]["id"],
        )

    def test_maintenance_removes_expired_call_receipts(self):
        calls = Calls(self.e)
        r = calls.start()
        calls.turn(r["call_id"], "book", "maintenance-turn-key", r["seq"])
        self.now += timedelta(minutes=31)
        self.e.maintenance()
        with self.e.db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM calls").fetchone()[0], 0)
            self.assertEqual(
                db.execute("SELECT count(*) FROM call_turns").fetchone()[0], 0
            )

    def test_hold_expiry_and_release(self):
        s = self.slot()
        h = self.e.hold("maya-patel", s, "hold-expire-key")
        self.now += timedelta(minutes=6)
        self.assert_problem(
            "hold_expired",
            self.e.commit,
            h["hold_id"],
            "Test Patient",
            "+14155550123",
            "commit-exp-key",
        )
        h2 = self.e.hold("maya-patel", s, "hold-second-key")
        self.e.release(h2["hold_id"], "hold-second-key")
        self.assertEqual(len(self.e.slots("maya-patel", "2026-10-06")), 14)

    def test_move_conflict_preserves_original_and_id(self):
        b = self.book()
        target = self.slot()
        other = self.book(key="other-key-000")
        a = b["appointments"][0]
        self.assert_problem(
            "slot_conflict", self.e.move, a["id"], target, b["manage_code"]
        )
        old = self.e.patient_bookings(b["reference"], b["manage_code"])["appointments"][
            0
        ]
        self.assertEqual(old["start"], a["start"])
        moved = self.e.move(a["id"], self.slot(index=1), b["manage_code"])
        self.assertEqual(moved["id"], a["id"])
        self.assertNotEqual(moved["start"], a["start"])

    def test_policy_and_out_of_hours(self):
        for start in [
            "2026-10-06T08:00:00-07:00",
            "2026-10-06T09:15:00-07:00",
            "2026-10-06T09:00:00",
            "2027-01-06T09:00:00-08:00",
        ]:
            with self.assertRaises(Problem):
                self.e.hold("maya-patel", start, "invalid-time-key")
        self.assertEqual(self.e.slots("maya-patel", "2026-10-04"), [])

    def test_cutoff_and_staff_override(self):
        b = self.book()
        a = b["appointments"][0]
        self.now = datetime.fromisoformat(a["start"]) - timedelta(minutes=60)
        self.assert_problem(
            "cancellation_cutoff", self.e.cancel, b["reference"], b["manage_code"]
        )
        self.e.cancel(b["reference"], staff=True, actor="manager")

    def test_wrong_patient_secret(self):
        b = self.book()
        self.assert_problem(
            "forbidden", self.e.patient_bookings, b["reference"], "000000000000"
        )
        self.assert_problem("forbidden", self.e.cancel, b["reference"], "000000000000")

    def test_availability_override_and_conflict_rollback(self):
        self.e.set_availability("maya-patel", [], day="2026-10-07")
        self.assertEqual(self.e.slots("maya-patel", "2026-10-07"), [])
        self.e.set_availability("maya-patel", [], day="2026-10-07", remove=True)
        self.assertEqual(len(self.e.slots("maya-patel", "2026-10-07")), 14)
        self.book()
        self.assert_problem(
            "schedule_conflict", self.e.set_availability, "maya-patel", [], weekday=1
        )
        self.assertEqual(
            self.e.schedule("maya-patel")["weekly"]["1"],
            [["09:00", "12:00"], ["13:00", "17:00"]],
        )

    def test_overlapping_windows_invalid(self):
        with self.assertRaises(Problem):
            self.e.set_availability(
                "maya-patel", [["09:00", "12:00"], ["11:00", "13:00"]], weekday=1
            )

    def test_dst_recurring_keeps_local_hour(self):
        b = self.book(day="2026-10-27", count=3)
        starts = [a["start"] for a in b["appointments"]]
        self.assertTrue(starts[0].endswith("-07:00"))
        self.assertTrue(starts[-1].endswith("-08:00"))
        self.assertTrue(all("T09:00:00" in x for x in starts))

    def test_dst_nonexistent_ambiguous_excluded(self):
        self.e.set_availability("maya-patel", [["00:00", "04:00"]], weekday=6)
        self.now = datetime(2026, 10, 30, 0, tzinfo=timezone.utc)
        slots = self.e.slots("maya-patel", "2026-11-01")
        self.assertFalse(any("T01:" in a["start"] for a in slots))
        self.now = datetime(2027, 3, 10, 0, tzinfo=timezone.utc)
        slots = self.e.slots("maya-patel", "2027-03-14")
        self.assertFalse(any("T02:" in a["start"] for a in slots))

    def test_doctor_deactivation_with_future_booking_rejected(self):
        self.book()
        with self.assertRaises(Problem):
            self.e.save_doctor(
                {
                    "id": "maya-patel",
                    "name": "Dr. Maya Patel",
                    "specialty": "Primary care",
                    "duration": 30,
                    "buffer": 0,
                    "active": False,
                },
                "admin",
            )

    def test_restart_persistence(self):
        b = self.book()
        other = Engine(self.cfg, self.e.db_path, lambda: self.now)
        self.assertEqual(
            other.patient_bookings(b["reference"], b["manage_code"])["appointments"][0][
                "id"
            ],
            b["appointments"][0]["id"],
        )

    def test_staff_sessions_passwords_and_reset(self):
        auth = Auth(self.e)
        auth.create_user("test-admin", "safe-password-123", "admin")
        with self.e.db() as db:
            self.assertNotIn(
                "safe-password", db.execute("SELECT password FROM users").fetchone()[0]
            )
        with self.assertRaises(Problem):
            auth.login("test-admin", "wrong")
        token, u = auth.login("test-admin", "safe-password-123")
        self.assertEqual(auth.session(token)["role"], "admin")
        auth.create_user("test-admin", "changed-password-123", "admin", replace=True)
        with self.assertRaises(Problem):
            auth.session(token)

    def test_call_booking_retry_and_manage(self):
        calls = Calls(self.e)
        r = calls.start()
        for i, t in enumerate(
            ["book", "1", "2026-10-06", "1", "4", "Test Patient", "+14155550123", "yes"]
        ):
            req = {
                "cid": r["call_id"],
                "text": t,
                "request_id": f"call-step-{i}",
                "seq": r["seq"],
            }
            r = calls.turn(**req)
        self.assertEqual(r["stage"], "done")
        self.assertEqual(len(r["result"]["appointments"]), 4)
        self.assertEqual(r, calls.turn(**req))
        b = r["result"]
        r = calls.start()
        for i, t in enumerate(
            ["manage", b["reference"], b["manage_code"], "cancel all", "yes"]
        ):
            r = calls.turn(r["call_id"], t, f"manage-step-{i}", r["seq"])
        self.assertEqual(r["stage"], "done")
        self.assertEqual(len(r["result"]["cancelled"]), 4)

    def test_call_no_confirmation_no_booking(self):
        calls = Calls(self.e)
        r = calls.start()
        for i, t in enumerate(
            ["book", "1", "2026-10-06", "1", "1", "Test Patient", "+14155550123", "no"]
        ):
            r = calls.turn(r["call_id"], t, f"no-confirm-{i}", r["seq"])
        self.assertEqual(r["stage"], "done")
        self.assertEqual(len(self.e.slots("maya-patel", "2026-10-06")), 14)

    def test_call_resume_and_stale_turn(self):
        calls = Calls(self.e)
        r = calls.start()
        r = calls.turn(r["call_id"], "book", "call-resume-key", r["seq"])
        restarted = Calls(self.e)
        self.assertEqual(restarted.start(r["call_id"]), r)
        self.assert_problem(
            "stale_turn", restarted.turn, r["call_id"], "1", "call-stale-key", 0
        )

    def test_advance_boundary_and_notice(self):
        self.now = datetime(2026, 10, 6, 16, 30, tzinfo=timezone.utc)
        slots = self.e.slots("maya-patel", "2026-10-06")
        self.assertEqual(slots[0]["start"], "2026-10-06T10:30:00-07:00")
        self.assertEqual(self.e.slots("maya-patel", "2027-01-05"), [])


if __name__ == "__main__":
    unittest.main()
