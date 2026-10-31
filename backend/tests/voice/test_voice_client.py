"""SarvamClient: auth header, retries with exponential backoff, errors, credit meter math."""

from __future__ import annotations

import base64
import json

import httpx
import pytest
from voice_helpers import fixture_response, load_fixture

from skypaw.config import Settings
from skypaw.voice.client import (
    MODEL_PLAN,
    CreditMeter,
    QuotaExhausted,
    RateLimiter,
    SarvamClient,
    SarvamError,
    SarvamUnavailable,
    wav_duration_s,
)


def test_meter_rates_match_research():
    m = CreditMeter()
    assert m.stt(60) == pytest.approx(0.49)
    assert m.stt(3) == pytest.approx(0.0245)
    assert m.tts(1000) == pytest.approx(3.0)
    assert m.tts(83) == pytest.approx(0.249)
    assert m.translate(1000) == pytest.approx(2.0)
    assert m.translate(34) == pytest.approx(0.068)
    assert m.llm(1_000_000, 0) == pytest.approx(15.0)
    assert m.llm(0, 1_000_000) == pytest.approx(60.0)
    assert m.llm(1180, 96) == pytest.approx(0.0177 + 0.00576)


async def test_auth_header_and_chat_body(make_client):
    client, rec = make_client([fixture_response("doc_chat_completion.json")])
    resp = await client.chat([{"role": "user", "content": "hi"}], max_tokens=1024)
    assert resp["choices"][0]["message"]["content"]
    req = rec.requests[0]
    assert req.url == "https://api.sarvam.ai/v1/chat/completions"
    assert req.headers["api-subscription-key"] == "test-key-not-real"
    body = json.loads(req.content)
    assert body["model"] == MODEL_PLAN == "sarvam-105b"
    assert body["reasoning_effort"] == "low"
    assert body["max_tokens"] == 1024
    # metered from usage: 1180 in + 96 out
    assert client.meter.by_kind["llm"] == pytest.approx(CreditMeter().llm(1180, 96))
    assert client.last["llm"].ttfb_ms is not None


async def test_retries_429_then_503_with_exponential_backoff(make_client, sleeps):
    ok = fixture_response("doc_translate.json")
    client, rec = make_client(
        [httpx.Response(429), httpx.Response(503), httpx.Response(429), ok], backoff_base=0.5
    )
    out = await client.translate("Go2 arrived at V-01. Radar 64 bpm.", "en-IN", "mr-IN")
    assert out.startswith("Go2 V-01")
    assert len(rec.requests) == 4
    assert sleeps == [0.5, 1.0, 2.0]
    assert client.last["translate"].attempts == 4
    assert client.meter.counts["translate"] == 1  # one charge, not four


async def test_gives_up_after_three_retries(make_client, sleeps):
    client, rec = make_client([httpx.Response(429, json={"error": {"code": "rate_limit"}})])
    with pytest.raises(SarvamError) as ei:
        await client.translate("x", "en-IN", "hi-IN")
    assert ei.value.status == 429
    assert len(rec.requests) == 4  # 1 + max_retries(3)
    assert len(sleeps) == 3
    assert client.meter.total == 0


async def test_retry_after_header_is_honoured(make_client, sleeps):
    client, _ = make_client(
        [httpx.Response(429, headers={"retry-after": "3"}), fixture_response("doc_translate.json")]
    )
    await client.translate("x", "en-IN", "hi-IN")
    assert sleeps == [3.0]


async def test_402_is_quota_exhausted_and_not_retried_or_billed(make_client, sleeps):
    client, rec = make_client([fixture_response("recorded_402_translate.json")])
    with pytest.raises(QuotaExhausted) as ei:
        await client.translate("Go2 arrived at V-01.", "en-IN", "mr-IN")
    assert ei.value.status == 402
    assert ei.value.code == "insufficient_quota_error"
    assert ei.value.request_id.startswith("20261031_")
    assert len(rec.requests) == 1 and sleeps == []
    assert client.meter.total == 0


async def test_400_is_not_retried(make_client):
    client, rec = make_client([fixture_response("recorded_400_reasoning_effort.json")])
    with pytest.raises(SarvamError) as ei:
        await client.chat([{"role": "user", "content": "x"}])
    assert ei.value.status == 400 and "low" in str(ei.value)
    assert len(rec.requests) == 1


async def test_connect_errors_are_retried_then_unavailable(make_client, sleeps):
    def boom(request):
        raise httpx.ConnectError("refused", request=request)

    client, rec = make_client([boom])
    with pytest.raises(SarvamUnavailable):
        await client.tts("नमस्ते", "hi-IN", "priya")
    assert len(rec.requests) == 4 and len(sleeps) == 3


async def test_no_key_is_unavailable_without_a_request(make_client):
    client, rec = make_client([fixture_response("doc_tts.json")], api_key=None)
    assert not client.available
    with pytest.raises(SarvamUnavailable):
        await client.tts("x", "en-IN", "shubh")
    assert rec.requests == []


def test_key_comes_from_settings_as_secret(monkeypatch):
    monkeypatch.setenv("SARVAM_API_KEY", "sk-from-env")
    client = SarvamClient.from_settings(Settings(_env_file=None))
    assert client.available
    assert "sk-from-env" not in repr(client.__dict__)


async def test_tts_decodes_audio_and_meters_chars(make_client):
    client, rec = make_client([fixture_response("doc_tts.json")])
    wav = await client.tts("Help is on the way.", "en-IN", "shubh")
    assert wav[:4] == b"RIFF" and wav_duration_s(wav) > 0
    body = rec.json_bodies()[0]
    assert body == {
        "text": "Help is on the way.",
        "language_code": "en-IN",
        "speaker": "shubh",
        "model": "bulbul:v3",
        "speech_sample_rate": 22050,
        "output_audio_codec": "wav",
    }
    assert client.meter.by_kind["tts"] == pytest.approx(19 / 1000 * 3)


async def test_stt_rest_multipart(make_client):
    from skypaw.voice.client import pcm_to_wav

    client, rec = make_client([fixture_response("doc_stt_rest.json")])
    wav = pcm_to_wav(b"\0\0" * 16000 * 3)
    out = await client.stt_rest(wav, keyterms=["V-01", "Go2"])
    assert out["transcript"]
    req = rec.requests[0]
    assert req.url.path == "/speech-to-text"
    assert b'name="model"' in req.content and b"saaras:v4" in req.content
    assert b"codemix" in req.content and b'["V-01", "Go2"]' in req.content
    assert client.meter.by_kind["stt_rest"] == pytest.approx(3 / 60 * 0.49)


async def test_rate_limiter_waits_when_window_full():
    now = [0.0]
    slept: list[float] = []

    async def sleep(s):
        slept.append(s)
        now[0] += s

    lim = RateLimiter(3, 60.0, clock=lambda: now[0], sleep=sleep)
    for _ in range(3):
        assert await lim.acquire() == 0
    now[0] = 10.0
    assert await lim.acquire() == pytest.approx(50.0)
    assert slept == [pytest.approx(50.0)]


def test_audio_fixture_is_base64_wav():
    body = load_fixture("doc_tts.json")["body"]
    assert base64.b64decode(body["audios"][0])[:4] == b"RIFF"
