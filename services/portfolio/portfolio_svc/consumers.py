"""Event handlers for portfolio-svc (Redis Streams consumer group ``portfolio``)."""
from __future__ import annotations

import uuid
from typing import Any

from fm_common.db.session import SessionLocal
from fm_common.events import EventConsumer

from . import corporate_actions
from .models import Relationship
from .service import bootstrap_household


async def on_member(env: dict[str, Any]) -> None:
    p = env["payload"]
    rel = Relationship.SELF if env["type"] == "member.registered" else Relationship.OTHER
    async with SessionLocal() as db:
        await bootstrap_household(db, uuid.UUID(env["household_id"]), uuid.UUID(p["user_id"]), p.get("full_name") or "Me", rel)


async def on_corporate_action(env: dict[str, Any]) -> None:
    await corporate_actions.apply(env["payload"])


consumer = EventConsumer(
    "portfolio",
    {"member.registered": on_member, "member.added": on_member, "corporate_action.detected": on_corporate_action},
)
