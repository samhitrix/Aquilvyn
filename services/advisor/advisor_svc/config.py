from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from fm_common.config import CommonSettings


class AdvisorSettings(CommonSettings):
    rulebook_path: Path = Path(__file__).parent / "rulebook.yaml"
    weekly_action_budget: int = 7            # E42: at most N "focus" actions surfaced per week
    rerun_debounce_seconds: int = 20         # coalesce bursts of txn/price events per household

    # E24/E25 defaults — household-level providers configured in the app override these.
    ai_primary_provider: str = "claude"      # claude | openai | gemini | ollama | none
    anthropic_api_key: str | None = None
    anthropic_model: str = "claude-opus-5"
    openai_api_key: str | None = None
    openai_model: str | None = None
    openai_base_url: str = "https://api.openai.com/v1"
    gemini_api_key: str | None = None
    gemini_model: str | None = None
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str | None = None
    groq_api_key: str | None = None
    groq_model: str | None = None
    cloudflare_api_token: str | None = None
    cloudflare_account_id: str | None = None
    cloudflare_model: str | None = None
    ai_review_monthly_budget_usd: float = 10.0
    ai_review_max_per_run: int = 25          # review only the highest-priority actionable calls


@lru_cache
def get_settings() -> AdvisorSettings:
    return AdvisorSettings()


settings = get_settings()
