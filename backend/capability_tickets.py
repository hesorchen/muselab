"""Short-lived, scope-bound credentials for header-less browser requests."""

from __future__ import annotations

import hashlib
import secrets
import threading
import time
from collections import OrderedDict


class CapabilityTicketStore:
    """Mint and validate opaque tickets without retaining their raw values."""

    def __init__(self, max_entries: int = 4096) -> None:
        self.max_entries = max_entries
        self._rows: OrderedDict[
            str, tuple[str, tuple[str, ...], float, bool]
        ] = OrderedDict()
        self._lock = threading.Lock()

    @staticmethod
    def _digest(raw: str) -> str:
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _prune(self, now: float) -> None:
        while self._rows:
            digest, row = next(iter(self._rows.items()))
            if row[2] >= now and len(self._rows) <= self.max_entries:
                break
            self._rows.pop(digest, None)

    def mint(
        self,
        kind: str,
        scope: tuple[str, ...],
        *,
        ttl: float,
        single_use: bool = True,
    ) -> str:
        raw = secrets.token_urlsafe(32)
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            self._rows[self._digest(raw)] = (
                kind,
                tuple(scope),
                now + max(1.0, ttl),
                single_use,
            )
            self._prune(now)
        return f"{kind}.{raw}"

    def validate(
        self,
        ticket: str,
        kind: str,
        scope: tuple[str, ...],
    ) -> bool:
        prefix = f"{kind}."
        if not ticket.startswith(prefix):
            return False
        digest = self._digest(ticket[len(prefix):])
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            row = self._rows.get(digest)
            if row is None or row[2] < now:
                return False
            if row[0] != kind or row[1] != tuple(scope):
                return False
            if row[3]:
                self._rows.pop(digest, None)
            else:
                self._rows.move_to_end(digest)
        return True


tickets = CapabilityTicketStore()
