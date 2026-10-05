# Optional telephone channel

The default browser voice/text channel is usable without external accounts. This adapter connects an actual phone number through Twilio Voice; free trials are restricted and normal phone-number/per-minute usage is paid. No number, credential or paid subscription is provisioned by the repository.

1. Run the app behind a public HTTPS reverse proxy.
2. Set `APP_ORIGIN` to that exact external origin (for example `https://clinic.example.com`) and securely provide the account's `TWILIO_AUTH_TOKEN` as an environment variable. Do not commit it or print it.
3. Configure the Twilio number's incoming-call webhook: **POST** `https://clinic.example.com/api/voice/twilio`.
4. Use a verified caller/number as required by your current trial restrictions. Call it and walk through booking, then management/cancellation.
5. Treat provider voice/transcription processing and any provider recording settings as part of your deployment’s data-handling review. This app does not request recordings.

The adapter uses TwiML `<Gather input="dtmf speech">` and `<Say>`; no Twilio SDK, LLM, or ElevenLabs key is needed. The signed full public URL includes the callback `?step=N` query. Reverse proxies must preserve it. Host/forwarded headers are not used to reconstruct the signing URL; the configured origin is authoritative. Invalid signatures fail closed. Retries replay the same server-side call turn.

Keypad conventions (finish every numeric input with `#`):

- Main menu: 1 book, 2 manage, 3 staff contact.
- Doctor/slot menus: enter the menu number.
- Date: eight digits `YYYYMMDD`, or 0 for next available; speech also accepts today/tomorrow.
- Recurrence: 1 for one visit, 2–12 for weekly visits.
- Name: speak it. Phone: speak it or enter digits with country code.
- Confirm: 1 yes, 2 no. A booking is written only after confirmation.
- Management: enter the ten-digit reference, then twelve-digit code. Use 0 to cancel all, or select an occurrence; 1 cancel, 2 move.
- A move asks for date, new slot, then explicit confirmation.

Booking references and management codes are spoken as individual digits. The `staff` option gives the configured clinic contact number; it does not pretend to transfer or notify staff. A real warm transfer would require a separately configured routing destination and provider integration.

Tests exercise signature validation, menu response generation, full booking, webhook retry replay, and disabled/invalid-signature rejection. Actual audio quality, provider callbacks over your proxy, verified-number restrictions and live call handling must be checked after credentials and a number are configured.

Official sources: [trial units and limits](https://www.twilio.com/docs/usage/trials), [Voice trial guide](https://www.twilio.com/docs/usage/trials/try-out-voice), [Gather](https://www.twilio.com/docs/voice/twiml/gather), [webhook signing](https://www.twilio.com/docs/usage/security).
