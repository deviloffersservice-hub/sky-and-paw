"""MissionState: everything the console renders, serialised exactly as the PROTOCOL snapshot.

The runtime mutates this object from the event loop only (the MQTT thread hands samples
over with ``call_soon_threadsafe``); :meth:`MissionState.to_snapshot` builds the
``{"type": "snapshot", ...}`` dict sent at 5 Hz and returned by ``GET /api/state``.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from skypaw.mission.coverage import Coverage

LANGS: tuple[str, ...] = ("en-IN", "hi-IN", "mr-IN", "ta-IN")
LOG_MAX = 50
LOG_SOURCES = frozenset({"STT", "LLM", "DET", "NAV", "TTS", "LEAD", "SIM", "SYS"})
MISSION_PHASES = ("idle", "mapping", "search", "reach", "contact", "done")
SIM_STATUSES = ("stopped", "starting", "running", "ready", "error")
DRONE_MODES = ("landed", "takeoff", "survey", "hold", "flying", "landing")
GO2_MODES = ("idle", "moving", "arrived", "blocked")

#: Demo team (SPEC §2, mockup ``TEAM``).
DEFAULT_TEAM: tuple[dict[str, str], ...] = (
    {"id": "t1", "name": "Rohan Deshmukh", "role": "lead", "lang": "hi-IN"},
    {"id": "t2", "name": "Priya Kulkarni", "role": "medic", "lang": "mr-IN"},
    {"id": "t3", "name": "Arun Selvam", "role": "extraction", "lang": "ta-IN"},
    {"id": "t4", "name": "Meera Nair", "role": "extraction", "lang": "en-IN"},
)

#: Minutes since the collapse when the mission starts (SPEC demo clock: 42 min).
SINCE_COLLAPSE_AT_START_S = 2520.0


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass
class MissionInfo:
    """``mission`` object."""

    id: str = "SP-0417"
    phase: str = "idle"
    since_collapse_at_start_s: float = SINCE_COLLAPSE_AT_START_S


@dataclass
class SimInfo:
    """``sim`` object."""

    status: str = "stopped"
    ready: bool = False
    env_id: str | None = None
    requested_s: float = 0.0
    started_mono: float | None = None
    credits_est: float = 0.0
    error: str | None = None

    def elapsed_s(self, now_mono: float) -> float:
        """Seconds since the simulation was requested (0 when not started)."""
        return round(now_mono - self.started_mono, 1) if self.started_mono is not None else 0.0


@dataclass
class RobotState:
    """Common robot pose/mode fields; ``pose_t`` is the epoch time of the last pose sample."""

    pos: list[float] | None = None
    yaw: float = 0.0
    mode: str = "landed"
    battery: float = 100.0
    pose_t: float | None = None
    frame_id: str | None = None

    def pose_age_s(self, now: float) -> float | None:
        """Seconds since the last pose sample, ``None`` before the first one."""
        return None if self.pose_t is None else round(max(0.0, now - self.pose_t), 2)


@dataclass
class Go2State(RobotState):
    """``robots.go2``."""

    mode: str = "idle"
    target: str | None = None
    path: list[list[float]] = field(default_factory=list)


@dataclass
class SurveyInfo:
    """``survey`` object (``pct_mapped`` comes from the coverage mask)."""

    frames: int = 0
    waypoint_idx: int = 0
    waypoints: list[list[float]] = field(default_factory=list)


@dataclass
class TranscriptInfo:
    """``transcript`` object."""

    active: bool = False
    partial: str = ""
    final: str = ""
    lang: str | None = None
    final_t: float | None = None


@dataclass
class MissionState:
    """The whole live state (PROTOCOL snapshot)."""

    mission: MissionInfo = field(default_factory=MissionInfo)
    sim: SimInfo = field(default_factory=SimInfo)
    drone: RobotState = field(default_factory=RobotState)
    go2: Go2State = field(default_factory=Go2State)
    survey: SurveyInfo = field(default_factory=SurveyInfo)
    coverage: Coverage = field(default_factory=Coverage)
    victims: list[dict[str, Any]] = field(default_factory=list)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    transcript: TranscriptInfo = field(default_factory=TranscriptInfo)
    tts_playing: dict[str, Any] | None = None
    log: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=LOG_MAX))
    team: list[dict[str, str]] = field(default_factory=lambda: [dict(m) for m in DEFAULT_TEAM])
    t0_mono: float = field(default_factory=time.monotonic)

    def t(self, now_mono: float | None = None) -> float:
        """Seconds since mission start."""
        return round((time.monotonic() if now_mono is None else now_mono) - self.t0_mono, 2)

    def add_log(
        self,
        src: str,
        key: str,
        text: str | dict[str, str],
        *,
        t: float | None = None,
    ) -> dict[str, Any]:
        """Append a log entry (``text`` is per-language; a str fills every language)."""
        if src not in LOG_SOURCES:
            raise ValueError(f"log src must be one of {sorted(LOG_SOURCES)}, got {src!r}")
        texts = {lang: text for lang in LANGS} if isinstance(text, str) else dict(text)
        entry = {"t": self.t() if t is None else t, "src": src, "key": key, "text": texts}
        self.log.append(entry)
        return entry

    def victim(self, victim_id: str) -> dict[str, Any] | None:
        """The victim entry with ``id``, if any."""
        return next((v for v in self.victims if v.get("id") == victim_id), None)

    def to_snapshot(self, *, now_mono: float | None = None, now: float | None = None) -> dict:
        """The PROTOCOL ``snapshot`` message (JSON-ready)."""
        mono = time.monotonic() if now_mono is None else now_mono
        wall = time.time() if now is None else now
        t = self.t(mono)
        sim_elapsed = self.sim.elapsed_s(mono)
        return {
            "type": "snapshot",
            "t": t,
            "ts": _now_iso(),
            "mission": {
                "id": self.mission.id,
                "phase": self.mission.phase,
                "since_collapse_s": round(self.mission.since_collapse_at_start_s + t),
            },
            "sim": {
                "status": self.sim.status,
                "ready": self.sim.ready,
                "env_id": self.sim.env_id,
                "requested_s": self.sim.requested_s,
                "elapsed_s": sim_elapsed,
                "credits_est": self.sim.credits_est,
                "error": self.sim.error,
            },
            "robots": {
                "drone": {
                    "pos": self.drone.pos,
                    "yaw": round(self.drone.yaw, 3),
                    "mode": self.drone.mode,
                    "battery": round(self.drone.battery),
                    "pose_age_s": self.drone.pose_age_s(wall),
                    "frame_id": self.drone.frame_id,
                },
                "go2": {
                    "pos": self.go2.pos,
                    "yaw": round(self.go2.yaw, 3),
                    "mode": self.go2.mode,
                    "battery": round(self.go2.battery),
                    "pose_age_s": self.go2.pose_age_s(wall),
                    "target": self.go2.target,
                    "frame_id": self.go2.frame_id,
                    "path": self.go2.path,
                },
            },
            "survey": {
                "pct_mapped": self.coverage.pct_mapped(),
                "frames": self.survey.frames,
                "waypoint_idx": self.survey.waypoint_idx,
                "waypoints": self.survey.waypoints,
            },
            "grid": self.coverage.to_grid(),
            "victims": [dict(v) for v in self.victims],
            "decisions": [dict(d) for d in self.decisions],
            "transcript": {
                "active": self.transcript.active,
                "partial": self.transcript.partial,
                "final": self.transcript.final,
                "lang": self.transcript.lang,
                "final_t": self.transcript.final_t,
            },
            "tts": {"playing": dict(self.tts_playing) if self.tts_playing else None},
            "log": list(self.log),
            "team": [dict(m) for m in self.team],
        }


#: Top-level and nested keys every snapshot carries (tests check against PROTOCOL).
SNAPSHOT_KEYS: dict[str, tuple[str, ...]] = {
    "": (
        "type",
        "t",
        "ts",
        "mission",
        "sim",
        "robots",
        "survey",
        "grid",
        "victims",
        "decisions",
        "transcript",
        "tts",
        "log",
        "team",
    ),
    "mission": ("id", "phase", "since_collapse_s"),
    "sim": ("status", "ready", "env_id", "requested_s", "elapsed_s", "credits_est", "error"),
    "robots.drone": ("pos", "yaw", "mode", "battery", "pose_age_s", "frame_id"),
    "robots.go2": ("pos", "yaw", "mode", "battery", "pose_age_s", "target", "frame_id", "path"),
    "survey": ("pct_mapped", "frames", "waypoint_idx", "waypoints"),
    "grid": ("x0", "y0", "cs", "w", "h", "cls", "seen"),
    "transcript": ("active", "partial", "final", "lang", "final_t"),
    "tts": ("playing",),
}


__all__ = [
    "DEFAULT_TEAM",
    "DRONE_MODES",
    "GO2_MODES",
    "LANGS",
    "LOG_MAX",
    "LOG_SOURCES",
    "MISSION_PHASES",
    "SIM_STATUSES",
    "SNAPSHOT_KEYS",
    "Go2State",
    "MissionInfo",
    "MissionState",
    "RobotState",
    "SimInfo",
    "SurveyInfo",
    "TranscriptInfo",
]
