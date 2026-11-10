# Global Intelligence & Wealth Brief

A private daily briefing on world affairs, the economy, markets, technology and business opportunities, built from
reputable free sources, verified link by link, analysed by Claude, delivered by 6:00 am Eastern to the app's
**Brief** tab and to Telegram. It is a separate module (`intel/`): it never changes the breakout scanner.

## 1. Architecture

```
            sources.yaml                                    ┌──────────────── one brief ────────────────┐
 RSS / Atom (CBC, BBC, CNBC, BoC, Fed, StatCan, Ontario,    │ 1 collect   last 30 h (Mon: +weekend)     │
 Quebec, Radio-Canada, La Presse, The National, ...)  ──────▶ 2 cluster   merge the same story, score:  │
 GDELT (Reuters, AP ...; server only)                 ──────▶             outlets, source tier, focus,  │
 NewsAPI (optional, paid plan)                        ──────▶             recency; route to sections    │
 Yahoo Finance (indexes, futures, yields, FX, oil, gold) ───▶ 3 verify    every link: HTTP 200, free,   │
                                                            │             not opinion, not paywalled    │
                                                            │ 4 analyse   Claude, strict JSON schema,   │
                                                            │             facts only from the articles; │
                                                            │             items without a verified      │
                                                            │             source are deleted            │
                                                            │ 5 publish   archive + pages + Telegram    │
                                                            └───────────────────────────────────────────┘
   GitHub mode (default, no server)                        Server mode (optional, Docker on a VPS)
   - scan.yml cron 05:20 ET daily (gated, auto-retry)       - `python -m intel serve`: scheduler (05:40 ET),
   - archive = JSON in reports/intel/ (in git)                Telegram commands, /healthz, watchdog alerts
   - pages on GitHub Pages: intel/index.html,               - PostgreSQL archive (full-text search), Redis
     archive.html (search, trends), weekly.html               cache + locks
   - Telegram push of the summary + CEO brief               - INTEL_SCHEDULE=off: GitHub builds, server answers
```

Why this design: everything that has to run at 6 am runs where the scanner already runs (free, monitored, no
server to maintain). The server is only needed for instant Telegram commands, and reads the same brief.

**Quality rules enforced in code** (not just asked of the AI):
- Only sources in `sources.yaml` and the GDELT domain list; hard-paywall and opinion-led domains are blocked.
- Every link is fetched before it can be shown: 404/410/5xx/timeouts, login or subscribe redirects, JSON-LD
  `isAccessibleForFree: false`, locked/metered content tiers and subscriber-wall text are all rejected; opinion
  pieces (URL, headline, `OpinionNewsArticle`, opinion sections) are rejected.
- GDELT (the way to find Reuters and AP stories, which have no public feeds) rate-limits GitHub's shared servers,
  so it is on only in server mode (`INTEL_GDELT=on`). Their big stories still arrive through the other outlets.
- Reuters, AP, La Presse and canada.ca block or stall automated checks; their links (straight from GDELT/feeds) are shown and marked
  "site blocks automated checks". They are free to read in a browser.
- The AI sees article ids, never writes URLs; any id it cites that wasn't given is removed; an item left with no
  verified source is deleted. Market numbers come from Yahoo Finance, not from the AI.
- A spending cap (`INTEL_MAX_DAILY_COST_USD`) stops runaway costs.

## 2. Folder structure

