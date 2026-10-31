"""TTS cache (hit/miss, dedupe, prewarm) and the /tts/{sha}.wav route (path safety)."""

from __future__ import annotations

import asyncio
import hashlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from skypaw.voice import tts
from skypaw.voice.client import FakeSarvam, QuotaExhausted
from skypaw.voice.phrases import PHRASES


def test_cache_key_is_sha_of_model_lang_speaker_text():
    want = hashlib.sha256("bulbul:v3|mr-IN|rohan|मदत येत आहे.".encode()).hexdigest()
    assert tts.cache_key("  मदत   येत आहे. ", "mr-IN") == want
    assert tts.SPEAKERS == {
        "hi-IN": "priya",
        "mr-IN": "rohan",
        "ta-IN": "kavitha",
        "en-IN": "shubh",
    }


async def test_miss_then_hit(tmp_path):
    fake = FakeSarvam()
    cache = tts.TtsCache(fake, tmp_path)
    url = await cache.speak("Help is on the way.", "en-IN")
    sha = tts.cache_key("Help is on the way.", "en-IN")
    assert url == f"/tts/{sha}.wav"
    assert (tmp_path / "tts" / f"{sha}.wav").read_bytes()[:4] == b"RIFF"
    assert await cache.speak("Help is on   the way.", "en-IN") == url  # same normalised text
    assert [c[0] for c in fake.calls] == ["tts"]
    _, kw = fake.calls[0]
    assert kw["language_code"] == "en-IN" and kw["model"] == "bulbul:v3"
    assert kw["sample_rate"] == 22050
    assert (cache.misses, cache.hits) == (1, 1)
    # a new cache over the same dir hits the disk
    cache2 = tts.TtsCache(fake, tmp_path)
    assert await cache2.speak("Help is on the way.", "en-IN") == url
    assert len(fake.calls) == 1


async def test_concurrent_requests_share_one_call(tmp_path):
    fake = FakeSarvam()
    cache = tts.TtsCache(fake, tmp_path)
    urls = await asyncio.gather(*(cache.speak("मदद आ रही है।", "hi-IN") for _ in range(5)))
    assert len(set(urls)) == 1
    assert len(fake.calls) == 1


async def test_different_language_is_a_different_entry(tmp_path):
    fake = FakeSarvam()
    cache = tts.TtsCache(fake, tmp_path)
    a = await cache.speak("V-01", "hi-IN")
    b = await cache.speak("V-01", "mr-IN")
    assert a != b and len(fake.calls) == 2


async def test_errors_are_not_cached(tmp_path):
    fake = FakeSarvam(tts=[QuotaExhausted("402", status=402)])
    cache = tts.TtsCache(fake, tmp_path)
    with pytest.raises(QuotaExhausted):
        await cache.speak("Help", "en-IN")
    assert not list((tmp_path).glob("tts/*.wav"))
    assert (await cache.speak("Help", "en-IN")).endswith(".wav")


@pytest.mark.parametrize(("text", "lang"), [("", "en-IN"), ("x" * 2501, "en-IN"), ("hi", "fr-FR")])
async def test_rejects_bad_input(tmp_path, text, lang):
    with pytest.raises(ValueError):
        await tts.TtsCache(FakeSarvam(), tmp_path).speak(text, lang)


async def test_prewarm_synthesises_each_phrase_once(tmp_path):
    fake = FakeSarvam()
    cache = tts.TtsCache(fake, tmp_path)
    first = await cache.prewarm()
    n = (len(PHRASES) + 1) * 4  # 4 phrases + evacuation line, 4 languages
    assert first == {"synthesised": n, "cached": 0, "errors": 0}
    assert await cache.prewarm() == {"synthesised": 0, "cached": n, "errors": 0}
    assert len(fake.calls) == n
    speakers = {c[1]["language_code"]: c[1]["text"] for c in fake.calls}
    assert set(speakers) == {"hi-IN", "mr-IN", "ta-IN", "en-IN"}


async def test_prewarm_stops_on_quota(tmp_path):
    fake = FakeSarvam(tts=[QuotaExhausted("402", status=402)])
    out = await tts.TtsCache(fake, tmp_path).prewarm()
    assert out == {"synthesised": 1, "cached": 0, "errors": 1}
    assert len(fake.calls) == 1


@pytest.fixture
def route(settings):
    app = FastAPI()
    app.state.settings = settings
    app.include_router(tts.router)
    return TestClient(app), settings.data_dir


async def test_route_serves_cached_wav(route):
    client, data_dir = route
    url = await tts.TtsCache(FakeSarvam(), data_dir).speak("Stay calm.", "en-IN")
    r = client.get(url)
    assert r.status_code == 200
    assert r.headers["content-type"] == "audio/wav"
    assert r.content[:4] == b"RIFF"


@pytest.mark.parametrize(
    "path",
    [
        "/tts/" + "0" * 64 + ".wav",  # well-formed but missing
        "/tts/abc.wav",
        "/tts/" + "A" * 64 + ".wav",  # uppercase hex is not ours
        "/tts/..%2F..%2Fcache.db.wav",
        "/tts/%2e%2e%2fsecret.wav",
        "/tts/" + "0" * 64 + ".wav.bak",
    ],
)
def test_route_path_safety(route, path):
    client, data_dir = route
    (data_dir / "tts").mkdir(parents=True, exist_ok=True)
    (data_dir / "cache.db.wav").write_bytes(b"RIFFsecret")
    (data_dir / "secret.wav").write_bytes(b"RIFFsecret")
    r = client.get(path)
    assert r.status_code == 404
    assert b"secret" not in r.content
