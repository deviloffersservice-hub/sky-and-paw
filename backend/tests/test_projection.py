"""Pixel <-> world projection: synthetic cameras and the real M2 overhead frame."""

from __future__ import annotations

import json
import math
from pathlib import Path

import cv2
import numpy as np
import pytest

from skypaw.perception.project import (
    MOUNT_DOWN_NAME,
    MOUNTS,
    CameraPose,
    Intrinsics,
    ground_footprint,
    pixel_to_world,
    quat_wxyz_to_matrix,
    world_to_pixel,
    yaw_to_quat_wxyz,
)
from skypaw.scene.site import load_site

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "frames"


def test_focal_length_from_vertical_fov() -> None:
    i = Intrinsics()
    assert i.f == pytest.approx(180 / math.tan(math.radians(29.36)))
    assert i.f == pytest.approx(320.0, abs=0.5)  # horizontal fov ~90 deg


def test_quaternion_yaw_matrix() -> None:
    r = quat_wxyz_to_matrix(yaw_to_quat_wxyz(math.pi / 2))
    assert np.allclose(r @ [1, 0, 0], [0, 1, 0], atol=1e-9)


def test_image_centre_projects_below_the_drone() -> None:
    """M3-A2: centre box from a drone at 6 m above (x, y) lands within 0.3 m of (x, y)."""
    for yaw in (0.0, 0.4, -2.0):
        pose = CameraPose.from_frame([2.0, -3.0, 6.0], yaw_to_quat_wxyz(yaw))
        p = pixel_to_world(pose, 320, 180, plane_z=0.0)
        assert p is not None
        assert math.hypot(p[0] - 2.0, p[1] + 3.0) < 1e-6


@pytest.mark.parametrize("yaw", [0.0, 0.7, math.pi, -1.2])
@pytest.mark.parametrize("pt", [(3.5, 2.0, 0.5), (-1.0, -1.5, 0.0), (0.5, 3.0, 0.6)])
def test_round_trip(yaw: float, pt: tuple[float, float, float]) -> None:
    """World -> pixel -> world on the same plane returns the point (well under 0.5 m)."""
    pose = CameraPose.from_frame([0.3, 0.1, 6.0], yaw_to_quat_wxyz(yaw))
    uv = world_to_pixel(pose, pt)
    assert uv is not None
    back = pixel_to_world(pose, *uv, plane_z=pt[2])
    assert back is not None
    assert math.dist(back, pt) < 1e-6


def test_off_centre_box_with_pixel_noise_within_half_metre() -> None:
    """M3-A2: a 3 px box-centre error at 6 m altitude stays within 0.5 m on the ground."""
    pose = CameraPose.from_frame([0.0, 0.0, 6.0], yaw_to_quat_wxyz(0.3))
    pt = (4.0, -2.0, 0.0)
    u, v = world_to_pixel(pose, pt)  # type: ignore[misc]
    back = pixel_to_world(pose, u + 3, v - 3, plane_z=0.0)
    assert back is not None and math.dist(back, pt) < 0.5


def test_mount_orientation_body_axes() -> None:
    """Downward camera: image down = body +x, image right = body +y (asset offset 90 deg)."""
    pose = CameraPose.from_frame([0.0, 0.0, 6.0], [1, 0, 0, 0])
    below_centre = pixel_to_world(pose, 320, 300)
    right_of_centre = pixel_to_world(pose, 500, 180)
    assert below_centre is not None and right_of_centre is not None
    assert below_centre[0] > 1.0 and abs(below_centre[1]) < 1e-9
    assert right_of_centre[1] > 1.0 and abs(right_of_centre[0]) < 1e-9


def test_ray_pointing_up_returns_none() -> None:
    pose = CameraPose.from_frame([0.0, 0.0, 6.0], [1, 0, 0, 0])
    assert pixel_to_world(pose, 320, 180, plane_z=7.0) is None


def test_footprint_size_at_six_metres() -> None:
    pose = CameraPose.from_frame([0.0, 0.0, 6.0], [1, 0, 0, 0])
    fp = np.array(ground_footprint(pose))
    assert np.ptp(fp[:, 1]) == pytest.approx(12.0, abs=0.05)  # image width along body y
    assert np.ptp(fp[:, 0]) == pytest.approx(2 * 6 * math.tan(math.radians(29.36)), abs=0.05)


def _rubble_iou(img: np.ndarray, pose: CameraPose) -> float:
    obs = (cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) > 150).astype(np.uint8)
    m = np.zeros_like(obs)
    for b in load_site().rubble:
        c, s = math.cos(b.yaw), math.sin(b.yaw)
        pts = []
        for dx, dy in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
            lx, ly = dx * b.sx / 2, dy * b.sz / 2
            uv = world_to_pixel(pose, (b.x + c * lx - s * ly, -b.z + s * lx + c * ly, b.sy))
            if uv is None:
                break
            pts.append((round(uv[0]), round(uv[1])))
        if len(pts) == 4:
            cv2.fillPoly(m, [np.array(pts, dtype=np.int32)], 1)
    return float((m & obs).sum()) / float((m | obs).sum())


def test_real_m2_frame_matches_rubble_only_with_chosen_mount() -> None:
    """The site.yaml rubble tops, projected with the default mount, cover the bright slabs of
    the real overhead frame (IoU >= 0.8); the other in-plane orientations do not."""
    img = cv2.imread(str(FIXTURES / "m2_overhead_bottom.jpg"))
    meta = json.loads((FIXTURES / "m2_overhead_bottom.json").read_text())
    pos = np.array(meta["position"]) + [*meta["fitted_camera_offset_xy"], 0.0]
    scores = {
        name: _rubble_iou(img, CameraPose.from_frame(pos, meta["rotation"], None, mount))
        for name, mount in MOUNTS.items()
    }
    assert scores[MOUNT_DOWN_NAME] >= 0.8
    assert all(scores[MOUNT_DOWN_NAME] > v + 0.1 for k, v in scores.items() if k != MOUNT_DOWN_NAME)
