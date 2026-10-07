"""Fine-grained RBAC: roles map to permission strings ``<resource>:<action>[:any]``.

Ownership/grant checks are done by the owning service (a MEMBER may write the profiles
granted to them); ``:any`` permissions grant household-wide access regardless of grants."""
from __future__ import annotations

from fm_common.identity.roles import Role

P_PROFILE_READ = "profile:read"
P_PROFILE_WRITE = "profile:write"
P_PROFILE_READ_ANY = "profile:read:any"
P_PROFILE_WRITE_ANY = "profile:write:any"
P_TXN_WRITE = "transaction:write"
P_IMPORT = "import:run"
P_MARKET_READ = "market:read"
P_ADVISOR_READ = "advisor:read"
P_ADVISOR_ACT = "advisor:act"          # accept / snooze / dismiss recommendations
P_ADVISOR_RUN = "advisor:run"          # trigger a fresh analysis run
P_AI_CONFIGURE = "ai:configure"        # manage AI reviewer providers & keys
P_AUDIT_READ = "audit:read"
P_MEMBERS_MANAGE = "members:manage"
P_HOUSEHOLD_MANAGE = "household:manage"
P_INTERNAL = "internal:call"           # service-to-service only

_READ = {P_PROFILE_READ, P_MARKET_READ, P_ADVISOR_READ}
_MEMBER = _READ | {P_PROFILE_WRITE, P_TXN_WRITE, P_IMPORT, P_ADVISOR_ACT, P_ADVISOR_RUN}
_ADMIN = _MEMBER | {P_PROFILE_READ_ANY, P_PROFILE_WRITE_ANY, P_AUDIT_READ, P_MEMBERS_MANAGE, P_AI_CONFIGURE}

ROLE_PERMISSIONS: dict[Role, frozenset[str]] = {
    Role.VIEWER: frozenset(_READ),
    Role.MEMBER: frozenset(_MEMBER),
    Role.ADVISOR: frozenset(_READ | {P_PROFILE_READ_ANY, P_AUDIT_READ, P_ADVISOR_RUN}),
    Role.ADMIN: frozenset(_ADMIN),
    Role.OWNER: frozenset(_ADMIN | {P_HOUSEHOLD_MANAGE}),
    Role.SERVICE: frozenset(_ADMIN | {P_INTERNAL}),
}


def permissions_for(role: Role | str) -> frozenset[str]:
    return ROLE_PERMISSIONS[Role(role)]


def has_permission(role: Role | str, permission: str) -> bool:
    return permission in permissions_for(role)
