"""Declarative strategy specification and its safe expression language.

A StrategySpec is data, not code: an agent (or a human) states a falsifiable hypothesis and
describes the rule in a restricted vocabulary. `nseql.strategies.codegen` turns it into a
Python strategy module, which is then statically validated and executed only in the sandbox.

Expression language (entry / exit): a single Python *expression* using
    names      feature names, the price fields open/high/low/close/volume, and parameter names
    prev(x)    the value of feature x one bar earlier
    operators  + - * /   < <= > >= == !=   and or not   abs() min() max()
Anything else (attribute access, subscripts, calls to other functions, lambdas, ...) is rejected.
"""
from __future__ import annotations

import ast
import hashlib
import json

from pydantic import BaseModel, Field, field_validator, model_validator

PRICE_FIELDS = {"open", "high", "low", "close", "volume"}
FEATURE_FNS = {"sma", "ema", "rsi", "stdev", "zscore", "roc", "highest", "lowest", "realized_vol", "atr"}
_ALLOWED_NODES = (ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not, ast.USub, ast.BinOp, ast.Add,
                  ast.Sub, ast.Mult, ast.Div, ast.Compare, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Eq, ast.NotEq,
                  ast.Name, ast.Load, ast.Constant, ast.Call)
_ALLOWED_CALLS = {"prev", "abs", "min", "max"}


class ExpressionError(ValueError):
    pass


def parse_expression(expr: str, names: set[str]) -> ast.Expression:
    """Parse and whitelist-check an expression. `names` = identifiers it may reference."""
    try:
        tree = ast.parse(expr.strip(), mode="eval")
    except SyntaxError as e:
        raise ExpressionError(f"syntax error in expression {expr!r}: {e.msg}") from None
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise ExpressionError(f"{type(node).__name__} is not allowed in expression {expr!r}")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in _ALLOWED_CALLS or node.keywords:
                raise ExpressionError(f"only {sorted(_ALLOWED_CALLS)} may be called in {expr!r}")
            if node.func.id == "prev" and not (len(node.args) == 1 and isinstance(node.args[0], ast.Name)):
                raise ExpressionError("prev() takes exactly one feature name")
        if isinstance(node, ast.Constant) and not isinstance(node.value, (int, float, bool)):
            raise ExpressionError(f"only numeric constants are allowed in {expr!r}")
        if isinstance(node, ast.Name) and node.id not in names and node.id not in _ALLOWED_CALLS:
            raise ExpressionError(f"unknown name {node.id!r} in {expr!r}; known: {sorted(names)}")
    return tree


class Feature(BaseModel):
    name: str
    fn: str
    source: str = "close"
    n: int | str = 20                 # a number, or the name of an entry in `params`

    @field_validator("fn")
    @classmethod
    def _fn(cls, v):
        if v not in FEATURE_FNS:
            raise ValueError(f"unknown feature function {v!r}; allowed: {sorted(FEATURE_FNS)}")
        return v

    @field_validator("name")
    @classmethod
    def _name(cls, v):
        if not v.isidentifier() or v.startswith("_") or v in PRICE_FIELDS | _ALLOWED_CALLS:
            raise ValueError(f"bad feature name {v!r}")
        return v


class Sizing(BaseModel):
    max_positions: int = Field(5, ge=1, le=50)
    invest: float = Field(0.95, gt=0, le=1.0)


class StrategySpec(BaseModel):
    id: str
    version: int = 1
    family: str
    hypothesis: str                                   # must be falsifiable: see `falsification`
    falsification: str                                # the observable outcome that would refute it
    universe: str = "EQ"
    interval: str = "1d"
    params: dict[str, float] = Field(default_factory=dict)
    param_grid: dict[str, list[float]] = Field(default_factory=dict)
    features: list[Feature]
    entry: str
    exit: str
    sizing: Sizing = Field(default_factory=Sizing)
    stop_loss_pct: float | None = Field(None, gt=0, lt=0.5)
    take_profit_pct: float | None = Field(None, gt=0, lt=2.0)
    max_hold: int | None = Field(None, ge=1)
    capital: float = 1_000_000.0
    regime_assumptions: list[str] = Field(default_factory=list)
    expected_failure_modes: list[str] = Field(default_factory=list)
    origin: str = "unknown"
    parent_id: str | None = None
    rationale: str = ""

    @model_validator(mode="after")
    def _check(self):
        if len(self.hypothesis.strip()) < 20 or len(self.falsification.strip()) < 20:
            raise ValueError("hypothesis and falsification criterion must both be stated in full sentences")
        if not self.id.replace("_", "").isalnum():
            raise ValueError("id must be alphanumeric/underscore")
        names = [f.name for f in self.features]
        if len(set(names)) != len(names):
            raise ValueError("duplicate feature names")
        for p in self.params:
            if not p.isidentifier() or p.startswith("_") or p in PRICE_FIELDS or p in names:
                raise ValueError(f"bad parameter name {p!r}")
        for f in self.features:
            if isinstance(f.n, str) and f.n not in self.params:
                raise ValueError(f"feature {f.name}: window {f.n!r} is not a declared parameter")
            if f.source not in PRICE_FIELDS:
                raise ValueError(f"feature {f.name}: unknown source {f.source!r}")
        known = set(names) | PRICE_FIELDS | set(self.params)
        parse_expression(self.entry, known)
        parse_expression(self.exit, known)
        for k in self.param_grid:
            if k not in self.params:
                raise ValueError(f"param_grid key {k!r} is not a declared parameter")
        return self

    def structural_hash(self) -> str:
        """Identity of the rule itself (ignores id/version/prose) — used to avoid re-testing duplicates."""
        d = self.model_dump(include={"family", "universe", "interval", "features", "entry", "exit", "sizing",
                                     "stop_loss_pct", "take_profit_pct", "max_hold"})
        # a parameter that is searched over a grid is not part of the rule's identity: two specs that
        # differ only in the starting value of a tuned parameter are the same experiment
        d["grid"] = {k: sorted(v) for k, v in self.param_grid.items()}
        d["fixed_params"] = {k: v for k, v in self.params.items() if k not in self.param_grid}
        return hashlib.sha256(json.dumps(d, sort_keys=True).encode()).hexdigest()[:16]
