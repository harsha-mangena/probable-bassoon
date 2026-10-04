"""Real-calendar proof: hold -> commit -> retry -> cancel against Google.

Run: python3 real_calendar_proof.py
Cleans up after itself: the demo booking is cancelled at the end.
"""

import sys
import tomllib

sys.path.insert(0, ".")
from google_adapter import GoogleCalendarAdapter, NotConnectedError

with open("salon.toml", "rb") as f:
    config = tomllib.load(f)

adapter = GoogleCalendarAdapter(config, db_path="ledger.db")
assert adapter.check_connected(), "Google Calendar should be connected now"
print("connected: adapter bound to Google Calendar")

slots = adapter.list_free_slots("haircut", "2026-10-06")
assert slots, "expected at least one free slot on Tue 2026-10-06"
slot = slots[0][0]
print(f"live slot from real availability: {slot.strftime('%a %b %d, %-I:%M %p %Z')}")

hold_id = adapter.hold_slot("haircut", slot, client_hold_id="real-proof-1")
print(f"hold placed: {hold_id}")

booking_id, replayed = adapter.commit_booking(
    hold_id, "Vam", "+14155550100", "real-idem-1")
print(f"committed: booking {booking_id} (replayed={replayed})")
assert booking_id.startswith("FD-") and not replayed

# retry with the same key: same id, no second event
booking_id2, replayed2 = adapter.commit_booking(
    hold_id, "Vam", "+14155550100", "real-idem-1")
assert booking_id2 == booking_id and replayed2
print(f"retry with same key -> {booking_id2} (same id, no second event)")

# confirm the real Google event
event = adapter._gws(
    "events", "get",
    params={"calendarId": adapter.calendar_id,
            "eventId": adapter.get_booking(booking_id)["provider_ref"]})
print(f"google event: '{event.get('summary')}' "
      f"{event.get('start', {}).get('dateTime')}")

# cleanup: cancel the demo booking (marks cancelled on Google, frees the slot)
adapter.cancel_booking(booking_id, "Front Desk real-calendar demo cleanup")
print(f"cancelled {booking_id}; demo event marked cancelled on Google Calendar")
print("\nREAL-CALENDAR PROOF COMPLETE")
print(f"  booking id: {booking_id}")
print(f"  retry:      same id, no duplicate")
print(f"  cleanup:    cancelled, slot freed")
