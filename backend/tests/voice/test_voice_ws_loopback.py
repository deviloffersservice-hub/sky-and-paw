"""The real SarvamClient socket path against a loopback websockets server (no internet)."""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator
from urllib.parse import parse_qs, urlparse

import pytest
from voice_helpers import load_fixture
from websockets.asyncio.server import serve

from skypaw.voice import stt
from skypaw.voice.client import QuotaExhausted, RealtimeUnavailable, SarvamClient


async def audio(n: int) -> AsyncIterator[bytes]:
    for _ in range(n):
        yield b"\x03\x00" * 1600
        await asyncio.sleep(0)


class Server:
    def __init__(self, script: str) -> None:
        self.script = script
        self.seen_key: str | None = None
        self.query: dict[str, str] = {}
        self.received: list[dict] = []

    def refuse(self, connection, request):
        if self.script == "refuse":
            return connection.respond(403, "forbidden\n")
        return None

    async def handler(self, ws) -> None:
        self.seen_key = ws.request.headers.get("api-subscription-key")
        self.query = {k: v[0] for k, v in parse_qs(urlparse(ws.request.path).query).items()}
        if self.script == "quota":
            fx = load_fixture("recorded_ws_quota.json")
            for ev in fx["events"]:
                await ws.send(json.dumps(ev))
            await ws.close(fx["close"]["code"], fx["close"]["reason"])
            return
        await ws.send(json.dumps({"event": "session.begin", "request_id": "loop"}))
        audio_bytes = 0
        async for raw in ws:
            msg = json.loads(raw)
            self.received.append(msg)
            if msg["event"] == "audio_input":
                audio_bytes += len(base64.b64decode(msg["audio"]))
                if audio_bytes == 6400:
                    await ws.send(
                        json.dumps(
                            {"event": "transcript.partial", "utterance_idx": 0, "text": "land"}
                        )
                    )
            elif msg["event"] == "end":
                await ws.send(
                    json.dumps(
                        {
                            "event": "transcript.final",
                            "utterance_idx": 0,
                            "text": "land the drone",
                            "language": "en-IN",
                        }
                    )
                )
                await ws.send(
                    json.dumps(
                        {
                            "event": "session.end",
                            "request_id": "loop",
                            "audio_duration_s": audio_bytes / 32000,
                        }
                    )
                )
                await ws.close()
                return


async def _run(script: str, n: int = 5):
    srv = Server(script)
    async with serve(srv.handler, "127.0.0.1", 0, process_request=srv.refuse) as server:
        port = server.sockets[0].getsockname()[1]
        client = SarvamClient("loopback-key", ws_url=f"ws://127.0.0.1:{port}/rt")
        try:
            events = [e async for e in stt.transcribe_stream(client, audio(n), "en-IN")]
        finally:
            await client.aclose()
    return srv, client, events


async def test_session_over_loopback():
    srv, client, events = await _run("ok")
    assert srv.seen_key == "loopback-key"
    assert srv.query == stt.realtime_params("en-IN")
    assert [e["kind"] for e in events] == ["partial", "final"]
    assert events[-1]["text"] == "land the drone"
    assert [m["event"] for m in srv.received][-2:] == ["flush", "end"]
    assert client.meter.by_kind["stt"] == pytest.approx(0.5 / 60 * 0.49)


async def test_recorded_quota_over_loopback():
    with pytest.raises(QuotaExhausted):
        await _run("quota")


async def test_refused_handshake_is_unavailable():
    with pytest.raises(RealtimeUnavailable) as ei:
        await _run("refuse")
    assert ei.value.status == 403
