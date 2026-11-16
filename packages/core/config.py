"""Process configuration.

Settings are read from the environment exactly once and frozen. Nothing in the
research / agent / strategy code path can mutate them: `Settings` is an immutable
dataclass and `get_settings()` caches the first read.

LIVE_TRADING_ENABLED defaults to false. Even when a human sets it to true the
execution gate (nseql.execution.gate) additionally requires a signed human
approval for the exact strategy version and a completed compliance checklist.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


def _truthy(v: str | None) -> bool:
    return (v or "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class Settings:
    live_trading_enabled: bool
    data_dir: Path
    experiments_dir: Path
    api_token: str | None
    anthropic_api_key_present: bool
    kite_credentials_present: bool

    @property
    def registry_path(self) -> Path:
        return self.experiments_dir / "registry.sqlite"


def load_settings(env: dict[str, str] | None = None) -> Settings:
    e = os.environ if env is None else env
    return Settings(
        live_trading_enabled=_truthy(e.get("LIVE_TRADING_ENABLED")),
        data_dir=Path(e.get("NSEQL_DATA_DIR", "datasets")),
        experiments_dir=Path(e.get("NSEQL_EXPERIMENTS_DIR", "experiments")),
        api_token=e.get("NSEQL_API_TOKEN") or None,
        anthropic_api_key_present=bool(e.get("ANTHROPIC_API_KEY")),
        kite_credentials_present=bool(e.get("KITE_API_KEY") and e.get("KITE_ACCESS_TOKEN")),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
