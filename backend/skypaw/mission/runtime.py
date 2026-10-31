"""MissionRuntime: the single asyncio runtime behind the console (PROTOCOL v1).

It owns the :class:`MissionState`, the decision queue, the simulation session (scene
handles, ``run_simulation``, MQTT pose), and routes plans (``ports.Plan``) to the executors:
the real drone executor (``skypaw.drone.executor``) and injected Go2 / Detector / Voice
ports (fakes by default, ``skypaw.mission.fakes``). Client messages arrive through
:meth:`MissionRuntime.apply` (WebSocket and ``POST /api/command``); server messages leave
through ``runtime.hub`` (snapshots at 5 Hz, events as they happen).

Every intent except ``stop_all`` becomes a plan decision that must be confirmed.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

from skypaw.config import Settings, get_settings
from skypaw.drone.io import CachePoseSource, SdkCamera, SdkFlight, pose_from_cache_entry
from skypaw.live.snapshot import Hub, snapshot_loop
from skypaw.logging import get_logger, log_event
from skypaw.mission.decisions import DecisionError, DecisionQueue
from skypaw.mission.fakes import FakeDetector, FakeGo2Executor, FakeVoice
from skypaw.mission.ports import Candidate, Detector, DroneExecutor, Frame, Go2Executor, Plan, Voice
from skypaw.mission.sim_session import SimSession
from skypaw.mission.state import LANGS, MissionState
from skypaw.scene.site import load_site, site_to_world

log = get_logger(__name__)

INTENTS = frozenset({"aerial_survey", "navigate_go2", "speak", "stop_all", "land", "unknown"})
TRIAGE = frozenset({"immediate", "delayed", "unknown", "expectant"})
LEAD_ONLY = frozenset({"command", "confirm", "cancel", "decide", "triage", "dismiss", "tts"})
POSE_HZ = 10.0
#: Go2 speaker phrases (``tts`` with ``phrase_id``), English source text.
PHRASES: dict[str, str] = {
    "p_help": "Help is here. We are coming to get you.",
    "p_still": "Stay still and keep calm.",
    "p_tap": "If you can hear me, tap three times.",
    "p_water": "We are sending water.",
}
DEFAULT_LEAD_LANG = "hi-IN"

DroneFactory = Callable[["MissionRuntime"], DroneExecutor]


class CommandRejected(ValueError):
    """A client message that cannot be applied (reason in ``str(exc)``)."""


class MissionRuntime:
    """Live mission: state, decisions, simulation, executors, hub."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        sim: SimSession | None = None,
        drone: DroneExecutor | None = None,
        drone_factory: DroneFactory | None = None,
        go2: Go2Executor | None = None,
        detector: Detector | None = None,
        voice: Voice | None = None,
        hub: Hub | None = None,
        autostart_survey: bool = False,
    ) -> None:
        self.settings = settings or get_settings()
        self.state = MissionState()
        self.decisions = DecisionQueue(clock=self.state.t)
        self.hub = hub or Hub()
        self.sim = sim
        self.drone: DroneExecutor | None = drone
        self._drone_factory = drone_factory
        self.go2: Go2Executor = go2 or FakeGo2Executor()
        self.detector: Detector = detector or FakeDetector()
        self.voice: Voice = voice or FakeVoice()
        self.autostart_survey = autostart_survey
        self.frames_dir = Path(self.settings.data_dir) / "frames"
        self.site = load_site()
        self.state.survey.waypoints = [
            [round(c, 3) for c in site_to_world(*p)] for p in self.site.survey.corners()
        ]
        self._victim_lang = {
            str(v["id"]): str(v.get("language", "hi-IN")) for v in self.site.victims
        }
        self._frame_n = 0
        self._tasks: set[asyncio.Task[Any]] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._mic: asyncio.Queue[bytes | None] | None = None
        self._drone_task: asyncio.Task[Any] | None = None
        self._go2_task: asyncio.Task[Any] | None = None
        self.started = False
        self.surveys_started = 0
        self.surveys_finished = 0
        self.sim_ready = asyncio.Event()
        self.sim_done = asyncio.Event()

    # --- lifecycle ------------------------------------------------------------------
    async def start(self) -> None:
        """Start the background loops (snapshot, pose) and the simulation if configured."""
        if self.started:
            return
        self.started = True
        self._loop = asyncio.get_running_loop()
        try:
            self.frames_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log_event(
                log, "frames_dir_unavailable", level=30, path=str(self.frames_dir), error=str(exc)
            )
        self._spawn(snapshot_loop(self.hub, self.snapshot), "snapshot")
        self.log("SYS", "mission_start", f"Mission {self.state.mission.id} started")
        if self.sim is not None:
            self.state.sim.requested_s = self.sim.duration_s
            self.state.sim.credits_est = self.sim.credits_est
            self.sim.start(self._on_sim_status_threadsafe)
            self._spawn(self._pose_loop(), "pose")
        elif self.drone is not None and self.autostart_survey:
            self._autostart()

    async def stop(self) -> None:
        """Hold the drone, stop the simulation (blocking stop runs in a thread), cancel loops."""
        if self._drone_task is not None and not self._drone_task.done() and self.drone is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.drone.hold(), 5)
        if self.sim is not None:
            await asyncio.to_thread(self.sim.stop)
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()

    def _spawn(self, coro: Any, name: str) -> asyncio.Task[Any]:
        task = asyncio.get_running_loop().create_task(coro, name=f"skypaw-{name}")
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log_event(
                log,
                "task_failed",
                level=40,
                task=task.get_name(),
                error=f"{type(exc).__name__}: {exc}",
            )
            self.log("SYS", "task_failed", f"{task.get_name()} failed: {exc}")

    # --- snapshot / events / log ----------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        """Current PROTOCOL snapshot."""
        self.state.decisions = self.decisions.to_list()
        return self.state.to_snapshot()

    def emit(self, name: str, data: dict[str, Any] | None = None) -> None:
        """Publish an ``event`` message."""
        self.hub.publish({"type": "event", "name": name, "data": data or {}, "t": self.state.t()})

    def log(self, src: str, key: str, text: str, *, lang: str = "en-IN") -> dict[str, Any]:
        """Append a log entry; other languages are filled in the background (Voice.translate)."""
        entry = self.state.add_log(src, key, text)
        log_event(log, "mission_log", src=src, key=key, text=text)
        if self._loop is not None and self._loop.is_running():
            with contextlib.suppress(RuntimeError):
                self._spawn(self._translate_entry(entry, text, lang), "translate")
        return entry

    async def _translate_entry(self, entry: dict[str, Any], text: str, src: str) -> None:
        async def one(dst: str) -> tuple[str, str]:
            if dst == src:
                return dst, text
            try:
                return dst, await self.voice.translate(text, src, dst)
            except Exception as exc:  # noqa: BLE001 - keep the source text
                log_event(log, "translate_failed", level=30, dst=dst, error=str(exc))
                return dst, text

        for dst, out in await asyncio.gather(*(one(d) for d in LANGS)):
            entry["text"][dst] = out

    # --- simulation -----------------------------------------------------------------
    def _on_sim_status_threadsafe(self, status: str, info: dict[str, Any]) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._on_sim_status, status, info)

    def _on_sim_status(self, status: str, info: dict[str, Any]) -> None:
        sim = self.state.sim
        sim.status = status
        if info.get("env_id"):
            sim.env_id = info["env_id"]
        if status == "running" and sim.started_mono is None:
            sim.started_mono = time.monotonic()
        if status == "ready":
            sim.ready = True
            self.log("SIM", "sim_ready", f"Simulation ready in {info.get('time_to_ready_s')} s")
            self.emit("sim.ready", info)
            self.sim_ready.set()
            self._on_ready()
        elif status == "error":
            sim.ready = False
            sim.error = info.get("error")
            self.log("SIM", "sim_error", f"Simulation error: {sim.error}")
            self.emit("sim.error", info)
            self.sim_done.set()
        elif status == "stopped":
            sim.ready = False
            self.log("SIM", "sim_stopped", "Simulation stopped")
            self.sim_done.set()
        else:
            self.log("SIM", f"sim_{status}", f"Simulation {status}")

    def _on_ready(self) -> None:
        if self.drone is None:
            factory = self._drone_factory or default_drone_factory
            try:
                self.drone = factory(self)
            except Exception as exc:  # noqa: BLE001
                self.log("SYS", "drone_unavailable", f"Drone executor unavailable: {exc}")
                return
        if self.autostart_survey:
            self._autostart()

    def _autostart(self) -> None:
        d = self.propose(
            Plan("aerial_survey", None, "en-IN", None, 1.0), source_text="--autostart-survey"
        )
        if d is not None:
            self.log("LEAD", "auto_confirm", "Survey plan auto-confirmed (--autostart-survey)")
            self.apply_sync_confirm(d["plan"]["id"])

    async def _pose_loop(self) -> None:
        """Copy MQTT poses into state at 10 Hz."""
        assert self.sim is not None  # noqa: S101
        period = 1.0 / POSE_HZ
        while True:
            for robot, uid in (
                (self.state.drone, self.sim.info.drone_uuid),
                (self.state.go2, self.sim.info.go2_uuid),
            ):
                if not uid:
                    continue
                p = pose_from_cache_entry(self.sim.cache.latest(uid))
                if p is not None:
                    robot.pos = p.xyz
                    robot.yaw = p.yaw
                    robot.pose_t = p.t
                    if robot is self.state.drone and robot.mode == "landed" and p.z > 1.0:
                        robot.mode = "hold"
            await asyncio.sleep(period)

    # --- DroneSink (called by the drone executor) -----------------------------------
    def drone_mode(self, mode: str) -> None:
        """Drone mode change."""
        self.state.drone.mode = mode

    def drone_waypoint(self, idx: int) -> None:
        """Next survey corner index."""
        self.state.survey.waypoint_idx = idx

    def drone_reveal(self, x: float, y: float, r: float) -> None:
        """Mark coverage."""
        self.state.coverage.reveal(x, y, r)

    def next_frame_id(self) -> str:
        """``f_000001``, ``f_000002``, … (shared by every camera)."""
        self._frame_n += 1
        return f"f_{self._frame_n:06d}"

    async def drone_frame(self, frame: Frame) -> None:
        """A captured drone frame: count it, show it, run the detector on it."""
        self.state.survey.frames += 1
        self.state.drone.frame_id = frame.frame_id
        try:
            candidates = await self.detector.on_frame(frame)
        except Exception as exc:  # noqa: BLE001 - detection errors must not stop the survey
            log_event(log, "detector_failed", level=40, error=f"{type(exc).__name__}: {exc}")
            return
        for c in candidates:
            self._merge_candidate(c)

    def drone_log(self, key: str, text: str, level: str = "info") -> None:
        """Drone log line (NAV)."""
        self.log("NAV", f"drone_{key}", text)

    def _merge_candidate(self, c: Candidate) -> None:
        v = self.state.victim(c.victim_id)
        if v is None:
            v = {
                "id": c.victim_id,
                "pos": list(c.position),
                "status": "located",
                "fused": c.fused,
                "life_sign": c.life_sign,
                "evidence": dict(c.evidence),
                "suggested_triage": c.suggested_triage,
                "triage": None,
                "frames": list(c.frames),
                "source": dict(c.source),
            }
            self.state.victims.append(v)
            self.log("DET", "detection", f"{c.victim_id} located, fused {c.fused:.0%}")
            self.emit("detection", {"victim_id": c.victim_id, "fused": c.fused})
            return
        v["fused"] = max(float(v.get("fused") or 0), c.fused)
        v["life_sign"] = bool(v.get("life_sign")) or c.life_sign
        v["evidence"].update({k: val for k, val in c.evidence.items() if val is not None})
        v["frames"] = (v["frames"] + [f for f in c.frames if f not in v["frames"]])[-10:]
        v["suggested_triage"] = c.suggested_triage

    # --- plans ----------------------------------------------------------------------
    def propose(
        self, plan: Plan, *, source_text: str = "", model: str = "ui"
    ) -> dict[str, Any] | None:
        """Turn a plan into a decision card (``stop_all`` executes at once; returns None)."""
        if plan.intent not in INTENTS:
            raise CommandRejected(f"unknown intent {plan.intent!r}")
        if plan.intent == "stop_all":
            self.log("LEAD", "stop_all", "Stop all: drone hold, Go2 stop")
            self._spawn(self.execute(plan), "stop_all")
            return None
        if plan.intent == "unknown":
            d = self.decisions.add(
                "warn", "Command not understood", source_text or "No action was planned."
            )
            self.log("LLM", "plan_unknown", f"Not understood: {source_text}")
            return d.to_dict()
        d = self.decisions.add_plan(
            plan, source_text=source_text, source_lang=plan.language, model=model
        )
        out = d.to_dict()
        self.log("LLM", "plan_proposed", f"Plan {d.plan_id}: {d.title}")
        self.emit("plan.proposed", {"decision_id": d.id, "plan": out["plan"]})
        return out

    def apply_sync_confirm(self, plan_id: str) -> None:
        """Confirm a plan and start executing it (no await needed)."""
        res = self.decisions.confirm(plan_id)
        self._after_confirm(res.plan, plan_id)

    def _after_confirm(self, plan: Plan | None, plan_id: str) -> None:
        if plan is None:
            return
        self.log("LEAD", "plan_confirmed", f"Plan {plan_id} confirmed: {plan.intent}")
        self.emit("plan.confirmed", {"plan_id": plan_id, "intent": plan.intent})
        self._spawn(self.execute(plan), f"plan-{plan_id}")

    async def execute(self, plan: Plan) -> None:
        """Run a confirmed plan."""
        if plan.intent == "aerial_survey":
            await self._exec_survey()
        elif plan.intent == "navigate_go2":
            await self._exec_go2(plan.target)
        elif plan.intent == "speak":
            await self._exec_speak(plan.text_to_speak or PHRASES["p_help"], plan.target, None)
        elif plan.intent == "land":
            if self.drone is None:
                raise CommandRejected("drone not available")
            await self.drone.land()
        elif plan.intent == "stop_all":
            if self.drone is not None:
                await self.drone.hold()
            await self.go2.stop()
            if self.state.go2.mode == "moving":
                self.state.go2.mode = "idle"

    async def _exec_survey(self) -> None:
        if self.drone is None:
            self.log("SYS", "drone_unavailable", "Drone not available (simulation not ready)")
            return
        if self._drone_task is not None and not self._drone_task.done():
            self.log("SYS", "drone_busy", "Survey already running")
            return
        self.state.mission.phase = "mapping"
        self.state.coverage.reset()
        self.state.survey.frames = 0
        self.log("NAV", "survey_start", "Drone survey started")
        self._drone_task = asyncio.current_task()
        self.surveys_started += 1
        try:
            await self.drone.start_survey()
        finally:
            self.surveys_finished += 1
            pct = self.state.coverage.pct_mapped()
            self.log(
                "DET",
                "survey_done",
                f"Mapping complete: {pct}% seen, {len(self.state.victims)} candidates",
            )
            self.emit("alert", {"kind": "survey_done", "pct_mapped": pct})
            if self.state.mission.phase == "mapping":
                self.state.mission.phase = "search"

    def _nearest_victim(self, pos: list[float] | None) -> dict[str, Any] | None:
        alive = [
            v for v in self.state.victims if v.get("life_sign") and v.get("status") != "dismissed"
        ]
        if not alive:
            return None
        if pos is None:
            return alive[0]
        return min(alive, key=lambda v: math.hypot(v["pos"][0] - pos[0], v["pos"][1] - pos[1]))

    async def _exec_go2(self, target: str | None) -> None:
        v = self.state.victim(target) if target else self._nearest_victim(self.state.go2.pos)
        if v is None:
            self.decisions.add(
                "warn", "Go2: no target", f"No located victim {target or ''}".strip()
            )
            return
        go2 = self.state.go2
        go2.target, go2.mode = v["id"], "moving"
        v["status"] = "enroute"
        self.state.mission.phase = "reach"
        self.log("NAV", "go2_goto", f"Go2 moving to {v['id']}")
        try:
            await self.go2.goto(list(v["pos"]), v["id"])
        except Exception as exc:  # noqa: BLE001
            go2.mode = "blocked"
            self.log("NAV", "go2_blocked", f"Go2 blocked: {exc}")
            return
        if go2.mode == "moving":
            go2.mode = "arrived"
            v["status"] = "arrived"
            self.log("NAV", "go2_arrived", f"Go2 arrived at {v['id']}")
            self.emit("nav.arrived", {"victim_id": v["id"]})

    async def _exec_speak(self, text: str, target: str | None, lang: str | None) -> None:
        if lang is None:
            v = self.state.victim(target) if target else self._nearest_victim(self.state.go2.pos)
            lang = self._victim_lang.get(v["id"], DEFAULT_LEAD_LANG) if v else DEFAULT_LEAD_LANG
        url = await self.voice.speak(text, lang)
        self.state.tts_playing = {
            "text": text,
            "lang": lang,
            "voice": None,
            "url": url,
            "started_t": self.state.t(),
        }
        self.log("TTS", "tts_started", f"Go2 speaker ({lang}): {text}")
        self.emit("tts.started", {"url": url, "lang": lang})

    # --- client messages ------------------------------------------------------------
    async def apply(self, msg: dict[str, Any], *, role: str = "lead") -> dict[str, Any]:
        """Apply a PROTOCOL client message; returns ``{accepted, reason?}``."""
        try:
            await self._apply(msg, role)
        except (CommandRejected, DecisionError, KeyError, TypeError, ValueError) as exc:
            reason = str(exc) if not isinstance(exc, KeyError) else f"missing field {exc}"
            return {"accepted": False, "reason": reason}
        return {"accepted": True}

    async def _apply(self, msg: dict[str, Any], role: str) -> None:
        kind = msg.get("type")
        if kind in LEAD_ONLY and role != "lead":
            raise CommandRejected(f"{kind} is for the lead only")
        if kind == "command":
            intent = str(msg["intent"])
            params = msg.get("params") or {}
            plan = Plan(
                intent=intent,
                target=msg.get("target"),
                language=str(params.get("lang") or DEFAULT_LEAD_LANG),
                text_to_speak=params.get("text"),
                confidence=1.0,
            )
            self.propose(plan, source_text=f"UI: {intent}")
        elif kind == "confirm":
            self.apply_sync_confirm(str(msg["plan_id"]))
        elif kind == "cancel":
            res = self.decisions.cancel(str(msg["plan_id"]))
            self.log("LEAD", "plan_cancelled", f"Plan {msg['plan_id']} cancelled")
            self.emit("plan.cancelled", {"plan_id": msg["plan_id"], "decision_id": res.decision.id})
        elif kind == "decide":
            res = self.decisions.decide(str(msg["decision_id"]), str(msg["action"]))
            if res.plan is not None:
                self._after_confirm(res.plan, str(res.decision.plan_id))
            elif res.decision.kind == "plan":
                self.log("LEAD", "plan_cancelled", f"Plan {res.decision.plan_id} cancelled")
                self.emit("plan.cancelled", {"plan_id": res.decision.plan_id})
            else:
                self.log("LEAD", "decided", f"{res.decision.id}: {res.action}")
        elif kind == "triage":
            category = str(msg["category"])
            if category not in TRIAGE:
                raise CommandRejected(f"unknown triage category {category!r}")
            v = self.state.victim(str(msg["victim_id"]))
            if v is None:
                raise CommandRejected(f"unknown victim {msg['victim_id']!r}")
            v["triage"] = category
            self.log("LEAD", "triage", f"{v['id']} triaged {category}")
        elif kind == "dismiss":
            v = self.state.victim(str(msg["victim_id"]))
            if v is None:
                raise CommandRejected(f"unknown victim {msg['victim_id']!r}")
            v["status"] = "dismissed"
            self.log("LEAD", "dismiss", f"{v['id']} dismissed")
        elif kind == "text_command":
            await self._final_text(str(msg["text"]), str(msg.get("lang") or DEFAULT_LEAD_LANG))
        elif kind == "tts":
            text = msg.get("text") or PHRASES.get(str(msg.get("phrase_id")), "")
            if not text:
                raise CommandRejected("tts needs text or a known phrase_id")
            self._spawn(self._exec_speak(str(text), None, msg.get("lang")), "tts")
        elif kind == "mic":
            if msg.get("state") == "start":
                self.mic_start(str(msg.get("lang") or "auto"))
            elif msg.get("state") == "stop":
                self.mic_stop()
            else:
                raise CommandRejected("mic state must be start or stop")
        elif kind in ("ping", "hello"):
            return
        else:
            raise CommandRejected(f"unknown message type {kind!r}")

    async def _final_text(self, text: str, lang: str) -> None:
        tr = self.state.transcript
        tr.active, tr.partial, tr.final, tr.lang, tr.final_t = False, "", text, lang, self.state.t()
        self.log("STT", "stt_final", text, lang=lang if lang in LANGS else "en-IN")
        self.emit("stt.final", {"text": text, "lang": lang})
        plan = await self.voice.plan(text, lang)
        self.propose(plan, source_text=text, model=getattr(self.voice, "model", "fake"))

    # --- microphone -----------------------------------------------------------------
    def mic_start(self, lang: str) -> None:
        """Begin a transcription stream fed by :meth:`mic_audio`."""
        if self._mic is not None:
            self.mic_stop()
        q: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=600)
        self._mic = q
        self.state.transcript.active = True
        self.state.transcript.partial = ""
        self._spawn(self._transcribe(q, lang), "stt")

    def mic_audio(self, chunk: bytes) -> bool:
        """Queue one PCM frame; False when no mic stream is open (frame dropped)."""
        if self._mic is None:
            return False
        with contextlib.suppress(asyncio.QueueFull):
            self._mic.put_nowait(chunk)
        return True

    def mic_stop(self) -> None:
        """End the current transcription stream."""
        q, self._mic = self._mic, None
        if q is not None:
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(None)

    async def _transcribe(self, q: asyncio.Queue[bytes | None], lang: str) -> None:
        async def chunks() -> AsyncIterator[bytes]:
            while True:
                item = await q.get()
                if item is None:
                    return
                yield item

        final: dict[str, Any] | None = None
        async for ev in self.voice.transcribe_stream(chunks(), lang):
            if ev.get("type") == "partial":
                self.state.transcript.partial = str(ev.get("text", ""))
                self.emit("stt.partial", {"text": ev.get("text"), "lang": ev.get("lang")})
            elif ev.get("type") == "final":
                final = ev
        self.state.transcript.active = False
        if final and final.get("text"):
            await self._final_text(str(final["text"]), str(final.get("lang") or lang))


def default_drone_factory(rt: MissionRuntime) -> DroneExecutor:
    """The real executor on the simulation's drone twin and MQTT pose."""
    from skypaw.drone.executor import SurveyDroneExecutor

    if rt.sim is None or rt.sim.info.drone_uuid is None:
        raise RuntimeError("no simulation / drone twin")
    twin = rt.sim.twin("drone")
    return SurveyDroneExecutor(
        SdkFlight(twin),
        CachePoseSource(rt.sim.cache, rt.sim.info.drone_uuid),
        SdkCamera(twin),
        rt,
        rt.frames_dir,
        site=rt.site,
        mission_t=rt.state.t,
    )


__all__ = [
    "INTENTS",
    "PHRASES",
    "CommandRejected",
    "MissionRuntime",
    "default_drone_factory",
]
