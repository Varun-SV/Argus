# Argus Dashboard — Web UI kit

A recreation of the **self-hosted web dashboard** (`argus serve`, runs on localhost). One rich Overview screen.

## Run it
Open `index.html`.

## What's shown
- **Top nav** — Overview / Runs / Tests / Flakiness, environment selector, search.
- **KPI row** — pass rate, runs today, avg duration, flaky tests, each with a week-over-week delta.
- **Pass / fail trend** — 14-day stacked bar chart (pure CSS, no chart library).
- **Flakiness** — per-test flakiness score with a last-12-runs sparkline.
- **Recent runs** — cross-adapter run history table with status, adapter, steps, provider, duration.

## Files
- `index.html` — layout CSS + wiring.
- `data.js` — KPIs, trend, flaky tests, runs (window.DASH_DATA).
- `app.jsx` — all dashboard components.
- Reuses `../console/base.jsx` for the shared `Icon` / `Status` / `Tag` atoms.

## Notes
Built on the design system's CSS layer + Lucide icons. Charts are hand-built from tokens so they theme automatically.
