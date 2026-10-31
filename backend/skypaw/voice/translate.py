"""Translation of comms, commands and log lines with sarvam-translate:v1, cached in SQLite.

Call (RESEARCH §2.5, SPEC §6.9): ``POST /translate`` ``{input (≤2000 chars), source_language_code,
target_language_code, model: "sarvam-translate:v1"}`` → ``translated_text``. 60 rpm (the client's
per-endpoint limiter enforces it).

Never translates the same string twice: hand-checked phrase tables first (phrases.lookup), then
an in-memory map, then ``DATA_DIR/cache.db`` keyed by (src, dst, sha256(text)); concurrent
requests for one key share a single call.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from pathlib import Path
from typing import Any

import aiosqlite

from skypaw.logging import get_logger, log_event
from skypaw.voice.client import MODEL_TRANSLATE
from skypaw.voice.phrases import TEAM_LANGS, lookup

log = get_logger(__name__)

MAX_INPUT_CHARS = 2000
_SCHEMA = """
CREATE TABLE IF NOT EXISTS translations (
    src TEXT NOT NULL,
    dst TEXT NOT NULL,
    sha TEXT NOT NULL,
    model TEXT NOT NULL,
    source_text TEXT NOT NULL,
    translated TEXT NOT NULL,
    created REAL NOT NULL,
    PRIMARY KEY (src, dst, sha)
)
"""
_SENTENCE_END = re.compile(r"(?<=[.!?।])\s+")


def normalise_text(text: str) -> str:
    return " ".join(text.split())


def text_sha(text: str) -> str:
    return hashlib.sha256(normalise_text(text).encode("utf-8")).hexdigest()


def split_input(text: str, limit: int = MAX_INPUT_CHARS) -> list[str]:
    """Split on sentence ends into pieces ≤ limit; hard-cut only a sentence longer than limit."""
    if len(text) <= limit:
        return [text]
    pieces: list[str] = []
    cur = ""
    for sent in _SENTENCE_END.split(text):
        while len(sent) > limit:
            if cur:
                pieces.append(cur)
                cur = ""
            pieces.append(sent[:limit])
            sent = sent[limit:]
        if cur and len(cur) + 1 + len(sent) > limit:
            pieces.append(cur)
            cur = sent
        else:
            cur = f"{cur} {sent}" if cur else sent
    if cur:
        pieces.append(cur)
    return pieces


class TranslateCache:
    def __init__(self, client: Any, db_path: Path) -> None:
        self.client = client
        self.db_path = Path(db_path)
        self._db: aiosqlite.Connection | None = None
        self._open_lock = asyncio.Lock()
        self._mem: dict[tuple[str, str, str], str] = {}
        self._inflight: dict[tuple[str, str, str], asyncio.Future[str]] = {}
        self.api_calls = 0
        self.hits = 0

    async def _conn(self) -> aiosqlite.Connection:
        async with self._open_lock:
            if self._db is None:
                self.db_path.parent.mkdir(parents=True, exist_ok=True)
                db = await aiosqlite.connect(self.db_path)
                await db.execute("PRAGMA journal_mode=WAL")
                await db.execute(_SCHEMA)
                await db.commit()
                self._db = db
            return self._db

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def _get(self, key: tuple[str, str, str]) -> str | None:
        if key in self._mem:
            return self._mem[key]
        db = await self._conn()
        async with db.execute(
            "SELECT translated FROM translations WHERE src=? AND dst=? AND sha=?", key
        ) as cur:
            row = await cur.fetchone()
        if row is not None:
            self._mem[key] = row[0]
            return row[0]
        return None

    async def _put(self, key: tuple[str, str, str], text: str, out: str) -> None:
        self._mem[key] = out
        db = await self._conn()
        await db.execute(
            "INSERT OR REPLACE INTO translations VALUES (?,?,?,?,?,?,?)",
            (*key, MODEL_TRANSLATE, text, out, time.time()),
        )
        await db.commit()

    async def translate(self, text: str, src: str, dst: str) -> str:
        text = normalise_text(text)
        if not text or src == dst:
            return text
        known = lookup(text, src, dst)
        if known is not None:
            self.hits += 1
            return known
        key = (src, dst, text_sha(text))
        cached = await self._get(key)
        if cached is not None:
            self.hits += 1
            return cached
        pending = self._inflight.get(key)
        if pending is not None:
            self.hits += 1
            return await asyncio.shield(pending)
        fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._inflight[key] = fut
        try:
            parts = []
            for piece in split_input(text):
                self.api_calls += 1
                parts.append(await self.client.translate(piece, src, dst, model=MODEL_TRANSLATE))
            out = " ".join(p.strip() for p in parts)
            await self._put(key, text, out)
            log_event(log, "translated", src=src, dst=dst, chars=len(text))
            fut.set_result(out)
        except asyncio.CancelledError:
            fut.cancel()
            raise
        except Exception as exc:
            fut.set_exception(exc)
            fut.exception()
            raise
        finally:
            self._inflight.pop(key, None)
        return out

    async def translate_all(
        self, text: str, src: str, langs: tuple[str, ...] = TEAM_LANGS
    ) -> dict[str, str]:
        """``{lang: text}`` for every team language (the source language keeps the original).

        A language whose translation fails falls back to the original text, so one refused
        call never blanks a log line."""
        targets = [lang for lang in langs if lang != src]
        results = await asyncio.gather(
            *(self.translate(text, src, dst) for dst in targets), return_exceptions=True
        )
        out = {src: normalise_text(text)} if src in langs else {}
        for dst, res in zip(targets, results, strict=True):
            if isinstance(res, BaseException):
                log_event(log, "translate_failed", src=src, dst=dst, error=type(res).__name__)
                out[dst] = normalise_text(text)
            else:
                out[dst] = res
        return {lang: out[lang] for lang in langs if lang in out}

    async def translate_many(self, texts: list[str], src: str, dst: str) -> list[str]:
        """Batch helper for log lines: unique strings are translated once each."""
        unique = list(dict.fromkeys(normalise_text(t) for t in texts))
        outs = await asyncio.gather(*(self.translate(u, src, dst) for u in unique))
        done = dict(zip(unique, outs, strict=True))
        return [done[normalise_text(t)] for t in texts]
