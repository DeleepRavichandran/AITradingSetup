"""The brain's own research agenda and its adaptation recommendations (docs/VALIDATION.md).

Research questions come from evidence the lab already records, never from a target count:
  losing patterns (daily review), refusal gates that may have cost money (opportunity ledger), strategy decay, entries
  taken without a positive expected edge (uncertainty gates), an uninformative allocator score (calibration),
  contradictions found by the critic, and concept/feature validation candidates awaiting independent data.
Each question carries a mechanism, the supporting and contradicting evidence, alternative explanations, a falsification
rule, the expected benefit, the proposed isolated experiment, and a priority = value x uncertainty / cost.
A question that was already rejected is not reopened unless its evidence has grown by at least half since the rejection.

Adaptation: for each methodology a recommendation (continue, observe, investigate, revalidate, suspend, roll back,
abstain) from drift, suspension rules, holdout and history evidence. Recommendations never switch strategies by
themselves and never follow recent profitability alone (tested); a change still needs validation and the operator.
"""
from __future__ import annotations

import hashlib

ACTIONS = ("continue", "observe", "investigate", "revalidate", "suspend", "roll back", "abstain")
REOPEN_GROWTH = 1.5


def _qid(topic: str) -> str:
    return "Q-" + hashlib.sha256(topic.encode()).hexdigest()[:8]


def _q(topic, question, mechanism, evidence, contradicting, alternatives, falsification, benefit, experiment, value, uncertainty, cost, size):
    return {"id": _qid(topic), "topic": topic, "question": question, "mechanism": mechanism, "evidence": evidence,
            "contradicting": contradicting, "alternatives": alternatives, "falsification": falsification, "expected_benefit": benefit,
            "experiment": experiment, "value": round(float(value), 4), "uncertainty": uncertainty, "cost": cost,
            "priority": round(float(value) * uncertainty / cost, 4), "evidence_size": size}


def questions(rep: dict, brain: dict | None = None, opportunities: dict | None = None, concepts: dict | None = None,
              features: dict | None = None, knowledge: dict | None = None) -> list[dict]:
    brain, knowledge = brain or {}, knowledge or {}
    out = []
    # 1. losing patterns from daily reviews (stored as unverified knowledge)
    for k in knowledge.values():
        if k["id"].startswith("K-PATTERN-"):
            ev = k.get("evidence", {})
            out.append(_q(k["id"], f"Would '{k['statement'].split('Proposed test: ')[-1]}' improve after-cost results?",
                          k["statement"].split(" Proposed test:")[0], [f"{ev.get('trades', 0)} losing trades over {ev.get('days', 0)} day(s), Rs {ev.get('lost_inr', 0):,.0f}"],
                          ["chosen from losing trades only: the same pattern may also occur in winners"],
                          ["chance clustering of losses", "a market-wide move on those days"],
                          "the variant does not beat the incumbent on paired sessions it was not designed on (block bootstrap lower bound <= 0)",
                          f"avoid about Rs {abs(ev.get('lost_inr', 0)):,.0f} per {max(ev.get('days', 1), 1)} day(s), if it generalises",
                          "shadow paper portfolio with the rule, same opportunities and capital, forward sessions",
                          abs(ev.get("lost_inr", 0)), 1.0, 1.0, ev.get("trades", 0)))
    # 2. refusal gates that may have cost money (hindsight, research store only)
    for g in (opportunities or {}).get("gate_review", []):
        if g["decision"] != "accepted" and (g.get("avg_30min_net") or 0) > 0 and g["setups"] >= 30:
            out.append(_q(f"gate:{g['reason']}", f"Is the '{g['reason']}' refusal costing good opportunities?",
                          "a limit or gate refuses setups whose later moves were positive after costs",
                          [f"{g['setups']:,} refused setups averaged {g['avg_30min_net']:+.2%} 30 minutes later"],
                          ["later outcomes are hindsight; refused setups could not all have been traded with the same capital"],
                          ["one strong session", "setups refused late in moves that reversed after 30 minutes"],
                          "a shadow portfolio relaxing only this gate does not beat the incumbent after costs on forward sessions",
                          "more after-cost return per unit of risk, if the gate is too strict", "shadow portfolio relaxing that one gate",
                          g["setups"] * g["avg_30min_net"], 0.8, 1.0, g["setups"]))
    # 3. strategy decay
    for d in brain.get("decay", []):
        if d.get("deterioration") == "alarm":
            out.append(_q(f"decay:{d['strategy_type']}", f"Has '{d['strategy_type']}' deteriorated structurally?",
                          "the mean net result per trade dropped (Page-Hinkley alarm)",
                          [f"first half {d['first_half_mean']:+.3%} vs second half {d['second_half_mean']:+.3%} per trade over {d['round_trips']} trades"],
                          ["a few large losses can trigger the alarm"], ["regime change", "execution or data change", "chance"],
                          "the decline does not persist in the next 30 trades of this type",
                          "earlier suspension of a failing strategy type", "revalidate on historical replay and forward shadow",
                          abs(d["net_inr"]), 0.9, 1.5, d["round_trips"]))
    # 4. entries without a positive expected edge
    u = brain.get("uncertainty") or {}
    if u.get("entries"):
        share = u["would_abstain"] / u["entries"]
        if share >= 0.25:
            out.append(_q("abstain", "Would abstaining when a mandatory uncertainty gate fails improve decisions?",
                          "trades taken without positive expected edge, fresh/complete data or familiar conditions have no reason to win",
                          [f"{u['would_abstain']} of {u['entries']} entries failed at least one gate"],
                          ["the expected-edge estimate is itself uncertain; abstaining also means trading less"],
                          ["any reduction in trading lowers costs whatever the selection"],
                          "H-ABSTAIN falsified, or the uncertainty-aware variant does not beat a random-abstention control with the same trade count",
                          "fewer weak trades, lower cost drag", "variant B vs incumbent vs random-abstention control (history replay + forward shadow)",
                          share, 1.0, 1.0, u["entries"]))
    # 5. calibration
    cal = next((b for b in rep.get("biases", []) if b["bias"].startswith("Overconfidence")), None)
    if cal and cal["status"] == "detected":
        out.append(_q("calibration", "Does the allocator's expected-edge score rank trades at all?", "the score orders opportunities",
                      [cal["evidence"]], [], ["too few trades", "score computed from too little evidence per configuration"],
                      "rank correlation stays at or below zero over the next 100 round trips", "a selection signal that works, or its removal",
                      "rank study on history replay and forward trades", 0.5, 0.9, 1.0, 0))
    # 6. critic contradictions
    for c in rep.get("critic", []):
        if not c["survives"]:
            out.append(_q(f"critic:{c['check']}", f"Contradiction: {c['check']}", "the incumbent's result depends on this", [c["detail"]], [], [],
                          "the result survives the check on new sessions", "know whether the edge (if any) is broad or concentrated",
                          "repeat the check on history replay segments", 0.3, 0.7, 0.5, 0))
    # 7. candidates awaiting independent data
    for src, rows in (("concept", (concepts or {}).get("results", [])), ("feature", (features or {}).get("results", []))):
        for r in rows:
            if r.get("verdict") == "validation candidate":
                name = r.get("concept") or r.get("feature")
                out.append(_q(f"{src}:{name}", f"Does {src} '{name}' hold on sealed holdout and forward data?", "it passed development only",
                              [r.get("reason") or "development pass"], ["development pass may be chance even after correction"], [],
                              "fails the sealed holdout or forward evaluation", "a context feature that improves selection",
                              "wait for holdout sessions, then a shadow portfolio using it", 0.4, 1.0, 1.0, r.get("n") or r.get("times") or 0))
    # do not reopen rejected questions without materially new evidence
    final = []
    for q in out:
        k = knowledge.get(q["id"])
        if k and k["status"] in ("contradicted finding", "invalidated finding"):
            before = (k.get("evidence") or {}).get("evidence_size", 0) or 0
            if q["evidence_size"] < REOPEN_GROWTH * max(before, 1):
                continue
            q["reopened"] = f"evidence grew from {before} to {q['evidence_size']}"
        final.append(q)
    return sorted(final, key=lambda q: -q["priority"])


