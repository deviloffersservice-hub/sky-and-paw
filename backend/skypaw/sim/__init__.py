"""Cyberwave simulation access (milestones M1, M2, M4).

``simulation_affect`` is the only path to ``Cyberwave.affect``; every other Cyberwave
call site calls ``assert_simulation_only()`` first.
"""

from skypaw.sim.affect import SIMULATION_MODE, simulation_affect
from skypaw.sim.guard import SimulationOnlyViolation, assert_simulation_only

__all__ = [
    "SIMULATION_MODE",
    "SimulationOnlyViolation",
    "assert_simulation_only",
    "simulation_affect",
]
