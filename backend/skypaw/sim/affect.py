"""The only sanctioned way to call ``Cyberwave.affect``.

``cw.affect(mode)`` decides whether SDK commands hit a simulator or a real robot, and
in a simulation mode it auto-starts a billable MuJoCo instance. Everything in Sky & Paw
goes through ``simulation_affect``: it checks the configured runtime mode with the
simulation-only guard, always passes the literal ``"simulation"``, and enforces the
per-run duration cap. ``tests/test_sim_affect.py`` fails if ``.affect(`` appears
anywhere else under ``skypaw/``.
"""

from __future__ import annotations

from typing import Any, Protocol, TypeVar

from skypaw.config import get_settings
from skypaw.logging import get_logger, log_event
from skypaw.sim.guard import assert_simulation_only

log = get_logger(__name__)

#: The only mode string ever passed to Cyberwave.affect.
SIMULATION_MODE = "simulation"


class _Affectable(Protocol):
    def affect(
        self, mode: str, *, environment_id: str | None = ..., duration: float | None = ...
    ) -> Any: ...


C = TypeVar("C", bound=_Affectable)


def simulation_affect(cw: C, *, duration: float, environment_id: str | None = None) -> C:
    """Put ``cw`` (a ``cyberwave.Cyberwave`` client) into simulation mode for ``duration`` s.

    Raises ``SimulationOnlyViolation`` if the configured ``CW_RUNTIME_MODE`` is not a
    simulation mode, and ``ValueError`` if ``duration`` is not in (0, SIM_MAX_DURATION_S].
    """
    settings = get_settings()
    assert_simulation_only(settings.cw_runtime_mode)
    if isinstance(duration, bool) or not isinstance(duration, int | float):
        raise ValueError(f"duration must be a number of seconds, got {duration!r}")
    if not 0 < duration <= settings.sim_max_duration_s:
        raise ValueError(
            f"duration must be in (0, {settings.sim_max_duration_s}] s "
            f"(SIM_MAX_DURATION_S), got {duration}"
        )
    log_event(
        log,
        "cyberwave_affect",
        mode=SIMULATION_MODE,
        duration_s=duration,
        environment_id=environment_id,
    )
    cw.affect(SIMULATION_MODE, environment_id=environment_id, duration=duration)
    return cw


__all__ = ["SIMULATION_MODE", "simulation_affect"]
