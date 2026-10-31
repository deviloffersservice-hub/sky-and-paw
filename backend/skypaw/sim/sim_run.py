"""``run_simulation()``: the only way Sky & Paw starts a MuJoCo simulation.

Sequence: guard checks -> budget log -> ``affect_with_retry`` (-> ``simulation_affect``) ->
``on_affected`` hook (MQTT connects here) -> ``wait_simulation_ready`` -> ``yield`` -> stop.
The stop runs in ``finally`` (normal exit, exceptions, ``KeyboardInterrupt``) with SIGINT /
SIGTERM ignored while it runs, and verifies that the environment lists no active run.
Budget is logged at start and end on the *requested* duration, because Cyberwave reports
``total_duration_s`` = requested even when a run is stopped early (RESEARCH §6).
"""

from __future__ import annotations

import signal
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from skypaw.config import Settings, get_settings
from skypaw.logging import get_logger, log_event
from skypaw.sim.client import assert_client_simulation
from skypaw.sim.guard import assert_simulation_only
from skypaw.sim.readiness import (
    ACTIVE_STATUSES,
    affect_with_retry,
    classify,
    wait_simulation_ready,
)

log = get_logger(__name__)

#: cyberwave.managers.simulations.SIMULATION_CREDITS_PER_MINUTE (0.7.4).
CREDITS_PER_MINUTE = 0.01


class PreflightBlocked(RuntimeError):
    """``simulations.preflight`` says the environment cannot start on MuJoCo."""


def estimate_credits(duration_s: float) -> float:
    """Credits billed for a requested duration (0.01 credits per minute)."""
    return round(float(duration_s) / 60.0 * CREDITS_PER_MINUTE, 4)


@dataclass
class SimRun:
    """What happened in one ``run_simulation`` block (timings in seconds)."""

    environment_id: str
    duration_s: float
    sim_id: str | None = None
    affected: bool = False
    affect_s: float | None = None
    time_to_ready_s: float | None = None
    t_affect_start: float | None = None  # time.monotonic()
    t_affect_return: float | None = None
    t_ready: float | None = None
    stopped_ids: tuple[str, ...] = ()
    stop_verified: bool = False

    @property
    def estimated_credits(self) -> float:
        """Credits for the requested duration."""
        return estimate_credits(self.duration_s)

    def as_dict(self) -> dict[str, Any]:
        """JSON-friendly summary (no secrets)."""
        return {
            "environment_id": self.environment_id,
            "simulation_id": self.sim_id,
            "requested_duration_s": self.duration_s,
            "estimated_credits": self.estimated_credits,
            "affect_s": self.affect_s,
            "time_to_ready_s": self.time_to_ready_s,
            "stopped_ids": list(self.stopped_ids),
            "stop_verified": self.stop_verified,
        }


def preflight(cw: Any, env_id: str) -> dict[str, Any]:
    """Run ``simulations.preflight(backend="mujoco")``; raise :class:`PreflightBlocked` if blocked.

    Note (RESEARCH §6): preflight said eligible for setups that then failed, so passing it is
    necessary, not sufficient; readiness still watches ``error_message``.
    """
    assert_client_simulation(cw)
    result = dict(cw.environments.simulations.preflight(env_id, backend="mujoco") or {})
    summary = {k: result.get(k) for k in ("enabled", "eligible", "blocking_assets", "reasons")}
    log_event(log, "simulation_preflight", environment_id=env_id, **summary)
    if not result.get("eligible", False) or result.get("blocking_assets"):
        raise PreflightBlocked(
            f"MuJoCo preflight refused environment {env_id}: "
            f"blocking_assets={result.get('blocking_assets')} reasons={result.get('reasons')}"
        )
    return result


def stop_active(cw: Any, env_id: str, *, reason: str) -> list[str]:
    """Stop every loading/running simulation of ``env_id``; returns the stopped ids."""
    stopped: list[str] = []
    for sim in cw.environments.simulations.list(env_id):
        status = str(getattr(sim, "status", ""))
        if status in ACTIVE_STATUSES:
            sim_id = str(getattr(sim, "simulation_id", ""))
            log_event(log, "simulation_stop", simulation_id=sim_id, status=status, reason=reason)
            sim.stop()
            stopped.append(sim_id)
    return stopped


