"""One call, in order.

Greet -> intent -> service from config -> live slots only -> hold -> commit
-> speak (only after the booking id exists) -> enqueue follow-up.
If a tool fails twice, hand off with the state already collected.
"""

from __future__ import annotations

from datetime import datetime, timezone

from engine import (
    BookingError,
    Engine,
    HoldExpiredError,
    SlotConflictError,
    _utcnow,
)


class TextCall:
    def __init__(self, engine: Engine, now: datetime | None = None):
        self.engine = engine
        self.now = now or _utcnow()
        self.transcript: list[tuple[str, str]] = []
        self.state: dict = {"stage": "greet"}

    # -- speech ------------------------------------------------------------

    def say(self, speaker: str, text: str) -> None:
        self.transcript.append((speaker, text))

    # -- tools with bounded retry ------------------------------------------

    def _attempt(self, stage: str, fn, *args, **kwargs):
        """Run a tool up to twice. On the second failure, hand off with state."""
        last: Exception | None = None
        for _ in range(2):
            try:
                return fn(*args, **kwargs)
            except BookingError as exc:
                last = exc
        return self._handoff(stage, last)

    def _handoff(self, stage: str, error: Exception | None):
        self.state.update(
            {"stage": f"handoff:{stage}", "error": str(error) if error else None}
        )
        phone = self.engine.config["business"]["transfer_phone"]
        self.say(
            "agent",
            "I'm having trouble with the booking system on my end, so I don't "
            f"want to guess. Let me connect you with the shop at {phone} — "
            "I've saved everything you've told me so far.",
        )
        return None

    # -- the call ------------------------------------------------------------

    def run(self, want: str, pick: int, name: str, phone: str) -> dict:
        cfg = self.engine.config
        self.say("agent", f"Hi, thanks for calling {cfg['business']['name']}. "
                          "What can I book for you today?")

        # intent -> service from config (keyword match; ambiguous -> handoff)
        self.say("caller", want)
        matches = [s for s in cfg["services"]
                   if s["id"] in want.lower() or s["name"].lower() in want.lower()]
        if len(matches) != 1:
            self._handoff("intent", BookingError("ambiguous_request"))
            return self._result()
        svc = matches[0]
        self.state.update({"stage": "service", "service_id": svc["id"]})

        # live slots only: search the next open days, never quote from cache
        self.say("agent", f"Got it — {svc['name'].lower()}. Let me check what's open.")
        slots: list = []
        day = self.now.astimezone(self.engine.tz).date()
        for _ in range(cfg["policy"]["max_advance_days"]):
            day_slots = self.engine.list_free_slots(svc["id"], day, now=self.now)
            if day_slots:
                slots = day_slots
                break
            day += __import__("datetime").timedelta(days=1)
        if not slots:
            self._handoff("slots", BookingError("no_availability"))
            return self._result()
        self.state["slots_offered"] = [s[0].isoformat() for s in slots[:4]]
        offered = ", ".join(s[0].strftime("%a %-I:%M %p") for s in slots[:4])
        self.say("agent", f"Here are the next open times: {offered}. Which works?")
        choice = slots[pick]
        self.say("caller", choice[0].strftime("%A at %-I:%M %p"))
        self.state.update({"stage": "hold", "slot": choice[0].isoformat()})

        # hold: atomic check-and-set; a second caller racing here loses loudly
        hold_id = self._attempt(
            "hold", self.engine.hold_slot,
            svc["id"], choice[0], client_hold_id=f"call-{id(self)}",
            now=self.now,
        )
        if hold_id is None:
            return self._result()
        self.state.update({"stage": "commit", "hold_id": hold_id})
        self.say("agent", "Let me hold that for you. Can I get your name?")

        # commit: idempotent on the key; retry returns the same id
        self.say("caller", f"{name}. My number is {phone}.")
        idem_key = f"call-{id(self)}-commit"
        booking_id, replayed = self._attempt(
            "commit", self.engine.commit_booking,
            hold_id, name, phone, idem_key, now=self.now,
        )
        if booking_id is None:
            return self._result()

        # speak: confirmation language ONLY after the booking id exists
        self.state.update(
            {"stage": "confirmed", "booking_id": booking_id, "replayed": replayed,
             "idempotency_key": idem_key})
        when = choice[0].strftime("%A, %B %-d at %-I:%M %p")
        self.say("agent", f"You're booked, {name} — {svc['name'].lower()} {when}. "
                          f"Your booking reference is {booking_id}.")

        # enqueue the follow-up: reminders were already derived at commit;
        # the call record is what "today's calls" shows
        self.state["follow_up"] = "reminders_enqueued"
        return self._result()

    def _result(self) -> dict:
        return {"transcript": self.transcript, "state": dict(self.state)}
