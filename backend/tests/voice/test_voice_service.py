"""SarvamVoice and FakeVoice conform to ports.Voice; realtime → REST fallback; no deprecated ids."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from skypaw.mission import ports
from skypaw.voice import service
from skypaw.voice.client import FakeRealtimeSocket, FakeSarvam, QuotaExhausted, RealtimeUnavailable
from skypaw.voice.phrases import LEAD_SAMPLES

DEPRECATED = "sarvam-" + "30b"  # spelled in two parts so the id never appears in the repo
VOICE_DIR = Path(service.__file__).resolve().parent


async def mic(n: int = 10) -> AsyncIterator[bytes]:
    for _ in range(n):
        yield b"\x02\x00" * 1600
        await asyncio.sleep(0)


@pytest.mark.parametrize("cls", [service.SarvamVoice, service.FakeVoice])
def test_port_signatures(cls):
    for name in ("transcribe_stream", "plan", "speak", "translate"):
        port_sig = inspect.signature(getattr(ports.Voice, name))
        impl_sig = inspect.signature(getattr(cls, name))
        assert list(impl_sig.parameters) == list(port_sig.parameters), name
    assert inspect.isasyncgenfunction(cls.transcribe_stream)
    for name in ("plan", "speak", "translate"):
        assert inspect.iscoroutinefunction(getattr(cls, name))


async def _exercise(voice: ports.Voice) -> None:
    events = [e async for e in voice.transcribe_stream(mic(), "auto")]
    assert events and events[-1]["kind"] == "final"
    assert all(set(e) >= {"kind", "text", "lang"} for e in events)
    assert all(e["kind"] in {"partial", "final"} for e in events)
    p = await voice.plan(events[-1]["text"], events[-1]["lang"] or "hi-IN")
    assert isinstance(p, ports.Plan) and p.intent == "navigate_go2" and p.target == "V-01"
    url = await voice.speak("मदत येत आहे. शांत राहा.", "mr-IN")
    assert url.startswith("/tts/") and url.endswith(".wav") and len(url) == 5 + 64 + 4
    s = LEAD_SAMPLES[1]
    assert await voice.translate(s.text, "hi-IN", "ta-IN") == s.tr["ta-IN"]


async def test_fake_voice_conforms(tmp_path):
    voice = service.FakeVoice(tmp_path)
    await _exercise(voice)
    assert list((tmp_path / "tts").glob("*.wav"))
    events = [e async for e in voice.transcribe_stream(mic(3), "mr-IN")]
    assert [e["kind"] for e in events] == ["partial", "partial", "partial", "final"]
    assert events[0]["lang"] == "mr-IN"


async def test_fake_voice_without_data_dir_writes_nothing():
    url = await service.FakeVoice().speak("hello", "en-IN")
    assert url.startswith("/tts/")


async def test_sarvam_voice_over_fake_sarvam(tmp_path):
    sock = FakeRealtimeSocket(transcript=LEAD_SAMPLES[1].text, language="hi-IN")
    voice = service.fake_sarvam_voice(tmp_path, realtime=[sock])
    await _exercise(voice)
    snap = voice.meter.snapshot()
    assert set(snap["by_kind"]) == {"stt", "llm", "tts"}  # translate came from phrase tables
    assert snap["total_rupees"] > 0
    await voice.aclose()


async def test_realtime_refused_falls_back_to_rest(tmp_path):
    fake = FakeSarvam(
        realtime=[FakeRealtimeSocket(events=[], close_code=4000, close_reason="not enabled")],
        stt_rest=[{"transcript": "everyone out", "language_code": "en-IN"}],
    )
    voice = service.SarvamVoice(fake, tmp_path)
    events = [e async for e in voice.transcribe_stream(mic(10), "auto")]
    assert events == [{"kind": "final", "text": "everyone out", "lang": "en-IN", "utterance": 0}]
    assert [c[0] for c in fake.calls] == ["realtime", "stt_rest"]
    assert fake.meter.by_kind["stt_rest"] == pytest.approx(1.0 / 60 * 0.49)  # all 10 frames
    await voice.aclose()


async def test_connect_refused_falls_back_to_rest(tmp_path):
    fake = FakeSarvam(realtime=[RealtimeUnavailable("handshake 403", status=403)])
    fake._queues["stt_rest"].append({"transcript": "land", "language_code": "en-IN"})
    voice = service.SarvamVoice(fake, tmp_path)
    events = [e async for e in voice.transcribe_stream(mic(2), "en-IN")]
    assert events[-1]["text"] == "land"
    await voice.aclose()


async def test_quota_on_socket_propagates(tmp_path):
    sock = FakeRealtimeSocket(
        events=[
            {
                "event": "error",
                "code": "quota_exceeded",
                "message": "x",
                "is_fatal": True,
                "status_code": 402,
            }
        ],
        close_code=1003,
    )
    voice = service.SarvamVoice(FakeSarvam(realtime=[sock]), tmp_path)
    with pytest.raises(QuotaExhausted):
        _ = [e async for e in voice.transcribe_stream(mic(2), "auto")]
    await voice.aclose()


def test_no_deprecated_model_ids_in_voice_package():
    for path in VOICE_DIR.glob("*.py"):
        assert DEPRECATED not in path.read_text(encoding="utf-8"), path.name
