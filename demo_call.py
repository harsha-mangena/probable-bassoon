"""The haircut call, tried by the builder.

Script: caller wants a haircut, picks a slot, gives a name.
The agent may confirm ONLY after the booking id exists.
Then the same commit is retried with the same idempotency key:
it must return the same id and must not create a second booking.

Run: python3 demo_call.py
"""

import json
import sys
import tomllib
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, ".")
from engine import Engine
from call_flow import TextCall

TZ = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 10, 5, 9, 0, tzinfo=TZ)  # Monday morning

with open("salon.toml", "rb") as f:
    config = tomllib.load(f)

engine = Engine(config)
call = TextCall(engine, now=NOW)
result = call.run(want="I want a haircut", pick=0, name="Vam",
                  phone="+14155550100")

print("=== the call ===\n")
for speaker, text in result["transcript"]:
    who = "Front Desk" if speaker == "agent" else "Caller"
    print(f"{who}: {text}")

booking_id = result["state"].get("booking_id")
idem_key = result["state"].get("idempotency_key")
hold_id = result["state"].get("hold_id")
assert booking_id, "the call must end with a booking id, or it is not done"

print("\n=== the retry ===")
print(f"Retrying commit with the same idempotency key ({idem_key}) ...")
booking_id2, replayed = engine.commit_booking(
    hold_id, "Vam", "+14155550100", idem_key, now=NOW)
print(f"Returned booking id: {booking_id2} (replayed={replayed})")
print(f"Confirmed bookings in the book: {engine.count_bookings()}")
assert booking_id2 == booking_id, "retry must return the SAME booking id"
assert engine.count_bookings() == 1, "retry must not create a second booking"

print("\n=== proof ===")
print(f"booking id:        {booking_id}")
print(f"retry returned:    {booking_id2} (same id, no second booking)")
print("three decisions vam did not specify:")
print("  1. Python + SQLite, stdlib only — the race guard is a real transaction.")
print("  2. Hold-then-commit — the slot is reserved while the caller confirms.")
print("  3. Text call now, voice as a channel later — the engine is channel-blind.")

with open("demo_output.json", "w") as f:
    json.dump({
        "transcript": result["transcript"],
        "booking_id": booking_id,
        "retry_booking_id": booking_id2,
        "retry_replayed": replayed,
        "confirmed_count": engine.count_bookings(),
        "booking": engine.get_booking(booking_id),
    }, f, indent=2, default=str)
print("\nSaved demo_output.json for the panel.")
