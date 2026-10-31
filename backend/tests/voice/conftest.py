"""Fixtures for the voice tests (helpers live in voice_helpers.py)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest
from voice_helpers import Recorder

from skypaw.voice.client import CreditMeter, SarvamClient


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
def make_client(sleeps: list[float]) -> Callable[..., tuple[SarvamClient, Recorder]]:
    async def fake_sleep(s: float) -> None:
        sleeps.append(s)

    def make(responses: list[Any], **kw: Any) -> tuple[SarvamClient, Recorder]:
        rec = Recorder(responses)
        client = SarvamClient(
            kw.pop("api_key", "test-key-not-real"),
            transport=httpx.MockTransport(rec),
            sleep=fake_sleep,
            meter=kw.pop("meter", CreditMeter()),
            **kw,
        )
        return client, rec

    return make
