from __future__ import annotations

import enum


class Role(str, enum.Enum):
    OWNER = "owner"        # household owner: everything incl. member management
    ADMIN = "admin"        # manage members + all data in household
    ADVISOR = "advisor"    # read all profiles in household (a CA / planner), write notes
    MEMBER = "member"      # full control of the profiles granted to them
    VIEWER = "viewer"      # read-only
    SERVICE = "service"    # internal service-to-service / worker calls
