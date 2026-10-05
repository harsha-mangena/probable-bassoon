# Validation record

Validated on 2026-10-05 against the clinic migration, using isolated disposable databases. No live patient data or provider credentials were used.

## Passed

- **34 Python unit/HTTP integration tests**: doctor isolation and buffers; simultaneous booking attempts (one winner); idempotent holds/commits and mismatched-key rejection; recurrence rollback; individual/series cancellation; skipping past occurrences in future-series cancellation; cancellation cutoff and staff override; atomic rescheduling; invalid/off-grid/past/out-of-horizon starts; schedule overrides and conflict rollback; daylight-saving recurrence and missing/ambiguous starts; doctor deactivation guards; persistent restarts; call state/turn retries; no booking without confirmation; expired call receipt cleanup; salted password hashes and session revocation; role enforcement, CSRF/origin checks; malformed/oversized inputs; rate limiting; disabled/invalid/signed phone webhooks; complete signed phone booking and replay.
- **Real-browser end-to-end suite** using Playwright 1.62.1 with Chromium 131 supplied as an alternate executable: two-visit recurring booking → partial cancellation → move → cancel remaining series; guided call booking; staff login and appointment retrieval; date-specific time off; doctor/staff creation; manager permission visibility and staff cancellation. No API mocking.
- Mobile viewport 390×844: no document-level horizontal overflow; no browser JavaScript errors. Desktop and mobile screenshots were visually inspected.
- JavaScript syntax checks, Python compilation, and Git whitespace checks passed.

The standard Playwright browser download was unavailable in this execution environment, so local browser validation used an alternate Chromium executable via `BROWSER_EXECUTABLE`. The committed browser suite defaults to Playwright’s normal Chromium installation in CI and supports that override for other environments.

## Not exercised

- Real microphone transcription/synthesis, microphone permission prompts, and browser-provider speech-service availability. The actual state machine was exercised through typed input and signed phone protocol fixtures.
- A live Twilio telephone number, provider trial eligibility, audio quality, or production reverse-proxy callback delivery. The phone channel remains disabled until configured.
- Docker build/runtime: Docker was not available in the execution environment.
- GitHub Actions execution is separate from the local results above; a workflow is committed for subsequent pushes.

Default seeded clinic/doctor/contact data is demonstrative. Production deployment configuration and actual clinic policies must be supplied by the operator.