def recommendations(rep: dict, brain: dict | None = None, history: dict | None = None) -> list[dict]:
    """One recommendation per methodology; never a switch by itself."""
    brain = brain or {}
    recs = []
    trig = rep.get("suspension", {}).get("triggered", [])
    drift = rep.get("validation", {}).get("drift", {})
    edge = next((h for h in rep.get("hypotheses", []) if h["id"] == "H-EDGE"), {})
    hold = ((history or {}).get("report") or {}).get("segments", {}).get("holdout", {})
    inc_vs_cash = hold.get("vs", {}).get("incumbent", {}).get("cash", {}).get("reading")

    def rec(name, action, why):
        assert action in ACTIONS
        recs.append({"methodology": name, "action": action, "why": why,
                     "note": "a recommendation; the active methodology changes only after validation and the operator's approval"})
    if trig:
        rec("incumbent", "suspend", "; ".join(f"{t['rule']}: {t['detail']}" for t in trig))
    elif inc_vs_cash and inc_vs_cash.startswith("behind"):
        rec("incumbent", "revalidate", "behind cash on the historical holdout after costs (interval below zero)")
    elif drift.get("alarm"):
        rec("incumbent", "investigate", f"deterioration alarm at round trip {drift.get('at')}")
    elif edge.get("status") in (None, "abstain", "not measurable yet"):
        rec("incumbent", "observe", "not enough confirmatory evidence either way")
    elif edge.get("status") == "falsified":
        rec("incumbent", "revalidate", "the edge hypothesis was falsified on confirmatory sessions")
    else:
        rec("incumbent", "continue", "no failure condition met")
    ch_vs = hold.get("challenger_vs_incumbent", {}).get("reading")
    rec("challenger", "observe" if not ch_vs or ch_vs == "indistinguishable" else "investigate" if ch_vs.startswith("ahead") else "observe",
        f"historical holdout: {ch_vs or 'no evidence yet'}; promotion still needs forward sessions and approval")
    for d in brain.get("decay", []):
        if d.get("deterioration") == "alarm":
            rec(f"strategy type {d['strategy_type']}", "investigate", "Page-Hinkley deterioration alarm")
    return recs
