"""Build ``rubble.glb`` from ``site.yaml``: one level box per rubble row, nothing else.

Usage::

    python -m skypaw.scene.build_glb            # writes backend/skypaw/scene/rubble.glb
    python -m skypaw.scene.build_glb --out x.glb

Frame conversion (see ``site.yaml``): the site rows are in the y-up mockup frame. With
``glb.vertex_frame: world_z_up`` (default) each box is written in the Cyberwave world frame,
``(X, Y, Z) = (x, -z, y)``, because Cyberwave's MuJoCo import reads GLB vertices as Z-up world
metres (day-one round 2, R2-3); the glTF y-up convention is deliberately not applied. With
``gltf_y_up`` the boxes are written in the site frame unchanged.

Every box is its own mesh node (``rubble_00`` .. ``rubble_17``), never concatenated: MuJoCo
collides mesh geoms by their convex hull, so one merged mesh would become one hull spanning
the whole site. Output is deterministic (same bytes for the same ``site.yaml`` and trimesh
version) and about 20 kB.
"""

from __future__ import annotations

import argparse
import hashlib
import math
from pathlib import Path

import numpy as np
import trimesh

from skypaw.logging import configure_logging, get_logger, log_event
from skypaw.scene.site import DEFAULT_GLB_PATH, DEFAULT_SITE_PATH, RubbleBox, Site, load_site

log = get_logger(__name__)

#: Hard size limit from the M1 task.
MAX_GLB_BYTES = 1_000_000


def box_transform(box: RubbleBox, vertex_frame: str = "world_z_up") -> np.ndarray:
    """4x4 transform placing a unit-centred box of the right extents for ``box``."""
    c, s = math.cos(box.yaw), math.sin(box.yaw)
    t = np.eye(4)
    if vertex_frame == "world_z_up":
        # Rotation about world +Z by yaw; centre (x, -z, height / 2).
        t[:3, :3] = [[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]]
        t[:3, 3] = [box.x, -box.z + 0.0, box.sy / 2.0]
    elif vertex_frame == "gltf_y_up":
        # three.js rotation.y = yaw: (1, 0, 0) -> (cos, 0, -sin); centre (x, height / 2, z).
        t[:3, :3] = [[c, 0.0, s], [0.0, 1.0, 0.0], [-s, 0.0, c]]
        t[:3, 3] = [box.x, box.sy / 2.0, box.z]
    else:
        raise ValueError(f"unknown vertex_frame {vertex_frame!r}")
    return t


def box_extents(box: RubbleBox, vertex_frame: str = "world_z_up") -> tuple[float, float, float]:
    """Box size along the output frame's axes (before yaw)."""
    if vertex_frame == "world_z_up":
        return (box.sx, box.sz, box.sy)
    return (box.sx, box.sy, box.sz)


def build_scene(site: Site) -> trimesh.Scene:
    """A trimesh Scene with one named mesh node per rubble box."""
    scene = trimesh.Scene()
    for i, box in enumerate(site.rubble):
        mesh = trimesh.creation.box(
            extents=box_extents(box, site.glb_vertex_frame),
            transform=box_transform(box, site.glb_vertex_frame),
        )
        name = f"rubble_{i:02d}"
        scene.add_geometry(mesh, node_name=name, geom_name=name)
    return scene


def build_glb_bytes(site: Site) -> bytes:
    """Serialise the rubble scene to GLB bytes (deterministic)."""
    data = build_scene(site).export(file_type="glb")
    if not isinstance(data, bytes | bytearray):
        raise TypeError(f"trimesh returned {type(data).__name__}, expected bytes")
    data = bytes(data)
    if len(data) >= MAX_GLB_BYTES:
        raise ValueError(f"rubble.glb is {len(data)} bytes; the limit is {MAX_GLB_BYTES}")
    return data


def sha256(data: bytes) -> str:
    """Hex SHA-256 of ``data``."""
    return hashlib.sha256(data).hexdigest()


def write_glb(
    out: Path | str = DEFAULT_GLB_PATH, site_path: Path | str = DEFAULT_SITE_PATH
) -> tuple[Path, str]:
    """Build the GLB from ``site_path`` and write it to ``out``; returns (path, sha256)."""
    site = load_site(site_path)
    data = build_glb_bytes(site)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    digest = sha256(data)
    log_event(
        log,
        "rubble_glb_built",
        path=str(out),
        bytes=len(data),
        sha256=digest,
        boxes=len(site.rubble),
        vertex_frame=site.glb_vertex_frame,
    )
    return out, digest


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: build the GLB and log its size and hash."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--site", default=str(DEFAULT_SITE_PATH), help="path to site.yaml")
    parser.add_argument("--out", default=str(DEFAULT_GLB_PATH), help="output .glb path")
    args = parser.parse_args(argv)
    configure_logging("INFO")
    write_glb(args.out, args.site)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
