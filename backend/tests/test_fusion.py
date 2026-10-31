"""SPEC 6.3 fusion and suggested triage, with the exact numbers of the scene truth."""

from __future__ import annotations

import pytest

from skypaw.perception.fusion import fuse, present, suggest_triage


def ev(**kw: object) -> dict[str, object]:
    base: dict[str, object] = {
        "rgb": None,
        "thermal": None,
        "radar": None,
        "acoustic": None,
        "co2": None,
        "radar_negative": False,
    }
    base.update(kw)
    return base


def test_v01_rgb_thermal_strong_radar_is_immediate() -> None:
    # 1 - (1-.91)(1-min(1,5.8/6)*.7)(1-.95) = 1 - .09 * .32333 * .05 = 0.998545
    f = fuse(ev(rgb=0.91, thermal=5.8, radar={"bpm": 64, "snr": "strong"}))
    assert f.fused >= 0.99
    assert f.fused == pytest.approx(0.9985, abs=5e-5)
    assert f.life_sign is True
    assert f.suggested_triage == "immediate"
    assert f.weights["thermal"] == pytest.approx(5.8 / 6 * 0.7)


def test_v02_thermal_weak_radar_is_delayed() -> None:
    # 1 - (1 - 3.1/6*.7)(1-.75) = 1 - .638333 * .25 = 0.840417
    f = fuse(ev(thermal=3.1, radar={"bpm": 71, "snr": "weak"}))
    assert round(f.fused, 2) == 0.84
    assert f.life_sign is True
    assert f.suggested_triage == "delayed"


def test_d01_thermal_only_radar_negative_is_expectant() -> None:
    f = fuse(ev(thermal=9.2, radar_negative=True))
    assert round(f.fused, 2) == 0.70  # min(1, 9.2/6) * 0.7
    assert f.life_sign is False
    assert f.suggested_triage == "expectant"


def test_v03_rgb_064_only_is_unknown() -> None:
    f = fuse(ev(rgb=0.64))
    assert round(f.fused, 2) == 0.64
    assert f.life_sign is False
    assert f.suggested_triage == "unknown"


def test_v03_with_negative_radar_stays_unknown_because_rgb_present() -> None:
    assert fuse(ev(rgb=0.64, radar_negative=True)).suggested_triage == "unknown"


def test_negative_radar_with_two_sensors_is_not_expectant() -> None:
    assert fuse(ev(thermal=9.2, co2=420, radar_negative=True)).suggested_triage == "unknown"


def test_thermal_without_radar_scan_is_unknown() -> None:
    assert fuse(ev(thermal=9.2)).suggested_triage == "unknown"


def test_acoustic_only_is_delayed_with_life_sign() -> None:
    f = fuse(ev(acoustic=True))
    assert f.fused == pytest.approx(0.9)
    assert f.life_sign and f.suggested_triage == "delayed"


def test_strong_radar_wins_over_acoustic() -> None:
    f = fuse(ev(acoustic=True, radar={"bpm": 60, "snr": "strong"}))
    assert f.suggested_triage == "immediate"
    assert f.fused == pytest.approx(1 - 0.1 * 0.05)


def test_co2_weight_uses_mockup_ambient() -> None:
    assert fuse(ev(co2=420)).fused == pytest.approx(min(1, 120 / 200) * 0.5)
    assert fuse(ev(co2=900)).fused == pytest.approx(0.5)
    assert fuse(ev(co2=250)).fused == 0.0


def test_no_sensors_is_zero_unknown() -> None:
    f = fuse(ev())
    assert f.fused == 0.0 and not f.life_sign and f.suggested_triage == "unknown"


def test_negative_radar_dict_is_not_a_present_sensor() -> None:
    e = ev(thermal=9.2, radar={"bpm": None, "snr": None, "negative": True}, radar_negative=True)
    assert present(e) == ["thermal"]
    assert suggest_triage(e) == "expectant"
