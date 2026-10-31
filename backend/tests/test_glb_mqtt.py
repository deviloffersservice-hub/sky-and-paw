"""build_glb determinism / hash and mqtt_ws configuration (offline)."""

from __future__ import annotations

import io
from typing import Any

import pytest
import trimesh

from skypaw.config import Settings
from skypaw.scene.build_glb import MAX_GLB_BYTES, build_glb_bytes, sha256
from skypaw.scene.site import DEFAULT_GLB_PATH, load_site, site_to_world
from skypaw.sim.mqtt_ws import (
    MqttNotAllowedYet,
    SceneMqtt,
    WssMqttClient,
    parse_pose_message,
    twin_topic,
    validate_topic,
)

U1 = "11111111-1111-4111-8111-111111111111"
U2 = "22222222-2222-4222-8222-222222222222"


def test_glb_is_deterministic_and_matches_committed_file() -> None:
    site = load_site()
    a, b = build_glb_bytes(site), build_glb_bytes(site)
    assert a == b
    assert len(a) < MAX_GLB_BYTES
    assert sha256(a) == sha256(DEFAULT_GLB_PATH.read_bytes()), "run `make glb` and commit"


def test_glb_has_one_node_per_box_on_the_ground_z_up() -> None:
    site = load_site()
    scene = trimesh.load(io.BytesIO(build_glb_bytes(site)), file_type="glb")
    assert len(scene.geometry) == len(site.rubble) == 18
    (_, _, zmin), (_, _, zmax) = scene.bounds
    assert zmin == pytest.approx(0.0, abs=1e-6)
    assert zmax == pytest.approx(max(b.sy for b in site.rubble), abs=1e-5)


def test_site_to_world_frame() -> None:
    assert site_to_world(-11.5, 0.6, 5.5) == (-11.5, -5.5, 0.6)


def _settings(**kw: Any) -> Settings:
    return Settings(_env_file=None, cyberwave_api_key="cw_test_dummy", **kw)


class FakePaho:
    instances: list[FakePaho] = []

    def __init__(self, ws_path: str = "/") -> None:
        self.ws_path = ws_path
        self.connected_to: tuple[str, int] | None = None
        self.creds: tuple[str, str | None] | None = None
        FakePaho.instances.append(self)

    def username_pw_set(self, username: str, password: str | None = None) -> None:
        self.creds = (username, password)

    def connect(self, host: str, port: int = 1883, keepalive: int = 60) -> int:
        self.connected_to = (host, port)
        return 0

    def subscribe(self, topic: str, qos: int = 0) -> tuple[int, int]:
        return (0, 1)

    def loop_start(self) -> None:
        self.on_connect(self, None, None, None, None)  # immediate CONNACK

    def loop_stop(self) -> None: ...

    def disconnect(self) -> None: ...


def test_scene_mqtt_uses_wss_443_path_root_after_affect() -> None:
    mqtt = SceneMqtt(_settings(), client_factory=FakePaho)
    assert mqtt.url == "wss://mqtt.cyberwave.com:443/"
    with pytest.raises(MqttNotAllowedYet):
        mqtt.connect(affected=False, twin_uuids=[U1])
    assert mqtt.connect(affected=True, twin_uuids=[U1, U2], environment_uuid=U2, timeout_s=1)
    client = FakePaho.instances[-1]
    assert client.connected_to == ("mqtt.cyberwave.com", 443)
    assert client.ws_path == "/"
    assert client.creds == ("mqttcyb", "cw_test_dummy")


def test_port_8883_refused() -> None:
    mqtt = SceneMqtt(_settings(cw_mqtt_port=8883), client_factory=FakePaho)
    with pytest.raises(ValueError):
        mqtt.connect(affected=True, twin_uuids=[U1])


def test_real_client_is_websockets_with_tls() -> None:
    c = WssMqttClient(ws_path="/")
    assert c._transport == "websockets"
    assert c._websocket_path == "/"
    assert c._ssl is True


@pytest.mark.parametrize(
    "topic", ["cyberwave/twin/+/position", "cyberwave/twin/#", f"cyberwave/twin/{U1}/position"]
)
def test_cross_twin_wildcards_refused(topic: str) -> None:
    with pytest.raises(ValueError):
        validate_topic(topic)
    assert twin_topic(U1) == f"cyberwave/twin/{U1}/#"


def test_pose_parse_and_cache() -> None:
    mqtt = SceneMqtt(_settings(), client_factory=FakePaho)
    payload = b'{"type":"position","position":{"x":1,"y":2,"z":0.35},"simulation_time_s":3}'
    sample = parse_pose_message(f"cyberwave/twin/{U1}/position", payload, now=10.0)
    assert sample is not None and sample.value == {"x": 1.0, "y": 2.0, "z": 0.35}
    assert parse_pose_message(f"cyberwave/twin/{U1}/lidar", payload) is None
    mqtt.cache.update(sample)
    latest = mqtt.cache.latest(U1)
    assert latest is not None and latest["position"]["z"] == 0.35 and latest["t"] == 10.0
    assert mqtt.cache.count(U1) == 1
