"""Fakes for the ports other streams own (Go2, Detector, Voice) and a kinematic drone.

They let the runtime, the WebSocket server and ``scripts/mission.py`` run without the
Go2 planner (stream E), detection (stream B) or Sarvam (stream C), and let the drone
executor be tested without Cyberwave.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import re
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

import numpy as np

from skypaw.drone.io import Pose
from skypaw.mission.ports import Candidate, Frame, Plan
from skypaw.scene.site import Site, load_site, site_to_world

VICTIM_ID = re.compile(r"\b([VD])\s*-?\s*0?(\d{1,2})\b", re.IGNORECASE)


class FakeGo2Executor:
    """Records goto/stop; ``goto`` takes ``travel_s`` and then returns (arrived)."""

    def __init__(self, travel_s: float = 0.2) -> None:
        self.travel_s = travel_s
        self.calls: list[tuple[str, Any]] = []

    async def goto(self, position: list[float], target_id: str | None) -> None:
        """Pretend to walk to ``position``."""
        self.calls.append(("goto", (list(position), target_id)))
        await asyncio.sleep(self.travel_s)

    async def stop(self) -> None:
        """Pretend to stop."""
        self.calls.append(("stop", None))


class FakeDetector:
    """Synthetic detector: a ground-truth person within ``radius`` of the frame's ground point.

    Uses the ``victims`` block of ``site.yaml`` (SPEC §6.3 detection rule, 2.6 m). The real
    detector (stream B) replaces this.
    """

    def __init__(self, site: Site | None = None, radius: float = 2.6) -> None:
        self.site = site or load_site()
        self.radius = radius
        self.frames: list[Frame] = []

    async def on_frame(self, frame: Frame) -> list[Candidate]:
        """Candidates for the people under ``frame``."""
        self.frames.append(frame)
        out: list[Candidate] = []
        fx, fy = frame.position[0], frame.position[1]
        for v in self.site.victims:
            wx, wy, wz = site_to_world(*v["pos"])
            if math.hypot(wx - fx, wy - fy) > self.radius:
                continue
            truth = dict(v.get("truth") or {})
            radar = truth.get("radar")
            life = bool(radar) and not v.get("decoy")
            if radar and radar.get("snr") == "strong":
                triage = "immediate"
            elif radar:
                triage = "delayed"
            elif v.get("decoy"):
                triage = "expectant"
            else:
                triage = "unknown"
            weights = []
            if "rgb" in truth:
                weights.append(float(truth["rgb"]))
            if "thermal" in truth:
                weights.append(min(1.0, float(truth["thermal"]) / 6) * 0.7)
            if radar:
                weights.append(0.95 if radar.get("snr") == "strong" else 0.75)
            fused = 1.0 - math.prod(1.0 - w for w in weights) if weights else 0.0
            out.append(
                Candidate(
                    victim_id=str(v["id"]),
                    position=[round(wx, 2), round(wy, 2), round(wz, 2)],
                    evidence={
                        "rgb": truth.get("rgb"),
                        "thermal": truth.get("thermal"),
                        "radar": radar,
                        "acoustic": None,
                        "co2": None,
                    },
                    fused=round(fused, 2),
                    life_sign=life,
                    suggested_triage=triage,
                    frames=[frame.frame_id],
                    source={"rgb": "synthetic", "thermal": "synthetic", "radar": "synthetic"},
                )
            )
        return out


class FakeVoice:
    """Keyword planner, echo transcription/translation, fake TTS URLs (no Sarvam calls)."""

    def __init__(self) -> None:
        self.spoken: list[tuple[str, str]] = []
        self.planned: list[tuple[str, str]] = []

    async def transcribe_stream(
        self, pcm_chunks: AsyncIterator[bytes], lang: str
    ) -> AsyncIterator[dict[str, Any]]:
        """One partial per ~1 s of audio, then a final with a fixed demo sentence."""
        n = 0
        async for _chunk in pcm_chunks:
            n += 1
            if n % 10 == 0:  # ~1 s of 100 ms frames
                yield {
                    "type": "partial",
                    "text": "drone" if n < 20 else "drone survey",
                    "lang": lang,
                }
        text = "drone survey" if n else ""
        yield {"type": "final", "text": text, "lang": "en-IN" if lang == "auto" else lang}

    async def plan(self, text: str, lang: str) -> Plan:
        """Keyword rules over English/Hindi words."""
        self.planned.append((text, lang))
        low = text.lower()
        m = VICTIM_ID.search(text)
        target = f"{m.group(1).upper()}-{int(m.group(2)):02d}" if m else None
        if any(k in low for k in ("stop", "रुको", "रोको")):
            intent = "stop_all"
        elif "land" in low or "उतार" in low:
            intent = "land"
        elif any(k in low for k in ("survey", "map", "ड्रोन", "drone", "मैप")):
            intent = "aerial_survey"
        elif any(k in low for k in ("say", "speak", "बोलो", "कहो")):
            intent = "speak"
        elif any(k in low for k in ("dog", "go2", "send", "भेजो", "कुत्त")):
            intent = "navigate_go2"
        else:
            intent = "unknown"
        return Plan(
            intent=intent,
            target=target,
            language=lang,
            text_to_speak=text if intent == "speak" else None,
            confidence=0.6 if intent != "unknown" else 0.1,
        )

    async def speak(self, text: str, lang: str) -> str:
        """A deterministic ``/tts/<sha>.wav`` URL (no audio is produced)."""
        self.spoken.append((text, lang))
        sha = hashlib.sha256(f"{lang}|{text}".encode()).hexdigest()[:16]
        return f"/tts/{sha}.wav"

    async def translate(self, text: str, src: str, dst: str) -> str:
        """Identity."""
        return text


class FakeDroneExecutor:
    """Mode bookkeeping only (for app tests without a simulation)."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.busy = False

    async def start_survey(self) -> None:
        """Record the call."""
        self.calls.append("start_survey")

    async def hold(self) -> None:
        """Record the call."""
        self.calls.append("hold")

    async def land(self) -> None:
        """Record the call."""
        self.calls.append("land")


