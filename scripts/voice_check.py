#!/usr/bin/env python3
"""Live Sarvam smoke checks for skypaw.voice: one call per endpoint, a table of status/latency/₹.

    # offline rehearsal (FakeSarvam, no network, ₹0):
    .venv/bin/python scripts/voice_check.py
    # live (needs SARVAM_API_KEY in the environment; ≈ ₹0.3 in total, every call < ₹1):
    SKYPAW_SARVAM_LIVE=1 .venv/bin/python scripts/voice_check.py --live
    # optionally also synthesise all scripted phrases once (≈ ₹2.8, cached afterwards):
    SKYPAW_SARVAM_LIVE=1 .venv/bin/python scripts/voice_check.py --live --prewarm

Order (cheapest first, stops at the first 402): translate → plan → TTS → STT REST → STT realtime.
The TTS clip (16 kHz) is reused as the STT input, so the STT rows are a real round trip.
Measured: translate latency; plan latency; TTS time to first byte; REST STT latency; realtime
first-partial latency (from the first audio chunk) and final latency (from the end of audio).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import wave
from io import BytesIO
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "backend"))

from skypaw.config import get_settings  # noqa: E402
from skypaw.voice import planner, stt  # noqa: E402
from skypaw.voice.client import (  # noqa: E402
    CreditMeter,
    FakeRealtimeSocket,
    FakeSarvam,
    QuotaExhausted,
    SarvamClient,
    SarvamError,
)
from skypaw.voice.phrases import LEAD_SAMPLES  # noqa: E402
from skypaw.voice.tts import TtsCache, speaker_for  # noqa: E402

CA_BUNDLE = Path("/root/.ccr/ca-bundle.crt")  # the cloud sandbox proxy CA, when present
MAX_RUPEES_PER_CALL = 1.0
TR_TEXT = "Go2 arrived at V-01. Radar 64 bpm."
CMD = LEAD_SAMPLES[1]  # "dog को V-01 के पास भेजो" (hi-IN)


class Row(dict):
    pass


def _pcm16k(wav: bytes) -> bytes:
    with wave.open(BytesIO(wav)) as w:
        if w.getframerate() != 16000 or w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise ValueError(f"expected 16 kHz mono int16, got {w.getframerate()} Hz")
        return w.readframes(w.getnframes())


def _spent(meter: CreditMeter, before: float) -> float:
    return meter.total - before


async def run(client: Any, out_dir: Path, *, prewarm: bool) -> list[Row]:
    meter: CreditMeter = client.meter
    rows: list[Row] = []

    async def check(name: str, est: float, fn) -> Any:
        if est >= MAX_RUPEES_PER_CALL:
            raise SystemExit(f"{name}: estimate ₹{est:.3f} is not under ₹1; refusing")
        before = meter.total
        t0 = time.perf_counter()
        row = Row(check=name, status="ok", ms=None, ttfb_ms=None, rupees=0.0, note="")
        try:
            note, extra = await fn()
            row.update(note=note, **extra)
        except QuotaExhausted as exc:
            row.update(status="402", note=str(exc)[:80])
            rows.append(row)
            raise
        except SarvamError as exc:
            row.update(status=str(exc.status or "err"), note=str(exc)[:80])
        if row["ms"] is None:
            row["ms"] = round((time.perf_counter() - t0) * 1000)
        row["rupees"] = _spent(meter, before)
        rows.append(row)
        return row

    async def do_translate():
        out = await client.translate(TR_TEXT, "en-IN", "mr-IN")
        rec = client.last.get("translate")
        return out, {"ms": rec and round(rec.latency_ms), "ttfb_ms": rec and round(rec.ttfb_ms)}

    async def do_plan():
        resp = await client.chat(
            planner.build_messages(CMD.text, CMD.lang),
            response_format=planner.RESPONSE_FORMAT,
            reasoning_effort="low",
            max_tokens=planner.PLAN_MAX_TOKENS,
        )
        content = resp["choices"][0]["message"].get("content")
        p = planner.parse_plan(content, CMD.lang)
        rec = client.last.get("llm")
        usage = resp.get("usage") or {}
        note = f"{p.intent} {p.target} conf={p.confidence} usage={usage}"
        (out_dir / "plan.json").write_text(json.dumps(resp, ensure_ascii=False, indent=2))
        return note, {"ms": rec and round(rec.latency_ms), "ttfb_ms": rec and round(rec.ttfb_ms)}

    clip: dict[str, bytes] = {}

    async def do_tts():
        wav = await client.tts(CMD.text, CMD.lang, speaker_for(CMD.lang), sample_rate=16000)
        clip["wav"] = wav
        (out_dir / "tts_hi_16k.wav").write_bytes(wav)
        secs = len(_pcm16k(wav)) / stt.BYTES_PER_S
        rec = client.last.get("tts")
        note = f"{len(wav)} bytes, {secs:.2f} s, speaker {speaker_for(CMD.lang)}"
        return note, {"ms": rec and round(rec.latency_ms), "ttfb_ms": rec and round(rec.ttfb_ms)}

    async def do_stt_rest():
        ev = await stt.transcribe_clip(client, clip["wav"], "auto")
        rec = client.last.get("stt_rest")
        return f"{ev['text']!r} ({ev['lang']})", {"ms": rec and round(rec.latency_ms)}

    async def do_stt_realtime():
        pcm = _pcm16k(clip["wav"]) + b"\0\0" * 8000  # + 0.5 s silence
        marks: dict[str, float] = {}

        async def source():
            for i in range(0, len(pcm), stt.CHUNK_BYTES):
                marks.setdefault("first_audio", time.perf_counter())
                yield pcm[i : i + stt.CHUNK_BYTES]
                await asyncio.sleep(0.1)  # real-time pace
            marks["audio_done"] = time.perf_counter()

        partial_ms = final_ms = None
        finals = []
        async for ev in stt.transcribe_stream(client, source(), "auto"):
            now = time.perf_counter()
            if ev["kind"] == "partial" and partial_ms is None:
                partial_ms = round((now - marks["first_audio"]) * 1000)
            if ev["kind"] == "final":
                finals.append(ev["text"])
                if "audio_done" in marks:
                    final_ms = round((now - marks["audio_done"]) * 1000)
        note = f"partial@{partial_ms} ms, final@{final_ms} ms after audio end: {' '.join(finals)!r}"
        return note, {"partial_ms": partial_ms, "final_ms": final_ms}

    try:
        await check(
            "translate en→mr (sarvam-translate:v1)", meter.translate(len(TR_TEXT)), do_translate
        )
        await check(
            "plan (sarvam-105b, strict, low)", meter.llm(2000, planner.PLAN_MAX_TOKENS), do_plan
        )
        await check("tts hi-IN priya (bulbul:v3)", meter.tts(len(CMD.text)), do_tts)
        if "wav" in clip:
            await check("stt REST (saaras:v4 codemix)", meter.stt(5), do_stt_rest)
            await check("stt realtime (saaras:v3-realtime)", meter.stt(10), do_stt_realtime)
        if prewarm:
            cache = TtsCache(client, Path(get_settings().data_dir))

            async def do_prewarm():
                return json.dumps(await cache.prewarm()), {}

            await check("tts prewarm (20 phrases)", 0.99, do_prewarm)
    except QuotaExhausted:
        print("Stopped at the first 402: the account has no credits.", file=sys.stderr)
    return rows


def print_table(rows: list[Row], meter: CreditMeter) -> None:
    hdr = f"{'check':40} {'status':6} {'ms':>6} {'ttfb':>6} {'₹':>8}  note"
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        ms = "" if r.get("ms") is None else r["ms"]
        ttfb = "" if r.get("ttfb_ms") is None else r["ttfb_ms"]
        print(
            f"{r['check']:40} {r['status']:6} {ms:>6} {ttfb:>6} "
            f"{r['rupees']:>8.4f}  {r['note'][:110]}"
        )
    print("-" * len(hdr))
    print(f"total ₹{meter.total:.4f}  by kind {meter.snapshot()['by_kind']}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--live", action="store_true", help="call the real API (SKYPAW_SARVAM_LIVE=1)")
    ap.add_argument("--prewarm", action="store_true", help="also synthesise all scripted phrases")
    ap.add_argument("--out", type=Path, default=None, help="where to save raw outputs")
    args = ap.parse_args()

    settings = get_settings()
    out_dir = args.out or Path(settings.data_dir) / "voice_check"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        out_dir = Path.cwd() / "voice_check_out"
        out_dir.mkdir(parents=True, exist_ok=True)

    if args.live:
        if os.environ.get("SKYPAW_SARVAM_LIVE") != "1":
            print("refusing: set SKYPAW_SARVAM_LIVE=1 to spend real credits", file=sys.stderr)
            return 2
        if settings.sarvam_api_key is None:
            print("SARVAM_API_KEY is not set", file=sys.stderr)
            return 2
        if CA_BUNDLE.exists():
            os.environ.setdefault("SSL_CERT_FILE", str(CA_BUNDLE))
        client: Any = SarvamClient.from_settings(settings)
        print("LIVE run against https://api.sarvam.ai")
    else:
        sock = FakeRealtimeSocket(transcript=CMD.text, language=CMD.lang)
        client = FakeSarvam(
            realtime=[sock], stt_rest=[{"transcript": CMD.text, "language_code": CMD.lang}]
        )
        print("OFFLINE rehearsal with FakeSarvam (pass --live for real calls)")

    async def go() -> list[Row]:
        try:
            return await run(client, out_dir, prewarm=args.prewarm)
        finally:
            await client.aclose()

    rows = asyncio.run(go())
    print_table(rows, client.meter)
    print(f"raw outputs: {out_dir}")
    return 0 if rows and all(r["status"] == "ok" for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
