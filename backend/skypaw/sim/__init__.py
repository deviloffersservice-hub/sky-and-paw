"""Cyberwave simulation access (milestones M1, M2, M4).

Every Cyberwave call site must call skypaw.sim.guard.assert_simulation_only() first.
"""

from skypaw.sim.guard import SimulationOnlyViolation, assert_simulation_only

__all__ = ["SimulationOnlyViolation", "assert_simulation_only"]
