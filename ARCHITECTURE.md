# Architecture: modular add-ons and the phone app

The existing scanner (`breakout_scanner.py`, `lookup.py`, the schedule in `.github/workflows/scan.yml`, the book rules,
`reports/holdings.csv` and `reports/tracker.csv`) is the core and stays as it is. Every new feature is a separate module
that reads the core's functions and writes its own files. A failure in an add-on cannot stop the core scan
(`continue-on-error` in the workflow).

```
                ┌────────────────────── GitHub Actions (same schedule) ──────────────────────┐
                │ 1. Self-test          2. CORE evening scan / morning check / backtest       │
                │ 3. ESG scan  (esg_scanner.py, imports the core, writes reports/esg/)        │
                │ 4. User portfolios (users_monitor.py, phase 2: reads/writes the account DB) │
                │ 5. Build site -> GitHub Pages                                               │
                └───────────────┬──────────────────────────────────────┬───────────────────────┘
                                │ static reports + JSON                │ service key (GitHub secret)
                                ▼                                      ▼
   GitHub Pages: index.html (core report), live.html,       Account service (Supabase: Auth + Postgres
   morning.html, lookup/, esg/, live_brief.json, app/        with row-level security), phase 2
                                │                                      │ user's own session only
                                └──────────────┬───────────────────────┘
                                               ▼
                       Phone app (app/): installable web app (PWA) today;
                       the same code can be wrapped for Google Play / App Store (Capacitor)
```

## Tabs and where they live
| Tab | Module | Output | Status |
|---|---|---|---|
| Scanner (core) | `breakout_scanner.py`, `lookup.py` | `index.html`, `live.html`, `morning.html`, `lookup/` | unchanged |
| Morning fix | `--live` run before `--morning`; `write_live_brief()` | `live_brief.json`, morning block | done |
| ESG scanner | `esg_scanner.py` | `esg/index.html`, `esg/lookup/`, `reports/esg/*.csv` | done |
| Phone app shell | `app/` (manifest, service worker, tabs) | `app/index.html` | done |
| Accounts / user portfolios | (not built) | - | dropped 2026-10-19: owner-only app, no sign-in |

## Owner access
No username or password. The app's Portfolio tab opens the owner's portfolio from the latest evening report. The existing link keeps working exactly as before.

## Accounts and user portfolios (not built - kept as a design note)
* **Sign-up / login:** Supabase Auth (email + password, stored hashed by Supabase; password reset by email). Either
  open sign-up, or invite-only: the owner adds users in the Supabase dashboard and each user sets and later changes
  their own password.
* **List of users:** Supabase dashboard -> Authentication -> Users (email, sign-up date, last sign-in).
* **Portfolios:** table `holdings(user_id, ticker, buy_price, buy_date, shares)`. Row-level security: each user can
  read and write only their own rows. Nothing about external users goes into this public repository.
* **Monitoring:** after the evening scan, `users_monitor.py` reads all holdings with the service key (a GitHub
  secret), runs the core's `holding_status()` (trend, 50/200-day, chandelier line) and breakout checks on each, and
  writes each user's results back to their own rows. The core's `holdings.csv` and tracker are never touched.
* **Free tier note:** a free Supabase project pauses after 7 days without activity; the nightly job keeps it active.

## Phone app
* **Now:** installable web app (PWA). Android/Chrome: "Install on this phone" button or menu -> Install app. iPhone:
  Safari -> Share -> Add to Home Screen. Full screen, own icon, opens offline to the last shell; reports always load fresh.
* **App stores (optional):** wrap `app/` with Capacitor. Google Play: one-time developer fee; Apple App Store: yearly
  developer fee, needs a Mac (or a cloud build), and Apple reviews web wrappers strictly (push notifications and other
  native features help). Same backend, no change to the scanner.

## Before opening to external users
* Sending buy/sell reads on other people's own portfolios may count as advice under Canadian securities rules;
  get a securities lawyer's view first.
* Holding other people's emails and portfolios needs a privacy notice and consent (PIPEDA).
* The current reports (including the owner's portfolio) are on a public link; consider moving the owner's holdings
  off the public site before sharing the app widely.
