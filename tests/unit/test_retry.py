"""Unit-Tests: Retry-Strategie (Spec Kap. 7.3)."""
from __future__ import annotations

import pytest

from app.retry import PersistentError, TransientError, retry_async


@pytest.mark.asyncio
async def test_transient_retried_then_succeeds():
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise TransientError("temp")
        return "ok"

    result = await retry_async(flaky, max_retries=3, backoff_base=0)
    assert result == "ok"
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_persistent_not_retried():
    calls = {"n": 0}

    async def bad():
        calls["n"] += 1
        raise PersistentError("nope")

    with pytest.raises(PersistentError):
        await retry_async(bad, max_retries=3, backoff_base=0)
    assert calls["n"] == 1  # kein Retry


@pytest.mark.asyncio
async def test_transient_exhausts_and_raises():
    async def always_fail():
        raise TransientError("temp")

    with pytest.raises(TransientError):
        await retry_async(always_fail, max_retries=3, backoff_base=0)
