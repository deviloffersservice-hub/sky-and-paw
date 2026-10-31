"""Realtime STT relay against scripted sockets (doc-shaped and recorded events) + REST fallback."""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator
from urllib.parse import parse_qs, urlparse

import pytest
from voice_helpers import fixture_response, load_fixture

from skypaw.voice import stt
from skypaw.voice.client import (
    FakeRealtimeSocket,
    FakeSarvam,
    QuotaExhausted,
    RealtimeUnavailable,
    SarvamClient,
    pcm_to_wav,
)


async def pcm(seconds: float, frame: int = 1600) -> AsyncIterator[bytes]:
    total = int(seconds * stt.BYTES_PER_S)
    for i in range(0, total, frame):
        yield b"\x01\x00" * (min(frame, total - i) // 2)
        await asyncio.sleep(0)


async def collect(agen) -> list[dict]:
    return [ev async for ev in agen]


def test_realtime_url_params():
    client = SarvamClient("k")
    url = client.realtime_url(stt.realtime_params("auto"))
    u = urlparse(url)
    assert f"{u.scheme}://{u.netloc}{u.path}" == "wss://api.sarvam.ai/speech-to-text-realtime/ws"
    q = {k: v[0] for k, v in parse_qs(u.query).items()}
    assert q == {
        "language_code": "auto",
        "model": "saaras:v3-realtime",
        "stream_type": "fast",
        "mode": "codemix",
        "encoding": "linear16",
        "sample_rate": "16000",
    }
    assert stt.realtime_params("od-IN")["language_code"] == "or-IN"
    assert stt.realtime_params("mr-IN")["language_code"] == "mr-IN"
    assert stt.realtime_params("xx-IN")["language_code"] == "auto"


async def test_scripted_session_yields_partials_and_final():
    fx = load_fixture("doc_ws_session.json")
    sock = FakeRealtimeSocket(events=fx["events"])
    fake = FakeSarvam(realtime=[sock])
    events = await collect(stt.transcribe_stream(fake, pcm(0.5), "auto"))
    kinds = [(e["kind"], e["text"]) for e in events]
    assert kinds == [
        ("partial", "dog को"),
        ("partial", "dog को V-01"),  # the duplicate partial is dropped
        ("partial", "dog को V-01 के पास"),
        ("final", "dog को V-01 के पास भेजो"),
    ]
    assert all(e["lang"] == "hi-IN" for e in events)
    assert events[-1]["confidence"] == 0.96
    # billed from session.end audio_duration_s
    assert fake.meter.by_kind["stt"] == pytest.approx(2.5 / 60 * 0.49)
    assert fake.calls[0] == ("realtime", stt.realtime_params("auto"))


async def test_live_like_socket_chunks_audio_and_sends_flush_end():
    sock = FakeRealtimeSocket(transcript="ड्रोन भेजो पूरी इमारत मैप करो", language="hi-IN")
    fake = FakeSarvam(realtime=[sock])
    events = await collect(stt.transcribe_stream(fake, pcm(1.0, frame=1000), "hi-IN"))
    audio = [m for m in sock.sent if m["event"] == "audio_input"]
    sizes = [len(base64.b64decode(m["audio"])) for m in audio]
    assert sizes == [3200] * 10  # 1 s of 16 kHz int16 re-sliced into 100 ms chunks
    assert [m["event"] for m in sock.sent[-2:]] == ["flush", "end"]
    partials = [e for e in events if e["kind"] == "partial"]
    assert partials and partials[-1]["text"] == "ड्रोन भेजो पूरी इमारत मैप"
    assert events[-1] == {
        "kind": "final",
        "text": "ड्रोन भेजो पूरी इमारत मैप करो",
        "lang": "hi-IN",
        "utterance": 0,
        "confidence": 0.97,
    }
    assert fake.meter.by_kind["stt"] == pytest.approx(1.0 / 60 * 0.49)


async def test_audio_waits_for_session_begin():
    events = [{"event": "transcript.final", "utterance_idx": 0, "text": "x"}]
    sock = FakeRealtimeSocket(events=events, close_code=1000)
    fake = FakeSarvam(realtime=[sock])
    # No session.begin: the relay must not stream audio and must report the socket unusable.
    with pytest.raises(RealtimeUnavailable):
        await collect(stt.transcribe_stream(fake, pcm(0.3), "auto", begin_timeout=0.5))
    assert not [m for m in sock.sent if m["event"] == "audio_input"]


async def test_recorded_quota_error_before_begin_raises_quota_exhausted():
    fx = load_fixture("recorded_ws_quota.json")
    sock = FakeRealtimeSocket(events=fx["events"], close_code=fx["close"]["code"])
    fake = FakeSarvam(realtime=[sock])
    with pytest.raises(QuotaExhausted) as ei:
        await collect(stt.transcribe_stream(fake, pcm(0.3), "hi-IN"))
    assert ei.value.status == 402
    assert fake.meter.total == 0
    assert not [m for m in sock.sent if m["event"] == "audio_input"]


async def test_close_1003_without_error_event_is_quota():
    sock = FakeRealtimeSocket(events=[], close_code=1003, close_reason="Credits exhausted")
    with pytest.raises(QuotaExhausted):
        await collect(stt.transcribe_stream(FakeSarvam(realtime=[sock]), pcm(0.1), "auto"))


async def test_close_4000_before_begin_is_unavailable():
    sock = FakeRealtimeSocket(events=[], close_code=4000, close_reason="account not enabled")
    with pytest.raises(RealtimeUnavailable) as ei:
        await collect(stt.transcribe_stream(FakeSarvam(realtime=[sock]), pcm(0.1), "auto"))
    assert ei.value.status == 4000


async def test_fatal_error_mid_session_raises_stt_error():
    events = [
        {"event": "session.begin", "request_id": "r"},
        {"event": "transcript.partial", "utterance_idx": 0, "text": "dog", "language": "en-IN"},
        {"event": "error", "code": "internal", "message": "boom", "is_fatal": True},
    ]
    sock = FakeRealtimeSocket(events=events)
    got = []
    with pytest.raises(stt.SttError):
        async for ev in stt.transcribe_stream(FakeSarvam(realtime=[sock]), pcm(0.2), "auto"):
            got.append(ev)
    assert got[0]["text"] == "dog"


async def test_pings_are_sent_on_interval():
    sock = FakeRealtimeSocket(transcript="hello there", language="en-IN")

    async def slow() -> AsyncIterator[bytes]:
        for _ in range(4):
            await asyncio.sleep(0.03)
            yield b"\0" * 3200

    events = await collect(
        stt.transcribe_stream(FakeSarvam(realtime=[sock]), slow(), "en-IN", ping_interval=0.02)
    )
    assert any(m["event"] == "ping" for m in sock.sent)
    assert events[-1]["kind"] == "final"


async def test_consumer_can_stop_early_and_tasks_are_cleaned_up():
    sock = FakeRealtimeSocket(transcript="a b c d e f", language="en-IN", chunks_per_word=1)
    agen = stt.transcribe_stream(FakeSarvam(realtime=[sock]), pcm(1.0), "en-IN")
    first = await agen.__anext__()
    assert first["kind"] == "partial"
    await agen.aclose()


async def test_rest_fallback_uses_saaras_v4_codemix_keyterms(make_client):
    client, rec = make_client([fixture_response("doc_stt_rest.json")])
    wav = pcm_to_wav(b"\0\0" * 16000 * 2)
    ev = await stt.transcribe_clip(client, wav, "auto")
    assert ev == {"kind": "final", "text": "dog को V-01 के पास भेजो", "lang": "hi-IN", "utterance": 0}
    body = rec.requests[0].content
    assert b"saaras:v4" in body and b"codemix" in body
    assert json.dumps(stt.KEYTERMS).encode() in body
    assert b'name="language_code"' not in body  # auto → let the model detect


async def test_rest_fallback_splits_clips_longer_than_30s():
    fake = FakeSarvam(
        stt_rest=[
            {"transcript": "first", "language_code": "en-IN"},
            {"transcript": "second", "language_code": "en-IN"},
        ]
    )
    wav = pcm_to_wav(b"\0\0" * 16000 * 40)
    ev = await stt.transcribe_clip(fake, wav, "en-IN")
    assert ev["text"] == "first second"
    assert [c[0] for c in fake.calls] == ["stt_rest", "stt_rest"]
    assert fake.meter.by_kind["stt_rest"] == pytest.approx(40 / 60 * 0.49)
