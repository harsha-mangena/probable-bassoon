# Front Desk — decisions (2026-10-04)

Research: three side chats (9 products; 8 failure modes; minimal integration design).
They agree on the core — the product is the booking write. I resolved the rest:

1. **Hold-then-commit, not direct book.** Nothing contradicted it; the failures work demands atomic check-and-set holds with TTL. Reason: a caller needs the slot reserved while giving their name; direct booking forces the agent to speak before it knows, or stall in silence. Commit re-verifies the hold inside one transaction.
2. **Python + SQLite, stdlib only.** Reason: the race guard needs real transactions and unique constraints — SQLite serializes writers, so check-and-insert in one transaction *is* the lock. Zero dependencies, runs anywhere. A second business is a config file either way, so this wins on "id comes back" and "retry is safe" with the least machinery.
3. **Reminders are derived, never configured.** Products verdict: reminders must be a consequence of the write, not a workflow the owner builds. Reason: at commit, reminder rows are inserted in the same transaction; an hourly sweep sends due ones with a sent-flag. No detached reminder store, no owner-built workflows.
4. **Text call now, voice as a channel later.** All nine products are voice-first, but telephony needs keys and spend. Reason: the engine doesn't care which channel drives it, so v1 proves the engine over text in the panel; voice becomes an adapter.

Kept where the chats agreed: 6-op calendar interface (slots → hold → idempotent commit → move → cancel → get); commit idempotent via client-generated key; move = one update to the same id, never cancel-plus-rebook; confirmation language only after a booking id exists. Hospital stays out: multi-resource atomic holds and preconditions (referral, insurance, credentialing) need a v2 interface, not config.