```
intel/
  config.py       settings (env vars), sections and keyword routing, focus, themes, prices
  sources.yaml    every feed and GDELT query (edit this to add/remove sources)
  collect.py      RSS/Atom parser, GDELT, NewsAPI; canonical URLs, dedupe
  cluster.py      story clustering, scoring, section routing
  verify.py       link checks, paywall / opinion detection, article text
  markets.py      market snapshot (Yahoo Finance)
  llm.py          Claude / OpenAI with JSON schemas, retries, cost tracking, spending cap
  prompts.py      editorial rules and schemas
  pipeline.py     daily brief and weekly summary
  render.py       web page, archive/search page, Telegram messages
  store.py        archive: JSON files (GitHub) or SQL (PostgreSQL / SQLite)
  cache.py        Redis or in-memory cache and locks
  telegram.py     sending, alerts, command bot (only your chat)
  service.py      server: scheduler, bot, health endpoint, watchdog
  __main__.py     CLI: run, weekly, probe, search, trends, status, serve, selftest
  selftest.py     offline tests (also run against PostgreSQL + Redis in CI)
deploy/
  Dockerfile, docker-compose.yml, .env.example, schema.sql, requirements-server.txt, backup.sh
.github/workflows/scan.yml      daily brief schedule (mode `brief`), weekly, source probe
.github/workflows/intel-ci.yml  tests on PostgreSQL + Redis + Docker build
reports/intel/                  archive in GitHub mode (briefs/, weekly/, index.json, trends.json, runs.json)
```

## 3. Database (PostgreSQL, `deploy/schema.sql`)

| Table | Holds |
|---|---|
| `briefs` | one row per daily/weekly brief: date, mode, model, cost, full JSON (JSONB) |
| `brief_items` | every item: section, position, headline, summary, why it matters, full body (JSONB); full-text index |
| `articles` | every article ever cited: URL, outlet, title, date, link-check result |
| `item_articles` | which articles back which item |
| `item_themes` | theme tags per item and date (trend reports) |
| `runs` | every run: status, error, stats (articles, links checked, AI tokens, cost) |

Search uses PostgreSQL full-text search (`websearch_to_tsquery`), e.g. `bank of canada -housing`.

## 4. Setup in GitHub mode (what runs today)

The brief already runs every morning; without keys it publishes a **headlines-only** brief. To switch on the AI
analysis and Telegram, add these in GitHub: repository → Settings → Secrets and variables → Actions.

| Secret | Where to get it |
|---|---|
| `ANTHROPIC_API_KEY` | console.anthropic.com → API keys (add a payment method; set a monthly limit there too) |
| `TELEGRAM_BOT_TOKEN` | Telegram → @BotFather → `/newbot` → copy the token |
| `TELEGRAM_CHAT_ID` | Telegram → @userinfobot → your numeric id. Then open your bot and press **Start** once |
| `NEWSAPI_KEY` (optional) | newsapi.org paid plan only (the free plan is for development) |

Optional **Variables** (same page, Variables tab): `INTEL_MODEL` (`claude-haiku-5-5` for low cost),
`INTEL_EFFORT` (`low` / `medium` / `high`).

Manual runs: Actions → Daily breakout scan → Run workflow → mode `brief` (now), `weekly`, or `brief_probe`
(checks every source and writes `intel/source_health.json`).

**Cost** (Nov 2026 prices): about 14 AI calls a day. Sonnet 5.5 at medium effort ≈ US$0.70-1.00/day (≈ $25/month);
Haiku 5.5 ≈ $0.05/day. The per-brief cap defaults to $3.

## 5. Server mode (Telegram commands any time)

Any Ubuntu 24.04 VPS with 1 vCPU / 1-2 GB RAM is enough (DigitalOcean Basic droplet, AWS Lightsail, Azure B1s/B1ms).

