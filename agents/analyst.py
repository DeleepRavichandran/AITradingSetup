"""AI Analyst chat. Grounded strictly in registry content; LLM-backed when credentials exist,
otherwise a deterministic digest (clearly labelled) so the page is never fake."""
from __future__ import annotations

import json

from nseql.agents import llm as llm_mod
from nseql.research.registry import Registry

KEYS = ("sharpe", "total_return", "max_drawdown", "trades", "win_rate", "total_costs", "cagr")


def build_context(reg: Registry, run_id: str | None = None) -> dict:
    ctx = {
        "recent_runs": [{"run_id": r["run_id"], "strategy": r["strategy"], "partition": r["partition"], "synthetic": bool(r["synthetic"]),
                         "period": [r["period_start"], r["period_end"]], "status": r["status"],
                         **{k: (r["metrics"] or {}).get(k) for k in KEYS}, "flags": r["flags"]} for r in reg.runs(limit=25)],
        "hypotheses": [{k: h[k] for k in ("id", "family", "statement", "status", "status_reason")} for h in reg.hypotheses(30)],
        "lessons": [{k: l[k] for k in ("family", "outcome", "text", "next_step")} for l in reg.lessons(30)],
        "experiments": [{"id": e["id"], "status": e["status"], "usage": e["usage"]} for e in reg.experiments(5)],
    }
    if run_id:
        ctx["focus_run"] = reg.run(run_id)
        dec = [a for a in reg.artifacts(typ="Decision") if any(run_id == v for v in ((a["payload"].get("sealed") or {}).get("run_id"),))]
        ctx["focus_decisions"] = [a["payload"] for a in dec]
    return ctx


def answer(reg: Registry, question: str, run_id: str | None = None, client: llm_mod.LLMClient | None = None) -> dict:
    ctx = build_context(reg, run_id)
    client = client if client is not None else llm_mod.default_client()
    if client is not None:
        try:
            text = client.text(llm_mod.ANALYST_SYSTEM, f"Context (JSON):\n{json.dumps(ctx, default=str)[:150000]}\n\nQuestion: {question}")
            return {"mode": f"llm ({client.name})", "answer": text, "context_runs": len(ctx["recent_runs"])}
        except llm_mod.LLMUnavailable as e:
            note = f"LLM call failed ({e}); showing the deterministic digest instead."
    else:
        note = "No LLM is configured (set ANTHROPIC_API_KEY to enable free-form answers). Showing a deterministic digest of the registry."
    lines = [note, ""]
    f = ctx.get("focus_run")
    if f:
        m = f["metrics"]
        lines += [f"Run {f['run_id']} — {f['strategy']} on {f['dataset_id']} ({'SYNTHETIC' if f['synthetic'] else 'real'} data), "
                  f"{f['period_start']} to {f['period_end']}: Sharpe {m.get('sharpe')}, return {m.get('total_return')}, "
                  f"max drawdown {m.get('max_drawdown')}, {m.get('trades')} trades, costs Rs {m.get('total_costs')}. Flags: {f['flags']}", ""]
    if ctx["recent_runs"]:
        lines.append("Most recent runs:")
        for r in ctx["recent_runs"][:8]:
            lines.append(f"  {r['run_id']}  {r['strategy']:<24} {r['partition']:<10} Sharpe {r['sharpe']}  trades {r['trades']}"
                         + ("  [synthetic]" if r["synthetic"] else ""))
    if ctx["lessons"]:
        lines += ["", "Latest lessons:"] + [f"  - {l['text']} Next: {l['next_step']}" for l in ctx["lessons"][:5]]
    lines += ["", "Backtests describe the past under modelling assumptions; none of this predicts profit."]
    return {"mode": "digest (no LLM)", "answer": "\n".join(lines), "context_runs": len(ctx["recent_runs"])}
