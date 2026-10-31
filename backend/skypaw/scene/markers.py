"""Victim markers for the live MuJoCo scene: coloured boxes that **stand in for people**.

The Cyberwave MuJoCo scene has no human models, so the drone camera can never see a person.
For live runs every victim in ``site.yaml`` gets a 0.6 x 0.6 x 0.4 m box at its ground-truth
position (site frame -> world frame via ``site_to_world``): **bright orange** for victims,
**slate grey** for the decoy (``D-01``, the sun-heated sheet-metal pile). The orange boxes are
found by :class:`skypaw.perception.detector.MarkerDetector` (an HSV colour heuristic); YOLO
stays the production person detector. Candidates that came from a marker say so in
``Candidate.source`` (``rgb = "marker"``, ``rgb_note = MARKER_NOTE``). The slate decoy box is
a visual prop only: the colour detector reports it as a non-person object, so the decoy keeps
the SPEC 6.3 decoy pattern (thermal only, radar negative, no RGB person).

Markers are built with trimesh exactly like ``build_glb.py`` (deterministic bytes, vertices in
world Z-up metres, box centred on the twin origin) and uploaded / PATCHed with the same raw
calls as ``skypaw.sim.scene`` (``ensure_asset_glb``: raw GLB upload, MuJoCo backends,
``fixed_base``). There are two assets (one per colour) and one named twin per victim,
``marker-<id>``. :func:`ensure_markers` is idempotent: a second run only reads.

The twin lookup is by exact name. ``skypaw.sim.scene.ensure_twin`` is not used for the twins
because it falls back to "any twin of the same asset", which would make ``marker-V-02`` reuse
``marker-V-01``; :func:`ensure_marker_twin` is the thin name-only wrapper.
"""

from __future__ import annotations

import argparse
import hashlib
from dataclasses import dataclass
from typing import Any

import trimesh

from skypaw.logging import configure_logging, get_logger, log_event
from skypaw.scene.site import Site, SitePose, load_site, site_to_world
from skypaw.sim.scene import (
    SceneContext,
    _by_name,
    _pose_fields,
    _uuid,
    ensure_asset_glb,
    place,
)

log = get_logger(__name__)

#: Marker box size in metres: (along X, along Y, height Z), world frame.
MARKER_EXTENTS = (0.6, 0.6, 0.4)
#: RGBA colours. Orange = victim (person stand-in); slate grey = decoy prop.
VICTIM_RGBA = (255, 110, 0, 255)
DECOY_RGBA = (96, 112, 140, 255)
ASSET_NAMES = {"victim": "skypaw-marker-victim", "decoy": "skypaw-marker-decoy"}
MARKER_NOTE = "marker box stands in for a person (no human models in the MuJoCo scene)"


def marker_name(victim_id: str) -> str:
    """Twin name of a victim's marker, e.g. ``marker-V-01``."""
    return f"marker-{victim_id}"


def marker_kind(victim: dict[str, Any]) -> str:
    """``"decoy"`` for decoys in site.yaml, else ``"victim"``."""
    return "decoy" if victim.get("decoy") else "victim"


def build_marker_glb(kind: str) -> bytes:
    """Deterministic GLB of one coloured marker box centred on the origin (world Z up).

    The colour is the material's PBR ``baseColorFactor`` (glTF core; whether Cyberwave's
    MuJoCo import keeps it is checked live, see tests/evidence/V1-B).
    """
    rgba = VICTIM_RGBA if kind == "victim" else DECOY_RGBA
    mesh = trimesh.creation.box(extents=MARKER_EXTENTS)
    material = trimesh.visual.material.PBRMaterial(
        name=f"skypaw_marker_{kind}",
        baseColorFactor=list(rgba),
        metallicFactor=0.0,
        roughnessFactor=1.0,
        doubleSided=False,
    )
    mesh.visual = trimesh.visual.TextureVisuals(material=material)
    scene = trimesh.Scene()
    scene.add_geometry(mesh, node_name=f"marker_{kind}", geom_name=f"marker_{kind}")
    data = scene.export(file_type="glb")
    if not isinstance(data, bytes | bytearray):
        raise TypeError(f"trimesh returned {type(data).__name__}, expected bytes")
    return bytes(data)


