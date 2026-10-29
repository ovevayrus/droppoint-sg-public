# Security policy

Use a separate test Telegram bot. Polling refuses an existing webhook unless explicitly authorized by `ALLOW_WEBHOOK_RESET`; do not casually set that override with production credentials.

Trip work has finite attempt/time limits, a per-user cooldown and a shared process request window. These safeguards assume one polling process and must be set below the actual provider entitlement. Parallel children are awaited before coordinate-cache cleanup. Telegram, OneMap, hosting and map-link providers retain their data independently.

## Reporting a vulnerability

Please use GitHub's private security-advisory feature instead of opening a public
issue. Include the affected version, impact, and a minimal reproduction where safe.

## Secrets and location data

- Keep Telegram and OneMap credentials in the ignored `.env` file or a deployment
  secret store.
- Never include credentials, addresses, coordinates, or full request URLs in logs,
  fixtures, screenshots, or bug reports.
- Rotate a credential immediately if it is committed or appears in shared logs.
- Keep real network tests behind the `integration` marker; the default suite must
  remain offline.
