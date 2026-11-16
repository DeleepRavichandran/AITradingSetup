"""Optional LLM backing for agents (Anthropic Claude).

The research loop is fully operational without an LLM: every agent has a deterministic
implementation. When ANTHROPIC credentials are available the Strategy Inventor and the Analyst
chat can use Claude. LLM output is never trusted: a proposed strategy is parsed into a
StrategySpec (schema + expression whitelist), turned into code by the code generator, and that
code goes through the same static validation and sandbox as everything else.

STATUS: UNVERIFIED in this environment — no API credentials were available, so the calls below
have only been exercised against a stub client in tests.
"""
from __future__ import annotations

import json
import os
from typing import Protocol

MODEL = "claude-opus-5-5"


class LLMUnavailable(RuntimeError):
    pass


class LLMClient(Protocol):
    name: str

    def json(self, system: str, prompt: str, schema: dict) -> dict: ...
    def text(self, system: str, prompt: str) -> str: ...


def credentials_present() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


class ClaudeClient:
    name = f"anthropic:{MODEL}"

    def __init__(self, effort: str = "medium"):
        try:
            import anthropic
        except ImportError as e:  # optional dependency: pip install .[llm]
            raise LLMUnavailable("the 'anthropic' package is not installed") from e
        if not credentials_present():
            raise LLMUnavailable("no Anthropic credentials in the environment")
        self._anthropic = anthropic
        self._client = anthropic.Anthropic()
        self._effort = effort

    def _call(self, system: str, prompt: str, output_config: dict):
        a = self._anthropic
        try:
            # server-side refusal fallback: on a policy decline the API re-runs the request on a fallback model
            resp = self._client.beta.messages.create(
                model=MODEL, max_tokens=16000, system=system, output_config=output_config,
                betas=["server-side-fallback-2026-07-01"], fallbacks="default",
                messages=[{"role": "user", "content": prompt}])
        except a.RateLimitError as e:
            raise LLMUnavailable(f"rate limited: {e.message}") from e
        except a.APIStatusError as e:
            raise LLMUnavailable(f"API error {e.status_code}: {e.message}") from e
        except a.APIConnectionError as e:
            raise LLMUnavailable("network error reaching the Anthropic API") from e
        if resp.stop_reason == "refusal":
            raise LLMUnavailable("the model declined the request")
        if resp.stop_reason == "max_tokens":
            raise LLMUnavailable("response truncated at max_tokens")
        return next((b.text for b in resp.content if b.type == "text"), "")

    def json(self, system: str, prompt: str, schema: dict) -> dict:
        text = self._call(system, prompt, {"effort": self._effort, "format": {"type": "json_schema", "schema": schema}})
        try:
            return json.loads(text)
        except ValueError as e:
            raise LLMUnavailable("model did not return valid JSON") from e

    def text(self, system: str, prompt: str) -> str:
        return self._call(system, prompt, {"effort": self._effort})


def default_client() -> LLMClient | None:
    try:
        return ClaudeClient()
    except LLMUnavailable:
        return None


SPEC_DRAFT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["family", "hypothesis", "falsification", "params", "features", "entry", "exit", "max_positions",
                 "max_hold", "stop_loss_pct", "regime_assumptions", "expected_failure_modes", "rationale"],
    "properties": {
        "family": {"type": "string", "enum": ["trend", "momentum", "mean_reversion", "breakout", "volatility"]},
        "hypothesis": {"type": "string"}, "falsification": {"type": "string"}, "rationale": {"type": "string"},
        "params": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                               "required": ["name", "value", "grid"],
                                               "properties": {"name": {"type": "string"}, "value": {"type": "number"},
                                                              "grid": {"type": "array", "items": {"type": "number"}}}}},
        "features": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                                 "required": ["name", "fn", "source", "window"],
                                                 "properties": {"name": {"type": "string"},
                                                                "fn": {"type": "string", "enum": ["sma", "ema", "rsi", "stdev", "zscore", "roc", "highest", "lowest", "realized_vol", "atr"]},
                                                                "source": {"type": "string", "enum": ["open", "high", "low", "close", "volume"]},
                                                                "window": {"type": "string", "description": "an integer, or the name of a declared param"}}}},
        "entry": {"type": "string"}, "exit": {"type": "string"},
        "max_positions": {"type": "integer"}, "max_hold": {"type": "integer"}, "stop_loss_pct": {"type": "number"},
        "regime_assumptions": {"type": "array", "items": {"type": "string"}},
        "expected_failure_modes": {"type": "array", "items": {"type": "string"}},
    },
}

INVENTOR_SYSTEM = """You are the Strategy Inventor in a quantitative research team studying Indian (NSE) equities.
Propose ONE falsifiable, long-only, daily-bar cash-equity hypothesis as a declarative specification.

Rules of the specification language:
- features are trailing-window indicators (sma, ema, rsi, stdev, zscore, roc, highest, lowest, realized_vol, atr) of a price field.
- entry and exit are single boolean expressions over feature names, the fields open/high/low/close/volume and declared
  parameter names, using + - * / comparisons and/or/not abs() min() max(), and prev(feature) for the previous bar's value.
- every numeric threshold you may want to vary must be a declared parameter with a small grid (at most 3 values).
- set stop_loss_pct to 0 for no stop, max_hold to 0 for no time limit.

You are given descriptive statistics of the development sample and the team's research memory. Do not repeat a
structure that has already been tested. Prefer a genuinely different idea over re-tuning a failed one. State what
observable result would refute the hypothesis. Never claim or imply the strategy will be profitable."""

ANALYST_SYSTEM = """You are the AI analyst of a quantitative research platform for Indian (NSE) markets.
Answer using ONLY the JSON context supplied (experiment registry extracts). Quote run ids and numbers from it.
If the context does not contain the answer, say so plainly. Always state when results come from SYNTHETIC data.
Never state or imply that any strategy will be profitable; backtests are evidence about the past, with the caveats
recorded in the context. Do not give personalised investment advice. Be concise."""
