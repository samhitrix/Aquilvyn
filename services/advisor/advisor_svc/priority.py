"""Action Budget & Priority Engine (E42): rank by ₹ impact × severity × confidence, surface at
most N 'focus' items per week, everything else is FYI. 'Do nothing' is an explicit outcome."""
from __future__ import annotations

import math
from typing import Any


def score(severity: int, urgent: bool, confidence: float, impact: float, actionable: bool) -> float:
    if not actionable:
        return 0.0
    return round(severity * 10 + math.log10(max(impact, 1)) * 6 + confidence * 10 + (40 if urgent else 0), 2)


def bucketise(items: list[dict[str, Any]], budget: int) -> None:
    ranked = sorted((i for i in items if i["actionable"]), key=lambda i: -i["priority"])
    focus_left = budget
    for i in ranked:
        if i["urgent"]:
            i["bucket"] = "urgent"
        elif focus_left > 0 and i["priority"] >= 25:
            i["bucket"] = "focus"
            focus_left -= 1
        else:
            i["bucket"] = "fyi"
    for i in items:
        if not i["actionable"]:
            i["bucket"] = "fyi"
