"""Translate cache: SQLite persistence, phrase tables, dedupe, batching, request shape."""

from __future__ import annotations

import asyncio

import pytest
from voice_helpers import fixture_response

from skypaw.voice import translate
from skypaw.voice.client import FakeSarvam, QuotaExhausted
from skypaw.voice.phrases import EVAC, LEAD_SAMPLES

TEXT = "Go2 arrived at V-01. Radar 64 bpm."


async def test_request_shape(make_client, tmp_path):
    client, rec = make_client([fixture_response("doc_translate.json")])
    cache = translate.TranslateCache(client, tmp_path / "cache.db")
    out = await cache.translate(TEXT, "en-IN", "mr-IN")
    assert out == "Go2 V-01 वर पोहोचला. रडार 64 bpm."
    assert rec.json_bodies()[0] == {
        "input": TEXT,
        "source_language_code": "en-IN",
        "target_language_code": "mr-IN",
        "model": "sarvam-translate:v1",
    }
    await cache.close()


async def test_cache_survives_restart(tmp_path):
    fake = FakeSarvam(translate=[{"translated_text": "अनुवाद"}])
    db = tmp_path / "cache.db"
    c1 = translate.TranslateCache(fake, db)
    assert await c1.translate(TEXT, "en-IN", "hi-IN") == "अनुवाद"
    assert await c1.translate(f"  {TEXT}  ", "en-IN", "hi-IN") == "अनुवाद"
    await c1.close()
    c2 = translate.TranslateCache(fake, db)
    assert await c2.translate(TEXT, "en-IN", "hi-IN") == "अनुवाद"
    await c2.close()
    assert len(fake.calls) == 1
    # keyed by (src, dst, sha): another target is a new call
    c3 = translate.TranslateCache(fake, db)
    await c3.translate(TEXT, "en-IN", "ta-IN")
    await c3.close()
    assert len(fake.calls) == 2


async def test_phrase_tables_need_no_call(tmp_path):
    fake = FakeSarvam()
    cache = translate.TranslateCache(fake, tmp_path / "c.db")
    assert await cache.translate(EVAC["en-IN"], "en-IN", "ta-IN") == EVAC["ta-IN"]
    s = LEAD_SAMPLES[1]
    assert await cache.translate(s.text, s.lang, "mr-IN") == s.tr["mr-IN"]
    assert await cache.translate("same", "hi-IN", "hi-IN") == "same"
    assert fake.calls == []
    await cache.close()


async def test_concurrent_identical_requests_share_one_call(tmp_path):
    fake = FakeSarvam()
    cache = translate.TranslateCache(fake, tmp_path / "c.db")
    outs = await asyncio.gather(*(cache.translate(TEXT, "en-IN", "hi-IN") for _ in range(6)))
    assert len(set(outs)) == 1 and len(fake.calls) == 1
    await cache.close()


async def test_translate_all_four_languages(tmp_path):
    fake = FakeSarvam()
    cache = translate.TranslateCache(fake, tmp_path / "c.db")
    out = await cache.translate_all(TEXT, "en-IN")
    assert list(out) == ["en-IN", "hi-IN", "mr-IN", "ta-IN"]
    assert out["en-IN"] == TEXT and out["mr-IN"] == f"[mr-IN] {TEXT}"
    assert len(fake.calls) == 3
    await cache.translate_all(TEXT, "en-IN")
    assert len(fake.calls) == 3
    await cache.close()


async def test_translate_all_keeps_original_on_failure(tmp_path):
    fake = FakeSarvam(translate=[QuotaExhausted("402", status=402)] * 3)
    cache = translate.TranslateCache(fake, tmp_path / "c.db")
    out = await cache.translate_all(TEXT, "en-IN")
    assert set(out.values()) == {TEXT}
    await cache.close()


async def test_failures_are_not_cached(tmp_path):
    fake = FakeSarvam(translate=[QuotaExhausted("402", status=402)])
    cache = translate.TranslateCache(fake, tmp_path / "c.db")
    with pytest.raises(QuotaExhausted):
        await cache.translate(TEXT, "en-IN", "hi-IN")
    assert (await cache.translate(TEXT, "en-IN", "hi-IN")).startswith("[hi-IN]")
    await cache.close()


async def test_translate_many_dedupes(tmp_path):
    fake = FakeSarvam()
    cache = translate.TranslateCache(fake, tmp_path / "c.db")
    out = await cache.translate_many(["a b", "c", "a  b", "c"], "en-IN", "ta-IN")
    assert out == ["[ta-IN] a b", "[ta-IN] c", "[ta-IN] a b", "[ta-IN] c"]
    assert len(fake.calls) == 2
    await cache.close()


def test_split_input_respects_limit():
    text = ("Sentence number one is here. " * 120).strip()
    parts = translate.split_input(text, 2000)
    assert all(len(p) <= 2000 for p in parts) and len(parts) == 2
    assert " ".join(parts) == text
    assert translate.split_input("x" * 4500, 2000) == ["x" * 2000, "x" * 2000, "x" * 500]


async def test_long_text_is_translated_in_pieces(tmp_path):
    fake = FakeSarvam()
    cache = translate.TranslateCache(fake, tmp_path / "c.db")
    await cache.translate(("Hold position. " * 200).strip(), "en-IN", "hi-IN")
    assert len(fake.calls) == 2
    assert all(len(c[1]["text"]) <= 2000 for c in fake.calls)
    await cache.close()
