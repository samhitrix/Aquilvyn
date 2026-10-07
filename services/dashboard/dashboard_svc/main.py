"""dashboard-bff — E09 Dashboard Aggregator. One request from the browser fans out to
portfolio / market / advisor / analytics in parallel (asyncio.gather) and returns a single
payload, so the family dashboard renders from one round-trip. A failing upstream degrades its
own panel (``errors``) instead of failing the page."""
from __future__ import annotations

import asyncio
import uuid
from typing import Any

from fastapi import APIRouter

from fm_common import http
from fm_common.app import create_app
from fm_common.config import settings
from fm_common.deps import CurrentUser

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


async def _gather(calls: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    keys = list(calls)
    results = await asyncio.gather(*calls.values(), return_exceptions=True)
    data, errors = {}, {}
    for k, v in zip(keys, results, strict=True):
        if isinstance(v, BaseException):
            errors[k] = str(v)[:300]
            data[k] = None
        else:
            data[k] = v
    return data, errors


@router.get("")
async def dashboard(principal: CurrentUser, scope: str = "household", id: uuid.UUID | None = None) -> dict[str, Any]:
    t = principal.token
    params: dict[str, Any] = {"scope": scope}
    if id:
        params["id"] = str(id)
    # every panel follows the selection: one person, or a group's members
    groups = await http.get(settings.portfolio_url, "/api/v1/groups", token=t)
    people: list[str] = []
    if scope == "profile" and id:
        people = [str(id)]
    elif scope == "group" and id:
        people = next((list(map(str, g.get("profile_ids") or [])) for g in groups or [] if str(g.get("id")) == str(id)), [])
    only = {"profile_ids": ",".join(people)} if people else {}
    async def _groups() -> Any:
        return groups
    data, errors = await _gather({
        "holdings": http.get(settings.portfolio_url, "/api/v1/holdings", token=t, params=params),
        "performance": http.get(settings.portfolio_url, "/api/v1/performance", token=t, params=only),
        "profiles": http.get(settings.portfolio_url, "/api/v1/profiles", token=t),
        "groups": _groups(),
        "advisor": http.get(settings.advisor_url, "/api/v1/advisor/summary", token=t, params=only),
        "market": http.get(settings.market_url, "/api/v1/market/overview", token=t),
    })
    return {**data, "errors": errors}


@router.get("/holding/{instrument_id}")
async def holding(instrument_id: uuid.UUID, principal: CurrentUser, profile_id: uuid.UUID | None = None, symbol: str | None = None) -> dict[str, Any]:
    t = principal.token
    rec_params: dict[str, Any] = {"instrument_id": str(instrument_id), "status": "open,accepted,snoozed,dismissed", "include_hold": "true"}
    tx_params: dict[str, Any] = {"instrument_id": str(instrument_id), "limit": 200}
    if profile_id:
        rec_params["profile_id"] = tx_params["profile_id"] = str(profile_id)
    calls: dict[str, Any] = {
        "recommendations": http.get(settings.advisor_url, "/api/v1/advisor/recommendations", token=t, params=rec_params),
        "transactions": http.get(settings.portfolio_url, "/api/v1/transactions", token=t, params=tx_params),
        "prefs": http.get(settings.portfolio_url, "/api/v1/holding-prefs", token=t, params={"profile_id": str(profile_id)} if profile_id else None),
    }
    if symbol:
        calls["analysis"] = http.get(settings.analytics_url, f"/api/v1/analysis/instrument/{symbol}", token=t, request_timeout=50)
        calls["history"] = http.get(settings.market_url, f"/api/v1/market/history/{symbol}", token=t, params={"days": 730})
        calls["news"] = http.get(settings.market_url, f"/api/v1/market/news/{symbol}", token=t)
    data, errors = await _gather(calls)
    if data.get("prefs") is not None:
        data["prefs"] = next((p for p in data["prefs"] if p["instrument_id"] == str(instrument_id)), None)
    return {**data, "errors": errors}


app = create_app("dashboard", [router], uses_db=False)
