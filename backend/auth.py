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


async def require_token_header_or_query(
    x_auth_token: str | None = Header(default=None),
    token: str | None = Query(default=None),
) -> None:
    """Accept the token from EITHER the `X-Auth-Token` header OR the `token`
    query param.

    Endpoints hit by `fetch()` (interrupt / reset / export / file download &
    raw) historically only read the query param (`require_token_query`), which
    forced the frontend to put the token in the URL — where it leaks into
    uvicorn access logs, any reverse-proxy (nginx/Caddy/Cloudflare) access log,
    browser history and the Referer header. fetch() *can* send custom headers,
    so the frontend now passes the token via header and the URL stays clean.

    The query param is still accepted as a fallback so that (a) old clients and
    (b) genuinely header-less contexts (an <iframe> download, a copied link)
    keep working unchanged. Header is preferred: if it's valid we accept even
    when no query token is present, and vice-versa.
    """
    if _token_ok(x_auth_token) or _token_ok(token):
        return
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="bad token")
