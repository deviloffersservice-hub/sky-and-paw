"""``ports.Voice`` implementations: ``SarvamVoice`` (live, cached) and ``FakeVoice`` (offline).

Stream A wires one of them into the mission runtime::

    voice = SarvamVoice.from_settings(settings)        # or FakeVoice(data_dir=settings.data_dir)
    app.include_router(skypaw.voice.tts.router)        # serves /tts/<sha>.wav

``transcribe_stream`` yields ``{"kind": "partial"|"final", "text", "lang", "utterance"}``. It
raises ``QuotaExhausted`` when Sarvam has no credits; when the realtime socket is refused for
any other reason it buffers the audio (≤30 s) and transcribes it once with REST ``saaras:v4``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from skypaw.logging import get_logger, log_event
from skypaw.mission.ports import Plan
from skypaw.voice import planner, stt
from skypaw.voice.client import (
    CreditMeter,
    FakeSarvam,
    RealtimeUnavailable,
    SarvamClient,
    tone_wav,
)
from skypaw.voice.phrases import LEAD_SAMPLES, TEAM_LANGS, lookup
from skypaw.voice.settings import VoiceSettings
from skypaw.voice.translate import TranslateCache
from skypaw.voice.tts import TtsCache, cache_key, normalise_text, speaker_for, tts_dir, url_for

log = get_logger(__name__)

#: Keep at most this much audio for the REST fallback (REST takes ≤30 s per request).
FALLBACK_MAX_BYTES = int(stt.REST_MAX_S * stt.BYTES_PER_S)


class SarvamVoice:
    """Live Voice port: realtime STT relay, sarvam-105b planner, cached Bulbul, cached translate."""

    def __init__(
        self, client: Any, data_dir: Path, voice_settings: VoiceSettings | None = None
    ) -> None:
        self.client = client
        vs = voice_settings or VoiceSettings()
        self.plan_model = vs.sarvam_plan_model
        self.plan_fallback_model = vs.sarvam_plan_fallback_model
        self.data_dir = Path(data_dir)
        self.tts = TtsCache(client, self.data_dir)
        self.translator = TranslateCache(client, self.data_dir / "cache.db")

    @classmethod
    def from_settings(cls, settings: Any, **client_kw: Any) -> SarvamVoice:
        return cls(SarvamClient.from_settings(settings, **client_kw), settings.data_dir)

    @property
    def meter(self) -> CreditMeter:
        return self.client.meter

    async def aclose(self) -> None:
        await self.translator.close()
        await self.client.aclose()

    async def transcribe_stream(
        self, pcm_chunks: AsyncIterator[bytes], lang: str
    ) -> AsyncIterator[dict[str, Any]]:
        buffered = bytearray()

        async def tee() -> AsyncIterator[bytes]:
            async for chunk in pcm_chunks:
                buffered.extend(chunk)
                if len(buffered) > FALLBACK_MAX_BYTES:
                    del buffered[: len(buffered) - FALLBACK_MAX_BYTES]
                yield chunk

        source = tee()
        try:
            async for ev in stt.transcribe_stream(self.client, source, lang):
                yield ev
            return
        except RealtimeUnavailable as exc:
            log_event(log, "stt_fallback_rest", reason=str(exc)[:120], status=exc.status)
        # Socket refused before session.begin: the tee was never started, so drain the source.
        async for _ in source:
            pass
        if buffered:
            yield await stt.transcribe_clip(self.client, stt.pcm_clip(bytes(buffered)), lang)

    async def plan(self, text: str, lang: str) -> Plan:
        return await planner.plan(
            self.client,
            text,
            lang,
            model=self.plan_model,
            fallback_model=self.plan_fallback_model,
        )

    async def speak(self, text: str, lang: str) -> str:
        return await self.tts.speak(text, lang)

    async def translate(self, text: str, src: str, dst: str) -> str:
        return await self.translator.translate(text, src, dst)

    async def translate_all(self, text: str, src: str) -> dict[str, str]:
        return await self.translator.translate_all(text, src)

    async def prewarm(self) -> dict[str, int]:
        return await self.tts.prewarm()


class FakeVoice:
    """Instant offline Voice port for stream A's tests and the runtime fake.

    - ``transcribe_stream`` consumes the audio and yields one partial per word of ``script``
      (default: the first lead sample), then the final.
    - ``plan`` is the rule-based planner (confidence 0.3).
    - ``speak`` writes a short tone WAV under DATA_DIR/tts with the real cache key, so the
      ``/tts/<sha>.wav`` route serves it (no file is written when ``data_dir`` is None).
    - ``translate`` returns hand-checked phrase translations, else ``"[dst] text"``.
    """

    def __init__(
        self,
        data_dir: Path | None = None,
        *,
        script: str | None = None,
        script_lang: str | None = None,
    ) -> None:
        self.data_dir = Path(data_dir) if data_dir is not None else None
        sample = LEAD_SAMPLES[1]
        self.script = script if script is not None else sample.text
        self.script_lang = script_lang or sample.lang
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.meter = CreditMeter()

    async def aclose(self) -> None:
        return None

    async def transcribe_stream(
        self, pcm_chunks: AsyncIterator[bytes], lang: str
    ) -> AsyncIterator[dict[str, Any]]:
        self.calls.append(("transcribe_stream", (lang,)))
        out_lang = self.script_lang if lang in (None, "", "auto") else lang
        words = self.script.split()
        n = 0
        async for _chunk in pcm_chunks:
            if n < len(words):
                n += 1
                yield {
                    "kind": "partial",
                    "text": " ".join(words[:n]),
                    "lang": out_lang,
                    "utterance": 0,
                }
            await asyncio.sleep(0)
        yield {"kind": "final", "text": self.script, "lang": out_lang, "utterance": 0}

    async def plan(self, text: str, lang: str) -> Plan:
        self.calls.append(("plan", (text, lang)))
        return planner.rule_based_plan(text, lang)

    async def speak(self, text: str, lang: str) -> str:
        self.calls.append(("speak", (text, lang)))
        text = normalise_text(text)
        sha = cache_key(text, lang, speaker_for(lang))
        if self.data_dir is not None:
            path = tts_dir(self.data_dir) / f"{sha}.wav"
            if not path.is_file():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(tone_wav(min(2.0, 0.04 * len(text) + 0.2)))
        return url_for(sha)

    async def translate(self, text: str, src: str, dst: str) -> str:
        self.calls.append(("translate", (text, src, dst)))
        if src == dst:
            return normalise_text(text)
        return lookup(text, src, dst) or f"[{dst}] {normalise_text(text)}"

    async def translate_all(self, text: str, src: str) -> dict[str, str]:
        return {lang: await self.translate(text, src, lang) for lang in TEAM_LANGS}

    async def prewarm(self) -> dict[str, int]:
        return {"synthesised": 0, "cached": 0, "errors": 0}


def fake_sarvam_voice(data_dir: Path, **fake_kw: Any) -> SarvamVoice:
    """The real SarvamVoice pipeline over FakeSarvam (fixture responses, offline)."""
    return SarvamVoice(FakeSarvam(**fake_kw), data_dir, VoiceSettings(_env_file=None))


__all__ = ["FakeVoice", "SarvamVoice", "fake_sarvam_voice"]
