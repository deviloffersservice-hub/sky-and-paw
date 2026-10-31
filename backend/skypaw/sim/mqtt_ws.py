"""MQTT over secure WebSocket for Cyberwave twin state (RESEARCH §6, day-one round 2 check A).

Verified facts this module relies on:

* The broker accepts MQTT over ``wss://mqtt.cyberwave.com:443`` at path ``/`` only (``/mqtt``
  and ``/ws`` are not upgraded). Username ``mqttcyb`` (or the SDK's ``cwh_<sha256>``),
  password = the API key. Port 8883 is blocked from cloud sandboxes; this module never
  falls back to it.
* Per-twin ``cyberwave/twin/{uuid}/#`` and ``cyberwave/environment/{uuid}/#`` subscriptions
  are granted; cross-twin ``+`` wildcards are refused, so :func:`validate_topic` forbids them.
* ``cw.affect`` drops the SDK's MQTT client when the runtime mode changes, so
  :class:`SceneMqtt` refuses to connect before the simulation was affected.
* Simulated pose is published only on MQTT: ``.../position`` ``{"position": {x, y, z}}`` and
  ``.../rotation`` ``{"rotation": {x, y, z, w}}``, world frame (Z up).

:func:`install_sdk_wss_transport` routes the SDK's own MQTT client (used by ``takeoff``,
``locomotion.*``) over the same WSS endpoint: cyberwave 0.7.4 builds
``paho.mqtt.client.Client`` with no transport option (``cyberwave/mqtt/__init__.py`` 308),
so the module-level ``mqtt`` name inside ``cyberwave.mqtt`` is replaced by a proxy whose
``Client`` forces ``transport="websockets"`` and path ``/``. Nothing else in the process
is affected.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
import types
import uuid as uuidlib
from collections import defaultdict, deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol

import paho.mqtt.client as paho
from paho.mqtt.enums import CallbackAPIVersion

from skypaw.config import Settings, get_settings
from skypaw.logging import get_logger, log_event

log = get_logger(__name__)

POSE_KINDS = ("position", "rotation")
_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_ALLOWED_TOPIC = re.compile(rf"^cyberwave/(twin|environment)/{_UUID}/#$")
_TWIN_TOPIC = re.compile(rf"^cyberwave/twin/(?P<uuid>{_UUID})/(?P<kind>[^/]+)$")


class MqttNotAllowedYet(RuntimeError):
    """MQTT was asked to connect before ``simulation_affect`` ran."""


def twin_topic(twin_uuid: str) -> str:
    """Per-twin wildcard topic ``cyberwave/twin/{uuid}/#``."""
    return validate_topic(f"cyberwave/twin/{twin_uuid}/#")


def environment_topic(env_uuid: str) -> str:
    """Per-environment wildcard topic ``cyberwave/environment/{uuid}/#``."""
    return validate_topic(f"cyberwave/environment/{env_uuid}/#")


def validate_topic(topic: str) -> str:
    """Allow only per-twin / per-environment ``#`` topics (no cross-twin wildcards)."""
    if not _ALLOWED_TOPIC.match(topic):
        raise ValueError(
            f"refusing MQTT topic {topic!r}: only cyberwave/twin/<uuid>/# and "
            "cyberwave/environment/<uuid>/# are allowed (cross-twin wildcards are refused)"
        )
    return topic


@dataclass(frozen=True)
class PoseSample:
    """One pose message: ``value`` is ``{x, y, z}`` (position) or ``{x, y, z, w}`` (rotation)."""

    twin_uuid: str
    kind: str
    value: dict[str, float]
    t: float  # receive time, epoch seconds
    sim_time_s: float | None = None
    source_ts: float | None = None

    def as_dict(self) -> dict[str, Any]:
        """JSON-friendly form for evidence files."""
        return {
            "twin_uuid": self.twin_uuid,
            "kind": self.kind,
            "value": self.value,
            "t": round(self.t, 3),
            "sim_time_s": self.sim_time_s,
            "source_ts": self.source_ts,
        }


