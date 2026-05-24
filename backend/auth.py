import hmac

from fastapi import Header, HTTPException, status, Query
from .settings import TOKEN


def _token_ok(presented: str | None) -> bool:
    """Constant-time token comparison.

    `==` on Python strings short-circuits at the first mismatched character,
    leaking the matched-prefix length via response timing. `hmac.compare_digest`
    runs in time proportional to the LONGER of the two inputs regardless of
    where they diverge. Cost is microseconds — irrelevant — and closes a
    side-channel that's trivial to exploit over LAN. `None` and empty string
    are both rejected up front so the comparator only sees real candidates.
    """
    if not presented or not TOKEN:
        return False
    return hmac.compare_digest(presented, TOKEN)


async def require_token(x_auth_token: str | None = Header(default=None)) -> None:
    if not _token_ok(x_auth_token):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="bad token")


async def require_token_query(token: str | None = Query(default=None)) -> None:
    """For endpoints where header injection is hard (file download, SSE in <iframe>)."""
    if not _token_ok(token):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="bad token")
