"""Account tools for the person who runs Aquilvyn (on that machine — no email server needed):

    python -m identity_svc.cli reset-link you@example.com   # one-time password-reset link (30 minutes)
    python -m identity_svc.cli token you@example.com        # short-lived access token (for `fm.py perf --as`)

`python scripts/fm.py reset-password <email>` and `python scripts/fm.py perf --as <email>` call these for you
(inside Docker, or directly without it).
"""
from __future__ import annotations

import asyncio
import sys

from sqlalchemy import func, select

from fm_common.db.session import SessionLocal
from fm_common.identity.tokens import create_access_token

from .api_auth import make_reset_link
from .models import User


async def main(cmd: str, email: str) -> int:
    async with SessionLocal() as db:
        if cmd == "reset-link":
            link = await make_reset_link(db, email)
            if not link:
                print(f"No account with the email {email} that signs in with a password", file=sys.stderr)
                return 1
            print(link)
            return 0
        if cmd == "token":
            user = (await db.execute(select(User).where(func.lower(User.email) == email.lower()))).scalar_one_or_none()
            if user is None or not user.is_active:
                print(f"No active account with the email {email}", file=sys.stderr)
                return 1
            token, _exp = create_access_token(user.id, user.household_id, user.role.value)
            print(token)
            return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    sys.exit(asyncio.run(main(sys.argv[1], sys.argv[2])))
