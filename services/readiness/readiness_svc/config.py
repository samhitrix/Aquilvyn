from __future__ import annotations

from functools import lru_cache

from fm_common.config import CommonSettings


class ReadinessSettings(CommonSettings):
    sweep_every_minutes: int = 15            # every household is checked at least this often
    event_debounce_seconds: int = 45         # coalesce bursts (a run finishing, reviews landing, an import)
    backoff_seconds: tuple[int, ...] = (600, 1800, 7200, 43200)  # 10 min → 30 min → 2 h → 12 h between fixes of one thing
    attention_after_attempts: int = 4        # still failing after this many fixes → "needs attention" (keeps retrying)
    max_data_fixes_per_pass: int = 25        # market refreshes per household per pass (the rest wait for the next pass)
    max_parallel_fixes: int = 3


@lru_cache
def get_settings() -> ReadinessSettings:
    return ReadinessSettings()


settings = get_settings()
