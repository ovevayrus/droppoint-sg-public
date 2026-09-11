# Security policy

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
