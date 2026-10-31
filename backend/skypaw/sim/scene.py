"""Idempotent Cyberwave scene setup: project, environment, rubble asset, twins, waypoints.

Every ``ensure_*`` function looks its object up by name first and only creates what is
missing, so a second run sends no POST at all. ``place`` writes a twin's editor pose only when
it differs from the target. With ``SceneContext(dry_run=True)`` nothing is written: missing
objects are recorded as planned steps and returned as ``None``.

All positions sent to Cyberwave are in the world frame (Z up); see ``skypaw.scene.site``.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from typing import Any

from skypaw.logging import get_logger, log_event
from skypaw.scene.site import Site, SitePose, site_to_world, yaw_quaternion
from skypaw.sim.client import assert_client_simulation, resolve_workspace_id

log = get_logger(__name__)

GO2_SLUG = "unitree/go2"
DRONE_SLUG = "skydio/skydio-x2"
RUBBLE_ASSET_NAME = "skypaw-rubble"
TWIN_NAMES = {"go2": "skypaw-go2", "drone": "skypaw-drone", "rubble": "skypaw-rubble"}
MUJOCO_BACKENDS = ["playground", "mujoco"]
GLB_SHA_KEY = "skypaw_glb_sha256"
FIXED_BASE_TRIED_KEY = "skypaw_fixed_base_requested"
POSE_TOLERANCE = 1e-3


@dataclass
class PlanStep:
    """One thing the setup did or would do."""

    action: str  # reuse | create | upload | patch | place | skip
    kind: str
    name: str
    detail: str = ""


@dataclass
class SceneContext:
    """Client plus mode; collects the plan/report of every step."""

    cw: Any
    dry_run: bool = False
    steps: list[PlanStep] = field(default_factory=list)
    notes: dict[str, Any] = field(default_factory=dict)

    def step(self, action: str, kind: str, name: str, detail: str = "") -> None:
        """Record and log a step."""
        self.steps.append(PlanStep(action, kind, name, detail))
        log_event(
            log,
            "scene_step",
            action=action,
            kind=kind,
            name=name,
            detail=detail,
            dry_run=self.dry_run,
        )


@dataclass
class SceneHandles:
    """Every uuid the mission needs (``None`` for objects a dry run would create)."""

    workspace_uuid: str | None = None
    project_uuid: str | None = None
    environment_uuid: str | None = None
    rubble_asset_uuid: str | None = None
    go2_twin_uuid: str | None = None
    drone_twin_uuid: str | None = None
    rubble_twin_uuid: str | None = None  # piece 00 (MQTT rubble-pose probe)
    waypoints: dict[str, str | None] = field(default_factory=dict)
    rubble_pieces: dict[str, str | None] = field(default_factory=dict)  # name -> twin uuid
    markers: dict[str, str | None] = field(default_factory=dict)  # marker-<id> -> twin uuid

    def as_dict(self) -> dict[str, Any]:
        """uuids only (safe for evidence/handles.json)."""
        return asdict(self)


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _uuid(obj: Any) -> str:
    return str(_get(obj, "uuid"))


def _by_name(items: Iterable[Any], name: str) -> list[Any]:
    return [i for i in items if _get(i, "name") == name]


# --- project / environment --------------------------------------------------------------
def ensure_project(ctx: SceneContext, name: str, workspace_uuid: str | None = None) -> str | None:
    """Project uuid by name; create it in the workspace if missing.

    The project's workspace uuid is stored in ``ctx.notes["workspace_uuid"]``.
    """
    cw = ctx.cw
    found = _by_name(cw.projects.list(), name)
    if found:
        ctx.step("reuse", "project", name)
        ctx.notes["workspace_uuid"] = _get(found[0], "workspace_uuid")
        return _uuid(found[0])
    if ctx.dry_run:
        ctx.step("create", "project", name, "planned")
        return None
    ws = workspace_uuid or resolve_workspace_id(cw)
    project = cw.projects.create(
        name=name, workspace_id=ws, description="Sky & Paw (simulation only)"
    )
    ctx.step("create", "project", name)
    ctx.notes["workspace_uuid"] = _get(project, "workspace_uuid") or ws
    return _uuid(project)


def ensure_environment(ctx: SceneContext, name: str, project_uuid: str | None) -> str | None:
    """Environment uuid by name inside the project; created (with project_id) if missing.

    ``project_id`` is always passed: without it the SDK crashes on accounts with no project
    (RESEARCH §6, "Environments").
    """
    cw = ctx.cw
    if project_uuid:
        found = _by_name(cw.environments.list(project_id=project_uuid), name)
        if found:
            ctx.step("reuse", "environment", name)
            return _uuid(found[0])
    if ctx.dry_run or project_uuid is None:
        ctx.step("create", "environment", name, "planned")
        return None
    env = cw.environments.create(
        name=name, project_id=project_uuid, description="Sky & Paw M1 scene (simulation only)"
    )
    ctx.step("create", "environment", name)
    return _uuid(env)


# --- rubble asset ---------------------------------------------------------------------------
def upload_glb_bytes(cw: Any, asset_uuid: str, filename: str, data: bytes) -> Any:
    """Attach GLB bytes to an asset with the raw generated API call.

    ``cw.assets.upload_glb`` is broken in cyberwave 0.7.4 (it passes an open file object and
    the generated client's pydantic validation rejects it); the raw call with a
    ``(filename, bytes)`` tuple works (RESEARCH §6, "Custom GLB").
    """
    return cw.api.src_app_api_assets_upload_glb(asset_uuid, file=(filename, data))


def _asset_update_schema(**fields: Any) -> Any:
    from cyberwave.rest.models.asset_update_schema import AssetUpdateSchema

    return AssetUpdateSchema(**fields)


def find_asset(
    ctx: SceneContext, name: str, env_uuid: str | None, twin_name: str | None
) -> str | None:
    """Existing asset uuid for ``name`` without listing all assets.

    ``cw.assets.list()`` does not return user-created assets (verified live 2026-10-31; it
    caused a duplicate asset in M1), so the lookup is: (1) the ``asset_uuid`` of the twin
    named ``twin_name`` in the environment, (2) ``cw.assets.search(name)`` filtered to an
    exact name match, preferring a ``registry_id`` that ends in ``/<name>``.
    """
    cw = ctx.cw
    if env_uuid and twin_name:
        twin = next(iter(_by_name(cw.twins.list(environment_id=env_uuid), twin_name)), None)
        if twin is not None and _get(twin, "asset_uuid"):
            return str(_get(twin, "asset_uuid"))
    matches = _by_name(cw.assets.search(name) or [], name)
    matches.sort(key=lambda a: not str(_get(a, "registry_id") or "").endswith(f"/{name}"))
    return _uuid(matches[0]) if matches else None


def ensure_asset_glb(
    ctx: SceneContext,
    name: str,
    glb: bytes,
    glb_sha256: str,
    *,
    filename: str = "rubble.glb",
    env_uuid: str | None = None,
    twin_name: str | None = None,
) -> str | None:
    """Asset uuid (see :func:`find_asset`), with this exact GLB attached and MuJoCo enabled.

    * upload only when ``glb_file`` is missing or the stored ``skypaw_glb_sha256`` differs;
    * PATCH ``supported_simulation_backends=["playground","mujoco"]`` and ``fixed_base=True``
      only when needed (a new asset defaults to ``["playground"]`` and blocks MuJoCo);
    * if the server rejects ``fixed_base``, PATCH again without it; whether it was accepted is
      recorded in ``ctx.notes["fixed_base_accepted"]`` either way.
    """
    cw = ctx.cw
    found = find_asset(ctx, name, env_uuid, twin_name)
    if found:
        asset_uuid = found
        ctx.step("reuse", "asset", name)
    elif ctx.dry_run:
        ctx.step("create", "asset", name, "planned")
        ctx.step("upload", "asset", name, f"planned, {len(glb)} bytes sha256 {glb_sha256[:12]}")
        ctx.step("patch", "asset", name, "planned: backends playground+mujoco, fixed_base")
        return None
    else:
        asset = cw.assets.create(
            name=name, description="Sky & Paw rubble (generated from site.yaml)"
        )
        asset_uuid = _uuid(asset)
        ctx.step("create", "asset", name)

    asset = cw.api.src_app_api_assets_get_asset(asset_uuid)
    metadata = dict(_get(asset, "metadata") or {})
    backends = list(_get(asset, "supported_simulation_backends") or [])
    needs_upload = not _get(asset, "glb_file") or metadata.get(GLB_SHA_KEY) != glb_sha256
    needs_backends = "mujoco" not in backends
    needs_fixed = _get(asset, "fixed_base") is not True and not metadata.get(FIXED_BASE_TRIED_KEY)

    if needs_upload:
        if ctx.dry_run:
            ctx.step("upload", "asset", name, f"planned, sha256 {glb_sha256[:12]}")
        else:
            upload_glb_bytes(cw, asset_uuid, filename, glb)
            ctx.step("upload", "asset", name, f"{len(glb)} bytes sha256 {glb_sha256[:12]}")

    if needs_upload or needs_backends or needs_fixed:
        metadata.update({GLB_SHA_KEY: glb_sha256, FIXED_BASE_TRIED_KEY: True})
        if ctx.dry_run:
            ctx.step("patch", "asset", name, "planned: backends playground+mujoco, fixed_base")
            return asset_uuid
        try:
            cw.api.src_app_api_assets_update_asset(
                asset_uuid,
                _asset_update_schema(
                    supported_simulation_backends=MUJOCO_BACKENDS,
                    fixed_base=True,
                    metadata=metadata,
                ),
            )
            ctx.step("patch", "asset", name, "backends playground+mujoco, fixed_base=True")
        except Exception as exc:  # noqa: BLE001 - record and retry without fixed_base
            ctx.notes["fixed_base_error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
            log_event(log, "fixed_base_rejected", level=30, error=ctx.notes["fixed_base_error"])
            cw.api.src_app_api_assets_update_asset(
                asset_uuid,
                _asset_update_schema(
                    supported_simulation_backends=MUJOCO_BACKENDS, metadata=metadata
                ),
            )
            ctx.step("patch", "asset", name, "backends playground+mujoco (fixed_base rejected)")
        asset = cw.api.src_app_api_assets_get_asset(asset_uuid)
    else:
        ctx.step("skip", "asset", name, "GLB, backends and fixed_base already set")

    final_backends = list(_get(asset, "supported_simulation_backends") or [])
    ctx.notes["fixed_base_accepted"] = _get(asset, "fixed_base") is True
    ctx.notes["asset_backends"] = final_backends
    ctx.notes["asset_glb_attached"] = bool(_get(asset, "glb_file"))
    log_event(
        log,
        "rubble_asset",
        asset_uuid=asset_uuid,
        backends=final_backends,
        fixed_base=_get(asset, "fixed_base"),
        glb_attached=bool(_get(asset, "glb_file")),
    )
    if "mujoco" not in final_backends:
        raise RuntimeError(f"asset {name} is still not MuJoCo-enabled: {final_backends}")
    return asset_uuid


# --- twins ------------------------------------------------------------------------------------
def _pose_fields(pose: SitePose) -> dict[str, float]:
    x, y, z = pose.world
    q = yaw_quaternion(pose.yaw)
    return {
        "position_x": x,
        "position_y": y,
        "position_z": z,
        "rotation_x": q["x"],
        "rotation_y": q["y"],
        "rotation_z": q["z"],
        "rotation_w": q["w"],
    }


def _pose_differs(twin: Any, target: dict[str, float]) -> bool:
    """True if the twin's editor position or rotation differs from ``target`` (q == -q)."""
    for axis in "xyz":
        have = _get(twin, f"position_{axis}")
        if have is None or not math.isclose(
            float(have), target[f"position_{axis}"], abs_tol=POSE_TOLERANCE
        ):
            return True
    have_q = [float(_get(twin, f"rotation_{k}") or 0.0) for k in "xyzw"]
    want_q = [target[f"rotation_{k}"] for k in "xyzw"]
    dot = abs(sum(a * b for a, b in zip(have_q, want_q, strict=True)))
    return not math.isclose(dot, 1.0, abs_tol=POSE_TOLERANCE)


def resolve_asset_uuid(ctx: SceneContext, asset: str) -> str:
    """Catalog slug (``vendor/name``) or asset uuid -> asset uuid."""
    if "/" not in asset:
        return asset
    found = ctx.cw.assets.get_by_registry_id(asset)
    if found is None:
        raise RuntimeError(f"catalog asset {asset!r} not found")
    return _uuid(found)


def ensure_twin(
    ctx: SceneContext,
    env_uuid: str | None,
    name: str,
    asset: str | None,
    pose: SitePose,
    *,
    fixed_base: bool | None = None,
) -> str | None:
    """Twin uuid by name in the environment; created at ``pose`` if missing.

    Falls back to an unnamed twin of the same asset (``cw.twin()`` semantics) before creating.
    """
    cw = ctx.cw
    twins = list(cw.twins.list(environment_id=env_uuid)) if env_uuid else []
    found = _by_name(twins, name)
    asset_uuid = resolve_asset_uuid(ctx, asset) if asset else None
    if not found and asset_uuid:
        found = [t for t in twins if _get(t, "asset_uuid") == asset_uuid]
        if found:
            log_event(
                log, "twin_reused_by_asset", level=30, name=name, found_name=_get(found[0], "name")
            )
    if found:
        ctx.step("reuse", "twin", name)
        return _uuid(found[0])
    if ctx.dry_run or env_uuid is None or asset_uuid is None:
        ctx.step("create", "twin", name, f"planned at world {pose.world}")
        return None
    kwargs: dict[str, Any] = {"name": name, **_pose_fields(pose)}
    if fixed_base is not None:
        kwargs["fixed_base"] = fixed_base
    twin = cw.twins.create(asset_id=asset_uuid, environment_id=env_uuid, **kwargs)
    ctx.step("create", "twin", name, f"at world {pose.world}")
    return _uuid(twin)


def place(
    ctx: SceneContext, env_uuid: str | None, twin_uuid: str | None, name: str, pose: SitePose
) -> bool:
    """Set the twin's editor pose to ``pose`` (world frame) if it differs; True if written."""
    if twin_uuid is None or env_uuid is None:
        return False
    target = _pose_fields(pose)
    current = next(
        (t for t in ctx.cw.twins.list(environment_id=env_uuid) if _uuid(t) == twin_uuid), None
    )
    if current is not None and not _pose_differs(current, target):
        ctx.step("skip", "place", name, f"already at world {pose.world}")
        return False
    if ctx.dry_run:
        ctx.step("place", "twin", name, f"planned to world {pose.world}")
        return False
    ctx.cw.twins.update_state(twin_uuid, target)
    ctx.step("place", "twin", name, f"world {pose.world}")
    return True


# --- waypoints --------------------------------------------------------------------------------
def waypoint_specs(site: Site) -> list[dict[str, Any]]:
    """``staging-west`` + ``survey-01..10`` (lawnmower corners at survey altitude), world frame."""
    sx, sy, sz = site.staging_world
    specs: list[dict[str, Any]] = [
        {
            "name": "staging-west",
            "position": {"x": sx, "y": sy, "z": sz},
            "metadata": {"kind": "staging", "order": 0},
        }
    ]
    for i, (x, y, z) in enumerate(site.survey.corners(), start=1):
        wx, wy, wz = site_to_world(x, y, z)
        specs.append(
            {
                "name": f"survey-{i:02d}",
                "position": {"x": wx, "y": wy, "z": wz},
                "metadata": {"kind": "survey", "order": i},
            }
        )
    return specs


def ensure_waypoints(
    ctx: SceneContext, env_uuid: str | None, specs: list[dict[str, Any]]
) -> dict[str, str | None]:
    """Create the missing waypoints (one bulk POST); returns name -> waypoint id."""
    cw = ctx.cw
    existing = list(cw.environments.get_waypoints(env_uuid)) if env_uuid else []
    ids = {str(_get(w, "name")): str(_get(w, "id")) for w in existing}
    missing = [s for s in specs if s["name"] not in ids]
    for s in specs:
        if s["name"] in ids:
            ctx.step("reuse", "waypoint", s["name"])
    if not missing:
        return {s["name"]: ids[s["name"]] for s in specs}
    if ctx.dry_run or env_uuid is None:
        for s in missing:
            ctx.step("create", "waypoint", s["name"], f"planned at {s['position']}")
        return {s["name"]: ids.get(s["name"]) for s in specs}
    after = cw.environments.create_waypoints(env_uuid, missing)
    ids = {str(_get(w, "name")): str(_get(w, "id")) for w in after}
    for s in missing:
        ctx.step("create", "waypoint", s["name"], f"at {s['position']}")
    return {s["name"]: ids.get(s["name"]) for s in specs}


# --- rubble collision pieces --------------------------------------------------------------------
def remove_legacy_rubble(ctx: SceneContext, env_uuid: str | None) -> bool:
    """Delete the single-mesh ``skypaw-rubble`` twin from the environment, if present.

    Cyberwave flattens a multi-node GLB into one MuJoCo mesh geom, which collides as the
    convex hull of the whole site (V1-E evidence); the per-box pieces replace it.
    """
    if env_uuid is None:
        return False
    legacy = _by_name(ctx.cw.twins.list(environment_id=env_uuid), TWIN_NAMES["rubble"])
    if not legacy:
        return False
    for twin in legacy:
        if ctx.dry_run:
            ctx.step("delete", "twin", TWIN_NAMES["rubble"], "planned: single-mesh convex hull")
        else:
            ctx.cw.twins.delete(_uuid(twin))
            ctx.step("delete", "twin", TWIN_NAMES["rubble"], "single-mesh convex hull replaced")
    return not ctx.dry_run


def ensure_rubble_pieces(
    ctx: SceneContext, env_uuid: str | None, site: Site
) -> dict[str, str | None]:
    """One asset + fixed twin per rubble box (``skypaw-rubble-NN``), each at the origin.

    Each piece GLB holds one box in world coordinates, so MuJoCo gets one convex collision
    geom per box. Idempotent: the same lookups and sha256 checks as the single asset.
    """
    from skypaw.scene.build_glb import build_piece_glbs, sha256

    pieces: dict[str, str | None] = {}
    origin = site.spawn["rubble"]
    for name, data in build_piece_glbs(site):
        asset_uuid = ensure_asset_glb(
            ctx,
            name,
            data,
            sha256(data),
            filename=f"{name}.glb",
            env_uuid=env_uuid,
            twin_name=name,
        )
        pieces[name] = ensure_twin(ctx, env_uuid, name, asset_uuid, origin, fixed_base=True)
        place(ctx, env_uuid, pieces[name], name, origin)
    return pieces


# --- everything -------------------------------------------------------------------------------
def build_scene(
    ctx: SceneContext,
    site: Site,
    *,
    project: str,
    environment: str,
    glb: bytes | None = None,
    glb_sha256: str | None = None,
    go2_pose: SitePose | None = None,
    with_markers: bool = True,
) -> SceneHandles:
    """Ensure the whole scene; returns the handles. ``go2_pose`` overrides the staging spawn.

    ``with_markers`` also ensures stream B's victim marker twins (``ensure_markers``).

    Rubble = one asset and fixed twin per box (:func:`ensure_rubble_pieces`); a legacy
    single-mesh ``skypaw-rubble`` twin is deleted from the environment. ``glb`` /
    ``glb_sha256`` (the combined rubble.glb) are accepted for compatibility and unused.
    """
    assert_client_simulation(ctx.cw)
    h = SceneHandles()
    h.project_uuid = ensure_project(ctx, project)
    h.environment_uuid = ensure_environment(ctx, environment, h.project_uuid)
    remove_legacy_rubble(ctx, h.environment_uuid)
    h.rubble_pieces = ensure_rubble_pieces(ctx, h.environment_uuid, site)
    first = next(iter(h.rubble_pieces), None)
    h.rubble_twin_uuid = h.rubble_pieces.get(first) if first else None
    if h.rubble_twin_uuid and not ctx.dry_run:
        h.rubble_asset_uuid = find_asset(ctx, first, h.environment_uuid, first)
    spawn = site.spawn
    go2_target = go2_pose or spawn["go2"]
    h.go2_twin_uuid = ensure_twin(ctx, h.environment_uuid, TWIN_NAMES["go2"], GO2_SLUG, go2_target)
    h.drone_twin_uuid = ensure_twin(
        ctx, h.environment_uuid, TWIN_NAMES["drone"], DRONE_SLUG, spawn["drone"]
    )
    place(ctx, h.environment_uuid, h.go2_twin_uuid, TWIN_NAMES["go2"], go2_target)
    place(ctx, h.environment_uuid, h.drone_twin_uuid, TWIN_NAMES["drone"], spawn["drone"])
    h.waypoints = ensure_waypoints(ctx, h.environment_uuid, waypoint_specs(site))
    if with_markers and h.environment_uuid:
        # Stream B victim markers (marker-V-01 ...); local import: markers imports this module.
        from skypaw.scene.markers import ensure_markers

        h.markers = dict(
            ensure_markers(ctx.cw, h.environment_uuid, site, dry_run=ctx.dry_run)["twins"]
        )
    ws = ctx.notes.get("workspace_uuid")
    h.workspace_uuid = str(ws) if ws else None
    return h


__all__ = [
    "DRONE_SLUG",
    "GO2_SLUG",
    "RUBBLE_ASSET_NAME",
    "TWIN_NAMES",
    "PlanStep",
    "SceneContext",
    "SceneHandles",
    "build_scene",
    "ensure_asset_glb",
    "ensure_environment",
    "ensure_project",
    "ensure_twin",
    "find_asset",
    "ensure_waypoints",
    "place",
    "remove_legacy_rubble",
    "ensure_rubble_pieces",
    "resolve_asset_uuid",
    "upload_glb_bytes",
    "waypoint_specs",
]
