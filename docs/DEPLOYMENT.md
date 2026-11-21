# Deployment — reaching the dashboard from anywhere

The dashboard is a web app. To open it from your phone or another computer it has to run on a
machine that is always on and reachable: a small cloud VM is the usual choice. If you later trade
live, that VM's static IP is also what you register with the broker.

> Verified so far: both images build and the API container starts, enforces the token and runs the
> strategy sandbox. Not yet run: the web container and the full compose stack with Caddy/TLS.

## 1. One-time setup on the server

```bash
git clone <your repo url> nse-ai-quant-lab && cd nse-ai-quant-lab
cp .env.example .env
```

Edit `.env`:

```
NSEQL_API_TOKEN=<long random string — this is your dashboard password>
NSEQL_DOMAIN=quant.example.com        # a DNS name you control, pointing at this server
LIVE_TRADING_ENABLED=false
# optional: ANTHROPIC_API_KEY, KITE_*, WHATSAPP_*
```

Generate a token with:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Open ports 80 and 443 on the server's firewall. Nothing else needs to be public.

## 2. Start

```bash
docker compose --env-file .env -f infra/docker-compose.yml --profile public up -d --build
```

Caddy obtains and renews a TLS certificate for `NSEQL_DOMAIN`. Open `https://<your domain>`,
enter the token, done. Without `--profile public` the UI is served only on
`http://localhost:8080` of that machine.

The API will not start in this stack without `NSEQL_API_TOKEN`. `HUMAN_APPROVAL_SECRET` is not
passed to any container.

## 3. Data and backups

Everything persistent lives in the `nseql-data` volume (`/data/datasets`, `/data/experiments`).
On first start the synthetic fixture is generated. Import your own data inside the API container:

```bash
docker compose --env-file .env -f infra/docker-compose.yml exec api nseql import-bhavcopy nse_eod_2024 /data/incoming/*.csv --licence "<your entitlement>"
```

Back up the volume; the registry is the lab's memory.

## 3b. Kite Connect: login and 2026 data

Needs a Kite Connect plan that includes historical data. In the Kite developer console create an
app; set `KITE_API_KEY` and `KITE_API_SECRET` in `.env` (never in git). Then, each trading day:

```bash
docker compose --env-file .env -f infra/docker-compose.yml exec api nseql kite-login          # prints the login URL
# log in, copy request_token=... from the redirect address, then:
docker compose --env-file .env -f infra/docker-compose.yml exec api nseql kite-login --request-token <value>
```

Import candles (Kite lists current instruments only; supply your own symbol list, e.g. one symbol per line):

```bash
docker compose --env-file .env -f infra/docker-compose.yml exec api nseql import-kite kite_2026 --symbols-file /data/nifty50.txt \
    --start 2026-01-01 --licence "Kite Connect subscription, personal research use"
docker compose --env-file .env -f infra/docker-compose.yml exec api nseql quality kite_2026
```

Check the quality report for unexplained price jumps (splits, bonuses) before running anything.
The token expires every morning; a stale token shows up as a clear error, not as bad data.

## 3c. An unattended paper-trading week

1. Do the Kite login for the day (section 3b). It has to be repeated every trading day: the token
   expires each morning and the login cannot be automated.
2. On the Paper Trading page choose feed **Kite daily**, enter your symbols and a library strategy,
   and start the session. It loads about 14 months of history as warm-up (no trading), then waits.
3. The scheduler (on in the compose stack) checks every 15 minutes. After 15:45 IST it pulls the
   day's completed candles, steps the session, and sends one summary notification. Orders created on
   day D are simulated against day D+1's candle.
4. If you miss a login, nothing breaks: you get one reminder per day and the session catches up, in
   order, the next time a login is in place.
5. Sessions and the kill switch are saved to disk; a restart or reboot resumes them exactly.

Optional: put NSE's holiday list in `experiments/holidays.json` as
`{"source": "...", "holidays": ["2026-01-26", ...]}`. Without it only weekends are treated as closed.

What this is not: it is end-of-day only (no intraday strategies, no tick data), it simulates fills
from daily candles, and it has been tested against a simulated Kite, not the real one.

## 4. WhatsApp alerts

Uses the WhatsApp Business Cloud API (Meta). You need a Meta developer app with the WhatsApp
product, a system-user access token, the sending number's phone-number ID, and — for alerts sent
when you have not messaged the number in the last 24 hours — an approved message template with
one body variable (for example `Quant Lab alert: {{1}}`). Put them in `.env`:

```
WHATSAPP_ACCESS_TOKEN=…
WHATSAPP_PHONE_NUMBER_ID=…
WHATSAPP_TO=9198XXXXXXXX
WHATSAPP_TEMPLATE=quant_alert
WHATSAPP_API_VERSION=<the Graph API version your app uses>
```

Restart, then press **Send test** on the System Monitoring page. Alerts are sent when a paper
session halts, the kill switch is engaged, a research run finishes, and a strategy becomes a paper
candidate. This integration is mock-tested only until you run that test.

## 5. Hardening before you rely on it

- Keep the token long and private; rotate it by changing `.env` and restarting.
- Restrict SSH; keep the host patched; consider putting the site behind a VPN or an identity-aware
  proxy as well — a single shared token is a minimum, not a multi-user access system.
- The API container runs as uid 10001 with every capability dropped and `no-new-privileges`. It never
  executes AI-generated strategy code: that goes to the separate `sandbox` service, the only container
  with `SYS_ADMIN` (plus the few capabilities needed to drop privileges) and AppArmor unconfined, so it
  can build the namespaces each job runs in. The sandbox service holds no broker, LLM or notification
  credentials, sees the datasets read-only, sits on an internal network with no internet route and
  accepts requests only with `NSEQL_SANDBOX_TOKEN` (24+ characters; set it in `.env`). If the worker is
  down or misconfigured, research and the code editor stop (fail closed); everything else continues.
  The System and Strategy Monitor pages show the sandbox state. Volumes from older versions are
  re-owned to uid 10001 by the one-shot `init-permissions` service.
- `NSEQL_VIEWER_TOKEN` (optional) gives read-only access; use it on devices that only need to look.