def parse_pose_message(
    topic: str, payload: bytes, *, now: float | None = None
) -> PoseSample | None:
    """Parse a ``cyberwave/twin/{uuid}/position|rotation`` message; ``None`` for anything else."""
    m = _TWIN_TOPIC.match(topic)
    if not m or m.group("kind") not in POSE_KINDS:
        return None
    kind = m.group("kind")
    try:
        body = json.loads(payload)
    except (ValueError, UnicodeDecodeError):
        return None
    value = body.get(kind) if isinstance(body, dict) else None
    if not isinstance(value, dict):
        return None
    try:
        clean = {k: float(v) for k, v in value.items() if k in ("x", "y", "z", "w")}
    except (TypeError, ValueError):
        return None
    sim_t = body.get("simulation_time_s")
    ts = body.get("timestamp")
    return PoseSample(
        twin_uuid=m.group("uuid").lower(),
        kind=kind,
        value=clean,
        t=time.time() if now is None else now,
        sim_time_s=float(sim_t) if isinstance(sim_t, int | float) else None,
        source_ts=float(ts) if isinstance(ts, int | float) else None,
    )


class PoseCache:
    """Thread-safe latest pose per twin, bounded sample history, optional asyncio fan-out."""

    def __init__(self, keep: int = 2000) -> None:
        self._lock = threading.Lock()
        self._latest: dict[str, dict[str, Any]] = {}
        self._history: dict[tuple[str, str], deque[PoseSample]] = defaultdict(
            lambda: deque(maxlen=keep)
        )
        self._counts: dict[tuple[str, str], int] = defaultdict(int)
        self._queues: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue[PoseSample]]] = []

    def update(self, sample: PoseSample) -> None:
        """Store a sample (called from the MQTT network thread)."""
        with self._lock:
            entry = self._latest.setdefault(sample.twin_uuid, {})
            entry[sample.kind] = dict(sample.value)
            entry["t"] = sample.t
            entry[f"{sample.kind}_t"] = sample.t
            key = (sample.twin_uuid, sample.kind)
            self._history[key].append(sample)
            self._counts[key] += 1
            queues = list(self._queues)
        for loop, q in queues:
            try:
                loop.call_soon_threadsafe(q.put_nowait, sample)
            except RuntimeError:  # loop closed
                pass

    def latest(self, twin_uuid: str) -> dict[str, Any] | None:
        """Latest ``{position, rotation, t, position_t, rotation_t}`` for a twin, or None."""
        with self._lock:
            entry = self._latest.get(twin_uuid.lower())
            return (
                {k: (dict(v) if isinstance(v, dict) else v) for k, v in entry.items()}
                if entry
                else None
            )

    def count(self, twin_uuid: str, kind: str = "position") -> int:
        """Number of ``kind`` samples received for a twin since start."""
        with self._lock:
            return self._counts.get((twin_uuid.lower(), kind), 0)

    def samples(self, twin_uuid: str, kind: str = "position") -> list[PoseSample]:
        """Retained samples (oldest first) for a twin and kind."""
        with self._lock:
            return list(self._history.get((twin_uuid.lower(), kind), ()))

    def attach_queue(
        self, loop: asyncio.AbstractEventLoop, queue: asyncio.Queue[PoseSample]
    ) -> None:
        """Also deliver every new sample to ``queue`` on ``loop`` (thread-safe)."""
        with self._lock:
            self._queues.append((loop, queue))


class WssMqttClient(paho.Client):
    """paho MQTTv5 client on the websockets transport, WS path ``ws_path``, TLS on."""

    def __init__(
        self,
        client_id: str | None = None,
        *,
        ws_path: str = "/",
        protocol: int = paho.MQTTv5,
    ) -> None:
        super().__init__(
            callback_api_version=CallbackAPIVersion.VERSION2,
            client_id=client_id or f"skypaw-{uuidlib.uuid4().hex[:12]}",
            protocol=protocol,
            transport="websockets",
        )
        self.ws_set_options(path=ws_path)
        self.tls_set()  # system CA store (SSL_CERT_FILE honoured)


