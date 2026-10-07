"""Rulebook Decision Engine (E21): YAML rules → first-match decision + full decision trace."""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from .config import settings

OPS = {
    "==": lambda a, b: a == b, "!=": lambda a, b: a != b,
    "<": lambda a, b: a is not None and a < b, "<=": lambda a, b: a is not None and a <= b,
    ">": lambda a, b: a is not None and a > b, ">=": lambda a, b: a is not None and a >= b,
    "in": lambda a, b: a in b, "not_in": lambda a, b: a not in b,
    "is_true": lambda a, _b: a is True, "is_false": lambda a, _b: a is False,  # unknown (None) is NOT false — missing data must never trigger a call
    "exists": lambda a, _b: a is not None,
}


@dataclass(slots=True)
class Rule:
    id: str
    action: str
    horizon: str
    base_confidence: float
    severity: int
    reason: str
    when: list[dict[str, Any]]
    urgent: bool = False
    actionable: bool = True
    trim_fraction: float | None = None   # TRIM rules: sell this share of the position (else 25% / back to the size cap)


@dataclass(slots=True)
class Rulebook:
    version: str
    rules: list[Rule]


@dataclass(slots=True)
class Decision:
    rule: Rule
    trace: list[dict[str, Any]] = field(default_factory=list)


@lru_cache(maxsize=4)
def _load(path: str, mtime: float) -> Rulebook:
    raw = yaml.safe_load(Path(path).read_text())
    rules = [Rule(**{k: v for k, v in r.items()}) for r in raw["rules"]]
    for r in rules:
        for c in r.when:
            if c["op"] not in OPS:
                raise ValueError(f"Rule {r.id}: unknown op {c['op']}")
    if not rules or rules[-1].when:
        raise ValueError("Rulebook must end with an unconditional default rule")
    return Rulebook(str(raw["version"]), rules)


def load_rulebook(path: Path | None = None) -> Rulebook:
    """Hot-reloadable: a changed file (new mtime) is re-parsed on the next evaluation."""
    p = path or settings.rulebook_path
    return _load(str(p), p.stat().st_mtime)


def _resolve(facts: dict[str, Any], value: Any) -> Any:
    if isinstance(value, str) and value.startswith("$"):
        return facts.get(value[1:])
    return value


def evaluate(facts: dict[str, Any], book: Rulebook | None = None) -> Decision:
    book = book or load_rulebook()
    trace: list[dict[str, Any]] = []
    for rule in book.rules:
        failed = None
        for cond in rule.when:
            actual = facts.get(cond["fact"])
            expected = _resolve(facts, cond.get("value"))
            try:
                ok = OPS[cond["op"]](actual, expected)
            except TypeError:
                ok = False
            if not ok:
                failed = {"fact": cond["fact"], "op": cond["op"], "expected": expected, "actual": actual}
                break
        trace.append({"rule": rule.id, "action": rule.action, "matched": failed is None, "failed_condition": failed})
        if failed is None:
            return Decision(rule, trace)
    raise RuntimeError("unreachable: default rule always matches")
