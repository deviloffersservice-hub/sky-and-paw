"""Cyberwave client factory, simulation-mode assertion and the REST call recorder.

* ``get_client()`` builds ``cyberwave.Cyberwave`` from settings (the key comes only from
  ``CYBERWAVE_API_KEY`` via ``skypaw.config``; it is never logged or printed), already in
  simulation runtime mode, with the SDK's own MQTT client routed over WSS:443.
* ``assert_client_simulation(cw)`` refuses to continue if the client is not in simulation
  runtime mode. Every module that drives Cyberwave calls it first.
* ``HttpRecorder`` wraps the generated REST client so every request is logged as
  ``cw_http`` (method, path, status, ms; never headers or bodies) and counted. In read-only
  mode (``make scene-dry``) any non-GET request raises ``ReadOnlyViolation`` before it is
  sent.
"""

from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from skypaw.config import Settings, get_settings
from skypaw.logging import get_logger, log_event
from skypaw.sim.guard import SimulationOnlyViolation, assert_simulation_only

if TYPE_CHECKING:  # pragma: no cover
    from cyberwave import Cyberwave

log = get_logger(__name__)

#: HTTP methods that change server state.
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class MissingCredentials(RuntimeError):
    """CYBERWAVE_API_KEY is not configured."""


class ReadOnlyViolation(RuntimeError):
    """A write request was attempted while the recorder is read-only (dry run)."""


def assert_client_simulation(cw: Any) -> str:
    """Raise ``SimulationOnlyViolation`` unless ``cw`` is in simulation runtime mode.

    Checks the SDK's own ``config.runtime_mode`` (what ``cw.affect`` and the constructor's
    ``mode=`` set), independently of ``CW_RUNTIME_MODE``.
    """
    config = getattr(cw, "config", None)
    mode = getattr(config, "runtime_mode", None)
    if mode is None:
        raise SimulationOnlyViolation("Cyberwave client has no runtime_mode; refusing to run.")
    return assert_simulation_only(str(mode))


def get_client(settings: Settings | None = None, *, sdk_mqtt_over_wss: bool = True) -> Cyberwave:
    """Return a ``Cyberwave`` client in simulation runtime mode, built from settings.

    The constructor's ``mode="simulation"`` only selects the runtime (no simulation is
    started; that happens in ``skypaw.sim.simulation_affect``). Because the client starts in
    simulation mode, the later ``affect("simulation")`` does not change mode and therefore
    does not drop the SDK's MQTT client (RESEARCH §6 "affect() side effect").
    """
    settings = settings or get_settings()
    assert_simulation_only(settings.cw_runtime_mode)
    key = settings.cyberwave_api_key
    if key is None or not key.get_secret_value().strip():
        raise MissingCredentials("CYBERWAVE_API_KEY is not set (environment or .env).")
    if sdk_mqtt_over_wss:
        from skypaw.sim.mqtt_ws import install_sdk_wss_transport

        install_sdk_wss_transport(settings)

    from cyberwave import Cyberwave

    cw = Cyberwave(
        api_key=key.get_secret_value().strip(),
        base_url=settings.cyberwave_base_url,
        workspace_id=settings.cyberwave_workspace_id,
        mode="simulation",
        mqtt_host=settings.cw_mqtt_host,
        mqtt_port=settings.cw_mqtt_port,
        mqtt_use_tls=True,
    )
    mode = assert_client_simulation(cw)
    log_event(
        log,
        "cyberwave_client",
        base_url=cw.config.base_url,
        runtime_mode=mode,
        mqtt=f"wss://{settings.cw_mqtt_host}:{settings.cw_mqtt_port}{settings.cw_mqtt_ws_path}",
    )
    return cw


def resolve_workspace_id(cw: Any, settings: Settings | None = None) -> str:
    """Workspace uuid: ``CYBERWAVE_WORKSPACE_ID`` if set, else the key's only workspace."""
    settings = settings or get_settings()
    if settings.cyberwave_workspace_id:
        return settings.cyberwave_workspace_id
    workspaces = list(cw.workspaces.list())
    if len(workspaces) == 1:
        return str(workspaces[0].uuid)
    if not workspaces:
        raise RuntimeError("The Cyberwave key sees no workspace.")
    names = ", ".join(sorted(str(getattr(w, "name", "?")) for w in workspaces))
    raise RuntimeError(f"The key sees several workspaces ({names}); set CYBERWAVE_WORKSPACE_ID.")


@dataclass(frozen=True)
class HttpCall:
    """One recorded REST request (no headers, no body, no query string)."""

    method: str
    path: str
    status: int | None
    ms: float


class HttpRecorder:
    """Records (and optionally forbids writes on) every request of a Cyberwave client."""

    def __init__(self, *, read_only: bool = False) -> None:
        self.read_only = read_only
        self.calls: list[HttpCall] = []
        self._installed_on: Any = None

    def install(self, cw: Any) -> HttpRecorder:
        """Wrap ``cw.api.api_client.rest_client.request`` (idempotent per client)."""
        rest = cw.api.api_client.rest_client
        if getattr(rest, "_skypaw_recorder", None) is self:
            return self
        original = rest.request

        def request(method: str, url: str, *args: Any, **kwargs: Any) -> Any:
            verb = str(method).upper()
            path = urlsplit(str(url)).path
            if self.read_only and verb in WRITE_METHODS:
                log_event(log, "cw_http_blocked", method=verb, path=path)
                raise ReadOnlyViolation(f"dry run: refusing {verb} {path}")
            t0 = time.monotonic()
            status: int | None = None
            try:
                response = original(method, url, *args, **kwargs)
                status = getattr(response, "status", None)
                return response
            finally:
                ms = round((time.monotonic() - t0) * 1000, 1)
                self.calls.append(HttpCall(verb, path, status, ms))
                log_event(log, "cw_http", method=verb, path=path, status=status, ms=ms)

        rest.request = request
        rest._skypaw_recorder = self
        self._installed_on = rest
        return self

    def writes(self) -> list[HttpCall]:
        """Recorded requests that change server state."""
        return [c for c in self.calls if c.method in WRITE_METHODS]

    def summary(self) -> dict[str, Any]:
        """Counts by method plus the list of write calls."""
        by_method = Counter(c.method for c in self.calls)
        return {
            "total": len(self.calls),
            "by_method": dict(sorted(by_method.items())),
            "writes": len(self.writes()),
            "write_calls": [f"{c.method} {c.path}" for c in self.writes()],
        }


__all__ = [
    "WRITE_METHODS",
    "HttpCall",
    "HttpRecorder",
    "MissingCredentials",
    "ReadOnlyViolation",
    "assert_client_simulation",
    "get_client",
    "resolve_workspace_id",
]