class _MqttLike(Protocol):  # what SceneMqtt needs from a client (tests pass a fake)
    on_connect: Any
    on_disconnect: Any
    on_message: Any
    on_subscribe: Any

    def username_pw_set(self, username: str, password: str | None = None) -> None: ...
    def connect(self, host: str, port: int = ..., keepalive: int = ...) -> Any: ...
    def subscribe(self, topic: Any, qos: int = ...) -> Any: ...
    def loop_start(self) -> Any: ...
    def loop_stop(self) -> Any: ...
    def disconnect(self) -> Any: ...


@dataclass
class MqttStats:
    """Connection facts for logs and evidence."""

    connected: bool = False
    connect_s: float | None = None
    error: str | None = None
    subacks: dict[str, list[str]] = field(default_factory=dict)
    messages: int = 0
    topics: dict[str, int] = field(default_factory=lambda: defaultdict(int))


class SceneMqtt:
    """Capture client for the scene: connect after affect, per-twin subscriptions, PoseCache."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        cache: PoseCache | None = None,
        client_factory: Callable[..., _MqttLike] = WssMqttClient,
    ) -> None:
        self.settings = settings or get_settings()
        self.cache = cache or PoseCache()
        self.stats = MqttStats()
        self._factory = client_factory
        self._client: _MqttLike | None = None
        self._topics: list[str] = []
        self._mid_topic: dict[int, str] = {}
        self._connected = threading.Event()

    @property
    def url(self) -> str:
        """The broker URL (no credentials)."""
        s = self.settings
        return f"wss://{s.cw_mqtt_host}:{s.cw_mqtt_port}{s.cw_mqtt_ws_path}"

    def connect(
        self,
        *,
        affected: bool,
        twin_uuids: Iterable[str],
        environment_uuid: str | None = None,
        timeout_s: float = 5.0,
    ) -> bool:
        """Connect over WSS and subscribe. Returns False (and logs) if it cannot connect.

        ``affected`` must be True: ``simulation_affect`` has already run (RESEARCH §6).
        """
        if not affected:
            raise MqttNotAllowedYet("MQTT connects only after simulation_affect().")
        s = self.settings
        if s.cw_mqtt_port == 8883:
            raise ValueError("port 8883 is not used; MQTT goes over WSS on 443 (RESEARCH §6)")
        key = s.cyberwave_api_key
        if key is None or not key.get_secret_value().strip():
            raise RuntimeError("CYBERWAVE_API_KEY is not set")
        self._topics = [twin_topic(u) for u in twin_uuids]
        if environment_uuid:
            self._topics.append(environment_topic(environment_uuid))

        client = self._factory(ws_path=s.cw_mqtt_ws_path)
        client.username_pw_set(s.cw_mqtt_username, key.get_secret_value().strip())
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        client.on_subscribe = self._on_subscribe
        self._client = client
        t0 = time.monotonic()
        log_event(
            log, "mqtt_connecting", url=self.url, username=s.cw_mqtt_username, topics=self._topics
        )
        try:
            client.connect(s.cw_mqtt_host, s.cw_mqtt_port, keepalive=30)
            client.loop_start()
        except Exception as exc:  # noqa: BLE001 - transport errors vary (socket, ws, tls)
            self.stats.error = f"{type(exc).__name__}: {exc}"
            log_event(log, "mqtt_wss_failed", level=30, url=self.url, error=self.stats.error)
            return False
        if not self._connected.wait(timeout_s):
            self.stats.error = self.stats.error or f"no CONNACK within {timeout_s}s"
            log_event(log, "mqtt_wss_failed", level=30, url=self.url, error=self.stats.error)
            return False
        self.stats.connect_s = round(time.monotonic() - t0, 3)
        log_event(log, "mqtt_connected", url=self.url, connect_s=self.stats.connect_s)
        return True

    def close(self) -> None:
        """Disconnect and stop the network thread (safe to call twice)."""
        client, self._client = self._client, None
        if client is None:
            return
        try:
            client.disconnect()
            client.loop_stop()
        except Exception as exc:  # noqa: BLE001
            log_event(log, "mqtt_close_error", level=30, error=f"{type(exc).__name__}: {exc}")
        log_event(log, "mqtt_closed", messages=self.stats.messages)

    # paho callbacks (network thread) ---------------------------------------------------
    def _on_connect(
        self, client: Any, userdata: Any, flags: Any, reason_code: Any, properties: Any = None
    ) -> None:
        if getattr(reason_code, "is_failure", False):
            self.stats.error = f"CONNACK {reason_code}"
            log_event(log, "mqtt_connack_refused", level=30, reason=str(reason_code))
            return
        self.stats.connected = True
        for topic in self._topics:
            result = client.subscribe(topic, qos=0)
            mid = result[1] if isinstance(result, tuple) else getattr(result, "mid", None)
            if mid is not None:
                self._mid_topic[int(mid)] = topic
        self._connected.set()

    def _on_disconnect(
        self, client: Any, userdata: Any, flags: Any, reason_code: Any, properties: Any = None
    ) -> None:
        self.stats.connected = False
        log_event(log, "mqtt_disconnected", reason=str(reason_code))

    def _on_subscribe(
        self, client: Any, userdata: Any, mid: int, reason_codes: Any, properties: Any = None
    ) -> None:
        topic = self._mid_topic.get(int(mid), str(mid))
        codes = [str(c) for c in (reason_codes or [])]
        self.stats.subacks[topic] = codes
        refused = any(getattr(c, "is_failure", False) for c in (reason_codes or []))
        log_event(log, "mqtt_suback", level=30 if refused else 20, topic=topic, codes=codes)

    def _on_message(self, client: Any, userdata: Any, msg: Any) -> None:
        self.stats.messages += 1
        parts = str(msg.topic).split("/")
        kind = parts[3] if len(parts) > 3 and parts[1] == "twin" else "/".join(parts[3:]) or "?"
        self.stats.topics[kind] += 1
        sample = parse_pose_message(str(msg.topic), msg.payload)
        if sample is not None:
            self.cache.update(sample)


# --- the SDK's own MQTT client over WSS -------------------------------------------------
_sdk_ws_path = "/"


class _SdkWssClient(paho.Client):
    """What ``cyberwave.mqtt`` gets for ``mqtt.Client``: same args, websockets transport."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs["transport"] = "websockets"
        super().__init__(*args, **kwargs)
        self.ws_set_options(path=_sdk_ws_path)


