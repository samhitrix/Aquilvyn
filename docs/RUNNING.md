# How to run Aquilvyn, step by step

There are two ways to run Aquilvyn locally:

- **Option A: Docker Compose (recommended).** Everything runs in containers with one command.
- **Option B: without Docker.** Use your own PostgreSQL (15–18) and Redis and start everything with `python scripts/dev.py`. Useful when developing a single service.

Both options use the same **`.env`** file for configuration.

---

## 0. Prerequisites

| Need | Option A (Docker) | Option B (no Docker) |
|---|---|---|
| Git | ✅ | ✅ |
| Docker Desktop / Docker Engine 24+ with Compose v2 | ✅ | — |
| Python 3.11 – 3.13 | only for the smoke test | ✅ |
| PostgreSQL 15–18 + Redis 7 | — (containers) | ✅ installed locally |
| Node.js 20+ | — (container) | ✅ for the web app |
| RAM | ~4 GB (lite) / ~7 GB (full with observability) | ~2 GB |

---

## 1. Get the code

```bash
git clone https://github.com/samhitrix/Aquilvyn.git
cd Aquilvyn
```

## 2. Create your configuration (`.env`)

> **One command set for every OS.** All commands below use `python scripts/fm.py <target>`, which works in Windows PowerShell/CMD, macOS and Linux with no extra tools. On macOS/Linux, `make <target>` is an equivalent shortcut. Windows has no `make`, so use the `python` form there.
> If `python` isn't found on Windows, use `py` instead, e.g. `py scripts/fm.py setup`.

```powershell
python scripts/fm.py setup
```

This creates `.env` from `.env.example` and **generates secure values automatically** for `JWT_SECRET`, `FM_ENCRYPTION_KEY`, `POSTGRES_PASSWORD` and `GRAFANA_ADMIN_PASSWORD`. Running it again is safe; nothing changes.

**Database settings are set once.** `.env` only holds `POSTGRES_USER`, `POSTGRES_PASSWORD` and `POSTGRES_DB`. Every service builds its connection URL from those. Docker uses the `pgbouncer`/`postgres` containers, and no-Docker mode uses `localhost:5432`. There is no URL to edit and no password to repeat.

Then check your machine:
```powershell
python scripts/fm.py doctor
```
Without Docker, `doctor` checks Option B instead: it logs in to your PostgreSQL with the `POSTGRES_*` values from `.env`, pings Redis/Memurai, and checks that the service ports are free. With Docker running, it checks the Docker ports and file line endings.

Prefer doing it by hand? Copy the file (`Copy-Item .env.example .env` in PowerShell, `cp .env.example .env` on macOS/Linux) and set these yourself:

| Setting | What to put | How to generate |
|---|---|---|
| `POSTGRES_PASSWORD` | any strong password (special characters are fine) | — |
| `JWT_SECRET` | long random string (≥ 32 chars) | `python -c "import secrets;print(secrets.token_urlsafe(48))"` |
| `FM_ENCRYPTION_KEY` | 32-byte key (encrypts PAN numbers + AI API keys) | `python -c "import os,base64;print(base64.urlsafe_b64encode(os.urandom(32)).decode())"` |
| `GRAFANA_ADMIN_PASSWORD` | password for Grafana | — |

Optional settings:

