from __future__ import annotations

import subprocess
import sys

import pytest

from skypaw.sim.guard import ENV_VAR, SimulationOnlyViolation, assert_simulation_only


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("simulation", "simulation"),
        ("playground", "playground"),
        ("sim", "sim"),
        ("mujoco", "mujoco"),
        ("SIMULATION", "simulation"),
        ("Playground", "playground"),
        ("MuJoCo", "mujoco"),
        ("SiM", "sim"),
        ("  simulation\n", "simulation"),
    ],
)
def test_simulation_modes_pass(mode: str, expected: str) -> None:
    assert assert_simulation_only(mode) == expected


@pytest.mark.parametrize(
    "mode",
    [
        "live",
        "LIVE",
        "real-world",
        "Real-World",
        "real_world",
        "real",
        "tele",
        "teleop",
        "",
        "   ",
        "simulation-live",
        "sim ulation",
        "production",
    ],
)
def test_other_modes_raise(mode: str) -> None:
    with pytest.raises(SimulationOnlyViolation):
        assert_simulation_only(mode)


@pytest.mark.parametrize("mode", [0, 1.0, b"simulation", ["simulation"]])
def test_non_string_modes_raise(mode: object) -> None:
    with pytest.raises(SimulationOnlyViolation):
        assert_simulation_only(mode)  # type: ignore[arg-type]


def test_violation_is_a_runtime_error() -> None:
    assert issubclass(SimulationOnlyViolation, RuntimeError)


def test_live_message_is_explicit() -> None:
    with pytest.raises(SimulationOnlyViolation, match="real hardware"):
        assert_simulation_only("live")


def test_no_argument_defaults_to_simulation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_VAR, raising=False)
    assert assert_simulation_only() == "simulation"


@pytest.mark.parametrize("value", ["playground", "MUJOCO", "sim"])
def test_no_argument_reads_env_allowed(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(ENV_VAR, value)
    assert assert_simulation_only() == value.lower()


@pytest.mark.parametrize("value", ["live", "real", "tele", "Real-World", "production"])
def test_no_argument_reads_env_refused(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(ENV_VAR, value)
    with pytest.raises(SimulationOnlyViolation):
        assert_simulation_only()


def test_no_argument_empty_env_means_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty CW_RUNTIME_MODE (e.g. a verbatim copy of .env.example) means the default."""
    monkeypatch.setenv(ENV_VAR, "")
    assert assert_simulation_only() == "simulation"


def test_no_argument_uses_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """The no-argument form goes through get_settings(), not os.environ directly."""
    from skypaw.config import Settings
    from skypaw.sim import guard

    monkeypatch.setenv(ENV_VAR, "simulation")
    fake = Settings(_env_file=None, cw_runtime_mode="live")
    monkeypatch.setattr(guard, "get_settings", lambda: fake)
    with pytest.raises(SimulationOnlyViolation):
        assert_simulation_only()


def test_explicit_argument_wins_over_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_VAR, "simulation")
    with pytest.raises(SimulationOnlyViolation):
        assert_simulation_only("live")


def test_guard_is_reexported_from_sim_package() -> None:
    from skypaw import sim

    assert sim.assert_simulation_only is assert_simulation_only
    assert sim.SimulationOnlyViolation is SimulationOnlyViolation


def test_acceptance_a3_one_liner() -> None:
    """A3: the exact command from the task raises SimulationOnlyViolation."""
    cmd = "from skypaw.sim.guard import assert_simulation_only; assert_simulation_only('live')"
    proc = subprocess.run([sys.executable, "-c", cmd], capture_output=True, text=True, check=False)
    assert proc.returncode != 0
    assert "SimulationOnlyViolation" in proc.stderr
