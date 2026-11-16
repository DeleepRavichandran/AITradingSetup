"""Paper trading, safety halts, the live gate, order router, broker adapters and notifications."""
import json
from datetime import datetime, timedelta

import httpx
import pandas as pd
import pytest

from nseql.backtester.engine import Allocation
from nseql.backtester.runner import BacktestConfig, StrategyRef, run_backtest
from nseql.config import load_settings
from nseql.data.store import Dataset
from nseql.execution.brokers.base import OrderRequest
from nseql.execution.brokers.stubs import AngelOneSmartAPI, Upstox
from nseql.execution.brokers.zerodha import ZerodhaKite
from nseql.execution.gate import COMPLIANCE_ITEMS, LiveGate, LiveTradingDisabled, sign_approval
from nseql.execution.router import OrderRouter, RouterLimits
from nseql.instruments.calendar import TradingCalendar
from nseql.notify.whatsapp import WhatsAppNotifier
from nseql.paper.feed import KiteTickerFeed, ReplayFeed
from nseql.paper.session import ManualBook, PaperSession
from nseql.risk.limits import KillSwitch
from nseql.strategies import registry

EQ = ["SYNBANK", "SYNIT", "SYNAUTO", "SYNPHARMA", "SYNFMCG", "SYNENERGY", "SYNMETAL", "SYNINFRA", "SYNTELCO", "SYNCEMENT",
      "SYNRETAIL", "SYNMEDIA", "SYNNIFTY"]
SECRET = "a-long-human-held-secret"
SHA = "ab" * 32


def session(data_dir, strat="sma_cross", params=None, **kw) -> PaperSession:
    feed = ReplayFeed(Dataset(data_dir / "synthetic_v1"), EQ, "1d", end="2022-12-30")
    allocs = [Allocation(registry.create(strat, {"fast": 5, "slow": 20} if params is None else params), "algo", 1_000_000), Allocation(ManualBook(), "manual", 500_000)]
    return PaperSession(feed, allocs, **kw)


# ------------------------------------------------------------------ paper trading
def test_paper_session_matches_the_backtest_exactly(data_dir):
    """Same bars, same strategy => the paper path must reproduce the backtest fill for fill."""
    feed = ReplayFeed(Dataset(data_dir / "synthetic_v1"), EQ, "1d", end="2022-12-30")
    ps = PaperSession(feed, [Allocation(registry.create("sma_cross", {"fast": 5, "slow": 20}), "sma_cross#1", 1_000_000)])
    while ps.advance(50):
        pass
    bt = run_backtest(BacktestConfig(strategies=[StrategyRef(name="sma_cross", params={"fast": 5, "slow": 20})], symbols=EQ,
                                     end="2022-12-30", allow_rule_extrapolation=True), data_dir)
    key = lambda fs: [(str(f.ts), f.symbol, f.side, f.qty, f.price, f.charges.get("total", 0)) for f in fs]
    assert len(ps.engine.account.fills) > 20
    assert key(ps.engine.account.fills) == key(bt.engine.account.fills)
    assert ps.engine.account.equity() == int(bt.engine.equity[-1])


def test_paper_orders_fill_and_positions_reconcile(data_dir):
    ps = session(data_dir)
    ps.advance(30)
    assert ps.submit_manual({"symbol": "SYNBANK", "qty": 10})["status"] == "QUEUED"
    ps.advance(1)
    assert not [f for f in ps.engine.account.fills if f.book == "manual"]          # submitted this bar, not filled yet
    ps.advance(1)
    mf = [f for f in ps.engine.account.fills if f.book == "manual"]
    assert len(mf) == 1 and mf[0].qty == 10
    ps.advance(60)
    st = ps.state()
    assert st["reconciliation"]["ok"] and st["reconciliation"]["position_breaks"] == []
    assert st["feed"] == "replay" and "not a live market" in st["feed_note"] and st["synthetic"] is True
    assert any(p["book"] == "manual" and p["symbol"] == "SYNBANK" and p["qty"] == 10 for p in st["positions"])


def test_stale_feed_halts_trading_and_cancels_orders(data_dir):
    sent = []
    ps = session(data_dir, notify=lambda e, t: sent.append((e, t)), stale_after_seconds=60)
    ps.advance(30)
    ps.manual.queue.append({"symbol": "SYNIT", "qty": 5, "type": "LIMIT", "limit": ps.portal.last("SYNIT") * 0.85, "tif": "GTC"})
    ps.advance(1)
    assert ps.state()["open_orders"]
    assert ps.check_feed(ps.last_bar_wallclock + timedelta(seconds=30))
    assert not ps.check_feed(ps.last_bar_wallclock + timedelta(seconds=300))
    st = ps.state()
    assert st["halted"].startswith("stale market-data feed") and st["kill_switch"]["engaged"] and st["open_orders"] == []
    assert ps.submit_manual({"symbol": "SYNBANK", "qty": 1})["status"] == "REJECTED"
    n = len(ps.engine.orders)
    ps.advance(5)                                             # strategies keep running but every new order is refused
    assert all(o.reason.startswith("KILL_SWITCH") for o in ps.engine.orders[n:])
    assert sent and sent[0][0] == "halt"


