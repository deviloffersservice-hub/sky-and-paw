"""SimSession: the mission's one MuJoCo run, in a worker thread.

Thread body: Cyberwave client (simulation mode, SDK MQTT over WSS) -> scene handles via
``skypaw.sim.scene.build_scene`` (idempotent: look up by name, create only what is missing)
-> stop leftovers -> preflight -> ``run_simulation(duration)`` whose ``on_affected`` hook
connects the MQTT pose client (after ``simulation_affect``, RESEARCH §6) -> wait for ready ->
report status -> block until :meth:`stop` or the requested duration ends -> leave the
``run_simulation`` block, which stops the run in its ``finally``.

Status changes are reported through ``on_status(status, info)`` from the worker thread; the
runtime marshals them onto its event loop.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from skypaw.config import Settings, get_settings
from skypaw.logging import get_logger, log_event
from skypaw.sim.mqtt_ws import PoseCache, SceneMqtt
from skypaw.sim.sim_run import SimRun, estimate_credits, preflight, run_simulation, stop_active

log = get_logger(__name__)

StatusCallback = Callable[[str, dict[str, Any]], None]


@dataclass
class SessionInfo:
    """What the session learned (uuids only, safe to log)."""

    env_id: str | None = None
    drone_uuid: str | None = None
    go2_uuid: str | None = None
    rubble_uuid: str | None = None
    run: dict[str, Any] = field(default_factory=dict)
    mqtt_connected: bool = False
    error: str | None = None


def ensure_scene_handles(cw: Any, settings: Settings, env_name: str) -> dict[str, str | None]:
    """Environment + twin uuids for ``env_name``, creating the scene if it is missing.

    Uses ``skypaw.sim.scene.build_scene`` (idempotent; a repeat run only sends GETs): 18 rubble
    pieces, Go2, drone, survey waypoints and stream B's victim markers.
    """
    from skypaw.scene.site import load_site
    from skypaw.sim.scene import SceneContext, build_scene

    handles = build_scene(
        SceneContext(cw),
        load_site(),
        project=settings.cw_project_name,
        environment=env_name,
    )
    return {
        "env_id": handles.environment_uuid,
        "drone_uuid": handles.drone_twin_uuid,
        "go2_uuid": handles.go2_twin_uuid,
        "rubble_uuid": handles.rubble_twin_uuid,
    }


class SimSession:
    """Owns the Cyberwave client, scene handles, MQTT pose cache and the simulation run."""

    def __init__(
        self,
        env_name: str,
        duration_s: float,
        *,
        settings: Settings | None = None,
        client_factory: Callable[[Settings], Any] | None = None,
        handles_factory: Callable[[Any, Settings, str], dict[str, str | None]] | None = None,
        mqtt_factory: Callable[[Settings, PoseCache], Any] | None = None,
        ready_timeout_s: float = 120.0,
    ) -> None:
        self.env_name = env_name
        self.duration_s = float(duration_s)
        self.settings = settings or get_settings()
        self.cache = PoseCache()
        self.info = SessionInfo()
        self.cw: Any = None
        self.ready = threading.Event()
        self.done = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._client_factory = client_factory
        self._handles_factory = handles_factory or ensure_scene_handles
        self._mqtt_factory = mqtt_factory or (lambda s, c: SceneMqtt(s, cache=c))
        self._ready_timeout_s = ready_timeout_s
        self._mqtt: Any = None
        self.t_requested: float | None = None

    @property
    def credits_est(self) -> float:
        """Credits for the requested duration."""
        return estimate_credits(self.duration_s)

    def start(self, on_status: StatusCallback) -> None:
        """Start the worker thread (returns immediately)."""
        if self._thread is not None:
            raise RuntimeError("SimSession already started")
        self._thread = threading.Thread(
            target=self._main, args=(on_status,), name="skypaw-sim", daemon=True
        )
        self._thread.start()

    def stop(self, timeout_s: float = 60.0) -> None:
        """Ask the run to end and wait for the stop in ``run_simulation``'s finally."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout_s)

    def twin(self, which: str) -> Any:
        """SDK twin handle for ``"drone"`` or ``"go2"`` (after ready)."""
        from skypaw.sim.scene import DRONE_SLUG, GO2_SLUG

        uid = self.info.drone_uuid if which == "drone" else self.info.go2_uuid
        slug = DRONE_SLUG if which == "drone" else GO2_SLUG
        if self.cw is None or uid is None:
            raise RuntimeError(f"no {which} twin (simulation not ready)")
        return self.cw.twin(twin_id=uid, asset_key=slug)

    # worker thread --------------------------------------------------------------------
    def _main(self, on_status: StatusCallback) -> None:
        run: SimRun | None = None
        try:
            on_status("starting", {"requested_s": self.duration_s})
            if self._client_factory is not None:
                self.cw = self._client_factory(self.settings)
            else:
                from skypaw.sim.client import get_client

                self.cw = get_client(self.settings)
            h = self._handles_factory(self.cw, self.settings, self.env_name)
            self.info.env_id = h["env_id"]
            self.info.drone_uuid = h["drone_uuid"]
            self.info.go2_uuid = h["go2_uuid"]
            self.info.rubble_uuid = h.get("rubble_uuid")
            env_id = self.info.env_id
            if not env_id:
                raise RuntimeError(f"environment {self.env_name!r} could not be resolved")
            on_status("starting", {"env_id": env_id})
            stop_active(self.cw, env_id, reason="mission_startup")
            preflight(self.cw, env_id)

            def on_affected(r: SimRun) -> None:
                self.t_requested = time.monotonic()
                on_status("running", {"env_id": env_id, "sim_id": r.sim_id})
                self._mqtt = self._mqtt_factory(self.settings, self.cache)
                ids = [u for u in (self.info.drone_uuid, self.info.go2_uuid) if u]
                self.info.mqtt_connected = bool(self._mqtt.connect(affected=True, twin_uuids=ids))
                if not self.info.mqtt_connected:
                    log_event(log, "mission_mqtt_not_connected", level=40)

            with run_simulation(
                self.cw,
                env_id,
                self.duration_s,
                on_affected=on_affected,
                ready_timeout_s=self._ready_timeout_s,
                settings=self.settings,
            ) as run:
                self.info.run = run.as_dict()
                self.ready.set()
                on_status("ready", {"time_to_ready_s": run.time_to_ready_s})
                started = self.t_requested or time.monotonic()
                remaining = self.duration_s - (time.monotonic() - started)
                self._stop.wait(max(0.0, remaining))
            self.info.run = run.as_dict()
            on_status("stopped", {"run": self.info.run})
        except Exception as exc:  # noqa: BLE001 - reported to the console, sim already stopped
            self.info.error = f"{type(exc).__name__}: {exc}"
            if run is not None:
                self.info.run = run.as_dict()
            log_event(log, "sim_session_error", level=40, error=self.info.error)
            on_status("error", {"error": self.info.error})
        finally:
            if self._mqtt is not None:
                self._mqtt.close()
            self.done.set()


__all__ = ["SessionInfo", "SimSession", "StatusCallback", "ensure_scene_handles"]
