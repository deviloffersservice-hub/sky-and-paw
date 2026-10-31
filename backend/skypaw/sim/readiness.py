"""Simulation readiness: wait for ``simulation_ready`` in the environment's simulations list.

RESEARCH §6: ``status == "running"`` arrives about 3 s after start but the simulator is only
usable when the list entry's ``simulation_ready`` turns true (about 45-55 s). A worker that
crashes at startup shows up as ``error_message`` (e.g. the px4vision ``color_mode`` crash).
``cw.affect`` can also race right after the start and raise "did not become active" although
the run is fine; :func:`affect_with_retry` checks the list and retries ``affect`` once.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Literal

from skypaw.logging import get_logger, log_event
from skypaw.sim.affect import simulation_affect

log = get_logger(__name__)

Phase = Literal["absent", "loading", "running", "ready", "failed", "ended"]
ACTIVE_STATUSES = frozenset({"loading", "running"})
TERMINAL_STATUSES = frozenset({"completed", "failed", "stopped"})
#: Substring of the SDK error raised when affect() loses the start race (0.7.4).
AFFECT_RACE_TEXT = "did not become active"


class SimulationFailed(RuntimeError):
    """The simulation reported an error or ended before it became ready."""

    def __init__(self, message: str, *, error_message: str | None = None) -> None:
        super().__init__(message)
        self.error_message = error_message


class SimulationNotReady(TimeoutError):
    """``simulation_ready`` did not turn true within the timeout."""


@dataclass(frozen=True)
class ReadyState:
    """One observation of the simulation, reduced to a phase."""

    phase: Phase
    sim_id: str | None = None
    status: str | None = None
    error_message: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, compare=False)


def _raw(sim: Any) -> dict[str, Any]:
    raw = getattr(sim, "raw", None)
    if isinstance(raw, dict):
        return raw
    return sim if isinstance(sim, dict) else {}


def _sim_id(sim: Any) -> str | None:
    value = getattr(sim, "simulation_id", None) or _raw(sim).get("simulation_id")
    return str(value) if value else None


def _status(sim: Any) -> str:
    return str(getattr(sim, "status", None) or _raw(sim).get("status") or "unknown")


def classify(sims: Iterable[Any], sim_id: str | None = None) -> ReadyState:
    """Reduce a simulations listing to a :class:`ReadyState` (pure; unit-tested).

    Picks the entry with ``sim_id`` if given, else the first active entry, else the first.
    """
    entries = list(sims)
    chosen = None
    if sim_id:
        chosen = next((s for s in entries if _sim_id(s) == str(sim_id)), None)
    if chosen is None and not sim_id:
        chosen = next((s for s in entries if _status(s) in ACTIVE_STATUSES), None)
        if chosen is None and entries:
            chosen = entries[0]
    if chosen is None:
        return ReadyState("absent", sim_id=sim_id)
    raw = _raw(chosen)
    status = _status(chosen)
    error = raw.get("error_message") or None
    sid = _sim_id(chosen)
    if error:
        return ReadyState("failed", sid, status, str(error), raw)
    if status == "failed":
        return ReadyState("failed", sid, status, None, raw)
    if status in TERMINAL_STATUSES:
        return ReadyState("ended", sid, status, None, raw)
    if raw.get("simulation_ready") is True:
        return ReadyState("ready", sid, status, None, raw)
    if status == "running":
        return ReadyState("running", sid, status, None, raw)
    return ReadyState("loading", sid, status, None, raw)


def _detail_error(cw: Any, env_id: str, sim_id: str | None) -> str | None:
    """Best effort: the ended run's ``error_message`` from the (private) detail route."""
    if not sim_id:
        return None
    getter = getattr(cw.environments.simulations, "_get_detail", None)
    if getter is None:
        return None
    try:
        detail = getter(env_id, sim_id)
    except Exception as exc:  # noqa: BLE001 - diagnostic only
        log_event(log, "simulation_detail_error", level=30, error=f"{type(exc).__name__}: {exc}")
        return None
    raw = _raw(detail) if detail is not None else {}
    msg = raw.get("error_message")
    return str(msg) if msg else None


def wait_simulation_ready(
    cw: Any,
    env_id: str,
    timeout_s: float = 120.0,
    *,
    sim_id: str | None = None,
    poll_s: float = 2.0,
    absent_grace_s: float = 10.0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> ReadyState:
    """Poll ``simulations.list(env_id)`` until ``simulation_ready``; return the ready state.

    Raises :class:`SimulationFailed` on ``error_message``, a terminal status, or when the run
    stays unlisted for ``absent_grace_s``; :class:`SimulationNotReady` after ``timeout_s``.
    """
    t0 = clock()
    absent_since: float | None = None
    last_phase: Phase | None = None
    while True:
        state = classify(cw.environments.simulations.list(env_id), sim_id)
        elapsed = round(clock() - t0, 1)
        if state.phase != last_phase:
            log_event(
                log,
                "simulation_phase",
                phase=state.phase,
                status=state.status,
                simulation_id=state.sim_id,
                elapsed_s=elapsed,
            )
            last_phase = state.phase
        if state.phase == "ready":
            return state
        if state.phase in ("failed", "ended"):
            error = state.error_message or _detail_error(cw, env_id, state.sim_id)
            raise SimulationFailed(
                f"simulation {state.sim_id} {state.phase} (status {state.status}) before ready"
                + (f": {error}" if error else ""),
                error_message=error,
            )
        if state.phase == "absent":
            absent_since = absent_since if absent_since is not None else clock()
            if clock() - absent_since >= absent_grace_s:
                error = _detail_error(cw, env_id, sim_id)
                raise SimulationFailed(
                    f"simulation {sim_id or '(any)'} is not listed for environment {env_id}"
                    + (f": {error}" if error else ""),
                    error_message=error,
                )
        else:
            absent_since = None
        if clock() - t0 >= timeout_s:
            raise SimulationNotReady(
                f"simulation_ready not true after {timeout_s:.0f}s (last phase {state.phase})"
            )
        sleep(poll_s)


def affect_with_retry(cw: Any, env_id: str, duration_s: float, *, retries: int = 1) -> None:
    """``simulation_affect`` that survives the SDK's start race (RESEARCH §6).

    If affect raises the "did not become active" error, the listing decides: an active run
    is accepted as is; otherwise affect is retried (once by default). Any other error
    propagates unchanged.
    """
    attempt = 0
    while True:
        try:
            simulation_affect(cw, duration=duration_s, environment_id=env_id)
            return
        except Exception as exc:  # noqa: BLE001 - the SDK raises a plain CyberwaveError
            if AFFECT_RACE_TEXT not in str(exc):
                raise
            state = classify(cw.environments.simulations.list(env_id))
            log_event(
                log,
                "affect_race",
                level=30,
                attempt=attempt + 1,
                phase=state.phase,
                simulation_id=state.sim_id,
                error=str(exc)[:300],
            )
            if state.phase in ("loading", "running", "ready"):
                return
            if attempt >= retries:
                raise
            attempt += 1


__all__ = [
    "ACTIVE_STATUSES",
    "AFFECT_RACE_TEXT",
    "ReadyState",
    "SimulationFailed",
    "SimulationNotReady",
    "affect_with_retry",
    "classify",
    "wait_simulation_ready",
]
