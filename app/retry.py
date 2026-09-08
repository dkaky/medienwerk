"""Retry-Strategie (Spec Kap. 7.3).

* Transient errors  -> Retry max 3x mit exponential backoff (1s, 2s, 4s)
* Persistent errors -> kein Retry, sofort durchreichen
* Rate-Limit errors -> warten und erneut versuchen
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, TypeVar

logger = logging.getLogger("app.retry")

T = TypeVar("T")


class TransientError(Exception):
    """Voruebergehender Fehler (Netzwerk, Timeout) – darf erneut versucht werden."""


class PersistentError(Exception):
    """Dauerhafter Fehler (Invalid Input, Not Found) – kein Retry."""


class RateLimitError(TransientError):
    """API-Quota erreicht. retry_after = Sekunden bis zum naechsten Versuch."""

    def __init__(self, message: str = "", retry_after: float = 5.0) -> None:
        super().__init__(message)
        self.retry_after = retry_after


async def retry_async(
    func: Callable[[], Awaitable[T]],
    *,
    max_retries: int = 3,
    backoff_base: float = 1.0,
    label: str = "task",
) -> T:
    """Fuehrt `func` mit exponential backoff aus.

    PersistentError wird nie wiederholt. RateLimitError wartet `retry_after`.
    """
    for attempt in range(max_retries):
        try:
            return await func()
        except PersistentError:
            raise  # kein Retry
        except RateLimitError as exc:
            wait = exc.retry_after
            logger.warning(
                "rate limit hit", extra={"label": label, "attempt": attempt, "wait_s": wait}
            )
            if attempt < max_retries - 1:
                await asyncio.sleep(wait)
            else:
                raise
        except TransientError as exc:
            wait = backoff_base * (2 ** attempt)
            logger.warning(
                "transient error, retrying",
                extra={"label": label, "attempt": attempt, "wait_s": wait,
                       "error": str(exc).strip() or repr(exc)},
            )
            if attempt < max_retries - 1:
                await asyncio.sleep(wait)
            else:
                raise
    raise RuntimeError("unreachable")  # pragma: no cover