def test_feed_failure_and_loss_limit_halt_the_session(data_dir):
    ps = session(data_dir)
    ps.advance(10)

    def broken():
        raise ConnectionError("socket closed")
    ps.feed.next = broken
    assert ps.advance(5) == 0 and "feed failure" in ps.halted_reason

    ps2 = session(data_dir, strat="buy_and_hold", params={}, max_session_loss_pct=0.0001)
    while ps2.advance(20) and not ps2.halted_reason:
        pass
    assert ps2.halted_reason and "loss limit" in ps2.halted_reason


def test_live_feed_is_honestly_not_implemented():
    with pytest.raises(NotImplementedError):
        KiteTickerFeed().connect()


# ------------------------------------------------------------------ live gate
def gate(tmp_path, *, live=False, secret=SECRET, kill=None) -> LiveGate:
    return LiveGate(tmp_path / "approvals", tmp_path / "compliance.json", kill or KillSwitch(),
                    settings=load_settings({"LIVE_TRADING_ENABLED": "true" if live else "false"}),
                    env={"HUMAN_APPROVAL_SECRET": secret} if secret else {})


def open_everything(tmp_path, params=None, days=5, cap=100_000.0):
    (tmp_path / "approvals").mkdir(exist_ok=True)
    (tmp_path / "compliance.json").write_text(json.dumps({k: True for k in COMPLIANCE_ITEMS}))
    (tmp_path / "approvals" / f"{SHA}.json").write_text(json.dumps(sign_approval(SECRET, SHA, params or {}, "R. Sharma", days, cap)))


def test_live_trading_is_disabled_by_default_and_settings_are_frozen(tmp_path):
    s = load_settings({})
    assert s.live_trading_enabled is False
    with pytest.raises(Exception):
        s.live_trading_enabled = True                       # frozen dataclass
    d = gate(tmp_path).check(SHA)
    assert not d.allowed and "LIVE_TRADING_ENABLED is false" in d.reasons


def test_every_gate_condition_is_independently_required(tmp_path):
    open_everything(tmp_path)
    assert gate(tmp_path, live=True).check(SHA, {}, 50_000).allowed                    # all four conditions met
    assert not gate(tmp_path, live=False).check(SHA, {}, 50_000).allowed               # flag
    ks = KillSwitch(); ks.engage("halt")
    assert not gate(tmp_path, live=True, kill=ks).check(SHA, {}).allowed               # kill switch
    assert not gate(tmp_path, live=True).check("cd" * 32, {}).allowed                  # different code version
    assert not gate(tmp_path, live=True).check(SHA, {"fast": 99}).allowed              # different parameters
    assert not gate(tmp_path, live=True).check(SHA, {}, 10_000_000).allowed            # over approved capital
    assert not gate(tmp_path, live=True, secret="").check(SHA, {}).allowed             # cannot verify signature
    assert not gate(tmp_path, live=True, secret="another-secret-value").check(SHA, {}).allowed
    c = json.loads((tmp_path / "compliance.json").read_text()); c["static_ip_registered"] = False
    (tmp_path / "compliance.json").write_text(json.dumps(c))
    assert not gate(tmp_path, live=True).check(SHA, {}).allowed                        # compliance item open


def test_forged_tampered_and_expired_approvals_are_rejected(tmp_path):
    open_everything(tmp_path)
    f = tmp_path / "approvals" / f"{SHA}.json"
    rec = json.loads(f.read_text())
    f.write_text(json.dumps({**rec, "max_capital": 1e12}))                             # edited after signing
    assert "approval signature is invalid" in gate(tmp_path, live=True).check(SHA, {}).reasons
    f.write_text(json.dumps({**rec, "signature": "0" * 64}))                           # forged without the secret
    assert not gate(tmp_path, live=True).check(SHA, {}).allowed
    open_everything(tmp_path, days=-1)
    assert "approval has expired" in gate(tmp_path, live=True).check(SHA, {}).reasons
    with pytest.raises(ValueError):
        sign_approval(SECRET, SHA, {}, "agent:research_director", 5, 1.0)              # agents cannot be approvers
    with pytest.raises(ValueError):
        sign_approval("", SHA, {}, "R. Sharma", 5, 1.0)


def test_broker_adapter_cannot_send_without_the_gate(tmp_path):
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        return httpx.Response(200, json={"status": "success", "data": {"order_id": "1"}})
    req = OrderRequest("c1", "INFY", 1, 5, "LIMIT", limit_price=1500.0, strategy_sha256=SHA, strategy_params={})
    closed = ZerodhaKite(gate(tmp_path), "k", "t", transport=httpx.MockTransport(handler))
    with pytest.raises(LiveTradingDisabled):
        closed.place_order(req)
    assert calls == []                                                                 # nothing left the process
    open_everything(tmp_path)
    opened = ZerodhaKite(gate(tmp_path, live=True), "k", "t", transport=httpx.MockTransport(handler))
    assert opened.place_order(req, 10_000)["status"] == "success" and len(calls) == 1  # mock transport only
    body = dict(x.split("=") for x in calls[0].content.decode().split("&"))
    assert body["tradingsymbol"] == "INFY" and body["transaction_type"] == "BUY" and body["order_type"] == "LIMIT"
    for stub in (Upstox(gate(tmp_path, live=True)), AngelOneSmartAPI(gate(tmp_path, live=True))):
        assert stub.describe()["status"] == "NOT IMPLEMENTED"
        with pytest.raises(NotImplementedError):
            stub.place_order(req, 10_000)


