"""`nseql angel-verify`: check the Angel One adapter against the REAL service, with the operator's own
credentials from .env, and write experiments/angel_verification.json.

The dashboard only labels live data "verified" when that report says passed=true. Every check below must
pass; checks that need market hours are "skipped" outside them, which leaves the report NOT passed.

Checks
  credentials        the four ANGEL_* variables are set (names only)
  login              TOTP login succeeds; the cached session file is owner-only (0600 on POSIX)
  session_recovery   a new client reuses the cached session; a corrupted token is refused by the server and
                     recovered by one automatic re-login
  instruments        the scrip master downloads, has the expected columns, resolves the watchlist
  candle_format      1-minute candles are [timestamp+05:30, o, h, l, c, v] rows, minute-aligned, sessions start
                     09:15 (stamped 09:16 here, at the bar's end), prices positive, high >= low
  completed_minute   (market hours) a bar for the minute still in progress is never passed on, and the last
                     completed bar is re-fetched 20 s later to see whether the server revises it
  rate_limit         paced requests (the adapter's 0.4 s spacing) are not refused; a short unpaced burst is
                     measured to record where AB1021 starts (at most `burst` requests)
  network_failure    an unreachable host fails fast with a clear error, and the minute poller records the error
                     instead of crashing
  stale_detection    the poller reports symbols with no bar for more than 3 minutes during market hours
  no_secrets         the report contains none of the API key, MPIN, TOTP secret or session tokens

Secrets are never printed or written: the report masks the client code and is scanned before it is saved.
"""
from __future__ import annotations

import json
import os
import stat
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
from nseql.data.adapters import angelone as A
from nseql.screening.provenance import VERIFICATION_FILE

IST = "Asia/Kolkata"


def _market_open(now: pd.Timestamp) -> bool:
    m = now.hour * 60 + now.minute
    return now.weekday() < 5 and 9 * 60 + 20 <= m <= 15 * 60 + 25


def mask(code: str | None) -> str:
    return (code[:2] + "*" * max(len(code) - 4, 1) + code[-2:]) if code and len(code) > 4 else "****"


