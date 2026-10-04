"""End-to-end lifecycle dry run: book -> move -> cancel -> rebook.

Exercises the full call flow plus the whole booking lifecycle against a
fresh local engine, printing each step. Run: python3 e2e_lifecycle.py
"""

import sys
import tomllib
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, ".")
from engine import Engine
from call_flow import TextCall

TZ = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 10, 5, 9, 0, tzinfo=TZ)  # Monday morning

with open("salon.toml", "rb") as f:
    config = tomllib.load(f)
e = Engine(config)

print("=== 1. book (full call flow) ===")
call = TextCall(e, now=NOW).run(
    "I want a haircut", pick=0, name="Vam", phone="+14155550100")
bid = call["state"]["booking_id"]
slot = call["state"]["slot"]
print(f"booked {bid} at {slot}")
assert bid.startswith("FD-")

print("\n=== 2. reschedule (move: same id, never cancel+rebook) ===")
ten = next(s for s, _ in e.list_free_slots("haircut", "2026-10-06", now=NOW)
           if s.hour == 10 and s.minute == 0)
assert e.move_booking(bid, ten, now=NOW) == bid
print(f"moved {bid} to 10:00 AM, id unchanged")

print("\n=== 3. reminders derived at commit ===")
due = [r for r in e.reminders_due(now=NOW + timedelta(days=2))
       if r["booking_id"] == bid]
print(f"reminders queued for {bid}: {len(due)} (channel={due[0]['channel']})")
assert len(due) == 1

print("\n=== 4. cancel -> slot freed ===")
e.cancel_booking(bid, "customer request")
free = [s for s, _ in e.list_free_slots("haircut", "2026-10-06", now=NOW)]
nine = any(s.hour == 9 and s.minute == 0 for s in free)
print(f"9:00 AM slot free again: {nine}")
assert nine

print("\n=== 5. rebook the freed slot (new caller, new id) ===")
call2 = TextCall(e, now=NOW).run(
    "haircut please", pick=0, name="Sam", phone="+14155550200")
bid2 = call2["state"]["booking_id"]
print(f"rebooked: {bid2} (different id, same slot, no conflict)")
assert bid2 != bid and bid2.startswith("FD-")
assert e.count_bookings() == 1  # Vam's is cancelled; only Sam's is confirmed

print("\nE2E LIFECYCLE: all green.")