# ------------------------------------------------------------------ router
def router(tmp_path, **kw):
    sent, log = [], []
    clock = lambda: datetime(2025, 1, 6, 10, 0)             # a Monday, inside session hours
    t = {"now": 0.0}
    r = OrderRouter(lambda req: sent.append(req) or {"paper_id": len(sent)}, kw.pop("kill", KillSwitch()),
                    limits=RouterLimits(**kw.pop("limits", {})), calendar=TradingCalendar(), clock=kw.pop("clock", clock),
                    monotonic=lambda: t["now"], audit=lambda actor, action, **d: log.append(action), **kw)
    return r, sent, log, t


def req(i, **kw):
    return OrderRequest(f"c{i}", "SYNBANK", 1, 10, **kw)


def test_router_controls(tmp_path):
    r, sent, log, t = router(tmp_path, limits={"max_orders_per_second": 2})
    assert r.submit(req(1), ref_price=100)["status"] == "ACCEPTED_PAPER"
    dup = r.submit(req(1), ref_price=100)
    assert dup["duplicate"] and len(sent) == 1                                        # idempotent
    r.submit(req(2), ref_price=100)
    assert r.submit(req(3), ref_price=100)["reason"] == "RATE_LIMIT"
    t["now"] = 2.0
    assert r.submit(req(4), ref_price=100_000)["reason"] == "MAX_ORDER_VALUE"
    r.daily_pnl = -30_000
    assert r.submit(req(5), ref_price=100)["reason"] == "DAILY_LOSS_LIMIT"
    closed, *_ = router(tmp_path, clock=lambda: datetime(2025, 1, 5, 10, 0))          # Sunday
    assert closed.submit(req(1), ref_price=100)["reason"] == "MARKET_CLOSED"
    assert "ORDER_ROUTED" in log and "ORDER_REJECTED" in log


def test_router_never_reaches_a_live_broker_by_default_and_breaker_trips(tmp_path):
    ks = KillSwitch()
    kite = ZerodhaKite(gate(tmp_path), "k", "t", transport=httpx.MockTransport(lambda q: pytest.fail("network call")))
    r, sent, log, t = router(tmp_path, live=kite, kill=ks, limits={"consecutive_reject_breaker": 3, "max_orders_per_second": 100})
    out = r.submit(OrderRequest("L1", "INFY", 1, 1, strategy_sha256=SHA), ref_price=100, live=True)
    assert out["status"] == "REJECTED" and "LIVE_TRADING_ENABLED is false" in out["reason"]
    for i in range(2, 4):
        r.submit(OrderRequest(f"L{i}", "INFY", 1, 1, strategy_sha256=SHA), ref_price=100, live=True)
    assert ks.engaged and "circuit breaker" in ks.reason and "KILL_SWITCH_ENGAGED" in log
    assert r.submit(req(9), ref_price=100)["reason"].startswith("KILL_SWITCH")


# ------------------------------------------------------------------ notifications
def test_whatsapp_notifier_payloads_and_failure_isolation():
    seen = []

    def handler(q: httpx.Request) -> httpx.Response:
        seen.append((str(q.url), q.headers["authorization"], json.loads(q.content)))
        return httpx.Response(200, json={"messages": [{"id": "wamid.x"}]})
    n = WhatsAppNotifier("tok", "12345", ["919999999999"], api_version="v21.0", transport=httpx.MockTransport(handler))
    assert n.send("halt", "feed stale")
    url, auth, body = seen[0]
    assert url == "https://graph.facebook.com/v21.0/12345/messages" and auth == "Bearer tok"
    assert body == {"messaging_product": "whatsapp", "to": "919999999999", "type": "text",
                    "text": {"preview_url": False, "body": "[halt] feed stale"}}
    n.template = "quant_alert"
    n.send("x", "y")
    assert seen[1][2]["type"] == "template" and seen[1][2]["template"]["components"][0]["parameters"][0]["text"] == "[x] y"
    assert WhatsAppNotifier().send("x", "y") is False and WhatsAppNotifier().status()["status"] == "NOT CONFIGURED"
    bad = WhatsAppNotifier("tok", "1", ["91"], transport=httpx.MockTransport(lambda q: httpx.Response(401, text="bad token")))
    assert bad.send("x", "y") is False and "401" in bad.log[-1]["detail"]             # failure is recorded, not raised
    e = WhatsAppNotifier.from_env({"WHATSAPP_ACCESS_TOKEN": "t", "WHATSAPP_PHONE_NUMBER_ID": "1", "WHATSAPP_TO": "+91 1, 912"})
    assert e.configured and e.recipients == ["91 1", "912"]
