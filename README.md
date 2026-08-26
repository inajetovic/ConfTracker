# Quantum CFP Tracker

A lightweight English web dashboard for QCrypt, QIP, TQC, QTML, QCTiP, AQIS, QCNC, QCMC, IEEE QCE / Quantum Week, QSim and QPL.

## Automatic updates

The frontend is intentionally static. The automatic part is a scheduled GitHub Action:

1. GitHub Actions runs every day.
2. `update_deadlines.py` downloads official CFP pages.
3. Conference-specific parser adapters extract deadlines.
4. `data.json` is updated.
5. The website reads `data.json` and immediately shows the new dates.

## How to use

1. run `update_deadlines.py`
2. run `python3 -m http.server 8000`
3. go to http://localhost:8000/
