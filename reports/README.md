# Generated Human Design reports

The `/report` Telegram command generates a Human Design report PDF and an adjacent JSON metadata file under `reports/generated/` by default.

Usage:

- `/report` — use the active profile from `family.json`.
- `/report michael` — use a saved family profile.
- `/report 12/10/1989 @17:07 Simi Valley, CA` — parse one-off birth data from the command text.

The PDF is sent back to Telegram as a document attachment. The metadata JSON captures the source input, resolved birth data, chart payload, generated timestamp, and PDF path so the artifact can be audited or reused later.

Override the artifact directory with `NEXTSTEP_REPORTS_DIR` if a deployment needs a persistent volume outside the repo checkout.