```bash
# 1. Server basics (as root, once)
adduser --disabled-password --gecos "" intel && usermod -aG sudo intel
mkdir -p /home/intel/.ssh && cp ~/.ssh/authorized_keys /home/intel/.ssh/ && chown -R intel:intel /home/intel/.ssh
sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/; s/^#\?PermitRootLogin.*/PermitRootLogin no/' /etc/ssh/sshd_config
systemctl restart ssh
ufw default deny incoming && ufw default allow outgoing && ufw allow OpenSSH && ufw --force enable
apt update && apt -y upgrade && apt -y install unattended-upgrades fail2ban git
dpkg-reconfigure -f noninteractive unattended-upgrades
timedatectl set-timezone America/Toronto

# 2. Docker (as intel)
curl -fsSL https://get.docker.com | sudo sh && sudo usermod -aG docker intel && newgrp docker

# 3. App
sudo mkdir -p /opt && sudo chown intel /opt && cd /opt
git clone https://github.com/becharamezannar-eng/breakout-scanner.git && cd breakout-scanner
cp deploy/.env.example deploy/.env && chmod 600 deploy/.env && nano deploy/.env     # keys, Telegram, password
docker compose -f deploy/docker-compose.yml --env-file deploy/.env up -d --build

# 4. Check
docker compose -f deploy/docker-compose.yml ps                      # all "healthy"
docker compose -f deploy/docker-compose.yml logs -f intel           # "service up", "bot started"
# In Telegram send /status, then /brief

# 5. Nightly database backup
(crontab -l 2>/dev/null; echo "30 3 * * * /opt/breakout-scanner/deploy/backup.sh >> /home/intel/backup.log 2>&1") | crontab -
```

Choose **one producer**: keep `INTEL_SCHEDULE=off` (GitHub builds the 6 am brief; the server answers commands and
archives each published brief in PostgreSQL), or set `on` and remove the `brief` crons from `scan.yml`.

**Commands:** `/brief` `/markets` `/geopolitics` `/business` `/wealth` `/canada` `/technology` `/watchlist`
`/weekly`, plus `/search <words>`, `/trends`, `/status`, `/help`. Add `new` (e.g. `/markets new`) to rebuild that
part from the latest news (takes a few minutes, costs one or two AI calls).

## 6. Security

- Secrets only in GitHub Secrets or `deploy/.env` (mode 600, git-ignored). Never in code, chat or commit messages.
- Telegram bot answers only `TELEGRAM_CHAT_ID`; other chats are ignored and logged. Long polling: no open ports.
- Containers: non-root user, read-only filesystem, `no-new-privileges`, all Linux capabilities dropped, memory
  limits; PostgreSQL and Redis are not exposed outside Docker's private network.
- Server: SSH keys only, root login off, firewall (SSH only), fail2ban, automatic security updates.
- Limit the API key's spend in the Anthropic console, plus the brief's own cap.
- Rotate keys yearly or at once if exposed: create the new key, update the secret/`.env`, restart, delete the old one.
- The GitHub Pages brief is public (like the scanner reports) but contains only news analysis, never your
  portfolio or keys. Your reader profile stays in the prompt, which is not published.

## 7. Maintenance

| When | What |
|---|---|
| Weekly (2 min) | Glance at the footer of a brief: sources OK out of total, links checked. Telegram alerts arrive on failure. |
| Monthly | Run mode `brief_probe`; fix or remove sources marked BAD in `intel/source_health.json` / `sources.yaml`. Check AI spend in the Anthropic console. |
| Server, monthly | `cd /opt/breakout-scanner && git pull && docker compose -f deploy/docker-compose.yml --env-file deploy/.env up -d --build`; `docker image prune -f`. |
| Server, quarterly | Test a restore: `gunzip -c /var/backups/intel/intel-DATE.sql.gz \| docker compose -f deploy/docker-compose.yml exec -T db psql -U intel -d intel` (on a test copy). |
| Model updates | Set `INTEL_MODEL` to the new model id and update `PRICES` in `intel/config.py`. |

**Troubleshooting**
- *No brief this morning:* GitHub → Actions → the run marked failed → step "Global Intelligence & Wealth Brief".
  The 6:20 run retries automatically. Server: `docker compose ... logs intel --since 2h`.
- *"Headlines only":* the AI key secret is missing or invalid.
- *Thin sections:* run `brief_probe`; a source may have changed its feed address.
- *Telegram silent:* press Start in the bot chat; check `TELEGRAM_CHAT_ID` is your numeric id.
