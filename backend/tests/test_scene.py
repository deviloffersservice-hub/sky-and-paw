"""M1 scene setup with an in-memory fake Cyberwave client (offline)."""

from __future__ import annotations

import itertools
from types import SimpleNamespace as NS
from typing import Any

import pytest

from skypaw.scene.build_glb import build_glb_bytes, sha256
from skypaw.scene.site import load_site
from skypaw.sim.guard import SimulationOnlyViolation
from skypaw.sim.scene import (
    DRONE_SLUG,
    GO2_SLUG,
    RUBBLE_ASSET_NAME,
    TWIN_NAMES,
    SceneContext,
    build_scene,
)

_ids = (f"00000000-0000-4000-8000-{n:012d}" for n in itertools.count(1))


class FakeCyberwave:
    """Just enough of cyberwave.Cyberwave for skypaw.sim.scene; records every write."""

    def __init__(self, *, list_user_assets: bool = False) -> None:
        self.config = NS(runtime_mode="simulation")
        self.writes: list[str] = []
        self.projects_db = [NS(uuid=next(_ids), name="skypaw", workspace_uuid="ws-1")]
        self.envs: list[Any] = []
        self.assets_db: list[Any] = []
        self.catalog = {GO2_SLUG: NS(uuid=next(_ids)), DRONE_SLUG: NS(uuid=next(_ids))}
        self.twins_db: list[Any] = []
        self.waypoints: dict[str, list[Any]] = {}
        # Live finding: assets.list() does not return user assets.
        self.list_user_assets = list_user_assets
        fake = self

        class Projects:
            def list(self) -> list[Any]:
                return list(fake.projects_db)

            def create(self, **kw: Any) -> Any:
                fake.writes.append("POST project")
                p = NS(uuid=next(_ids), name=kw["name"], workspace_uuid=kw["workspace_id"])
                fake.projects_db.append(p)
                return p

        class Environments:
            def list(self, project_id: str | None = None) -> list[Any]:
                return [e for e in fake.envs if project_id in (None, e.project_uuid)]

            def create(self, name: str, project_id: str, description: str = "") -> Any:
                fake.writes.append("POST environment")
                e = NS(uuid=next(_ids), name=name, project_uuid=project_id)
                fake.envs.append(e)
                return e

            def get_waypoints(self, env: str) -> list[Any]:
                return list(fake.waypoints.get(env, []))

            def create_waypoints(self, env: str, wps: list[dict]) -> list[Any]:
                fake.writes.append("POST waypoints")
                for w in wps:
                    fake.waypoints.setdefault(env, []).append(NS(id=next(_ids), name=w["name"]))
                return list(fake.waypoints[env])

        class Assets:
            def list(self) -> list[Any]:
                return list(fake.assets_db) if fake.list_user_assets else []

            def search(self, q: str) -> list[Any]:
                return [a for a in fake.assets_db if q in a.name]

            def create(self, name: str, description: str = "") -> Any:
                fake.writes.append("POST asset")
                a = NS(
                    uuid=next(_ids),
                    name=name,
                    registry_id=f"ws/{name}",
                    glb_file=None,
                    metadata={},
                    supported_simulation_backends=["playground"],
                    fixed_base=False,
                )
                fake.assets_db.append(a)
                return a

            def get_by_registry_id(self, rid: str) -> Any:
                return fake.catalog.get(rid)

        class Api:
            def _asset(self, uuid: str) -> Any:
                return next(a for a in fake.assets_db if a.uuid == uuid)

            def src_app_api_assets_get_asset(self, uuid: str) -> Any:
                return self._asset(uuid)

            def src_app_api_assets_upload_glb(self, uuid: str, file: tuple[str, bytes]) -> Any:
                assert isinstance(file, tuple) and isinstance(file[1], bytes)
                fake.writes.append("POST glb-file")
                self._asset(uuid).glb_file = "https://storage.example/x.glb"
                return self._asset(uuid)

            def src_app_api_assets_update_asset(self, uuid: str, body: Any) -> Any:
                fake.writes.append("PUT asset")
                a = self._asset(uuid)
                a.supported_simulation_backends = body.supported_simulation_backends
                a.fixed_base = bool(body.fixed_base)
                a.metadata = dict(body.metadata or {})
                return a

        class Twins:
            def list(self, environment_id: str | None = None) -> list[Any]:
                return [t for t in fake.twins_db if t.environment_uuid == environment_id]

            def create(self, asset_id: str, environment_id: str, **kw: Any) -> Any:
                fake.writes.append("POST twin")
                t = NS(uuid=next(_ids), asset_uuid=asset_id, environment_uuid=environment_id, **kw)
                fake.twins_db.append(t)
                return t

            def update_state(self, uuid: str, data: dict) -> None:
                fake.writes.append("PATCH twin state")
                t = next(t for t in fake.twins_db if t.uuid == uuid)
                for k, v in data.items():
                    setattr(t, k, v)

        self.projects, self.environments, self.assets = Projects(), Environments(), Assets()
        self.api, self.twins = Api(), Twins()
        self.workspaces = NS(list=lambda: [NS(uuid="ws-1", name="w")])