@dataclass(frozen=True)
class MarkerSpec:
    """One marker twin to place."""

    victim_id: str
    kind: str
    pose: SitePose  # site frame (``site.yaml`` victim pos)

    @property
    def name(self) -> str:
        return marker_name(self.victim_id)

    @property
    def world(self) -> tuple[float, float, float]:
        return site_to_world(*self.pose.pos)


def marker_specs(site: Site) -> list[MarkerSpec]:
    """One spec per victim in ``site.yaml`` (positions come only from the scene config)."""
    out = []
    for v in site.victims:
        x, y, z = (float(c) for c in v["pos"])
        out.append(MarkerSpec(str(v["id"]), marker_kind(v), SitePose(pos=(x, y, z), yaw=0.0)))
    return out


def ensure_marker_twin(
    ctx: SceneContext, env_uuid: str | None, name: str, asset_uuid: str | None, pose: SitePose
) -> str | None:
    """Twin uuid by exact name; created (``fixed_base=True``) at ``pose`` if missing."""
    cw = ctx.cw
    twins = list(cw.twins.list(environment_id=env_uuid)) if env_uuid else []
    found = _by_name(twins, name)
    if found:
        ctx.step("reuse", "twin", name)
        return _uuid(found[0])
    if ctx.dry_run or env_uuid is None or asset_uuid is None:
        ctx.step("create", "twin", name, f"planned at world {pose.world}")
        return None
    twin = cw.twins.create(
        asset_id=asset_uuid,
        environment_id=env_uuid,
        name=name,
        fixed_base=True,
        **_pose_fields(pose),
    )
    ctx.step("create", "twin", name, f"at world {pose.world}")
    return _uuid(twin)


def ensure_markers(
    cw: Any, env_id: str, site: Site | None = None, *, dry_run: bool = False
) -> dict:
    """Ensure both marker assets and one ``marker-<id>`` twin per victim; idempotent.

    Returns ``{"assets": {kind: uuid}, "twins": {name: uuid}, "world": {name: [x, y, z]},
    "steps": [...]}``.
    """
    site = site or load_site()
    ctx = SceneContext(cw, dry_run=dry_run)
    specs = marker_specs(site)
    assets: dict[str, str | None] = {}
    for kind in sorted({s.kind for s in specs}):
        glb = build_marker_glb(kind)
        first = next(s for s in specs if s.kind == kind)
        assets[kind] = ensure_asset_glb(
            ctx,
            ASSET_NAMES[kind],
            glb,
            hashlib.sha256(glb).hexdigest(),
            filename=f"{ASSET_NAMES[kind]}.glb",
            env_uuid=env_id,
            twin_name=first.name,
        )
    twins: dict[str, str | None] = {}
    for s in specs:
        uid = ensure_marker_twin(ctx, env_id, s.name, assets[s.kind], s.pose)
        place(ctx, env_id, uid, s.name, s.pose)
        twins[s.name] = uid
    result = {
        "assets": assets,
        "twins": twins,
        "world": {s.name: list(s.world) for s in specs},
        "steps": [f"{st.action} {st.kind} {st.name} {st.detail}".strip() for st in ctx.steps],
        "notes": {k: v for k, v in ctx.notes.items() if k != "workspace_uuid"},
    }
    log_event(log, "markers_ensured", twins=len(twins), dry_run=dry_run)
    return result


def main(argv: list[str] | None = None) -> int:
    """``python -m skypaw.scene.markers --env <name> [--dry-run]``."""
    from skypaw.config import get_settings
    from skypaw.sim.client import get_client

    ap = argparse.ArgumentParser(description="Ensure victim marker boxes in an environment.")
    ap.add_argument("--env", default=get_settings().cw_environment_name)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    configure_logging(get_settings().log_level)
    cw = get_client()
    env = next(e for e in cw.environments.list() if e.name == args.env)
    out = ensure_markers(cw, str(env.uuid), dry_run=args.dry_run)
    for line in out["steps"]:
        print(line)
    return 0


__all__ = [
    "ASSET_NAMES",
    "DECOY_RGBA",
    "MARKER_EXTENTS",
    "MARKER_NOTE",
    "VICTIM_RGBA",
    "MarkerSpec",
    "build_marker_glb",
    "ensure_marker_twin",
    "ensure_markers",
    "marker_kind",
    "marker_name",
    "marker_specs",
]

if __name__ == "__main__":
    raise SystemExit(main())
