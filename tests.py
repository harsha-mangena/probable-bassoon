"""Local proof: the checks are not optional.

Run: python3 tests.py
"""

import sys
import tomllib
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, ".")
from engine import (
    BookingError,
    Engine,
    HoldExpiredError,
    SlotConflictError,
)

TZ = ZoneInfo("America/Los_Angeles")
# Monday 2026-10-05 09:00 PT; salon opens Tue 2026-10-06.
NOW = datetime(2026, 10, 5, 9, 0, tzinfo=TZ)


def load() -> Engine:
    with open("salon.toml", "rb") as f:
        config = tomllib.load(f)
    return Engine(config)


def first_slot(engine: Engine):
    slots = engine.list_free_slots("haircut", "2026-10-06", now=NOW)
    assert slots, "expected open slots on Tue 2026-10-06"
    return slots[0][0]


passed = []


def check(name, fn):
    fn()
    passed.append(name)
    print(f"  ok: {name}")


print("Front Desk local tests")


def t_commit_returns_id():
    e = load()
    s = first_slot(e)
    hold = e.hold_slot("haircut", s, client_hold_id="k-1", now=NOW)
    booking_id, replayed = e.commit_booking(
        hold, "Vam", "+14155550100", "idem-1", now=NOW)
    assert booking_id.startswith("FD-") and not replayed
    assert e.get_booking(booking_id)["status"] == "confirmed"


def t_retry_same_key_returns_same_id():
    e = load()
    s = first_slot(e)
    hold = e.hold_slot("haircut", s, client_hold_id="k-2", now=NOW)
    b1, _ = e.commit_booking(hold, "Vam", "+14155550100", "idem-2", now=NOW)
    # simulate: response lost, client retries with the SAME key
    b2, replayed = e.commit_booking(hold, "Vam", "+14155550100", "idem-2", now=NOW)
    assert b1 == b2 and replayed
    assert e.count_bookings() == 1, "retry must not create a second booking"


def t_second_hold_on_same_slot_fails():
    e = load()
    s = first_slot(e)
    e.hold_slot("haircut", s, client_hold_id="k-3a", now=NOW)
    try:
        e.hold_slot("haircut", s, client_hold_id="k-3b", now=NOW)
    except SlotConflictError:
        return
    raise AssertionError("second hold on the same slot must fail")


def t_hold_retry_same_client_id_returns_same_hold():
    e = load()
    s = first_slot(e)
    h1 = e.hold_slot("haircut", s, client_hold_id="k-4", now=NOW)
    h2 = e.hold_slot("haircut", s, client_hold_id="k-4", now=NOW)
    assert h1 == h2


def t_move_is_one_change_to_same_id():
    e = load()
    slots = e.list_free_slots("haircut", "2026-10-06", now=NOW)
    s1, s2 = slots[0][0], slots[4][0]  # 09:00 and 10:00: windows must not overlap
    hold = e.hold_slot("haircut", s1, client_hold_id="k-5", now=NOW)
    b1, _ = e.commit_booking(hold, "Vam", "+14155550100", "idem-5", now=NOW)
    moved = e.move_booking(b1, s2, now=NOW)
    assert moved == b1, "move must keep the same booking id"
    # old slot is free again
    free = [s for s, _ in e.list_free_slots("haircut", "2026-10-06", now=NOW)]
    assert s1 in free


def t_failed_move_leaves_original_untouched():
    e = load()
    slots = e.list_free_slots("haircut", "2026-10-06", now=NOW)
    s1, s2 = slots[0][0], slots[4][0]  # 09:00 and 10:00: windows must not overlap
    hold = e.hold_slot("haircut", s1, client_hold_id="k-6a", now=NOW)
    b1, _ = e.commit_booking(hold, "Vam", "+14155550100", "idem-6", now=NOW)
    # someone else takes s2
    hold2 = e.hold_slot("haircut", s2, client_hold_id="k-6b", now=NOW)
    e.commit_booking(hold2, "Sam", "+14155550200", "idem-6b", now=NOW)
    try:
        e.move_booking(b1, s2, now=NOW)
    except SlotConflictError:
        pass
    else:
        raise AssertionError("move into a taken slot must fail")
    assert e.get_booking(b1)["start_utc"] == __import__("engine")._iso_utc(s1)


def t_expired_hold_frees_slot_and_rejects_commit():
    e = load()
    s = first_slot(e)
    hold = e.hold_slot("haircut", s, client_hold_id="k-7", now=NOW)
    later = NOW + timedelta(minutes=11)  # ttl is 10
    assert e.expire_holds(now=later) == 1
    try:
        e.commit_booking(hold, "Vam", "+14155550100", "idem-7", now=later)
    except (HoldExpiredError, BookingError):
        pass
    else:
        raise AssertionError("commit on expired hold must fail")
    # slot is bookable again
    hold2 = e.hold_slot("haircut", s, client_hold_id="k-7b", now=later)
    b, _ = e.commit_booking(hold2, "Vam", "+14155550100", "idem-7b", now=later)
    assert b.startswith("FD-")


def t_reminders_derived_at_commit():
    e = load()
    s = first_slot(e)
    hold = e.hold_slot("haircut", s, client_hold_id="k-8", now=NOW)
    b, _ = e.commit_booking(hold, "Vam", "+14155550100", "idem-8", now=NOW)
    due = e.reminders_due(now=NOW + timedelta(days=2))
    mine = [r for r in due if r["booking_id"] == b]
    assert len(mine) == 1 and mine[0]["sent"] == 0
    e.mark_reminder_sent(mine[0]["id"])
    assert not [r for r in e.reminders_due(now=NOW + timedelta(days=2))
                if r["booking_id"] == b]


for name, fn in sorted(
    [(k, v) for k, v in list(globals().items()) if k.startswith("t_")]
):
    check(name, fn)

print(f"\n{len(passed)} tests passed.")