@pytest.fixture(scope="module")
def site_glb() -> tuple[Any, bytes, str]:
    site = load_site()
    glb = build_glb_bytes(site)
    return site, glb, sha256(glb)


def _build(cw: FakeCyberwave, site_glb: tuple[Any, bytes, str], *, dry_run: bool = False) -> Any:
    site, glb, digest = site_glb
    ctx = SceneContext(cw, dry_run=dry_run)
    handles = build_scene(
        ctx, site, project="skypaw", environment="skypaw-m1", glb=glb, glb_sha256=digest
    )
    return ctx, handles


def test_first_run_creates_everything(site_glb: tuple[Any, bytes, str]) -> None:
    cw = FakeCyberwave()
    ctx, h = _build(cw, site_glb)
    assert cw.writes.count("POST twin") == 3
    assert cw.writes.count("POST asset") == 1
    assert "POST glb-file" in cw.writes and "PUT asset" in cw.writes
    assert len(h.waypoints) == 11 and all(h.waypoints.values())
    asset = cw.assets_db[0]
    assert "mujoco" in asset.supported_simulation_backends and asset.fixed_base is True
    drone = next(t for t in cw.twins_db if t.name == TWIN_NAMES["drone"])
    assert drone.position_z == pytest.approx(0.6)  # RESEARCH §6: black frames at z = 0
    assert ctx.notes["fixed_base_accepted"] is True


def test_second_run_makes_zero_writes_and_same_handles(site_glb: tuple[Any, bytes, str]) -> None:
    """M1-A2 regression: assets.list() hides user assets; the lookup must still find it."""
    cw = FakeCyberwave(list_user_assets=False)
    _, first = _build(cw, site_glb)
    cw.writes.clear()
    _, second = _build(cw, site_glb)
    assert cw.writes == []
    assert second.as_dict() == first.as_dict()
    assert len([a for a in cw.assets_db if a.name == RUBBLE_ASSET_NAME]) == 1


def test_asset_found_by_search_when_twin_missing(site_glb: tuple[Any, bytes, str]) -> None:
    cw = FakeCyberwave()
    _build(cw, site_glb)
    cw.twins_db = [t for t in cw.twins_db if t.name != TWIN_NAMES["rubble"]]
    cw.writes.clear()
    _build(cw, site_glb)
    assert "POST asset" not in cw.writes
    assert cw.writes == ["POST twin"]


def test_dry_run_writes_nothing(site_glb: tuple[Any, bytes, str]) -> None:
    cw = FakeCyberwave()
    ctx, h = _build(cw, site_glb, dry_run=True)
    assert cw.writes == []
    assert h.environment_uuid is None
    assert any(s.action == "create" and s.kind == "twin" for s in ctx.steps)


def test_refuses_client_not_in_simulation(site_glb: tuple[Any, bytes, str]) -> None:
    cw = FakeCyberwave()
    cw.config.runtime_mode = "live"
    with pytest.raises(SimulationOnlyViolation):
        _build(cw, site_glb)
    assert cw.writes == []
