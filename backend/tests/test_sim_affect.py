from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

import skypaw
from skypaw.sim import SIMULATION_MODE, SimulationOnlyViolation, simulation_affect

PACKAGE_DIR = Path(skypaw.__file__).resolve().parent
# The one file allowed to call Cyberwave.affect.
AFFECT_MODULE = PACKAGE_DIR / "sim" / "affect.py"


class FakeCyberwave:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def affect(
        self, mode: str, *, environment_id: str | None = None, duration: float | None = None
    ) -> FakeCyberwave:
        self.calls.append((mode, {"environment_id": environment_id, "duration": duration}))
        return self


def test_passes_literal_simulation_and_duration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CW_RUNTIME_MODE", raising=False)
    cw = FakeCyberwave()
    assert simulation_affect(cw, duration=120, environment_id="env-1") is cw
    assert cw.calls == [("simulation", {"environment_id": "env-1", "duration": 120})]
    assert SIMULATION_MODE == "simulation"


@pytest.mark.parametrize("configured", ["playground", "MUJOCO", "sim"])
def test_allowed_configured_modes_still_send_simulation(
    monkeypatch: pytest.MonkeyPatch, configured: str
) -> None:
    monkeypatch.setenv("CW_RUNTIME_MODE", configured)
    cw = FakeCyberwave()
    simulation_affect(cw, duration=60)
    assert [mode for mode, _ in cw.calls] == ["simulation"]


@pytest.mark.parametrize("configured", ["live", "real-world", "real", "tele"])
def test_refuses_when_configured_mode_is_live(
    monkeypatch: pytest.MonkeyPatch, configured: str
) -> None:
    monkeypatch.setenv("CW_RUNTIME_MODE", configured)
    cw = FakeCyberwave()
    with pytest.raises(SimulationOnlyViolation):
        simulation_affect(cw, duration=60)
    assert cw.calls == []


@pytest.mark.parametrize("duration", [0, -1, 601, 10_000, True, "60", None])
def test_rejects_bad_duration(monkeypatch: pytest.MonkeyPatch, duration: Any) -> None:
    monkeypatch.delenv("CW_RUNTIME_MODE", raising=False)
    monkeypatch.delenv("SIM_MAX_DURATION_S", raising=False)
    cw = FakeCyberwave()
    with pytest.raises(ValueError):
        simulation_affect(cw, duration=duration)
    assert cw.calls == []


def test_duration_cap_follows_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIM_MAX_DURATION_S", "90")
    cw = FakeCyberwave()
    simulation_affect(cw, duration=90)
    with pytest.raises(ValueError):
        simulation_affect(cw, duration=91)
    assert len(cw.calls) == 1


def test_duration_is_keyword_only() -> None:
    with pytest.raises(TypeError):
        simulation_affect(FakeCyberwave(), 60)  # type: ignore[misc]


def test_no_affect_calls_outside_the_sanctioned_module() -> None:
    """simulation_affect is the only path to cw.affect: grep the package for `.affect(`."""
    pattern = re.compile(r"\.affect\s*\(")
    offenders = []
    for path in sorted(PACKAGE_DIR.rglob("*.py")):
        if path == AFFECT_MODULE:
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{path.relative_to(PACKAGE_DIR.parent)}:{lineno}: {line.strip()}")
    assert not offenders, "Use skypaw.sim.simulation_affect instead of:\n" + "\n".join(offenders)
    # Sanity: the scan does see the sanctioned call, so the pattern is not vacuous.
    assert pattern.search(AFFECT_MODULE.read_text(encoding="utf-8"))