class KinematicDrone:
    """Scripted world for the drone executor: FlightControls + PoseSource + FrameGrabber.

    Commands integrate instantly (a burst moves ``speed * duration * gain`` along the
    heading; a turn rotates ``rate * duration * turn_gain``). ``freeze_after`` stops pose
    updates (stale pose) once that many commands were sent.
    """

    def __init__(
        self,
        x: float,
        y: float,
        z: float = 0.6,
        yaw: float = 0.0,
        *,
        clock: Callable[[], float] = time.time,
        gain: float = 0.95,
        turn_gain: float = 0.8,
        freeze_after: int | None = None,
        realtime: bool = False,
    ) -> None:
        self.x, self.y, self.z, self.yaw = x, y, z, yaw
        self.realtime = realtime
        self.clock = clock
        self.gain = gain
        self.turn_gain = turn_gain
        self.freeze_after = freeze_after
        self.commands: list[tuple[str, float, float]] = []
        self.track: list[tuple[float, float, float]] = []
        self._t = clock()
        self._frozen: Pose | None = None

    def _wait(self, seconds: float) -> None:
        """In real-time mode a command takes as long as it would on the robot."""
        if self.realtime and seconds > 0:
            time.sleep(seconds)

    def _tick(self) -> None:
        self._t = self.clock()
        self.track.append((self.x, self.y, self.z))

    # FlightControls
    def takeoff(self, altitude: float) -> None:
        self.commands.append(("takeoff", altitude, 0.0))
        self._wait(3.0)
        self.z = altitude
        self._tick()

    def turn(self, rate: float, duration: float) -> None:
        self.commands.append(("turn", rate, duration))
        self._wait(duration)
        self.yaw = (self.yaw + rate * duration * self.turn_gain + math.pi) % (2 * math.pi) - math.pi
        self._tick()

    def forward(self, speed: float, duration: float) -> None:
        self.commands.append(("forward", speed, duration))
        self._wait(duration)
        d = speed * duration * self.gain
        self.x += math.cos(self.yaw) * d
        self.y += math.sin(self.yaw) * d
        self._tick()

    def stop(self) -> None:
        self.commands.append(("stop", 0.0, 0.0))

    def land(self) -> None:
        self.commands.append(("land", 0.0, 0.0))
        self._wait(3.0)
        self.z = 0.1
        self._tick()

    # PoseSource
    def latest(self) -> Pose | None:
        if self.realtime and self.freeze_after is None:
            self._t = self.clock()  # a hovering drone still publishes a fresh pose
        half = self.yaw / 2
        pose = Pose(
            self.x, self.y, self.z, self.yaw, (math.cos(half), 0.0, 0.0, math.sin(half)), self._t
        )
        if self.freeze_after is not None and len(self.commands) >= self.freeze_after:
            if self._frozen is None:
                self._frozen = pose
            return self._frozen
        self._t = self.clock()
        return pose

    # FrameGrabber
    def grab(self) -> np.ndarray | None:
        img = np.full((360, 640, 3), 120, dtype=np.uint8)
        img[::20, :, :] = 60
        return img


__all__ = [
    "FakeDetector",
    "FakeDroneExecutor",
    "FakeGo2Executor",
    "FakeVoice",
    "KinematicDrone",
]
