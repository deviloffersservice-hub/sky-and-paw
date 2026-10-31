"""Sensor fusion and suggested triage, exactly as SPEC.md 6.3:

    fused = 1 - prod(1 - w_i) over present sensors, with
      RGB      = confidence
      thermal  = min(1, dT / 6) * 0.7
      radar    = 0.95 strong / 0.75 weak
      acoustic = 0.9
      CO2      = min(1, dppm / 200) * 0.5
    lifeSign = radar or acoustic

    Suggested triage: strong radar -> Immediate; weak radar or acoustic only -> Delayed;
    radar negative with no RGB and one sensor -> Expectant (decoy pattern); otherwise Unknown.
    The lead's choice always wins (not decided here).

``dppm`` is the excess over :data:`AMBIENT_CO2_PPM` = 300 ppm, the constant of the approved
mockup engine (``docs/mockups/ops-v2.html``: ``(v - 300) / 200``), clamped at 0.

Evidence dict (PROTOCOL snapshot shape): ``rgb`` float | None, ``thermal`` float (dT) | None,
``radar`` ``{bpm, snr}`` | None, ``acoustic`` bool | None, ``co2`` ppm | None, plus
``radar_negative`` bool (scanned, no life sign; SPEC 4 ``radarNegative``). A negative radar
contributes no weight and is not a "present sensor".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

AMBIENT_CO2_PPM = 300.0
SENSOR_KEYS = ("rgb", "thermal", "radar", "acoustic", "co2")
TRIAGE = ("immediate", "delayed", "unknown", "expectant")


def w_rgb(conf: float) -> float:
    return max(0.0, min(1.0, float(conf)))


def w_thermal(delta_t: float) -> float:
    return max(0.0, min(1.0, float(delta_t) / 6.0)) * 0.7


def w_radar(radar: dict[str, Any]) -> float:
    return 0.95 if radar.get("snr") == "strong" else 0.75


def w_acoustic(_: Any = True) -> float:
    return 0.9


def w_co2(ppm: float) -> float:
    return max(0.0, min(1.0, (float(ppm) - AMBIENT_CO2_PPM) / 200.0)) * 0.5


WEIGHTS = {
    "rgb": w_rgb,
    "thermal": w_thermal,
    "radar": w_radar,
    "acoustic": w_acoustic,
    "co2": w_co2,
}


def present(evidence: dict[str, Any]) -> list[str]:
    """Sensors with a positive reading (``None``/``False``/negative radar are absent)."""
    out = []
    for k in SENSOR_KEYS:
        v = evidence.get(k)
        if v is None or v is False:
            continue
        if k == "radar" and (not isinstance(v, dict) or v.get("negative") or not v.get("snr")):
            continue
        out.append(k)
    return out


@dataclass(frozen=True)
class Fusion:
    fused: float
    life_sign: bool
    suggested_triage: str
    sensors: tuple[str, ...]
    weights: dict[str, float]


def suggest_triage(evidence: dict[str, Any], sensors: list[str] | None = None) -> str:
    """SPEC 6.3 suggested triage (mockup ``fuse()`` order)."""
    sensors = present(evidence) if sensors is None else sensors
    radar = evidence.get("radar") if "radar" in sensors else None
    if radar is not None or "acoustic" in sensors:  # life sign
        return "immediate" if radar is not None and radar.get("snr") == "strong" else "delayed"
    if evidence.get("radar_negative") and "rgb" not in sensors and len(sensors) <= 1:
        return "expectant"
    return "unknown"


def fuse(evidence: dict[str, Any]) -> Fusion:
    """Fused confidence, life sign and suggested triage for one candidate."""
    sensors = present(evidence)
    weights = {k: WEIGHTS[k](evidence[k]) for k in sensors}
    miss = 1.0
    for w in weights.values():
        miss *= 1.0 - w
    fused = 1.0 - miss if sensors else 0.0
    life = "radar" in sensors or "acoustic" in sensors
    return Fusion(round(fused, 4), life, suggest_triage(evidence, sensors), tuple(sensors), weights)


__all__ = [
    "AMBIENT_CO2_PPM",
    "SENSOR_KEYS",
    "TRIAGE",
    "WEIGHTS",
    "Fusion",
    "fuse",
    "present",
    "suggest_triage",
]
