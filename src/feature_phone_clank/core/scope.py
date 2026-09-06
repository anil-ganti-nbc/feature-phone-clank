"""Production-scope lock (brief section 8 / user constraint 8).

Smartphone Clank learned the hard way that a collector which can run must
not thereby be allowed to write to the production catalogue — a test run
polluted it with 73 junk devices. FEATURE-01 starts with the guardrail
already in place: `config/scope.yaml` is the single, explicit allowlist of
collectors approved to persist into the production database. A collector
absent from it can still be run (e.g. `--experimental`) but only against an
in-memory/throwaway store, never the real one.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class ScopeConfig(BaseModel):
    production_collectors: list[str] = Field(default_factory=list)
    # Collectors retired by an explicit operator decision rather than held
    # back by missing qualification evidence. Membership here changes nothing
    # about what may run or persist -- a mothballed collector is outside
    # `production_collectors` exactly like an unqualified one -- it records
    # *why* it is outside, so an operator surface can say "retired" instead
    # of implying the source is still soaking toward promotion.
    mothballed_collectors: list[str] = Field(default_factory=list)


def load_scope(path: str | Path) -> ScopeConfig:
    path = Path(path)
    if not path.exists():
        return ScopeConfig()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return ScopeConfig.model_validate(data)


def is_production(source_key: str, scope: ScopeConfig) -> bool:
    return source_key in scope.production_collectors


def is_mothballed(source_key: str, scope: ScopeConfig) -> bool:
    """Retired by operator decision -- a reason, never a permission.

    Deliberately not consulted by `is_production`: a key listed in both
    would still be production, because the allowlist alone decides what
    persists. This answers "why is it not in production", not "may it run".
    """
    return source_key in scope.mothballed_collectors
