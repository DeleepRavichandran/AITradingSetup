"""StrategySpec -> Python strategy module (source text).

The output is ordinary, readable Python that subclasses `Strategy`. It is never imported by
the API or execution services directly: it is stored, hashed, statically validated by
`nseql.research.sandbox.validate_source` and run in a resource-limited subprocess.
"""
from __future__ import annotations

import ast

from nseql.contracts.spec import PRICE_FIELDS, StrategySpec, parse_expression


class _Rewrite(ast.NodeTransformer):
    def __init__(self, features: set[str], params: set[str]):
        self.features, self.params = features, params

    def visit_Call(self, node: ast.Call):
        if isinstance(node.func, ast.Name) and node.func.id == "prev":
            return ast.Subscript(ast.Name("pf", ast.Load()), ast.Constant(node.args[0].id), ast.Load())
        return self.generic_visit(node)

    def visit_Name(self, node: ast.Name):
        if node.id in self.features or node.id in PRICE_FIELDS:
            return ast.Subscript(ast.Name("f", ast.Load()), ast.Constant(node.id), ast.Load())
        if node.id in self.params:
            return ast.Subscript(ast.Name("p", ast.Load()), ast.Constant(node.id), ast.Load())
        return node


def _expr(expr: str, spec: StrategySpec) -> str:
    names = {f.name for f in spec.features}
    tree = parse_expression(expr, names | PRICE_FIELDS | set(spec.params))
    return ast.unparse(ast.fix_missing_locations(_Rewrite(names, set(spec.params)).visit(tree)))


def class_name(spec: StrategySpec) -> str:
    return "Gen_" + spec.id


def generate_source(spec: StrategySpec) -> str:
    def win(f):
        return f'int(p["{f.n}"])' if isinstance(f.n, str) else str(int(f.n))
    lines = []
    for f in spec.features:
        if f.fn == "atr":
            lines.append(f'            "{f.name}": ind.atr(a["high"], a["low"], a["close"], {win(f)}),')
        else:
            lines.append(f'            "{f.name}": ind.{f.fn}(a["{f.source}"], {win(f)}),')
    need = " + ".join(["2"] + [f"{win(f)} * {12 if f.fn in ('rsi', 'ema', 'atr') else 1}" for f in spec.features])
    max_hold = f"age[s] >= {spec.max_hold}" if spec.max_hold else "False"
    protect = []
    if spec.stop_loss_pct or spec.take_profit_pct:
        protect.append("    def on_fill(self, ctx, fill):")
        protect.append('        if fill.tag != "entry" or ctx.position(fill.symbol) <= 0 or ctx.open_orders(fill.symbol):')
        protect.append("            return")
        protect.append("        q, px = ctx.position(fill.symbol), ctx.entry_price(fill.symbol)")
        if spec.stop_loss_pct:
            protect.append(f'        ctx.order(fill.symbol, -q, type="STOP", stop=px * {1 - spec.stop_loss_pct!r}, tif="GTC", oco="x", tag="stop_loss")')
        if spec.take_profit_pct:
            protect.append(f'        ctx.order(fill.symbol, -q, type="LIMIT", limit=px * {1 + spec.take_profit_pct!r}, tif="GTC", oco="x", tag="take_profit")')
    src = f'''"""GENERATED STRATEGY — do not edit by hand. Spec {spec.id} v{spec.version} (origin: {spec.origin})."""
import math

from nseql import indicators as ind
from nseql.strategies.api import Strategy


class {class_name(spec)}(Strategy):
    name = {("gen_" + spec.id)!r}
    family = {spec.family!r}
    version = {str(spec.version)!r}
    hypothesis = {spec.hypothesis!r}
    falsification = {spec.falsification!r}
    default_params = {dict(spec.params)!r}
    param_grid = {dict(spec.param_grid)!r}
    requires = {spec.universe!r}

    def features(self, ctx, s, shift):
        p = self.params
        need = {need}
        a = {{k: ctx.history(s, k, need + shift) for k in ("open", "high", "low", "close", "volume")}}
        if len(a["close"]) < need + shift:
            return None
        if shift:
            a = {{k: v[:-shift] for k, v in a.items()}}
        f = {{
{chr(10).join(lines)}
        }}
        for k in ("open", "high", "low", "close", "volume"):
            f[k] = float(a[k][-1])
        if any(isinstance(v, float) and math.isnan(v) for v in f.values()):
            return None
        return f

    def on_bar(self, ctx):
        p = self.params
        age = self.state.setdefault("age", {{}})
        slots = {spec.sizing.max_positions}
        held = len([q for q in ctx.positions().values() if q > 0])
        for s in ctx.universe({spec.universe!r}):
            f = self.features(ctx, s, 0)
            pf = self.features(ctx, s, 1)
            if f is None or pf is None:
                continue
            if ctx.position(s) > 0:
                age[s] = age.get(s, 0) + 1
                if ({_expr(spec.exit, spec)}) or {max_hold}:
                    ctx.cancel_all(s)
                    ctx.order_target(s, 0, tag="exit")
                    age.pop(s, None)
            elif held < slots and not ctx.open_orders(s) and ({_expr(spec.entry, spec)}):
                ctx.order_target_value(s, ctx.equity() * {spec.sizing.invest!r} / slots, tag="entry")
                held += 1

{chr(10).join(protect)}
'''
    ast.parse(src)   # generated code must at least be syntactically valid
    return src
