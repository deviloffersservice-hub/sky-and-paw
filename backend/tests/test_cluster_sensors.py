"""Clustering (1.0 m) and the synthetic sensors (seeded, labelled, read from site.yaml)."""

from __future__ import annotations

import math

import pytest

from skypaw.perception.cluster import Hit, add_hits, cluster_hits
from skypaw.sensors.synthetic import DETECTION_RADIUS_M, SyntheticSensors, load_truth


def test_hits_within_one_metre_merge_and_keep_best_conf_and_frames() -> None:
    hits = [
        Hit((1.0, 1.0, 0.5), "person", 0.7, "yolo", "f1"),
        Hit((1.6, 1.2, 0.5), "person", 0.9, "yolo", "f2"),
        Hit((1.3, 0.6, 0.5), "thermal", None, "synthetic", "f2"),
        Hit((4.0, 1.0, 0.5), "person", 0.6, "marker", "f3"),
    ]
    cl = cluster_hits(hits)
    assert len(cl) == 2
    big = max(cl, key=lambda c: len(c.hits))
    assert big.rgb_conf == 0.9
    assert big.frames == ["f1", "f2"]
    # position = mean of visual hits only (thermal hotspot excluded when visual exists)
    assert big.position[0] == pytest.approx(1.3) and big.position[1] == pytest.approx(1.1)


def test_hits_further_than_one_metre_stay_apart() -> None:
    cl = cluster_hits([Hit((0, 0, 0), "person", 0.8), Hit((1.05, 0, 0), "person", 0.8)])
    assert len(cl) == 2


def test_running_set_merges_clusters_that_drift_together() -> None:
    cl = cluster_hits([Hit((0, 0, 0), "person", 0.8), Hit((1.2, 0, 0), "person", 0.8)])
    assert len(cl) == 2
    add_hits(cl, [Hit((0.65, 0, 0), "person", 0.8)])  # joins 1.2 -> centroid 0.925 -> merge
    assert len(cl) == 1 and len(cl[0].hits) == 3


def test_thermal_only_cluster_uses_hotspot_position() -> None:
    cl = cluster_hits([Hit((2.0, 3.0, 0.4), "thermal", None, "synthetic")])
    assert cl[0].position == (2.0, 3.0, 0.4) and cl[0].rgb_conf is None


# --- synthetic sensors ------------------------------------------------------------------
def test_truth_is_read_from_site_yaml_in_world_frame() -> None:
    truths = {t.id: t for t in load_truth()}
    assert set(truths) == {"V-01", "V-02", "V-03", "D-01"}
    assert truths["V-01"].world == (3.5, 2.0, 0.5)  # site (3.5, 0.5, -2.0) -> world
    assert truths["D-01"].decoy and truths["D-01"].thermal == 9.2


def test_readings_are_labelled_and_deterministic() -> None:
    a, b = SyntheticSensors(seed=7), SyntheticSensors(seed=7)
    pos = (3.6, 2.1, 0.5)
    ta, tb = a.thermal(pos), b.thermal(pos)
    assert ta == tb and ta is not None
    assert ta["source"] == "synthetic" and ta["truth_id"] == "V-01"
    assert abs(ta["delta_t"] - 5.8) < 1.0
    ra = a.uwb_radar(pos, sensor_pos=(3.0, 2.0, 6.0))
    assert ra == b.uwb_radar(pos, sensor_pos=(3.0, 2.0, 6.0))
    assert ra is not None and ra["snr"] == "strong" and ra["source"] == "synthetic"
    assert abs(ra["bpm"] - 64) <= 8
    assert SyntheticSensors(seed=8).thermal(pos) != ta  # noise depends on the seed


def test_radar_needs_the_sensor_within_spec_radius() -> None:
    s = SyntheticSensors()
    v01 = (3.5, 2.0, 0.5)
    assert s.uwb_radar(v01, sensor_pos=(3.5 + DETECTION_RADIUS_M + 0.1, 2.0, 6.0)) is None
    assert s.uwb_radar(v01, sensor_pos=(3.5 + DETECTION_RADIUS_M - 0.1, 2.0, 6.0)) is not None


def test_decoy_radar_is_negative_and_hot() -> None:
    s = SyntheticSensors()
    d01 = (-7.5, 5.2, 0.4)
    r = s.uwb_radar(d01, sensor_pos=(-7.0, 5.0, 6.0))
    assert r is not None and r["negative"] is True and r["bpm"] is None
    t = s.thermal(d01)
    assert t is not None and t["delta_t"] > 8.0


def test_nothing_near_gives_negative_radar_and_no_thermal() -> None:
    s = SyntheticSensors()
    assert s.thermal((0.0, 0.0, 0.0)) is None
    r = s.uwb_radar((0.0, 0.0, 0.0), sensor_pos=(0.0, 0.0, 6.0))
    assert r is not None and r["negative"] and r["truth_id"] is None


def test_thermal_hotspots_only_inside_footprint() -> None:
    s = SyntheticSensors(seed=1)
    square = [(2.0, 0.0), (6.0, 0.0), (6.0, 4.0), (2.0, 4.0)]  # contains V-01 only
    hs = s.thermal_hotspots(square)
    assert [h["truth_id"] for h in hs] == ["V-01"]
    assert math.dist(hs[0]["position"][:2], (3.5, 2.0)) < 0.6
    assert hs[0]["source"] == "synthetic"
    assert s.thermal_hotspots([(-1, -1), (1, -1), (1, 1), (-1, 1)]) == []


def test_acoustic_and_co2_hooks() -> None:
    s = SyntheticSensors()
    assert s.acoustic((3.5, 2.0, 0.5)) is None
    assert s.acoustic((3.5, 2.0, 0.5), after_speech=True)["source"] == "synthetic"  # type: ignore[index]
    assert s.acoustic((-7.5, 5.2, 0.4), after_speech=True) is None  # decoy never taps
    co2 = s.co2((3.5, 2.0, 0.5))
    assert co2 is not None and abs(co2["ppm"] - 420) < 60 and co2["source"] == "synthetic"