class _PahoProxy(types.ModuleType):
    """Stands in for ``paho.mqtt.client`` inside ``cyberwave.mqtt`` only."""

    Client = _SdkWssClient
    _skypaw_wss = True

    def __getattr__(self, name: str) -> Any:
        return getattr(paho, name)


def install_sdk_wss_transport(settings: Settings | None = None) -> bool:
    """Route ``cyberwave.mqtt``'s paho client over WSS. Idempotent; returns True if patched now.

    Port and TLS are set by ``get_client`` (``mqtt_port=443``, ``mqtt_use_tls=True``).
    """
    global _sdk_ws_path
    settings = settings or get_settings()
    _sdk_ws_path = settings.cw_mqtt_ws_path
    import cyberwave.mqtt as cw_mqtt

    if getattr(cw_mqtt.mqtt, "_skypaw_wss", False):
        return False
    cw_mqtt.mqtt = _PahoProxy("paho.mqtt.client")  # type: ignore[attr-defined]
    log_event(log, "sdk_mqtt_wss_installed", path=_sdk_ws_path, port=settings.cw_mqtt_port)
    return True


__all__ = [
    "MqttNotAllowedYet",
    "MqttStats",
    "PoseCache",
    "PoseSample",
    "SceneMqtt",
    "WssMqttClient",
    "environment_topic",
    "install_sdk_wss_transport",
    "parse_pose_message",
    "twin_topic",
    "validate_topic",
]
