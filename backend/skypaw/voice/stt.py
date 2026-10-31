"""Speech to text: the realtime relay (browser mic → Sarvam socket) and the REST fallback.

Realtime (RESEARCH §2.2, SPEC §3.1/§6.2): ``wss://api.sarvam.ai/speech-to-text-realtime/ws``
with ``language_code`` (``auto`` or ``xx-IN``; Odia is ``or-IN`` here only),
``model=saaras:v3-realtime``, ``stream_type=fast``, ``mode=codemix``, ``encoding=linear16``,
``sample_rate=16000``. Client events: ``audio_input{audio: base64}`` (3200-byte chunks = 100 ms),
``flush``, ``end``, ``ping``. Server events (sarvamai 0.1.41 types): ``session.begin``,
``transcript.partial{utterance_idx,text,language}``, ``transcript.final{...,language_confidence}``,
``session.end{audio_duration_s}``, ``error{code,message,is_fatal,status_code}``, ``pong``.
Out of credits arrives as ``error`` (``quota_exceeded``, 402) *before* ``session.begin``, then
close 1003 (docs/evidence/day1/sarvam.md).

REST fallback: ``POST /speech-to-text`` with ``saaras:v4``, ``mode=codemix`` and keyterms.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import json
import time
import wave
from collections.abc import AsyncIterator
from typing import Any

from skypaw.logging import get_logger, log_event
from skypaw.voice.client import (
    MODEL_STT_REALTIME,
    MODEL_STT_REST,
    QuotaExhausted,
    RealtimeClosed,
    RealtimeUnavailable,
    SarvamError,
    pcm_to_wav,
)

log = get_logger(__name__)

SAMPLE_RATE = 16000
CHUNK_BYTES = 3200  # 100 ms of int16 mono at 16 kHz
BYTES_PER_S = SAMPLE_RATE * 2
REST_MAX_S = 29.0  # REST accepts ≤30 s per request (422 above)
KEYTERMS: list[str] = ["V-01", "V-02", "V-03", "Go2", "drone"]
REALTIME_LANGS = frozenset(
    {"hi-IN", "mr-IN", "ta-IN", "en-IN", "bn-IN", "te-IN", "gu-IN", "kn-IN", "ml-IN", "pa-IN"}
)


class SttError(SarvamError):
    """A fatal error on the realtime socket after the session began."""


def realtime_language(lang: str | None) -> str:
    if not lang or lang == "auto":
        return "auto"
    if lang in {"od-IN", "or-IN"}:
        return "or-IN"  # the realtime socket's Odia code (od-IN everywhere else)
    return lang if lang in REALTIME_LANGS else "auto"


def realtime_params(lang: str | None, *, stream_type: str = "fast") -> dict[str, str]:
    return {
        "language_code": realtime_language(lang),
        "model": MODEL_STT_REALTIME,
        "stream_type": stream_type,
        "mode": "codemix",
        "encoding": "linear16",
        "sample_rate": str(SAMPLE_RATE),
    }


async def rechunk(source: AsyncIterator[bytes], size: int = CHUNK_BYTES) -> AsyncIterator[bytes]:
    """Re-slice arbitrary PCM frames into ``size``-byte chunks (the tail may be shorter)."""
    buf = bytearray()
    async for frame in source:
        buf.extend(frame)
        while len(buf) >= size:
            yield bytes(buf[:size])
            del buf[:size]
    if buf:
        yield bytes(buf)


def _lang_of(ev: dict[str, Any], requested: str) -> str | None:
    lang = ev.get("language") or ev.get("language_code")
    if lang:
        return str(lang)
    return None if requested == "auto" else requested


def _is_quota(ev: dict[str, Any]) -> bool:
    return ev.get("status_code") == 402 or ev.get("code") in {
        "quota_exceeded",
        "insufficient_quota_error",
    }


async def transcribe_stream(
    client: Any,
    pcm_chunks: AsyncIterator[bytes],
    lang: str | None = "auto",
    *,
    ping_interval: float = 20.0,
    begin_timeout: float = 10.0,
    final_timeout: float = 10.0,
    stream_type: str = "fast",
) -> AsyncIterator[dict[str, Any]]:
    """Relay PCM to the realtime socket; yield ``{"kind": "partial"|"final", "text", "lang"}``.

    Audio is sent only after ``session.begin``. When ``pcm_chunks`` ends (mic released) the
    relay sends ``flush`` and ``end``, then waits up to ``final_timeout`` for the last final.

    Raises ``QuotaExhausted`` for a 402 before the session began, ``RealtimeUnavailable`` when
    the socket is refused or closes before ``session.begin`` (callers fall back to REST), and
    ``SttError`` for a fatal error mid-session.
    """
    params = realtime_params(lang, stream_type=stream_type)
    requested = params["language_code"]
    t0 = time.perf_counter()
    sent = {"bytes": 0}
    billed: dict[str, float] = {}
    begun = asyncio.Event()
    q: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

    async with client.connect_realtime(params) as ws:

        async def reader() -> None:
            try:
                while True:
                    raw = await ws.recv()
                    try:
                        await q.put(("event", json.loads(raw)))
                    except ValueError:
                        log_event(log, "stt_bad_frame", size=len(raw))
            except RealtimeClosed as exc:
                await q.put(("closed", exc))
            except Exception as exc:  # noqa: BLE001 - surfaced to the consumer below
                await q.put(("closed", RealtimeClosed(None, f"{type(exc).__name__}")))

        async def sender() -> None:
            try:
                await begun.wait()
                async for chunk in rechunk(pcm_chunks):
                    b64 = base64.b64encode(chunk).decode()
                    await ws.send(json.dumps({"event": "audio_input", "audio": b64}))
                    sent["bytes"] += len(chunk)
                await ws.send(json.dumps({"event": "flush"}))
                await ws.send(json.dumps({"event": "end"}))
                await q.put(("ended", None))
            except RealtimeClosed:
                return  # the reader reports the close
            except Exception as exc:  # noqa: BLE001
                await q.put(("sender_error", exc))

        async def pinger() -> None:
            with contextlib.suppress(RealtimeClosed):
                while True:
                    await asyncio.sleep(ping_interval)
                    await ws.send(json.dumps({"event": "ping"}))

        tasks = [asyncio.create_task(f()) for f in (reader, sender, pinger)]
        ended = False
        last_partial = None
        try:
            while True:
                timeout = begin_timeout if not begun.is_set() else final_timeout if ended else None
                try:
                    kind, item = await asyncio.wait_for(q.get(), timeout)
                except TimeoutError:
                    if not begun.is_set():
                        raise RealtimeUnavailable("no session.begin from realtime socket") from None
                    log_event(log, "stt_final_timeout")
                    break
                if kind == "ended":
                    ended = True
                    continue
                if kind == "sender_error":
                    raise item
                if kind == "closed":
                    if not begun.is_set():
                        if item.code == 1003:
                            raise QuotaExhausted(f"realtime closed 1003: {item.reason}", status=402)
                        raise RealtimeUnavailable(
                            f"realtime closed before session.begin: {item.code} {item.reason}",
                            status=item.code,
                        )
                    break
                ev: dict[str, Any] = item
                name = ev.get("event") or ev.get("type")
                if name == "session.begin":
                    begun.set()
                    log_event(log, "stt_session_begin", ms=round((time.perf_counter() - t0) * 1000))
                elif name == "transcript.partial":
                    text = str(ev.get("text") or "")
                    if text and text != last_partial:
                        last_partial = text
                        yield {
                            "kind": "partial",
                            "text": text,
                            "lang": _lang_of(ev, requested),
                            "utterance": ev.get("utterance_idx", 0),
                        }
                elif name == "transcript.final":
                    last_partial = None
                    yield {
                        "kind": "final",
                        "text": str(ev.get("text") or ""),
                        "lang": _lang_of(ev, requested),
                        "utterance": ev.get("utterance_idx", 0),
                        "confidence": ev.get("language_confidence"),
                    }
                elif name == "session.end":
                    secs = ev.get("audio_duration_s")
                    if secs is not None:
                        billed["s"] = float(secs)
                    break
                elif name == "error":
                    msg = f"{ev.get('code')}: {ev.get('message')}"
                    if not begun.is_set():
                        if _is_quota(ev):
                            raise QuotaExhausted(msg, status=402, code=ev.get("code"))
                        raise RealtimeUnavailable(msg, status=ev.get("status_code"))
                    if ev.get("is_fatal", True):
                        raise SttError(msg, status=ev.get("status_code"), code=ev.get("code"))
                    log_event(log, "stt_error_nonfatal", code=ev.get("code"))
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if begun.is_set():
                secs = billed.get("s", sent["bytes"] / BYTES_PER_S)
                client.record_realtime(secs, (time.perf_counter() - t0) * 1000)


def _split_wav(wav: bytes, max_s: float = REST_MAX_S) -> list[bytes]:
    with wave.open(io.BytesIO(wav)) as w:
        rate, width, channels = w.getframerate(), w.getsampwidth(), w.getnchannels()
        frames = w.readframes(w.getnframes())
    step = int(max_s * rate) * width * channels
    if len(frames) <= step:
        return [wav]
    out = []
    for i in range(0, len(frames), step):
        buf = io.BytesIO()
        with wave.open(buf, "wb") as o:
            o.setnchannels(channels)
            o.setsampwidth(width)
            o.setframerate(rate)
            o.writeframes(frames[i : i + step])
        out.append(buf.getvalue())
    return out


async def transcribe_clip(
    client: Any,
    wav_bytes: bytes,
    lang: str | None = None,
    *,
    keyterms: list[str] | None = None,
) -> dict[str, Any]:
    """REST fallback: transcribe a WAV clip (split into ≤29 s pieces). Returns a final event."""
    texts: list[str] = []
    detected: str | None = None
    for piece in _split_wav(wav_bytes):
        resp = await client.stt_rest(
            piece,
            language_code=None if lang in (None, "auto") else lang,
            model=MODEL_STT_REST,
            mode="codemix",
            keyterms=KEYTERMS if keyterms is None else keyterms,
        )
        if resp.get("transcript"):
            texts.append(str(resp["transcript"]).strip())
        detected = detected or resp.get("language_code")
    out_lang = detected or (None if lang in (None, "auto") else lang)
    return {"kind": "final", "text": " ".join(texts), "lang": out_lang, "utterance": 0}


def pcm_clip(pcm: bytes) -> bytes:
    """PCM (16 kHz int16 mono) → WAV for the REST fallback."""
    return pcm_to_wav(pcm, SAMPLE_RATE)
