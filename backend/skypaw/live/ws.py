"""PROTOCOL v1 live routes: ``WS /ws``, ``GET /api/state``, ``POST /api/command``,
``GET /frames/{frame_id}.jpg``.

WebSocket flow: the ``Origin`` header (when present) must be in the CORS allowlist or the
page's own origin, else the socket is accepted and closed with 1008. The first message must
be ``hello`` with a valid role and, when ``SKYPAW_LEAD_TOKEN`` / ``SKYPAW_TEAM_TOKEN`` is set,
the matching token (else 1008). Then the server streams snapshots (5 Hz) and events, and
applies client messages through ``MissionRuntime.apply``. Binary frames are microphone PCM,
accepted only between ``mic start`` and ``mic stop``. Team clients may send ``hello``,
``ping``, ``mic`` (+ audio) and ``text_command``; everything else is lead-only.

``POST /api/command``: when ``SKYPAW_LEAD_TOKEN`` is set, the request needs
``Authorization: Bearer <lead token>``.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import re
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response

from skypaw.config import Settings
from skypaw.logging import get_logger, log_event
from skypaw.mission.runtime import MissionRuntime

log = get_logger(__name__)

POLICY_VIOLATION = 1008
HELLO_TIMEOUT_S = 10.0
FRAME_ID = re.compile(r"^f_\d{6,9}$")
ROLES = ("lead", "team")


def origin_allowed(settings: Settings, origin: str | None, host: str | None) -> bool:
    """True for no Origin (non-browser), an allowlisted origin, or the page's own host."""
    if not origin:
        return True
    if origin in settings.cors_origins():
        return True
    if re.fullmatch(settings.cors_origin_regex(), origin):
        return True
    parts = urlsplit(origin)
    return bool(host) and parts.scheme in ("http", "https") and parts.netloc == host


def token_ok(settings: Settings, role: str, token: Any) -> bool:
    """Shared-token check for ``role`` (any token accepted when the role's token is unset)."""
    secret = settings.skypaw_lead_token if role == "lead" else settings.skypaw_team_token
    if secret is None or not secret.get_secret_value():
        return True
    return isinstance(token, str) and hmac.compare_digest(
        token.encode(), secret.get_secret_value().encode()
    )


def build_router(runtime: MissionRuntime, settings: Settings) -> APIRouter:
    """Routes bound to one runtime."""
    router = APIRouter()

    @router.get("/api/state")
    async def api_state() -> dict[str, Any]:
        return runtime.snapshot()

    @router.post("/api/command")
    async def api_command(request: Request) -> JSONResponse:
        auth = request.headers.get("authorization", "")
        token = auth[7:] if auth.lower().startswith("bearer ") else None
        if not token_ok(settings, "lead", token):
            return JSONResponse({"accepted": False, "reason": "unauthorized"}, status_code=401)
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({"accepted": False, "reason": "invalid_json"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"accepted": False, "reason": "not_an_object"}, status_code=400)
        result = await runtime.apply(body, role="lead")
        return JSONResponse(result, status_code=200 if result["accepted"] else 422)

    @router.get("/frames/{frame_id}.jpg")
    async def frame(frame_id: str) -> Response:
        if not FRAME_ID.match(frame_id):
            return JSONResponse({"error": "not_found"}, status_code=404)
        path = (runtime.frames_dir / f"{frame_id}.jpg").resolve()
        if path.parent != runtime.frames_dir.resolve() or not path.is_file():
            return JSONResponse({"error": "not_found"}, status_code=404)
        return FileResponse(path, media_type="image/jpeg")

    @router.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        client = f"{websocket.client.host}:{websocket.client.port}" if websocket.client else "?"
        origin = websocket.headers.get("origin")
        await websocket.accept()
        if not origin_allowed(settings, origin, websocket.headers.get("host")):
            log_event(log, "ws_origin_refused", level=30, origin=origin, client=client)
            await websocket.close(code=POLICY_VIOLATION, reason="origin not allowed")
            return
        role = await _hello(websocket, settings, client)
        if role is None:
            return
        queue = runtime.hub.subscribe()
        sender = asyncio.create_task(_sender(websocket, queue))
        mic_open = False
        reply = _replier(queue)
        try:
            reply(runtime.snapshot())
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    break
                if message.get("bytes") is not None:
                    if mic_open:
                        runtime.mic_audio(message["bytes"])
                    continue
                try:
                    payload = json.loads(message.get("text") or "")
                except (ValueError, RecursionError):
                    reply(_error("invalid_json"))
                    continue
                if not isinstance(payload, dict):
                    reply(_error("not_an_object"))
                    continue
                if payload.get("type") == "ping":
                    reply({"type": "pong"})
                    continue
                result = await runtime.apply(payload, role=role)
                if not result["accepted"]:
                    reply(_error(str(result.get("reason")), payload.get("type")))
                elif payload.get("type") == "mic":
                    mic_open = payload.get("state") == "start"
        except WebSocketDisconnect:
            pass
        finally:
            if mic_open:
                runtime.mic_stop()
            runtime.hub.unsubscribe(queue)
            sender.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await sender
            log_event(log, "ws_disconnect", client=client, role=role)

    return router


def _error(reason: str, about: Any = None) -> dict[str, Any]:
    return {"type": "event", "name": "error", "data": {"reason": reason, "about": about}, "t": None}


def _replier(queue: asyncio.Queue[dict[str, Any]]) -> Any:
    """Direct replies go through the client's queue so only the sender task writes."""

    def reply(msg: dict[str, Any]) -> None:
        if queue.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                queue.get_nowait()
        queue.put_nowait(msg)

    return reply


async def _hello(ws: WebSocket, settings: Settings, client: str) -> str | None:
    """Wait for a valid ``hello``; returns the role or closes with 1008 and returns None."""
    try:
        first = await asyncio.wait_for(ws.receive_json(), HELLO_TIMEOUT_S)
    except (TimeoutError, ValueError, WebSocketDisconnect, RuntimeError):
        with contextlib.suppress(Exception):
            await ws.close(code=POLICY_VIOLATION, reason="hello required")
        return None
    role = first.get("role") if isinstance(first, dict) else None
    if not isinstance(first, dict) or first.get("type") != "hello" or role not in ROLES:
        await ws.close(code=POLICY_VIOLATION, reason="hello required")
        return None
    if not token_ok(settings, role, first.get("token")):
        log_event(log, "ws_token_refused", level=30, client=client, role=role)
        await ws.close(code=POLICY_VIOLATION, reason="bad token")
        return None
    log_event(log, "ws_hello", client=client, role=role, member=first.get("member"))
    return str(role)


async def _sender(ws: WebSocket, queue: asyncio.Queue[dict[str, Any]]) -> None:
    while True:
        msg = await queue.get()
        await ws.send_json(msg)


__all__ = ["POLICY_VIOLATION", "build_router", "origin_allowed", "token_ok"]
