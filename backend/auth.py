from fastapi import Header, HTTPException, status, Query
from .settings import TOKEN


async def require_token(x_auth_token: str | None = Header(default=None)) -> None:
    if x_auth_token != TOKEN:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="bad token")


async def require_token_query(token: str | None = Query(default=None)) -> None:
    """For endpoints where header injection is hard (file download, SSE in <iframe>)."""
    if token != TOKEN:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="bad token")