@contextmanager
def _signals_ignored() -> Iterator[None]:
    """Ignore SIGINT/SIGTERM (main thread only) so a second Ctrl-C cannot skip the stop."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        previous[sig] = signal.getsignal(sig)
        signal.signal(sig, signal.SIG_IGN)
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def _stop_and_verify(cw: Any, run: SimRun, *, attempts: int = 3, pause_s: float = 2.0) -> None:
    stopped: list[str] = []
    for attempt in range(1, attempts + 1):
        try:
            stopped += stop_active(cw, run.environment_id, reason="run_simulation.finally")
            state = classify(cw.environments.simulations.list(run.environment_id))
            if state.phase not in ("loading", "running", "ready"):
                run.stop_verified = True
                break
            log_event(log, "simulation_still_active", level=30, attempt=attempt, phase=state.phase)
        except Exception as exc:  # noqa: BLE001 - never mask the original exception
            log_event(
                log,
                "simulation_stop_error",
                level=40,
                attempt=attempt,
                error=f"{type(exc).__name__}: {exc}",
            )
        time.sleep(pause_s)
    run.stopped_ids = tuple(dict.fromkeys(stopped))
    if not run.stop_verified:
        log_event(
            log,
            "simulation_stop_unverified",
            level=50,
            environment_id=run.environment_id,
            hint="check the Cyberwave dashboard and stop the run by hand",
        )


@contextmanager
def run_simulation(
    cw: Any,
    env_id: str,
    duration_s: float | None = None,
    *,
    ready_timeout_s: float = 120.0,
    on_affected: Callable[[SimRun], None] | None = None,
    settings: Settings | None = None,
) -> Iterator[SimRun]:
    """Start a MuJoCo simulation of ``env_id``, wait until ready, yield, always stop it.

    ``duration_s`` defaults to ``SIM_DEFAULT_DURATION_S`` (120) and must not exceed
    ``SIM_MAX_DURATION_S`` (600).
    """
    settings = settings or get_settings()
    assert_simulation_only(settings.cw_runtime_mode)
    assert_client_simulation(cw)
    duration = float(duration_s if duration_s is not None else settings.sim_default_duration_s)
    if not 0 < duration <= settings.sim_max_duration_s:
        raise ValueError(
            f"duration must be in (0, {settings.sim_max_duration_s}] s, got {duration}"
        )
    run = SimRun(environment_id=env_id, duration_s=duration)
    log_event(
        log,
        "sim_budget_start",
        environment_id=env_id,
        requested_duration_s=duration,
        estimated_credits=run.estimated_credits,
    )
    try:
        run.t_affect_start = time.monotonic()
        affect_with_retry(cw, env_id, duration)
        run.t_affect_return = time.monotonic()
        run.affect_s = round(run.t_affect_return - run.t_affect_start, 2)
        run.affected = True
        run.sim_id = classify(cw.environments.simulations.list(env_id)).sim_id
        log_event(log, "simulation_started", simulation_id=run.sim_id, affect_s=run.affect_s)
        if on_affected is not None:
            on_affected(run)
        wait_simulation_ready(cw, env_id, ready_timeout_s, sim_id=run.sim_id)
        run.t_ready = time.monotonic()
        run.time_to_ready_s = round(run.t_ready - run.t_affect_start, 1)
        log_event(
            log, "simulation_ready", simulation_id=run.sim_id, time_to_ready_s=run.time_to_ready_s
        )
        yield run
    finally:
        with _signals_ignored():
            _stop_and_verify(cw, run)
        log_event(log, "sim_budget_end", **run.as_dict())


__all__ = [
    "CREDITS_PER_MINUTE",
    "PreflightBlocked",
    "SimRun",
    "estimate_credits",
    "preflight",
    "run_simulation",
    "stop_active",
]
