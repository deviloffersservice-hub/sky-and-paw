"""Voice test helpers: Sarvam fixtures (tests/fixtures/sarvam) and a mock HTTP transport."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "sarvam"


def load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def fixture_response(name: str) -> httpx.Response:
    fx = load_fixture(name)
    return httpx.Response(fx["status"], json=fx["body"], headers=fx.get("headers") or {})


class Recorder:
    """httpx MockTransport that answers from a list of responses and records requests."""

    def __init__(self, responses: list[httpx.Response | Callable[[httpx.Request], Any]]):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        return item(request) if callable(item) else item

    def json_bodies(self) -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.requests]
