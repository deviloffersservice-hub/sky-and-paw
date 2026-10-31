"""Go2 speaker: Bulbul v3 text to speech with an on-disk cache, and the ``/tts/{sha}.wav`` route.

Call (RESEARCH §2.4, SPEC §3.1/§6.10): ``POST /text-to-speech`` with ``text``, ``language_code``,
``speaker``, ``model=bulbul:v3``, ``speech_sample_rate=22050``, ``output_audio_codec=wav``;
the response carries base64 WAV in ``audios[0]``.

Cache: ``DATA_DIR/tts/<sha>.wav`` with sha = sha256("model|lang|speaker|text") over the
whitespace-normalised text, so the same string is never synthesised twice (concurrent requests
for one key share a single call). ``speak()`` returns the URL ``/tts/<sha>.wav``.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from skypaw.logging import get_logger, log_event
from skypaw.voice.client import MODEL_TTS, QuotaExhausted, SarvamUnavailable
from skypaw.voice.phrases import all_speakable

log = get_logger(__name__)

SAMPLE_RATE = 22050
SPEAKERS: dict[str, str] = {
    "hi-IN": "priya",
    "mr-IN": "rohan",
    "ta-IN": "kavitha",
    "en-IN": "shubh",
}
#: bulbul:v3 languages (RESEARCH §2.4); speakers outside the four team languages use shubh.
TTS_LANGS = frozenset(
    {"hi-IN", "bn-IN", "ta-IN", "te-IN", "gu-IN", "kn-IN", "ml-IN", "mr-IN", "pa-IN", "od-IN"}
    | {"en-IN"}
)
MAX_CHARS = 2500
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


def normalise_text(text: str) -> str:
    return " ".join(text.split())


def speaker_for(lang: str) -> str:
    return SPEAKERS.get(lang, "shubh")


def cache_key(text: str, lang: str, speaker: str | None = None, model: str = MODEL_TTS) -> str:
    speaker = speaker or speaker_for(lang)
    raw = f"{model}|{lang}|{speaker}|{normalise_text(text)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def url_for(sha: str) -> str:
    return f"/tts/{sha}.wav"


def tts_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "tts"


class TtsCache:
    """Synthesise once per (model, language, speaker, text); serve from disk afterwards."""

    def __init__(self, client: Any, data_dir: Path, *, sample_rate: int = SAMPLE_RATE) -> None:
        self.client = client
        self.dir = tts_dir(data_dir)
        self.sample_rate = sample_rate
        self._inflight: dict[str, asyncio.Future[str]] = {}
        self.hits = 0
        self.misses = 0

    def path_for(self, sha: str) -> Path:
        return self.dir / f"{sha}.wav"

    async def speak(self, text: str, lang: str) -> str:
        text = normalise_text(text)
        if not text:
            raise ValueError("empty text")
        if len(text) > MAX_CHARS:
            raise ValueError(f"text longer than {MAX_CHARS} chars")
        if lang not in TTS_LANGS:
            raise ValueError(f"bulbul:v3 does not speak {lang}")
        speaker = speaker_for(lang)
        sha = cache_key(text, lang, speaker)
        if self.path_for(sha).is_file():
            self.hits += 1
            return url_for(sha)
        pending = self._inflight.get(sha)
        if pending is not None:
            self.hits += 1
            return await asyncio.shield(pending)
        fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._inflight[sha] = fut
        try:
            self.misses += 1
            wav = await self.client.tts(
                text, lang, speaker, model=MODEL_TTS, sample_rate=self.sample_rate
            )
            self._write(sha, wav)
            log_event(log, "tts_cached", lang=lang, speaker=speaker, chars=len(text), sha=sha[:12])
            fut.set_result(url_for(sha))
        except asyncio.CancelledError:
            fut.cancel()
            raise
        except Exception as exc:
            fut.set_exception(exc)
            fut.exception()  # mark retrieved: waiters (if any) re-raise it
            raise
        finally:
            self._inflight.pop(sha, None)
        return fut.result()

    def _write(self, sha: str, wav: bytes) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        final = self.path_for(sha)
        tmp = final.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_bytes(wav)
        os.replace(tmp, final)

    async def prewarm(self) -> dict[str, int]:
        """Synthesise every scripted phrase (and the evacuation line) in the 4 languages once."""
        before_hits, before_misses = self.hits, self.misses
        errors = 0
        for text, lang in all_speakable():
            try:
                await self.speak(text, lang)
            except Exception as exc:  # noqa: BLE001 - prewarm is best effort
                errors += 1
                log_event(log, "tts_prewarm_failed", lang=lang, error=type(exc).__name__)
                if isinstance(exc, QuotaExhausted | SarvamUnavailable):
                    break  # every further call would be refused too
        return {
            "synthesised": self.misses - before_misses,
            "cached": self.hits - before_hits,
            "errors": errors,
        }


# ---------------------------------------------------------------- route

router = APIRouter()


def _data_dir(request: Request) -> Path:
    settings = getattr(request.app.state, "settings", None)
    if settings is None:
        from skypaw.config import get_settings

        settings = get_settings()
    return Path(settings.data_dir)


@router.get("/tts/{sha}.wav", include_in_schema=False)
async def get_tts(sha: str, request: Request) -> FileResponse:
    """Serve a cached Bulbul WAV. Only 64-hex names inside DATA_DIR/tts are served."""
    if not _SHA_RE.fullmatch(sha):
        raise HTTPException(status_code=404)
    base = tts_dir(_data_dir(request)).resolve()
    path = (base / f"{sha}.wav").resolve()
    if path.parent != base or not path.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(
        path,
        media_type="audio/wav",
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )
