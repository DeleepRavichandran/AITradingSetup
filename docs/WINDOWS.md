# Running on a Windows laptop

Status: the scripts below are written for Windows 10/11 but have **not been run on a Windows machine
yet**, and they were not syntax-checked with PowerShell. The Python code has Windows-specific guards
(time-zone data, POSIX-only calls) that are tested on Linux only. Report anything that fails.

## Zero-touch mode with Angel One (recommended)

Only a one-time setup is needed. After it, nothing has to be done by hand.

1. In the Angel One app or website, enable **SmartAPI** and create an app. Note the **API key**.
2. On SmartAPI, enable TOTP two-factor login. Copy the **text shown under the QR code** (the TOTP secret).
   You will also need your **client ID** and **4-digit MPIN**.
3. Run `scripts\windows\setup.ps1` once. It asks for those four values (typing is hidden), saves them only
   in `.env` on the laptop, tests the login, and installs auto-start at Windows logon.

From then on, every time you log in to Windows the lab starts in the background. It then:

- logs in to Angel One by itself every day, generating the TOTP code from the secret;
- downloads 2 years of daily history for the watchlist, the first time only;
- warms up its indicators on the last few days of minute data;
- screens every minute during market hours;
- paper-trades in the shared portfolio, from the moment it first went live.

Nothing is ever sent as a real order. Check it any time at http://localhost:3000 → *Strategy Monitor*.
The watchlist is `config/watchlist_default.txt`; put your own list in `experiments/watchlist.txt` to replace it.

Caveats:

- **Untested on the real service.** The Angel One code has been tested only against a simulated server.
  Expect a first-run fix or two; the dashboard's status card and `experiments\logs\api.log` show the error.
- **The laptop must be on.** Screening only happens while it is awake during market hours.
- **Protect the secret.** The TOTP secret plus your MPIN can log in to your account. They stay in `.env`
  on the laptop, which is never committed to git.

There are two ways to run it. Pick one.

| | Native (Python + Node) | Docker Desktop |
|---|---|---|
| Install | Python 3.12+, Node.js LTS | Docker Desktop (uses WSL2) |
| Backtests, screener, Learning Brain, paper trading, dashboard | yes | yes |
| Running AI-generated strategy code | **no** — the sandbox needs Linux and refuses to run it (fails closed) | yes |
| Disk / memory | about 1 GB | about 3 GB, Docker uses ~2–4 GB RAM |
| Dashboard address | http://localhost:3000 | http://localhost:8080 |

Live trading stays off in both. The scripts force `LIVE_TRADING_ENABLED=false` and never load
`HUMAN_APPROVAL_SECRET` into the app.

## Native setup

1. Install Python and Node.js (PowerShell):
   ```powershell
   winget install Python.Python.3.13
   winget install OpenJS.NodeJS.LTS
   ```
   Close and reopen PowerShell afterwards so they are on the PATH.
2. In the repository folder:
   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\windows\setup.ps1
   ```
   This creates `.env` with a random dashboard password (`NSEQL_API_TOKEN`), turns on the paper-trading
   scheduler, creates `.venv`, installs the Python packages with `python -m pip` (works where `pip.exe`
   is blocked by policy), and builds the dashboard.
3. Start it:
   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\windows\start.ps1
   ```
   Two windows open (API and dashboard) and the browser opens http://localhost:3000. Log in with the
   `NSEQL_API_TOKEN` value from `.env`. Close the two windows to stop.

Run any command with the `.env` settings through `scripts\windows\nseql.ps1`, for example:
```powershell
powershell -ExecutionPolicy Bypass -File scripts\windows\nseql.ps1 kite-login
```

## Docker Desktop setup

Install Docker Desktop, start it, then:
```powershell
powershell -ExecutionPolicy Bypass -File scripts\windows\setup.ps1 -Docker
```
Commands then run inside the container:
```powershell
docker compose --env-file .env -f infra/docker-compose.yml exec api nseql kite-login
```

## Daily routine on a laptop

1. Morning: `nseql kite-login` (Kite tokens expire daily, around 06:00 IST).
2. Keep the laptop awake after 15:45 IST if you want that day processed the same evening. If it was
   off or asleep, the missed days are processed in order after the next login — nothing is lost.
3. Load the NSE holiday list once a year, so holidays are not mistaken for missing data:
   `nseql holidays nse_holidays_2027.csv --source "NSE circular <number> dated <date>"`.

Windows sleep settings: *Settings → System → Power* — set "When plugged in, put my device to sleep
after" to *Never* on trading days, or accept that the day is processed when the laptop next wakes.

## Seeing the dashboard from your phone

The dashboard listens on this laptop only. To reach it from your phone, the laptop must be on and you
need a private tunnel such as Tailscale (install on both, then open `http://<laptop-name>:3000`).
Do not forward router ports to it.

## First real data (after you have a Kite Connect subscription)

```powershell
$n = "scripts\windows\nseql.ps1"
powershell -ExecutionPolicy Bypass -File $n import-kite kite_2026 --symbols RELIANCE,TCS,INFY,HDFCBANK,ICICIBANK --start 2024-01-01 --licence "Kite Connect, personal research"
powershell -ExecutionPolicy Bypass -File $n corporate-actions kite_2026          # look for unlisted splits/bonuses
powershell -ExecutionPolicy Bypass -File $n import-context --start 2023-01-01 --licence "Kite Connect, personal research"
powershell -ExecutionPolicy Bypass -File $n context-test --dataset kite_2026     # does VIX / rupee / crude context help?
```
