"""The one Sarvam client: every Sarvam call in Sky & Paw goes through ``SarvamClient``.

Facts (docs/RESEARCH.md §2, §6; docs/evidence/day1/sarvam.md):
- REST base ``https://api.sarvam.ai``, header ``api-subscription-key``; bad key 403,
  no credits 402 ``insufficient_quota_error``, rate limits 429; 60 rpm per REST endpoint.
- Realtime STT socket ``wss://api.sarvam.ai/speech-to-text-realtime/ws`` with header auth.
- Prices (PAYG): STT ₹0.49/min, TTS bulbul:v3 ₹3/1k chars, translate ₹2/1k chars,
  sarvam-105b ₹15/M input + ₹60/M output tokens.

Transport choice: plain ``httpx`` + ``websockets`` (both pinned in backend/requirements*.txt;
``websockets==17.4`` arrives via ``sarvamai==0.1.41``) rather than the SDK, because the SDK's
``reasoning_effort`` literals are wrong (RESEARCH §2.3) and we need our own retries, timeouts,
first-byte timing and the credit meter around every call.

``FakeSarvam`` has the same public interface and answers from fixtures, instantly, offline.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import json
import math
import struct
import time
import wave
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol
from urllib.parse import urlencode

import httpx
from pydantic import SecretStr

from skypaw.logging import get_logger, log_event

log = get_logger(__name__)

SARVAM_BASE_URL = "https://api.sarvam.ai"
REALTIME_WS_URL = "wss://api.sarvam.ai/speech-to-text-realtime/ws"

#: Model identifiers (SPEC §3.1); the only planning model is sarvam-105b.
MODEL_PLAN = "sarvam-105b"
MODEL_STT_REALTIME = "saaras:v3-realtime"
MODEL_STT_REST = "saaras:v4"
MODEL_TTS = "bulbul:v3"
MODEL_TRANSLATE = "sarvam-translate:v1"

#: Rupee prices used by the credit meter (RESEARCH §2.2-2.5, PAYG).
RATES: dict[str, float] = {
    "stt_per_min": 0.49,
    "tts_per_1k_chars": 3.0,
    "translate_per_1k_chars": 2.0,
    "llm_in_per_m": 15.0,
    "llm_out_per_m": 60.0,
}

RETRY_STATUSES = frozenset({429, 503})
DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


# ---------------------------------------------------------------- errors


class SarvamError(RuntimeError):
    """A Sarvam call failed. ``status`` is the HTTP status (or None for transport errors)."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: str | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.request_id = request_id


class QuotaExhausted(SarvamError):
    """402 ``insufficient_quota_error`` / realtime ``quota_exceeded``: no credits left."""


class SarvamUnavailable(SarvamError):
    """No key configured, or the service could not be reached after retries."""


def _error_from_response(status: int, body: Any, request_id: str | None) -> SarvamError:
    err = body.get("error") if isinstance(body, dict) else None
    err = err if isinstance(err, dict) else {}
    message = str(err.get("message") or body)[:300]
    code = err.get("code")
    request_id = err.get("request_id") or request_id
    cls = QuotaExhausted if status == 402 else SarvamError
    return cls(
        f"Sarvam {status} {code or ''}: {message}".strip(),
        status=status,
        code=code,
        request_id=request_id,
    )


# ---------------------------------------------------------------- credit meter


@dataclass
class CallRecord:
    kind: str
    status: int
    latency_ms: float
    ttfb_ms: float | None = None
    rupees: float = 0.0
    units: dict[str, float] = field(default_factory=dict)
    attempts: int = 1


