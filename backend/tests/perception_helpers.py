"""Shared helpers for the perception tests (not a test module)."""

from __future__ import annotations

import os
from pathlib import Path

import cv2
import numpy as np

from skypaw.perception.project import CameraPose, world_to_pixel
from skypaw.scene.markers import DECOY_RGBA, MARKER_EXTENTS, VICTIM_RGBA
from skypaw.sensors.synthetic import Truth

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "fixtures"


def bgr(rgba: tuple[int, int, int, int]) -> tuple[int, int, int]:
    return (rgba[2], rgba[1], rgba[0])


def paint_markers(img: np.ndarray, pose: CameraPose, truths: list[Truth]) -> list[str]:
    """Paint the top face of each truth's marker box (0.6 m, top at centre + 0.2 m) into
    ``img`` where the camera at ``pose`` would see it; returns the ids painted."""
    hx, hy, hz = (e / 2 for e in MARKER_EXTENTS)
    painted = []
    rng = np.random.default_rng(0)
    for t in truths:
        x, y, z = t.world
        pts = [
            world_to_pixel(pose, (x + dx * hx, y + dy * hy, z + hz))
            for dx, dy in ((-1, -1), (1, -1), (1, 1), (-1, 1))
        ]
        if any(p is None for p in pts):
            continue
        poly = np.array([(round(p[0]), round(p[1])) for p in pts if p], dtype=np.int32)
        if not (
            (poly[:, 0] >= 0)
            & (poly[:, 0] < img.shape[1])
            & (poly[:, 1] >= 0)
            & (poly[:, 1] < img.shape[0])
        ).all():
            continue
        colour = bgr(DECOY_RGBA if t.decoy else VICTIM_RGBA)
        layer = np.zeros(img.shape[:2], np.uint8)
        cv2.fillPoly(layer, [poly], 255)
        noise = rng.normal(0, 6, img.shape).astype(np.int16)
        shaded = np.clip(np.array(colour, np.int16) * 0.85 + noise, 0, 255).astype(np.uint8)
        img[layer > 0] = shaded[layer > 0]
        painted.append(t.id)
    return painted


def weights_dir() -> Path | None:
    """``DATA_DIR/models`` (or ``<repo>/data/models``) if it holds the YOLO weights."""
    for base in (os.environ.get("DATA_DIR"), str(REPO / "data")):
        if base and (Path(base) / "models" / "yolov8n.pt").exists():
            return Path(base) / "models"
    return None