| Setting | Effect |
|---|---|
| `MARKET_DATA_PROVIDER` | `yahoo` (default): live NSE/BSE prices, needs internet. `simulated`: a fully offline demo. MF NAVs always come from AMFI. |
| `FINNHUB_API_KEY`, `ALPHAVANTAGE_API_KEY` | Optional extra fundamentals sources. They only fill fields Yahoo, NSE and Screener.in left empty (free plans mostly cover US listings). |
| `ANTHROPIC_API_KEY` | Claude narration and review. |
| `OPENAI_API_KEY` + `OPENAI_MODEL`, `GEMINI_API_KEY` + `GEMINI_MODEL` | More AI reviewers. |
| `GROQ_API_KEY` + `GROQ_MODEL`, `CLOUDFLARE_API_TOKEN` + `CLOUDFLARE_ACCOUNT_ID` + `CLOUDFLARE_MODEL` | Free-tier AI reviewers, good as fallbacks. |
| `OLLAMA_MODEL` (+ `OLLAMA_BASE_URL`) | A fully local AI reviewer running through Ollama. |
| `AI_PRIMARY_PROVIDER` | Which AI is tried first. The rest follow in the order set under *Settings → AI*; one that runs out of quota is skipped for 24 hours. |
| `OIDC_*` | "Login with Gmail" — shown when `OIDC_ENABLED=true` and `OIDC_CLIENT_ID` + `OIDC_CLIENT_SECRET` are set. Step-by-step: [Login with Gmail](#login-with-gmail-optional). |

With no AI keys at all, everything still works in **rules-only** mode, and the UI says so.
AI keys can also be added later in the web app under **Settings → AI providers**. Those are stored encrypted per household. **Settings → Test all** checks every AI model and every data source in one go and shows why one fails.

> **Keep keys private.** API keys go only in `.env` (git-ignored) or in Settings — never in `.env.example`, a commit, an issue or a chat. If a key was ever pasted somewhere shared, revoke it at the provider and create a new one. Error messages in the app mask keys automatically.

---

## Login with Gmail (optional)

Aquilvyn can let people sign in with their Google account. You need a **Client ID** and **Client Secret** from Google — free, about 10 minutes, done once.

**Which address to use.** Everything below uses your app's address:
- Docker (`fm.py up` / `up-lite`): `http://localhost:8080` — or the port `fm.py` printed if 8080 was busy.
- Without Docker (`fm.py dev` + `npm run dev`): `http://localhost:3000`.

So the **redirect URI** is `<app address>/api/v1/auth/oidc/callback`, e.g. `http://localhost:8080/api/v1/auth/oidc/callback`.

### 1. Create or select a Google Cloud project
1. Go to the [Google Cloud Console](https://console.cloud.google.com/) and sign in with your Google account.
2. Click the project drop-down at the top left → **New Project** (or pick an existing project).
3. Give it a name (e.g. *Aquilvyn*) and click **Create**.

### 2. Configure the OAuth consent screen
This must be done before you can create credentials.
1. In the left menu go to **APIs & Services → OAuth consent screen** (newer consoles call it **Google Auth Platform → Branding / Audience / Data Access**).
2. Choose the user type:
   - **External** — anyone with a Gmail account can sign in (the usual choice for a family).
   - **Internal** — only accounts in your own Google Workspace organisation.
3. Click **Create** and fill in the required fields:
   - **App name:** e.g. *Aquilvyn*
   - **User support email** and **Developer contact information:** your email address
4. Click **Save and Continue**.
5. On the **Scopes** page click **Add or Remove Scopes** and select `openid`, `.../auth/userinfo.email` and `.../auth/userinfo.profile`, then **Save and Continue**.
6. If you chose **External**: under **Test users**, add the Gmail address of **every family member** who will sign in. While the app is in *Testing* mode, only these addresses can log in.

### 3. Create the OAuth client (this gives you the ID and secret)
1. In the left menu click **Credentials**.
2. Click **+ Create Credentials → OAuth client ID**.
3. **Application type:** *Web application*. **Name:** e.g. *Aquilvyn login*.
4. Under **Authorized redirect URIs** click **+ Add URI** and paste your redirect URI, for example:
   ```
   http://localhost:8080/api/v1/auth/oidc/callback
   ```
   It must match `OIDC_REDIRECT_URI` in `.env` **character for character** — same `http`, host, port and no trailing slash.
5. Click **Create**. A pop-up shows your **Client ID** and **Client Secret** — copy both.

### 4. Put them in `.env`
```ini
# ---------- Login with Gmail ----------
OIDC_ENABLED=true
OIDC_PROVIDER_NAME=google
OIDC_ISSUER=https://accounts.google.com
OIDC_CLIENT_ID=your-client-id.apps.googleusercontent.com
OIDC_CLIENT_SECRET=GOCSPX-your-client-secret
OIDC_REDIRECT_URI=http://localhost:8080/api/v1/auth/oidc/callback
OIDC_POST_LOGIN_REDIRECT=http://localhost:8080/dashboard
```
Use your own app address in the last two lines (e.g. `http://localhost:3000/...` without Docker). The **Login with Gmail** button appears only when `OIDC_ENABLED=true` **and** both the Client ID and Secret are set — set `OIDC_ENABLED=false` to hide it again without deleting the keys.

Restart so the new settings are picked up: `python scripts/fm.py up-lite` (Docker) or stop and start `python scripts/fm.py dev`. The login page now shows **Login with Gmail**.

**If it doesn't work:**

| What you see | Fix |
|---|---|
| Google says `redirect_uri_mismatch` | The URI in Google Cloud and `OIDC_REDIRECT_URI` differ — check port, `http` vs `https` and trailing slash |
| `access_denied` / "app has not completed verification" | Add that Gmail address under **Test users** (step 2.6) |
| No "Login with Gmail" button | `OIDC_ENABLED` isn't `true`, `OIDC_CLIENT_ID` or `OIDC_CLIENT_SECRET` is empty, or the app wasn't restarted |

Keep the Client Secret like a password: only in `.env`, never in a commit, issue or chat.

---

## Option A: Docker Compose

### A2. Install Docker (once)

| OS | How |
|---|---|
| **Windows 10/11** | Open **PowerShell as Administrator** and run `wsl --install`. Restart the PC, then run `winget install -e --id Docker.DockerDesktop`. Start **Docker Desktop** from the Start menu and wait for "Engine running". |
| macOS | `brew install --cask docker`, or download Docker Desktop from docker.com. Start it once. |
| Ubuntu/Debian | `curl -fsSL https://get.docker.com \| sh && sudo usermod -aG docker $USER`, then log out and back in. |

Check it works with `docker version` and `docker compose version`, then run `python scripts/fm.py doctor`.
If `winget` isn't available, download *Docker Desktop for Windows* from https://www.docker.com/products/docker-desktop/.

### A3. Start the stack

`up` / `up-lite` start Docker Desktop automatically if it isn't running (Windows/macOS) and wait for the engine. Then run:

```powershell
python scripts/fm.py up-lite     # core only, no observability stack   (≈ 4 GB RAM)  ← start here
python scripts/fm.py up          # full stack incl. Grafana/Loki/Tempo  (≈ 7 GB RAM)
```

<details><summary>Plain <code>docker compose</code> equivalents</summary>

| | Windows PowerShell | macOS / Linux |
|---|---|---|
| lite | `$env:OTEL_ENABLED="false"; docker compose up -d --build` | `OTEL_ENABLED=false docker compose up -d --build` |
| full | `docker compose --profile observability up -d --build` | same |
| stop | `docker compose --profile observability down` | same |
</details>

The first build takes a few minutes. What happens:
1. `postgres`, `pgbouncer` and `redis` start.
2. `migrate` creates all schemas, tables and audit triggers, then exits.
3. The seven microservices (identity, portfolio, market, analytics, advisor, dashboard, readiness), four workers (market, portfolio, advisor, readiness), the web app and the `gateway` start.
4. `healthping` starts keeping everything warm.

Check the status:
```powershell
python scripts/fm.py ps          # all "running"/"healthy"; migrate = "exited (0)"
curl.exe http://localhost:8080/health/advisor     # PowerShell: use curl.exe (plain `curl` is an alias there)
```

### Host ports (checked automatically)

Before starting containers, `up` / `up-lite` check every port they publish on your PC. If a port is **free**, it's used. If it's **busy or reserved by Windows**, the next free port is chosen automatically, saved in `.env`, and printed, e.g. `port 8080 is busy → using 8081`. The URLs stay the same on later runs.

| `.env` key | Default | What |
|---|---|---|
| `FM_HTTP_PORT` | 8080 | the app → `http://localhost:8080` |
| `FM_GRAFANA_PORT` | 3001 | Grafana (full `up` only) |
| `FM_DB_PORT` | 55432 | Docker's PostgreSQL, for pgAdmin/psql if you want to look inside |

Your own PostgreSQL on 5432 and Redis on 6379 **don't clash**. Docker's database and cache stay inside Docker; only the ports above are published.

### A4. Open the app

| URL | What |
|---|---|
| http://localhost:8080 (or the port `up` printed) | **Aquilvyn web app**. Register a new account; you become the owner of a new household. |
| http://localhost:8080/api/v1/advisor/docs | Interactive API docs; every service has `/api/v1/<service>/docs`. |
| http://localhost:3001 | Grafana (user `admin`, your `GRAFANA_ADMIN_PASSWORD`). Explore → Loki for logs, Tempo for traces. |

### A5. Useful commands

| What | Any OS | macOS/Linux shortcut |
|---|---|---|
| Follow all logs | `python scripts/fm.py logs` | `make logs` |
| Logs of one service | `python scripts/fm.py logs advisor advisor-worker` | — |
| Re-run migrations | `python scripts/fm.py migrate` | `make migrate` |
| Something looks wrong — collect status, migration log, data counts, recent errors | `python scripts/fm.py diag` | — |
| Free Docker disk space (old images, build cache — never your data) | `python scripts/fm.py prune` (add `--all` to also clear the whole build cache) | — |
| End-to-end test through the gateway | `python scripts/fm.py smoke` (needs `pip install -r requirements-dev.txt`) | `make smoke` |
| Stop (data kept) | `python scripts/fm.py down` | `make down` |
| Stop and **delete all data** | `python scripts/fm.py clean` (asks for confirmation) | `make clean` |

---

## Option B: without Docker (tested with Python 3.13 · works with PostgreSQL 15–18)

### B3. Start PostgreSQL and Redis, then create the database (one command)
**PostgreSQL** (any version from 15 to 18; the migrations use standard features only) must be running. Then:
```powershell
python scripts/fm.py setup       # if not done yet: creates .env with a generated POSTGRES_PASSWORD
python scripts/fm.py db-init     # creates (or updates) the DB user + database from .env
```
`db-init` reads `POSTGRES_USER`, `POSTGRES_PASSWORD` and `POSTGRES_DB` from `.env` and runs `psql` as the `postgres` superuser, which asks for that superuser's password once. On Windows it finds `psql.exe` in `C:\Program Files\PostgreSQL\<version>\bin` automatically. If `psql` isn't available, it prints the two SQL statements for you to paste into pgAdmin. To change the password later, edit `POSTGRES_PASSWORD` in `.env` and run `db-init` again.

**Redis 7:**

| OS | Command |
|---|---|
| macOS | `brew install redis && brew services start redis` |
| Ubuntu/Debian | `sudo apt install redis-server` |
| Windows | Redis has no native Windows build. Pick one: **(a)** Docker Desktop: `docker run -d --name fm-redis -p 6379:6379 redis:7-alpine`; **(b)** WSL: `sudo apt install redis-server && sudo service redis-server start`; **(c)** [Memurai](https://www.memurai.com/) (Redis-compatible). |

### B4. Create a Python environment and install everything (one command)
```bash
python -m venv .venv
# macOS/Linux:
source .venv/bin/activate
# Windows (PowerShell):
.venv\Scripts\Activate.ps1

pip install -r requirements.txt        # all backend services + migrations + workers
pip install -r requirements-dev.txt    # optional: tests, linter, smoke-test deps
```
On Windows, `uvloop` is skipped automatically because it doesn't support Windows; the standard asyncio loop is used instead. Each service also has its own `services/<name>/requirements.txt`, which the Docker images use.

### B5. Non-default host or port (only if needed)
No-Docker mode connects to PostgreSQL on `localhost:5432` and Redis on `localhost:6379`, using the same `POSTGRES_*` values. If yours differ, add to `.env`:
```ini
LOCAL_DB_HOST=localhost
LOCAL_DB_PORT=5433
LOCAL_REDIS_URL=redis://localhost:6379/0
```

### B6. Start all services and workers (one command, any OS)
```bash
python scripts/fm.py dev        # same as: python scripts/dev.py
```
This does the following:
1. Runs the database migrations (creates schemas, tables and audit triggers).
2. Starts the seven services on ports `8001`–`8007`: identity, portfolio, market, analytics, advisor, dashboard and readiness.
3. Starts the four background workers (market, portfolio, advisor, readiness).
4. Writes logs to `.dev/logs/`. Press **Ctrl+C** to stop everything.

Useful flags: `--no-workers`, `--no-migrate`, `--only advisor,market`.

Quick check in a second terminal:
```bash
curl http://localhost:8005/health/ready        # {"service":"advisor","checks":{"redis":"ok","postgres":"ok"}}
python scripts/fm.py smoke --direct            # full end-to-end test against the local services (needs requirements-dev.txt)
```

### B7. Start the web app (second terminal)
Needs Node.js 20+.
```bash
cd web
npm install
npm run dev          # open http://localhost:3000 — /api calls are proxied to the local services
```

### B8. Stop
Press **Ctrl+C** in the `scripts/dev.py` terminal and in the `npm run dev` terminal.

---

## First-time walkthrough in the app

1. **Register** at `/register`. This creates your household and a "Self" profile with three empty portfolios: Stocks & ETFs, Mutual Funds, and Retirement.
2. **Family:** add family members as **profiles** under *Family → Add profile* (spouse, parents, kids, HUF). Give each one their **PAN** — statements are then matched to the right person automatically (a PAN can belong to only one profile). Tax slab, risk profile and target allocation drive the advice. Edit any profile (including your own) with **Edit** on its card, or under *Settings → Profiles*.
3. **Add holdings.** Choose one of:
   - *Transactions → Add*: search a stock or MF and enter a buy, sell or SIP. It appears instantly (optimistic UI).
   - Every import is two steps: **Read file** shows whose statement it is (PAN on a CAS, Client ID on a Zerodha statement) and which profile it will go to; **Import** writes it. A statement is never put into a profile with a different PAN, an untagged profile gets tagged, and the Zerodha account is remembered.
   - *Import → holdings statement* (**recommended**): Zerodha Console → Portfolio → Holdings → Download (.xlsx, stocks + mutual funds), or the holdings download from ICICI Direct, Upstox, Groww, Paytm Money, SBI Securities, Kotak or any other broker (CSV or Excel) with quantity and average price. Choose **Replace** so it becomes your current portfolio.
   - *Import → transaction history*: any broker's trade book / tradebook (Zerodha, ICICI Direct, Upstox, SBI, …), a CAMS/KFintech **Detailed** CAS PDF (password = PAN in capitals) for mutual funds, or an NPS CRA transaction statement. Only accurate if the history is complete.
   - **A layout it doesn't recognise?** The import screen shows the file's columns with sample rows and a best guess; confirm which column is which **once** and every later file with that layout imports automatically (*Remembered layouts* lists them).
   - **Tested brokers:** Zerodha is fully tested; other brokers' files should work but haven't been tested with real exports yet. If one fails, [open an issue](https://github.com/samhitrix/Aquilvyn/issues) — remove names, PAN, account numbers and amounts from any sample you attach.
   - *Import → upload your NSDL/CDSL eCAS PDF* (the monthly depository statement; it is recognised automatically): not imported — it has no buy prices — but compared with what Aquilvyn holds, account by account: **ok / differs / missing / extra**. Use it to spot a missed trade or a broker you haven't imported yet.
   - *Tax → import tax P&L*: your broker's capital-gains / tax P&L statement for the realised side.
   - *Import → EPFO passbook PDF* (passbook.epfindia.gov.in → Download Passbook, one PDF per financial year): contributions, withdrawals and interest go onto that member ID's EPF account. A passbook only shows the current employer, so the import asks for your **total EPF balance** (optional): enter it and today's EPF value matches it.
   - **Zerodha holdings + CAMS CAS together?** Fine in any order: a mutual fund in both counts once — the CAS wins (it has demat and non-demat units with real purchase dates), matched by ISIN. Funds imported twice before this rule are corrected in the background.
   - *Retirement*: add an EPF/PPF account with its interest rate and log contributions.
4. **Dashboard:** family net worth, today's change, XIRR, allocation, per-profile cards, top actions and live prices.
5. **Advisor → Run analysis** (runs for the filter you picked; the button on a single holding analyses only that one). Each holding gets a verdict — Buy more / Hold / Sell some / Sell / Switch — with a confidence level and one line saying exactly what to do (for trades, the stop-loss price). "Hold · data missing" means price history or fundamentals weren't available; the Readiness engine fetches them and re-analyses by itself within ~15 minutes. Click a card to open the **full report**: evidence, the rules tried, scores, drawdown attribution, tax lots, risks, and the AI review panel.
6. **Accept / Snooze / Dismiss.** *Accept* records the decision and a **virtual** trade in the Shadow Portfolio. **No real order is ever placed in this version** (broker actions are Phase 3, switched off).
7. **Markets:** indices, VIX, sectors, gainers and losers, and per-stock technical and fundamental analysis.
8. **Analytics:** asset classes (and what's inside debt, equity and gold), stocks vs funds, sectors and company size; with **look-through**, your true stock exposure through your funds, how much your funds overlap, and each person's fund plan (keep one fund per role, hold the rest or switch laggards — see the README).
9. **Tax:** this financial year's capital gains per person, loss set-off, LTCG exemption used, and harvesting suggestions you can mark as done.
10. **Settings:** your name and the family name, AI providers and their order, AI on/off, and **Test all** for every AI model and data source.

---

## MF look-through (what's inside your funds)

**Analytics → True stock exposure** shows each stock you own directly *plus* your share of it inside your mutual funds and ETFs, and **Fund overlap** shows which funds hold the same stocks. The holdings are fetched automatically once a day (only when a newer month is due); nothing to upload. Sources, in order:
1. **Groww's fund pages** — every scheme in one format (the monthly disclosures, collected by Groww). Not an official API, so if it changes or is unreachable the next source takes over.
2. **The fund house's own monthly portfolio file** — pages listed in `services/market/market_svc/fund_sources.json`, opened in a headless browser (built into the Docker market image) when the site builds its download list with JavaScript.
3. **A free public holdings API** (`holdings_api` in `fund_sources.json`; `""` switches it off).

Liquid, debt, gilt, gold and silver funds/ETFs are skipped — they hold no stocks.

Check that it works on your machine (needs internet):
```powershell
python scripts/fm.py funds-check              # fetch now for every fund you hold, fund house by fund house
python scripts/fm.py funds-check --amc HDFC   # just one fund house
python scripts/fm.py funds-check --file "C:\Downloads\Monthly Portfolio July 2026.xlsx"   # read a file you downloaded yourself
```
Each fund house shows ✔ (with the month and how many of your funds were found) or ✘ with every step it tried. If a fund house's website moved, update its page under `services/market/market_svc/fund_sources.json` — or report the ✘ lines in an issue. The same status is in **Settings → Data sources** ("Fund holdings — <fund house>").

## Troubleshooting

| Symptom | Fix |
|---|---|
| After an update: login works but profiles, transactions and imports are empty | The data is still there — the gateway is pointing at the old containers. Run `docker compose restart gateway` (newer `fm.py up` does this automatically). If still empty, run `python scripts/fm.py diag` and check the migration log. |
| Docker keeps using more disk (10–20+ GB) | Older versions rebuilt ~6 GB of libraries on every update and kept the old copies. Now all services share one ~1 GB library layer, updates add a few MB, and logs are capped at 30 MB per container. To reclaim what was used before: `python scripts/fm.py prune --all`. On Windows the space is freed inside Docker's virtual disk; `prune` prints the steps to compact it back to C:. |
| `error while creating mount source path '/run/desktop/mnt/host/…/nginx.conf': mkdir … file exists` | Docker Desktop couldn't share your drive (common with mapped/`subst` drives, or after sleep). Fixed: configs are now built into the images — no files are mounted from your drive. `git pull`, then `python scripts/fm.py up-lite`. If it ever reappears for another reason: quit Docker Desktop, run `wsl --shutdown`, start Docker Desktop again. |
| `password authentication failed` | The database password doesn't match `.env`. No Docker: run `python scripts/fm.py db-init`. Docker: the Postgres volume keeps the password from its first start, so either put the old password back in `.env` or run `python scripts/fm.py clean` (this deletes data). |
| Forgot your password | Still signed in (e.g. with Gmail)? **Settings → Password & recovery → Set password** — no old password needed when the account has none. Signed out: **Forgot password?** on the sign-in page: your email + your **recovery code** (shown once when you signed up — accounts made before recovery codes get one shown at their next sign-in; create or renew it in **Settings → Password & recovery**) + a new password. You then get a fresh code — each works once. Lost the code too? A family member asks the household owner (or an admin): **Settings → Household members → Password reset link** makes a one-time link (30 minutes) to hand over. The owner: on the computer that runs Aquilvyn, `python scripts/fm.py reset-password you@example.com`, then open the link it prints. |
| Pages are slow to open, or the site doesn't open at all | Run `python scripts/fm.py perf` — **no login needed**: it shows each container's state, restarts, CPU and memory, the recent error types in each log, how fast the gateway, the web app and every service answer, and **what each service is busy with**: its CPU over the last minute, the code line that kept its event loop stuck (and for how long), and its slowest routes. Add `--as you@example.com` for every page's timings too (no password needed). Output has no personal data — safe to paste into an issue. Services also log requests slower than 1.5 s as `http.slow_request`. |
| Prices are "simulated" / values shown "at cost" | Set `MARKET_DATA_PROVIDER=yahoo` in `.env` and run `python scripts/fm.py up-lite`. *Settings → Data sources* shows each price source's status and last error. |
| Recommendations show "Rules-only: not AI-checked" | No AI configured. Add a key in `.env` or under Settings → AI providers. |
| AI review "paused" / "failed" on cards | That provider hit its quota or rate limit and is skipped for 24 h; the next one in your order takes over. *Settings → Test all* shows the exact (key-masked) error. |
| "Hold · data missing" or "No fundamentals available" stays | The Readiness engine retries with backoff (10 min → 12 h). *Advisor → Health check* shows which check fails and why; *Settings → Test all* shows which fundamentals sources answer. Free sources are sometimes blocked for hours. |
| Docker Desktop still lists `foliosense/*` or `foliomatrix/*` images | Leftovers from before the rename — nothing uses them. `python scripts/fm.py prune` (and every `up` / `up-lite`) removes them. Only images are removed; your data volumes are kept. |
| After updating, the app is empty (Docker) | The project was renamed (FolioMatrix → FolioSense → Aquilvyn). `fm.py up` / `up-lite` finds the old data volumes and adds `COMPOSE_PROJECT_NAME=<old name>` to `.env` by itself; if you ran `docker compose` directly, add `COMPOSE_PROJECT_NAME=foliomatrix` (or `foliosense`, whichever `docker volume ls` shows) to `.env` and start again. Nothing is deleted. |
| Import says the layout is unknown | Map the columns once on the import screen; it's remembered for next time. |
| `429 Too Many Requests` | The rate limiter is working. Tune `RATE_LIMIT_*` in `.env` for local testing. |
| Port 8080 or 5432 already in use | Stop the other program, or change the published port in `docker-compose.yml`. |
| Out of memory | Use `python scripts/fm.py up-lite`. |
| `make : The term 'make' is not recognized` (Windows) | Windows has no `make`. Use `python scripts/fm.py <target>` instead, e.g. `python scripts/fm.py up-lite`. |
| `docker: command not found` / engine not running | Install and **start Docker Desktop**, then run `python scripts/fm.py doctor`. |
| `doctor` shows `CRLF` for `ping.sh` / `nginx.conf` | Only matters for Docker. Run `python scripts/fm.py fixeol`; it re-checks out just those files with LF. |
| `ports are not available: exposing port TCP 127.0.0.1:5432 … forbidden by its access permissions` | Fixed: Docker's database no longer uses 5432, and `up` now checks every port and moves to a free one. Run `git pull` and `python scripts/fm.py up-lite` again. To see which ports Windows reserves: `netsh interface ipv4 show excludedportrange protocol=tcp` |
| `failed to connect to the docker API at npipe:////./pipe/docker_engine` | Docker Desktop is installed but not running. `python scripts/fm.py up-lite` now starts it and waits. Or open Docker Desktop yourself, wait for "Engine running", and retry. The first start after installing can take 1–2 minutes and may ask you to accept terms. |
| `docker CLI not found` (Windows) | Install Docker (section A2), or skip Docker and use Option B: `python scripts/fm.py dev`. |

## Where things live
- Configuration: `.env` (template: `.env.example`)
- Advisor rules (editable, hot-reloaded): `services/advisor/advisor_svc/rulebook.yaml` — add a golden case in `tests/unit/test_rules.py` for every change
- Readiness checks and their thresholds: `services/readiness/readiness_svc/checks.py`
- Broker file readers: `services/portfolio/portfolio_svc/importers/`
- Before pushing a change: `python scripts/fm.py lint`, `python scripts/fm.py test`, and for web changes `cd web && npx tsc --noEmit && npm run build`
- Database migrations: `migrations/versions/`
- Gateway routes: `infra/nginx/nginx.conf`