class CreditMeter:
    """Estimates rupees per call from usage and logs the cumulative spend.

    Only successful calls are charged (refused calls are not billed: evidence/day1).
    """

    def __init__(self, rates: dict[str, float] | None = None, history: int = 200) -> None:
        self.rates = dict(RATES if rates is None else rates)
        self.total = 0.0
        self.by_kind: dict[str, float] = defaultdict(float)
        self.counts: dict[str, int] = defaultdict(int)
        self.calls: deque[CallRecord] = deque(maxlen=history)

    # Estimators -------------------------------------------------------------
    def stt(self, seconds: float) -> float:
        return max(0.0, seconds) / 60.0 * self.rates["stt_per_min"]

    def tts(self, chars: int) -> float:
        return chars / 1000.0 * self.rates["tts_per_1k_chars"]

    def translate(self, chars: int) -> float:
        return chars / 1000.0 * self.rates["translate_per_1k_chars"]

    def llm(self, tokens_in: int, tokens_out: int) -> float:
        return (
            tokens_in / 1e6 * self.rates["llm_in_per_m"]
            + tokens_out / 1e6 * self.rates["llm_out_per_m"]
        )

    # Bookkeeping ------------------------------------------------------------
    def record(self, rec: CallRecord) -> CallRecord:
        self.total += rec.rupees
        self.by_kind[rec.kind] += rec.rupees
        self.counts[rec.kind] += 1
        self.calls.append(rec)
        log_event(
            log,
            "sarvam_spend",
            kind=rec.kind,
            status=rec.status,
            latency_ms=round(rec.latency_ms),
            rupees=round(rec.rupees, 4),
            total_rupees=round(self.total, 4),
            **{f"u_{k}": v for k, v in rec.units.items()},
        )
        return rec

    def snapshot(self) -> dict[str, Any]:
        return {
            "total_rupees": round(self.total, 4),
            "by_kind": {k: round(v, 4) for k, v in self.by_kind.items()},
            "counts": dict(self.counts),
        }


# ---------------------------------------------------------------- rate limiting


