# Deployment — reaching the dashboard from anywhere

The dashboard is a web app. To open it from your phone or another computer it has to run on a
machine that is always on and reachable: a small cloud VM is the usual choice. If you later trade
live, that VM's static IP is also what you register with the broker.

> Not verified in the build environment: the Docker images below were written but not built or
> run there. Expect to fix small things on first deploy.

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
docker compose -f infra/docker-compose.yml --profile public up -d --build
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
docker compose -f infra/docker-compose.yml exec api nseql import-bhavcopy nse_eod_2024 /data/incoming/*.csv --licence "<your entitlement>"
```

Back up the volume; the registry is the lab's memory.

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
- The API container has `SYS_ADMIN` so the strategy sandbox can create a network namespace. If you
  remove it, the sandbox still runs with a scrubbed environment and resource limits but without
  network isolation; the System page shows which mode is active.
