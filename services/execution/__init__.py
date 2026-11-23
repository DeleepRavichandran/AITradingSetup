"""Execution service: live-trading gate, order router and broker adapters.

This package refuses to load inside the strategy sandbox, so generated code has no import
path to an order router even if static validation were somehow bypassed.
"""
import os

if os.environ.get("NSEQL_SANDBOX") == "1":
    raise ImportError("nseql.execution is not available inside the strategy sandbox")
