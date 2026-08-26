# Quantum CFP Tracker

A lightweight English web dashboard for QCrypt, QIP, TQC, QTML, QCTiP, AQIS, QCNC, QCMC, IEEE QCE / Quantum Week, QSim and QPL.

## Automatic updates

The frontend is intentionally static. The automatic part is a scheduled GitHub Action:

1. GitHub Actions runs every day.
2. `update_deadlines.py` downloads official CFP pages.
3. Conference-specific parser adapters extract deadlines.
4. `data.json` is updated.
5. The website reads `data.json` and immediately shows the new dates.

### Why one parser per conference?

There is no universal CFP format. Some conferences use HTML tables, some use plain text, some use CMT/OpenReview/ConfTool pages, and some publish deadlines in PDFs or announcements. A single generic scraper will eventually produce false dates.

For reliability, each conference should have an adapter with:
- official source URL(s)
- CSS selectors / regex / PDF extraction rules
- timezone
- deadline type
- validation rules
- fallback behavior

The updater should never guess. If extraction fails, keep the previous value and flag the source for manual review.

## Deployment

Put this folder in a GitHub repository and enable GitHub Pages. The repository can then serve `index.html` while the scheduled Action keeps `data.json` fresh.

## Next step

Add adapters for all 11 conferences and a notification layer (email, Telegram, Discord or calendar) for changes such as:
- new deadline published
- deadline extended
- deadline moved
- CFP opened