class RateLimiter:
    """Sliding-window limiter: at most ``limit`` acquisitions per ``window`` seconds."""

    def __init__(
        self,
        limit: int = 60,
        window: float = 60.0,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.limit, self.window = limit, window
        self._clock, self._sleep = clock, sleep
        self._stamps: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> float:
        """Wait for a slot; returns the seconds waited."""
        waited = 0.0
        async with self._lock:
            while True:
                now = self._clock()
                while self._stamps and now - self._stamps[0] >= self.window:
                    self._stamps.popleft()
                if len(self._stamps) < self.limit:
                    self._stamps.append(now)
                    return waited
                delay = self.window - (now - self._stamps[0])
                waited += delay
                await self._sleep(delay)


# ---------------------------------------------------------------- audio helpers


def pcm_to_wav(pcm: bytes, sample_rate: int = 16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


def wav_duration_s(wav: bytes) -> float:
    try:
        with wave.open(io.BytesIO(wav)) as w:
            return w.getnframes() / float(w.getframerate())
    except (wave.Error, EOFError):
        return 0.0


def tone_wav(seconds: float, sample_rate: int = 22050, freq: float = 440.0) -> bytes:
    """A short quiet tone: the fake's stand-in for synthesised speech."""
    n = max(1, int(seconds * sample_rate))
    amp = 0.1 * 32767
    pcm = b"".join(
        struct.pack("<h", int(amp * math.sin(2 * math.pi * freq * i / sample_rate)))
        for i in range(n)
    )
    return pcm_to_wav(pcm, sample_rate)


# ---------------------------------------------------------------- realtime socket


class RealtimeSocket(Protocol):
    """The subset of a websockets connection that skypaw.voice.stt uses."""

    async def send(self, message: str) -> None: ...
    async def recv(self) -> str | bytes: ...
    async def close(self) -> None: ...


class RealtimeClosed(Exception):
    """The realtime socket closed; ``code``/``reason`` from the close frame when known."""

    def __init__(self, code: int | None, reason: str = "") -> None:
        super().__init__(f"closed {code} {reason}".strip())
        self.code, self.reason = code, reason


class RealtimeUnavailable(SarvamError):
    """The realtime socket could not be used (handshake refused, closed before session.begin)."""


class _WsAdapter:
    """Wraps a websockets connection: ``recv`` raises RealtimeClosed instead of library errors."""

    def __init__(self, ws: Any) -> None:
        self._ws = ws

    async def send(self, message: str) -> None:
        import websockets

        try:
            await self._ws.send(message)
        except websockets.ConnectionClosed as exc:
            raise RealtimeClosed(*_close_info(exc)) from exc

    async def recv(self) -> str | bytes:
        import websockets

        try:
            return await self._ws.recv()
        except websockets.ConnectionClosed as exc:
            raise RealtimeClosed(*_close_info(exc)) from exc

    async def close(self) -> None:
        await self._ws.close()


def _close_info(exc: Any) -> tuple[int | None, str]:
    rcvd = getattr(exc, "rcvd", None)
    return (rcvd.code, rcvd.reason) if rcvd is not None else (None, "")


# ---------------------------------------------------------------- the client


class SarvamClient:
    """Async Sarvam client with retries (429/503, exponential backoff), timeouts and metering."""

    def __init__(
        self,
        api_key: SecretStr | str | None,
        *,
        base_url: str = SARVAM_BASE_URL,
        ws_url: str = REALTIME_WS_URL,
        timeout: httpx.Timeout = DEFAULT_TIMEOUT,
        max_retries: int = 3,
        backoff_base: float = 0.5,
        backoff_cap: float = 8.0,
        rpm: int = 60,
        meter: CreditMeter | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if isinstance(api_key, str):
            api_key = SecretStr(api_key)
        self._key = api_key if api_key and api_key.get_secret_value() else None
        self.base_url = base_url.rstrip("/")
        self.ws_url = ws_url
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        self.meter = meter or CreditMeter()
        self._sleep = sleep
        self._limiters: dict[str, RateLimiter] = defaultdict(lambda: RateLimiter(rpm, 60.0))
        self._http = httpx.AsyncClient(base_url=self.base_url, timeout=timeout, transport=transport)
        self.last: dict[str, CallRecord] = {}

    @classmethod
    def from_settings(cls, settings: Any, **kw: Any) -> SarvamClient:
        return cls(getattr(settings, "sarvam_api_key", None), **kw)

    @property
    def available(self) -> bool:
        return self._key is not None

    def _headers(self) -> dict[str, str]:
        if self._key is None:
            raise SarvamUnavailable("SARVAM_API_KEY is not set")
        return {"api-subscription-key": self._key.get_secret_value()}

    async def aclose(self) -> None:
        await self._http.aclose()

    def _backoff(self, attempt: int, retry_after: str | None) -> float:
        delay = min(self.backoff_cap, self.backoff_base * (2**attempt))
        if retry_after:
            with contextlib.suppress(ValueError):
                delay = min(self.backoff_cap, max(delay, float(retry_after)))
        return delay

    async def _post(
        self,
        kind: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], CallRecord]:
        headers = self._headers()
        await self._limiters[kind].acquire()
        t0 = time.perf_counter()
        attempt = 0
        while True:
            ttfb: float | None = None
            try:
                req = self._http.build_request(
                    "POST", path, json=json_body, files=files, data=data, headers=headers
                )
                resp = await self._http.send(req, stream=True)
                ttfb = (time.perf_counter() - t0) * 1000
                try:
                    raw = await resp.aread()
                finally:
                    await resp.aclose()
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
                # The request never reached the server: safe to retry without double billing.
                if attempt < self.max_retries:
                    await self._sleep(self._backoff(attempt, None))
                    attempt += 1
                    continue
                raise SarvamUnavailable(f"Sarvam unreachable: {type(exc).__name__}") from exc
            except httpx.TimeoutException as exc:
                raise SarvamUnavailable(f"Sarvam timed out: {type(exc).__name__}") from exc
            except httpx.HTTPError as exc:
                raise SarvamUnavailable(f"Sarvam transport error: {type(exc).__name__}") from exc

            if resp.status_code in RETRY_STATUSES and attempt < self.max_retries:
                delay = self._backoff(attempt, resp.headers.get("retry-after"))
                log_event(
                    log,
                    "sarvam_retry",
                    kind=kind,
                    status=resp.status_code,
                    attempt=attempt + 1,
                    delay_s=delay,
                )
                await self._sleep(delay)
                attempt += 1
                continue
            break

        latency = (time.perf_counter() - t0) * 1000
        try:
            body = json.loads(raw) if raw else {}
        except ValueError:
            body = {"raw": raw[:300].decode(errors="replace")}
        rec = CallRecord(kind, resp.status_code, latency, ttfb, attempts=attempt + 1)
        self.last[kind] = rec
        if resp.status_code >= 400:
            log_event(log, "sarvam_error", kind=kind, status=resp.status_code)
            raise _error_from_response(resp.status_code, body, resp.headers.get("x-request-id"))
        return body, rec

    # Endpoints --------------------------------------------------------------
    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        response_format: dict[str, Any] | None = None,
        reasoning_effort: str | None = "low",
        max_tokens: int = 1024,
        temperature: float | None = 0.0,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"model": MODEL_PLAN, "messages": messages, "max_tokens": max_tokens}
        if reasoning_effort is not None:
            body["reasoning_effort"] = reasoning_effort
        if response_format is not None:
            body["response_format"] = response_format
        if temperature is not None:
            body["temperature"] = temperature
        resp, rec = await self._post("llm", "/v1/chat/completions", json_body=body)
        usage = resp.get("usage") or {}
        t_in = int(usage.get("prompt_tokens") or sum(len(m["content"]) for m in messages) // 3)
        t_out = int(usage.get("completion_tokens") or max_tokens)
        rec.units = {"tokens_in": t_in, "tokens_out": t_out}
        rec.rupees = self.meter.llm(t_in, t_out)
        self.meter.record(rec)
        return resp

    async def tts(
        self,
        text: str,
        language_code: str,
        speaker: str,
        *,
        model: str = MODEL_TTS,
        sample_rate: int = 22050,
        codec: str = "wav",
    ) -> bytes:
        body = {
            "text": text,
            "language_code": language_code,
            "speaker": speaker,
            "model": model,
            "speech_sample_rate": sample_rate,
            "output_audio_codec": codec,
        }
        resp, rec = await self._post("tts", "/text-to-speech", json_body=body)
        audios = resp.get("audios") or []
        if not audios:
            raise SarvamError("TTS returned no audio", status=rec.status)
        rec.units = {"chars": len(text)}
        rec.rupees = self.meter.tts(len(text))
        self.meter.record(rec)
        return base64.b64decode("".join(audios))

    async def translate(
        self, text: str, src: str, dst: str, *, model: str = MODEL_TRANSLATE
    ) -> str:
        body = {
            "input": text,
            "source_language_code": src,
            "target_language_code": dst,
            "model": model,
        }
        resp, rec = await self._post("translate", "/translate", json_body=body)
        rec.units = {"chars": len(text)}
        rec.rupees = self.meter.translate(len(text))
        self.meter.record(rec)
        return str(resp.get("translated_text", ""))

    async def stt_rest(
        self,
        wav: bytes,
        *,
        language_code: str | None = None,
        model: str = MODEL_STT_REST,
        mode: str = "codemix",
        keyterms: list[str] | None = None,
    ) -> dict[str, Any]:
        data: dict[str, Any] = {"model": model, "mode": mode}
        if language_code and language_code != "auto":
            data["language_code"] = language_code
        if keyterms:
            data["keyterms"] = json.dumps(keyterms, ensure_ascii=False)
        files = {"file": ("clip.wav", wav, "audio/wav")}
        resp, rec = await self._post("stt_rest", "/speech-to-text", files=files, data=data)
        seconds = wav_duration_s(wav)
        rec.units = {"seconds": round(seconds, 3)}
        rec.rupees = self.meter.stt(seconds)
        self.meter.record(rec)
        return resp

    def record_realtime(self, seconds: float, latency_ms: float, status: int = 101) -> None:
        rec = CallRecord("stt", status, latency_ms, units={"seconds": round(seconds, 3)})
        rec.rupees = self.meter.stt(seconds)
        self.last["stt"] = rec
        self.meter.record(rec)

    def realtime_url(self, params: dict[str, str]) -> str:
        return f"{self.ws_url}?{urlencode(params)}"

    @contextlib.asynccontextmanager
    async def connect_realtime(
        self, params: dict[str, str], *, open_timeout: float = 15.0
    ) -> AsyncIterator[RealtimeSocket]:
        """Open the realtime STT socket (header auth). Raises RealtimeUnavailable if refused."""
        import websockets
        from websockets.asyncio.client import connect

        headers = self._headers()
        try:
            ws = await connect(
                self.realtime_url(params),
                additional_headers=headers,
                open_timeout=open_timeout,
                max_size=None,
            )
        except websockets.InvalidStatus as exc:
            status = exc.response.status_code
            cls = QuotaExhausted if status == 402 else RealtimeUnavailable
            raise cls(f"realtime handshake refused: {status}", status=status) from exc
        except (OSError, TimeoutError, websockets.WebSocketException) as exc:
            raise RealtimeUnavailable(f"realtime connect failed: {type(exc).__name__}") from exc
        try:
            yield _WsAdapter(ws)
        finally:
            with contextlib.suppress(Exception):
                await ws.close()


# ---------------------------------------------------------------- the fake


class FakeRealtimeSocket:
    """A scripted realtime socket.

    With ``events``: every event is delivered in order, then the socket closes with
    ``close_code``. Without: behaves like the server: ``session.begin`` at once, one
    ``transcript.partial`` per word every ``chunks_per_word`` audio chunks, and on
    ``flush``/``end`` a ``transcript.final`` followed by ``session.end`` and a 1000 close.
    """

    def __init__(
        self,
        *,
        transcript: str = "",
        language: str | None = "hi-IN",
        events: list[dict[str, Any]] | None = None,
        close_code: int = 1000,
        close_reason: str = "",
        chunks_per_word: int = 2,
    ) -> None:
        self.sent: list[dict[str, Any]] = []
        self._q: asyncio.Queue[Any] = asyncio.Queue()
        self._words = transcript.split()
        self._lang = language
        self._chunks_per_word = max(1, chunks_per_word)
        self._audio_chunks = 0
        self._audio_bytes = 0
        self._finalised = False
        self._close = (close_code, close_reason)
        self.closed = False
        if events is not None:
            for ev in events:
                self._q.put_nowait(json.dumps(ev))
            self._q.put_nowait(RealtimeClosed(close_code, close_reason))
            self._scripted = True
        else:
            self._q.put_nowait(json.dumps({"event": "session.begin", "request_id": "fake_rt"}))
            self._scripted = False

    async def send(self, message: str) -> None:
        if self.closed:
            raise RealtimeClosed(*self._close)
        msg = json.loads(message)
        self.sent.append(msg)
        if self._scripted:
            return
        ev = msg.get("event")
        if ev == "audio_input":
            self._audio_chunks += 1
            self._audio_bytes += len(base64.b64decode(msg["audio"]))
            n = min(len(self._words), self._audio_chunks // self._chunks_per_word)
            if n and self._audio_chunks % self._chunks_per_word == 0 and not self._finalised:
                text = " ".join(self._words[:n])
                self._q.put_nowait(
                    json.dumps(
                        {
                            "event": "transcript.partial",
                            "utterance_idx": 0,
                            "text": text,
                            "language": self._lang,
                        }
                    )
                )
        elif ev in {"flush", "end"} and not self._finalised:
            self._finalised = True
            self._q.put_nowait(
                json.dumps(
                    {
                        "event": "transcript.final",
                        "utterance_idx": 0,
                        "text": " ".join(self._words),
                        "language": self._lang,
                        "language_confidence": 0.97,
                    }
                )
            )
        if ev == "end":
            secs = self._audio_bytes / 32000.0
            self._q.put_nowait(
                json.dumps(
                    {"event": "session.end", "request_id": "fake_rt", "audio_duration_s": secs}
                )
            )
            self._q.put_nowait(RealtimeClosed(1000, ""))
        elif ev == "ping":
            self._q.put_nowait(json.dumps({"event": "pong"}))

    async def recv(self) -> str | bytes:
        item = await self._q.get()
        if isinstance(item, RealtimeClosed):
            self.closed = True
            raise item
        return item

    async def close(self) -> None:
        self.closed = True


Responder = Callable[..., Any]


class FakeSarvam:
    """Offline stand-in for SarvamClient with the same public interface.

    Each endpoint answers from a queue of fixture responses when one was given (dicts are
    responses, exceptions are raised), else from a built-in default. The meter is charged
    exactly as the real client would charge, so spend tests use the fake too.
    """

    def __init__(
        self,
        *,
        chat: list[Any] | None = None,
        tts: list[Any] | None = None,
        translate: list[Any] | None = None,
        stt_rest: list[Any] | None = None,
        realtime: list[Any] | None = None,
        meter: CreditMeter | None = None,
        available: bool = True,
    ) -> None:
        self._queues: dict[str, deque[Any]] = {
            "chat": deque(chat or []),
            "tts": deque(tts or []),
            "translate": deque(translate or []),
            "stt_rest": deque(stt_rest or []),
            "realtime": deque(realtime or []),
        }
        self.meter = meter or CreditMeter()
        self._available = available
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.sockets: list[FakeRealtimeSocket] = []
        self.last: dict[str, CallRecord] = {}

    @property
    def available(self) -> bool:
        return self._available

    async def aclose(self) -> None:
        return None

    def _next(self, name: str) -> Any:
        q = self._queues[name]
        if not q:
            return None
        item = q.popleft()
        if isinstance(item, BaseException):
            raise item
        return item

    def _charge(self, kind: str, rupees: float, **units: float) -> None:
        rec = CallRecord(kind, 200, 0.0, 0.0, rupees=rupees, units=dict(units))
        self.last[kind] = rec
        self.meter.record(rec)

    async def chat(self, messages: list[dict[str, str]], **kw: Any) -> dict[str, Any]:
        self.calls.append(("chat", {"messages": messages, **kw}))
        if not self._available:
            raise SarvamUnavailable("fake: unavailable")
        resp = self._next("chat")
        if resp is None:
            from skypaw.voice.planner import rule_based_plan

            last_user = next(m["content"] for m in reversed(messages) if m["role"] == "user")
            p = rule_based_plan(last_user)
            content = json.dumps(asdict(p) | {"confidence": 0.9}, ensure_ascii=False)
            resp = {
                "id": "fake",
                "model": MODEL_PLAN,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": content}}],
                "usage": {"prompt_tokens": 900, "completion_tokens": 60},
            }
        usage = resp.get("usage") or {}
        t_in = int(usage.get("prompt_tokens") or 0)
        t_out = int(usage.get("completion_tokens") or 0)
        self._charge("llm", self.meter.llm(t_in, t_out), tokens_in=t_in, tokens_out=t_out)
        return resp

    async def tts(self, text: str, language_code: str, speaker: str, **kw: Any) -> bytes:
        self.calls.append(("tts", {"text": text, "language_code": language_code, **kw}))
        if not self._available:
            raise SarvamUnavailable("fake: unavailable")
        resp = self._next("tts")
        if resp is None:
            wav = tone_wav(min(3.0, 0.05 * len(text)), kw.get("sample_rate", 22050))
        else:
            wav = base64.b64decode("".join(resp["audios"]))
        self._charge("tts", self.meter.tts(len(text)), chars=len(text))
        return wav

    async def translate(self, text: str, src: str, dst: str, **kw: Any) -> str:
        self.calls.append(("translate", {"text": text, "src": src, "dst": dst}))
        if not self._available:
            raise SarvamUnavailable("fake: unavailable")
        resp = self._next("translate")
        if resp is None:
            from skypaw.voice.phrases import lookup

            out = lookup(text, src, dst) or f"[{dst}] {text}"
        else:
            out = resp["translated_text"]
        self._charge("translate", self.meter.translate(len(text)), chars=len(text))
        return out

    async def stt_rest(self, wav: bytes, **kw: Any) -> dict[str, Any]:
        self.calls.append(("stt_rest", kw))
        if not self._available:
            raise SarvamUnavailable("fake: unavailable")
        resp = self._next("stt_rest") or {
            "request_id": "fake",
            "transcript": "",
            "language_code": kw.get("language_code") or "hi-IN",
        }
        secs = wav_duration_s(wav)
        self._charge("stt_rest", self.meter.stt(secs), seconds=secs)
        return resp

    def record_realtime(self, seconds: float, latency_ms: float, status: int = 101) -> None:
        self._charge("stt", self.meter.stt(seconds), seconds=seconds)

    @contextlib.asynccontextmanager
    async def connect_realtime(
        self, params: dict[str, str], **kw: Any
    ) -> AsyncIterator[RealtimeSocket]:
        self.calls.append(("realtime", dict(params)))
        if not self._available:
            raise RealtimeUnavailable("fake: unavailable")
        item = self._next("realtime")
        sock = item if isinstance(item, FakeRealtimeSocket) else FakeRealtimeSocket()
        self.sockets.append(sock)
        yield sock


__all__ = [
    "MODEL_PLAN",
    "MODEL_STT_REALTIME",
    "MODEL_STT_REST",
    "MODEL_TRANSLATE",
    "MODEL_TTS",
    "RATES",
    "REALTIME_WS_URL",
    "SARVAM_BASE_URL",
    "CallRecord",
    "CreditMeter",
    "FakeRealtimeSocket",
    "FakeSarvam",
    "QuotaExhausted",
    "RateLimiter",
    "RealtimeClosed",
    "RealtimeSocket",
    "RealtimeUnavailable",
    "SarvamClient",
    "SarvamError",
    "SarvamUnavailable",
    "pcm_to_wav",
    "tone_wav",
    "wav_duration_s",
]
