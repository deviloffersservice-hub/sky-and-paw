"""Simulation-only guard.

Sky & Paw never drives real hardware. Every Cyberwave call site (sim start,
navigation, waypoints) must call ``assert_simulation_only()`` first, so a mis-set
environment variable or a bad code path fails loudly instead of moving a real robot.
Switching the SDK's runtime mode is done only by ``skypaw.sim.affect.simulation_affect``.
"""

from __future__ import annotations

from skypaw.config import get_settings

ENV_VAR = "CW_RUNTIME_MODE"
DEFAULT_MODE = "simulation"

#: Modes that are allowed (compared case-insensitively, surrounding whitespace ignored).
ALLOWED_MODES: frozenset[str] = frozenset({"simulation", "sim", "mujoco", "playground"})

#: Modes that are known to mean real hardware; refused with an explicit message.
#: Anything not in ALLOWED_MODES is refused too: this list only improves the error text.
KNOWN_LIVE_MODES: frozenset[str] = frozenset(
    {"live", "real-world", "real_world", "realworld", "real", "tele", "teleop", "teleoperation"}
)


class SimulationOnlyViolation(RuntimeError):
    """Raised when code would talk to Cyberwave in anything other than simulation."""


def assert_simulation_only(mode: str | None = None) -> str:
    """Return the normalised mode if it is a simulation mode, otherwise raise.

    With no argument (or ``None``) the mode is the configured ``cw_runtime_mode``
    (env var ``CW_RUNTIME_MODE`` or .env, via ``get_settings()``), default ``"simulation"``.
    """
    if mode is None:
        mode = get_settings().cw_runtime_mode
    if not isinstance(mode, str):
        raise SimulationOnlyViolation(
            f"Cyberwave mode must be a string, got {type(mode).__name__}; "
            "Sky & Paw is simulation-only."
        )
    normalised = mode.strip().lower()
    if normalised in ALLOWED_MODES:
        return normalised
    if normalised in KNOWN_LIVE_MODES:
        reason = "it targets real hardware"
    else:
        reason = "it is not a recognised simulation mode"
    raise SimulationOnlyViolation(
        f"Refusing Cyberwave mode {mode!r}: {reason}. Sky & Paw is simulation-only; "
        f"allowed modes are {sorted(ALLOWED_MODES)}."
    )


__all__ = [
    "ALLOWED_MODES",
    "DEFAULT_MODE",
    "ENV_VAR",
    "KNOWN_LIVE_MODES",
    "SimulationOnlyViolation",
    "assert_simulation_only",
]
