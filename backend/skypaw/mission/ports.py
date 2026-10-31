"""Ports (interfaces) between the v1 work streams. See docs/PROTOCOL.md.

Each stream implements its port and ships a fake for the others to test against.
Positions are Cyberwave world-frame [x, y, z] metres (z up).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class Frame:
    frame_id: str
    path: str
    t: float
    sensor_id: str
    position: list[float]
    rotation: list[float]  # quaternion [w, x, y, z]
    intrinsics: dict[str, Any] = field(default_factory=dict)


@dataclass
class Candidate:
    victim_id: str
    position: list[float]
    evidence: dict[str, Any]
    fused: float
    life_sign: bool
    suggested_triage: str
    frames: list[str] = field(default_factory=list)
    source: dict[str, str] = field(default_factory=dict)


@dataclass
class Plan:
    intent: str
    target: str | None
    language: str
    text_to_speak: str | None = None
    confidence: float = 0.0


class DroneExecutor(Protocol):
    async def start_survey(self) -> None: ...
    async def hold(self) -> None: ...
    async def land(self) -> None: ...


class Go2Executor(Protocol):
    async def goto(self, position: list[float], target_id: str | None) -> None: ...
    async def stop(self) -> None: ...


class Detector(Protocol):
    async def on_frame(self, frame: Frame) -> list[Candidate]: ...


class Voice(Protocol):
    def transcribe_stream(
        self, pcm_chunks: AsyncIterator[bytes], lang: str
    ) -> AsyncIterator[dict[str, Any]]: ...
    async def plan(self, text: str, lang: str) -> Plan: ...
    async def speak(self, text: str, lang: str) -> str: ...  # returns /tts/<sha>.wav URL
    async def translate(self, text: str, src: str, dst: str) -> str: ...