def verify(experiments_dir: Path, *, now: datetime | None = None, symbol: str | None = None, burst: int = 8,
           make_api=None, sleep=time.sleep, revision_wait_s: float = 20.0, progress=print) -> dict:
    """Run every check; returns the report (also written to experiments_dir/angel_verification.json)."""
    exp = Path(experiments_dir)
    now_ts = pd.Timestamp(now) if now is not None else pd.Timestamp.now(tz=IST).tz_localize(None)
    make_api = make_api or (lambda **kw: A.AngelHistorical(session_dir=exp, **kw))
    checks: list[dict] = []
    secrets: list[str] = [v for k in A.ENV if k != "ANGEL_CLIENT_CODE" for v in [os.environ.get(k)] if v]

    def add(name, status, detail, **extra):
        checks.append({"name": name, "status": status, "detail": detail, **extra})
        progress(f"[{status:7}] {name}: {detail}")

    miss = A.missing_credentials()
    add("credentials", "failed" if miss else "passed", f"missing: {', '.join(miss)}" if miss else "all four ANGEL_* variables are set")
    api = None
    if not miss:
        try:
            api = make_api()
            api.login()
            secrets += [x for x in (api.jwt, api.feed_token, api.api_key, api.pin, api.totp_secret) if x]
            perm = ""
            if api.session_file and api.session_file.exists() and os.name == "posix":
                mode = stat.S_IMODE(api.session_file.stat().st_mode)
                perm = f"; session file mode {oct(mode)}"
                if mode & 0o077:
                    raise AssertionError(f"session file is readable by others ({oct(mode)})")
            add("login", "passed", f"TOTP login OK for client {mask(api.client_code)}{perm}")
        except Exception as e:  # noqa: BLE001 - every failure becomes a reported check
            add("login", "failed", f"{type(e).__name__}: {e}")
            api = None

    found: list[dict] = []
    if api:
        try:
            master = api.instruments(exp)
            need = {"token", "symbol", "exch_seg", "instrumenttype", "tick_size"}
            lacking = need - set(master.columns)
            if lacking:
                raise AssertionError(f"scrip master lacks columns {sorted(lacking)}")
            watch = [symbol] if symbol else A.watchlist(exp)
            found, missing = A.resolve(master, watch)
            if not found:
                raise AssertionError(f"none of {watch[:5]}... resolved")
            add("instruments", "passed", f"{len(master):,} instruments; watchlist {len(found)} resolved"
                + (f", not found: {', '.join(missing)}" if missing else ""), unresolved=missing)
        except Exception as e:  # noqa: BLE001
            add("instruments", "failed", f"{type(e).__name__}: {e}")

    tok = found[0] if found else None
    if api and tok:
        try:
            df = api.candles(tok["token"], "1m", (now_ts - timedelta(days=4)).normalize().to_pydatetime(), now_ts.to_pydatetime())
            raw = api.last_raw
            if not raw:
                raise AssertionError("no 1-minute candles returned for the last 4 days")
            bad = [r for r in raw[:200] if not (isinstance(r, list) and len(r) >= 6 and "+05:30" in str(r[0]))]
            if bad:
                raise AssertionError(f"unexpected row shape, e.g. {str(bad[0])[:80]}")
            secs = df["ts"].dt.second.ne(0).sum()
            firsts = df.groupby(df["ts"].dt.date)["ts"].min().dt.strftime("%H:%M").unique().tolist()
            if secs or (df[["open", "high", "low", "close"]] <= 0).any().any() or (df["high"] < df["low"]).any():
                raise AssertionError("bars not minute-aligned, non-positive prices, or high < low")
            add("candle_format", "passed" if firsts == ["09:16"] else "failed",
                f"{len(df):,} bars for {tok['symbol']}; first bar of each session stamped {firsts} (expected 09:16 = 09:15 start + 1 min)")
        except Exception as e:  # noqa: BLE001
            add("candle_format", "failed", f"{type(e).__name__}: {e}")

        try:
            fresh = make_api()
            reused = bool(fresh.jwt)
            fresh.jwt = "corrupted-token"
            fresh.candles(tok["token"], "1d", (now_ts - timedelta(days=7)).to_pydatetime(), now_ts.to_pydatetime())
            ok = reused and fresh.jwt != "corrupted-token"
            secrets += [x for x in (fresh.jwt, fresh.feed_token) if x]
            add("session_recovery", "passed" if ok else "failed",
                f"cached session reused: {reused}; corrupted token recovered by automatic re-login: {fresh.jwt != 'corrupted-token'}")
        except Exception as e:  # noqa: BLE001
            add("session_recovery", "failed", f"{type(e).__name__}: {e}")

        if _market_open(now_ts):
            try:
                from nseql.screening.feeds import BrokerMinutePoller
                end = now_ts
                poller = BrokerMinutePoller([tok], lambda: api, lookback_minutes=6)
                bars = poller.fetch(end)                                      # the same code path the live loop uses
                forming = [str(r[0]) for r in api.last_raw if pd.Timestamp(r[0]).tz_convert(IST).tz_localize(None) > end - timedelta(minutes=1)]
                passed_on = [b for b in bars if b["ts"] > end]
                df = pd.DataFrame(bars)
                last = df.tail(1) if len(df) else df
                sleep(revision_wait_s)
                again = api.candles(tok["token"], "1m", (end - timedelta(minutes=6)).to_pydatetime(), end.to_pydatetime())
                revised = bool(len(last)) and not again[again["ts"] == last["ts"].iloc[0]][["open", "high", "low", "close", "volume"]].reset_index(drop=True) \
                    .equals(last[["open", "high", "low", "close", "volume"]].reset_index(drop=True))
                add("completed_minute", "failed" if len(passed_on) else "passed",
                    f"server returned the in-progress minute: {bool(forming)} (dropped by end-stamping: {not len(passed_on)}); "
                    f"last completed bar revised after {revision_wait_s:.0f} s: {revised}", in_progress_returned=bool(forming), revised=revised)
            except Exception as e:  # noqa: BLE001
                add("completed_minute", "failed", f"{type(e).__name__}: {e}")
        else:
            add("completed_minute", "skipped", "market closed: rerun on a trading day between 09:20 and 15:25 IST")

        try:
            paced = make_api()
            t0, errs = time.perf_counter(), 0
            for k in range(6):
                try:
                    paced.candles(tok["token"], "1d", (now_ts - timedelta(days=5 + k)).to_pydatetime(), now_ts.to_pydatetime())
                except A.AngelError:
                    errs += 1
            el = time.perf_counter() - t0
            burst_api = make_api(min_interval_s=0.0, max_retries=0)
            refused_at = None
            for k in range(burst):
                try:
                    burst_api.candles(tok["token"], "1d", (now_ts - timedelta(days=5 + k)).to_pydatetime(), now_ts.to_pydatetime())
                except A.AngelError:
                    refused_at = k + 1
                    break
            add("rate_limit", "passed" if errs == 0 else "failed",
                f"6 paced requests: {errs} refused, {6 / el:.2f} req/s; unpaced burst first refused at request "
                f"{refused_at if refused_at else f'none of {burst}'}", paced_req_per_s=round(6 / el, 2), burst_refused_at=refused_at)
        except Exception as e:  # noqa: BLE001
            add("rate_limit", "failed", f"{type(e).__name__}: {e}")
    else:
        for n in ("candle_format", "session_recovery", "completed_minute", "rate_limit"):
            add(n, "skipped", "needs a successful login and a resolved symbol")

    add_net = _network_check()
    add(*add_net)
    from nseql.screening.feeds import BrokerMinutePoller
    p = BrokerMinutePoller([{"symbol": "X", "token": "1"}], lambda: (_ for _ in ()).throw(A.AngelError("simulated outage")))
    p.fetch(now_ts)
    st = p.health(now_ts, market_open=True)
    add("stale_detection", "passed" if st["stale_symbols"] == ["X"] and p.status.startswith("error") else "failed",
        f"poller after an outage: status '{p.status}', stale symbols {st['stale_symbols']}")

    report = {"passed": False, "at": datetime.now().isoformat(timespec="seconds"), "client": mask(os.environ.get("ANGEL_CLIENT_CODE")),
              "adapter_base_url": A.BASE_URL, "checks": checks}
    text = json.dumps(report, default=str)
    leaked = [s for s in secrets if s and len(s) >= 4 and s in text]
    checks.append({"name": "no_secrets", "status": "failed" if leaked else "passed",
                   "detail": f"{len(leaked)} secret value(s) found in the report; not saved" if leaked else "no secret values in the report"})
    report["passed"] = all(c["status"] == "passed" for c in checks)
    failed = [c["name"] for c in checks if c["status"] != "passed"]
    report["detail"] = "all checks passed against the real service" if report["passed"] else f"not passed: {', '.join(failed)}"
    if not leaked:
        exp.mkdir(parents=True, exist_ok=True)
        (exp / VERIFICATION_FILE).write_text(json.dumps(report, indent=2, default=str))
    progress(report["detail"])
    return report


def _network_check() -> tuple[str, str, str]:
    """An unreachable host must fail fast with a clear error (no hang, no crash). Purely local."""
    import httpx
    t0 = time.perf_counter()
    try:
        with httpx.Client(base_url="http://127.0.0.1:9", timeout=5.0) as c:
            c.post("/x", json={})
        return "network_failure", "failed", "request to a closed local port unexpectedly succeeded"
    except httpx.HTTPError as e:
        el = time.perf_counter() - t0
        return "network_failure", "passed" if el < 6 else "failed", f"{type(e).__name__} after {el:.2f} s"
