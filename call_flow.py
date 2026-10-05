"""Persistent, explicit-confirmation voice/text flow shared by web and phone."""

import json
import re
import secrets
import threading
from datetime import timedelta
from engine import Problem, digest, iso


class Calls:
    def __init__(self, engine):
        self.engine = engine
        self.lock = threading.RLock()

    def start(self, call_id=None):
        cid = call_id or secrets.token_urlsafe(24)
        with self.lock, self.engine.db(True) as db:
            existing = db.execute(
                "SELECT state FROM calls WHERE id=? AND expires>?",
                (cid, iso(self.engine.clock())),
            ).fetchone()
            if existing:
                return json.loads(existing[0])["last"]
            old = [
                x[0]
                for x in db.execute(
                    "SELECT id FROM calls WHERE expires<=?", (iso(self.engine.clock()),)
                )
            ]
            for x in old:
                db.execute("DELETE FROM call_turns WHERE call_id=?", (x,))
                db.execute("DELETE FROM calls WHERE id=?", (x,))
            state = {"stage": "intent", "round": 0, "seq": 0, "tries": 0}
            response = self.response(
                cid,
                state,
                "Welcome to "
                + self.engine.config["clinic"]["name"]
                + ". Say book, manage, or staff. For emergencies, contact local emergency services.",
                ["book", "manage", "staff"],
            )
            state["last"] = response
            db.execute(
                "INSERT INTO calls VALUES (?,?,?,?)",
                (
                    cid,
                    json.dumps(state),
                    iso(self.engine.clock() + timedelta(minutes=30)),
                    iso(self.engine.clock()),
                ),
            )
            return response

    @staticmethod
    def response(cid, state, message, choices=None, result=None):
        return {
            "call_id": cid,
            "stage": state["stage"],
            "seq": state["seq"],
            "message": message,
            "choices": choices or [],
            "result": result,
        }

    def turn(self, cid, text, request_id, seq):
        if not isinstance(text, str) or len(text) > 500 or not isinstance(cid, str):
            raise Problem("Call response must be text of at most 500 characters")
        self.engine.key(request_id)
        fingerprint = digest(json.dumps([text, seq]))
        with self.lock:
            with self.engine.db() as db:
                prior = db.execute(
                    "SELECT * FROM call_turns WHERE call_id=? AND request_id=?",
                    (cid, request_id),
                ).fetchone()
                if prior:
                    if prior["fingerprint"] != fingerprint:
                        raise Problem("Turn key already used", 409)
                    return json.loads(prior["response"])
                row = db.execute(
                    "SELECT state FROM calls WHERE id=? AND expires>?",
                    (cid, iso(self.engine.clock())),
                ).fetchone()
            if not row:
                raise Problem("Call expired. Start a new call.", 404)
            state = json.loads(row[0])
            if type(seq) is not int or seq != state["seq"]:
                raise Problem(
                    "Call state changed. Start a new call.", 409, "stale_turn"
                )
            state["seq"] += 1
            try:
                response = self.advance(cid, state, text.strip())
                state["tries"] = 0
            except Problem as exc:
                state["tries"] += 1
                if exc.code in ("slot_conflict", "hold_expired", "unavailable"):
                    if state.get("hold"):
                        self.engine.release(state["hold"], state["hold_key"])
                    moving = state["stage"].startswith("manage-move")
                    state.update(
                        stage="manage-move-day" if moving else "day",
                        round=state["round"] + 1,
                    )
                    response = self.response(
                        cid,
                        state,
                        str(exc) + " Please give another date in YYYY-MM-DD.",
                    )
                elif state["tries"] >= 3:
                    state["stage"] = "done"
                    response = self.response(
                        cid,
                        state,
                        "I couldn't complete this request. Please call clinic staff at "
                        + self.engine.config["clinic"]["phone"]
                        + ".",
                    )
                else:
                    response = self.response(
                        cid,
                        state,
                        str(exc) + " " + state["last"]["message"],
                        state["last"].get("choices"),
                    )
            state["last"] = response
            with self.engine.db(True) as db:
                db.execute(
                    "UPDATE calls SET state=?,updated=? WHERE id=?",
                    (json.dumps(state), iso(self.engine.clock()), cid),
                )
                db.execute(
                    "INSERT INTO call_turns VALUES (?,?,?,?)",
                    (cid, request_id, fingerprint, json.dumps(response)),
                )
            return response

    def advance(self, cid, s, text):
        t = text.lower().strip().rstrip(".")
        if t in ("staff", "agent", "help", "3") and s["stage"] == "intent":
            s["stage"] = "done"
            return self.response(
                cid,
                s,
                "Please call clinic staff at "
                + self.engine.config["clinic"]["phone"]
                + ". No appointment has been booked.",
            )
        if t in ("stop", "goodbye", "exit"):
            if s.get("hold"):
                self.engine.release(s["hold"], s["hold_key"])
            s["stage"] = "done"
            return self.response(
                cid, s, "Call ended. Unconfirmed holds have been released."
            )
        stage = s["stage"]
        if stage == "intent":
            if t in ("manage", "cancel", "reschedule", "2"):
                s["stage"] = "manage-reference"
                return self.response(cid, s, "Enter your ten digit booking reference.")
            if t not in ("book", "appointment", "book appointment", "1"):
                raise Problem("Please choose book, manage, or staff.")
            s["doctors"] = self.engine.doctors()
            s["stage"] = "doctor"
            choices = [
                f"{i+1}. {d['name']} — {d['specialty']}"
                for i, d in enumerate(s["doctors"])
            ]
            return self.response(
                cid, s, "Choose a doctor by number. " + ". ".join(choices), choices
            )
        if stage == "doctor":
            d = s["doctors"][self.pick(t, len(s["doctors"]))]
            s.update(doctor_id=d["id"], stage="day")
            return self.response(
                cid,
                s,
                "What date? Say today, tomorrow, next available, or enter YYYY-MM-DD.",
                ["tomorrow", "next available"],
            )
        if stage in ("day", "manage-move-day"):
            today = self.engine.clock().astimezone(self.engine.tz).date()
            if t in ("today", "tomorrow", "next available", "next", "0"):
                day = today + timedelta(days=1 if t == "tomorrow" else 0)
            else:
                compact = re.sub(r"[^0-9]", "", text)
                if len(compact) == 8:
                    text = compact[:4] + "-" + compact[4:6] + "-" + compact[6:]
                day = self.engine.parse_day(text)
            slots = self.engine.slots(s["doctor_id"], day.isoformat())
            if t in ("next available", "next", "0"):
                for i in range(self.engine.policy["max_advance_days"] + 1):
                    day = today + timedelta(days=i)
                    slots = self.engine.slots(s["doctor_id"], day.isoformat())
                    if slots:
                        break
            if not slots:
                return self.response(
                    cid,
                    s,
                    "There are no slots on that date. Give another date or say next available.",
                    ["next available"],
                )
            s["slots"] = slots[:6]
            s["stage"] = "manage-move-slot" if stage == "manage-move-day" else "slot"
            choices = [f"{i+1}. {a['label']}" for i, a in enumerate(s["slots"])]
            return self.response(
                cid,
                s,
                "Available times in " + str(self.engine.tz) + ". " + ". ".join(choices),
                choices,
            )
        if stage == "slot":
            s["start"] = s["slots"][self.pick(t, len(s["slots"]))]["start"]
            s["stage"] = "repeat"
            return self.response(
                cid,
                s,
                "How many appointments? Enter 1 for one visit, or 2 through 12 for weekly visits.",
                ["1", "4", "8"],
            )
        if stage == "repeat":
            try:
                count = int(t)
            except ValueError:
                raise Problem("Enter a number from 1 through 12.")
            s["hold_key"] = f"call-{cid}-{s['round']}"
            hold = self.engine.hold(s["doctor_id"], s["start"], s["hold_key"], count)
            s.update(hold=hold["hold_id"], count=count, stage="name")
            return self.response(
                cid,
                s,
                "Those times are held for five minutes. What is the patient's full name?",
            )
        if stage == "name":
            if not 2 <= len(text) <= 100:
                raise Problem("Please provide a name of 2–100 characters.")
            s.update(name=text, stage="phone")
            return self.response(
                cid, s, "What is your phone number, including country code?"
            )
        if stage == "phone":
            if (
                not re.fullmatch(r"\+?[0-9 ()-]{7,25}", text)
                or not 7 <= len(re.sub(r"\D", "", text)) <= 15
            ):
                raise Problem("Please enter a valid phone number with country code.")
            s.update(phone=text, stage="confirm")
            return self.response(
                cid,
                s,
                f"Confirm {s['count']} appointment(s) starting {s['slots'][self.pick_start(s)]['label']}, for {s['name']}. Say yes to book or no to release.",
                ["yes", "no"],
            )
        if stage == "confirm":
            if t in ("no", "2"):
                self.engine.release(s["hold"], s["hold_key"])
                s["stage"] = "done"
                return self.response(
                    cid, s, "The hold was released. No appointment was booked."
                )
            if t not in ("yes", "confirm", "1"):
                raise Problem("Say yes or no.")
            result = self.engine.commit(
                s["hold"], s["name"], s["phone"], f"commit-{cid}-{s['round']}"
            )
            s["stage"] = "done"
            return self.response(
                cid,
                s,
                f"Booked {len(result['appointments'])} appointment(s). Your reference is {result['reference']}. Your private management code is {result['manage_code']}. Keep both to cancel or reschedule.",
                result=result,
            )
        if stage == "manage-reference":
            value = re.sub(r"\s", "", text)
            if not re.fullmatch(r"\d{10}", value):
                raise Problem("Enter a ten digit reference.")
            s.update(reference=value, stage="manage-code")
            return self.response(
                cid, s, "Enter your twelve digit private management code."
            )
        if stage == "manage-code":
            result = self.engine.patient_bookings(
                s["reference"], re.sub(r"\s", "", text)
            )
            s.update(
                code=re.sub(r"\s", "", text),
                appointments=[
                    a
                    for a in result["appointments"]
                    if a["status"] == "confirmed" and a["future"]
                ],
            )
            if not s["appointments"]:
                s["stage"] = "done"
                return self.response(
                    cid, s, "This booking has no confirmed appointments remaining."
                )
            s["stage"] = "manage-action"
            return self.response(
                cid,
                s,
                "Say cancel all, or choose an appointment number to cancel or move. "
                + ". ".join(
                    f"{i+1}. {a['label']}" for i, a in enumerate(s["appointments"])
                ),
                ["cancel all"] + [str(i + 1) for i in range(len(s["appointments"]))],
            )
        if stage == "manage-action":
            if t == "cancel all" or t == "0":
                s.update(selected=None, stage="manage-cancel-confirm")
                return self.response(
                    cid,
                    s,
                    "Cancel all appointments in this series? Say yes or no.",
                    ["yes", "no"],
                )
            a = s["appointments"][self.pick(t, len(s["appointments"]))]
            s.update(
                selected=a["id"], doctor_id=a["doctor_id"], stage="manage-operation"
            )
            return self.response(cid, s, "Say cancel or move.", ["cancel", "move"])
        if stage == "manage-operation":
            if t in ("cancel", "1"):
                s["stage"] = "manage-cancel-confirm"
                return self.response(
                    cid, s, "Cancel this appointment? Say yes or no.", ["yes", "no"]
                )
            if t in ("move", "reschedule", "2"):
                s["stage"] = "manage-move-day"
                return self.response(
                    cid,
                    s,
                    "What new date? Use YYYY-MM-DD, tomorrow, or next available.",
                    ["tomorrow", "next available"],
                )
            raise Problem("Say cancel or move.")
        if stage == "manage-cancel-confirm":
            if t in ("yes", "1"):
                result = self.engine.cancel(
                    s["reference"], s["code"], s.get("selected")
                )
                s["stage"] = "done"
                return self.response(cid, s, "Cancellation completed.", result=result)
            if t not in ("no", "2"):
                raise Problem("Say yes or no.")
            s["stage"] = "done"
            return self.response(cid, s, "Your appointments remain booked.")
        if stage == "manage-move-slot":
            s["move_start"] = s["slots"][self.pick(t, len(s["slots"]))]["start"]
            s["stage"] = "manage-move-confirm"
            return self.response(
                cid,
                s,
                "Move to "
                + s["slots"][self.pick(t, len(s["slots"]))]["label"]
                + "? Say yes or no.",
                ["yes", "no"],
            )
        if stage == "manage-move-confirm":
            if t in ("yes", "1"):
                result = self.engine.move(s["selected"], s["move_start"], s["code"])
                s["stage"] = "done"
                return self.response(
                    cid, s, "Your appointment was rescheduled.", result=result
                )
            if t not in ("no", "2"):
                raise Problem("Say yes or no.")
            s["stage"] = "done"
            return self.response(cid, s, "Your original appointment remains booked.")
        return self.response(
            cid,
            s,
            "This call is complete. Start a new call to continue.",
            result=s["last"].get("result"),
        )

    @staticmethod
    def pick(text, count):
        words = {
            "one": "1",
            "two": "2",
            "three": "3",
            "four": "4",
            "five": "5",
            "six": "6",
        }
        text = words.get(text, text)
        try:
            value = int(text)
            if not 1 <= value <= count:
                raise ValueError()
            return value - 1
        except ValueError:
            raise Problem(f"Choose a number from 1 through {count}.")

    @staticmethod
    def pick_start(state):
        return next(
            i for i, a in enumerate(state["slots"]) if a["start"] == state["start"]
        )
