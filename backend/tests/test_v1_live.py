"""Runtime + live routes: snapshot schema, plan flow, WS hello/origin/snapshot, HTTP routes."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from starlette.websockets import WebSocketDisconnect

from skypaw.app import create_app
from skypaw.config import Settings
from skypaw.mission.fakes import FakeDetector, FakeGo2Executor, FakeVoice, KinematicDrone
from skypaw.mission.ports import Frame
from skypaw.mission.runtime import MissionRuntime
from skypaw.mission.state import SNAPSHOT_KEYS

PROTOCOL = Path(__file__).resolve().parents[2] / "docs" / "PROTOCOL.md"


@pytest.fixture
def settings(settings: Settings) -> Settings:
    """The shared settings with the offline fakes (stream C FakeVoice, stream E kinematic Go2)."""
    return settings.model_copy(update={"skypaw_fake_go2": True, "skypaw_fake_voice": True})


@pytest.fixture
def client(settings: Settings) -> Any:
    with TestClient(create_app(settings)) as c:
        yield c


def get_path(d: dict[str, Any], path: str) -> dict[str, Any]:
    for part in filter(None, path.split(".")):
        d = d[part]
    return d


def check_snapshot(snap: dict[str, Any]) -> None:
    for path, keys in SNAPSHOT_KEYS.items():
        obj = get_path(snap, path)
        missing = [k for k in keys if k not in obj]
        assert not missing, f"{path or 'snapshot'} lacks {missing}"
    assert snap["type"] == "snapshot"
    assert len(snap["log"]) <= 50


def test_snapshot_keys_cover_protocol_example() -> None:
    """Every key of the PROTOCOL snapshot example is in SNAPSHOT_KEYS (guards drift)."""
    text = PROTOCOL.read_text(encoding="utf-8")
    block = text[text.index('{"type":"snapshot"') : text.index('{"type":"event"')]
    for path in ("mission", "sim", "survey", "grid", "transcript"):
        inner = re.search(rf'"{path}":\{{([^{{}}]*)\}}', block)
        assert inner, path
        for key in re.findall(r'"(\w+)":', inner.group(1)):
            assert key in SNAPSHOT_KEYS[path], f"{path}.{key}"
    for robot in ("drone", "go2"):
        inner = re.search(rf'"{robot}":\{{(.*?)\}}', block)
        assert inner, robot
        for key in re.findall(r'"(\w+)":', inner.group(1)):
            assert key in SNAPSHOT_KEYS[f"robots.{robot}"], f"{robot}.{key}"
    top = re.findall(r'^ "(\w+)":', block, flags=re.M)
    for key in top:
        assert key in SNAPSHOT_KEYS[""], key


def runtime(settings: Settings, **kw: Any) -> MissionRuntime:
    return MissionRuntime(
        settings, go2=FakeGo2Executor(0.01), detector=FakeDetector(), voice=FakeVoice(), **kw
    )


async def settle(n: int = 5) -> None:
    for _ in range(n):
        await asyncio.sleep(0.01)


async def test_initial_snapshot_schema(settings: Settings) -> None:
    rt = runtime(settings)
    snap = rt.snapshot()
    check_snapshot(snap)
    assert snap["robots"]["drone"]["pos"] is None
    assert snap["survey"]["pct_mapped"] == 0 and len(snap["survey"]["waypoints"]) == 10
    assert [m["id"] for m in snap["team"]] == ["t1", "t2", "t3", "t4"]


async def test_plan_flow_confirm_cancel_and_stop_all(settings: Settings) -> None:
    rt = runtime(settings)
    await rt.start()
    try:
        r = await rt.apply({"type": "command", "intent": "land", "target": None, "params": {}})
        assert r == {"accepted": True}
        snap = rt.snapshot()
        assert snap["decisions"][0]["plan"]["id"] == "P-1"
        assert (await rt.apply({"type": "cancel", "plan_id": "P-1"}))["accepted"]
        assert rt.snapshot()["decisions"] == []
        assert not (await rt.apply({"type": "confirm", "plan_id": "P-1"}))["accepted"]
        # stop_all needs no confirmation
        assert (await rt.apply({"type": "command", "intent": "stop_all"}))["accepted"]
        await settle()
        assert ("stop", None) in rt.go2.calls  # type: ignore[attr-defined]
        assert rt.snapshot()["decisions"] == []
        # team role may not confirm
        r = await rt.apply({"type": "confirm", "plan_id": "P-9"}, role="team")
        assert not r["accepted"] and "lead" in r["reason"]
        bad = await rt.apply({"type": "nope"})
        assert not bad["accepted"]
    finally:
        await rt.stop()


async def test_detection_triage_go2_and_speak(settings: Settings) -> None:
    rt = runtime(settings)
    await rt.start()
    try:
        # A frame right above V-01 (world 3.5, 2.0) -> synthetic detection.
        await rt.drone_frame(Frame("f_000001", "x.jpg", 1.0, "cam", [3.5, 2.0, 6.0], [1, 0, 0, 0]))
        v = rt.state.victim("V-01")
        assert v is not None and v["status"] == "located" and v["life_sign"]
        assert v["suggested_triage"] == "immediate"
        assert (await rt.apply({"type": "triage", "victim_id": "V-01", "category": "delayed"}))[
            "accepted"
        ]
        assert v["triage"] == "delayed"
        await rt.apply({"type": "text_command", "text": "dog को V-01 के पास भेजो", "lang": "hi-IN"})
        await settle()
        snap = rt.snapshot()
        assert snap["transcript"]["final"].startswith("dog")
        card = snap["decisions"][-1]
        assert card["plan"]["intent"] == "navigate_go2" and card["plan"]["target"] == "V-01"
        assert (await rt.apply({"type": "decide", "decision_id": card["id"], "action": "go"}))[
            "accepted"
        ]
        await settle(20)
        assert rt.state.go2.mode == "arrived" and v["status"] == "arrived"
        await rt.apply({"type": "tts", "phrase_id": "p_help", "lang": "mr-IN"})
        await settle()
        playing = rt.snapshot()["tts"]["playing"]
        assert playing["lang"] == "mr-IN" and playing["url"].startswith("/tts/")
        log = rt.snapshot()["log"]
        assert {"STT", "LLM", "NAV", "TTS", "LEAD", "DET"} <= {e["src"] for e in log}
        assert all(set(e["text"]) >= {"en-IN", "hi-IN", "mr-IN", "ta-IN"} for e in log)
        assert (await rt.apply({"type": "dismiss", "victim_id": "V-01"}))["accepted"]
    finally:
        await rt.stop()


async def test_mic_stream_produces_final_and_plan(settings: Settings) -> None:
    rt = runtime(settings)
    await rt.start()
    try:
        await rt.apply({"type": "mic", "state": "start", "lang": "en-IN"})
        assert rt.state.transcript.active
        for _ in range(12):
            assert rt.mic_audio(b"\x00" * 3200)
        await settle()
        await rt.apply({"type": "mic", "state": "stop"})
        await settle(10)
        assert rt.state.transcript.final == "drone survey"
        assert rt.snapshot()["decisions"][-1]["plan"]["intent"] == "aerial_survey"
        assert not rt.mic_audio(b"\x00")
    finally:
        await rt.stop()


async def test_survey_with_kinematic_drone_updates_state(settings: Settings) -> None:
    from skypaw.drone.executor import SurveyDroneExecutor

    rt = runtime(settings)
    kd = KinematicDrone(-11.5, -5.5, 0.6)
    rt.drone = SurveyDroneExecutor(kd, kd, kd, rt, rt.frames_dir, mission_t=rt.state.t)
    await rt.start()
    try:
        await rt.apply({"type": "command", "intent": "aerial_survey"})
        await rt.apply({"type": "confirm", "plan_id": "P-1"})
        for _ in range(200):
            await asyncio.sleep(0.02)
            if rt.state.mission.phase == "search":
                break
        snap = rt.snapshot()
        check_snapshot(snap)
        assert snap["mission"]["phase"] == "search"
        assert snap["robots"]["drone"]["mode"] == "hold"
        assert snap["survey"]["pct_mapped"] >= 95 and snap["survey"]["frames"] >= 35
        assert {v["id"] for v in snap["victims"]} >= {"V-01", "V-02", "V-03"}
        fid = snap["robots"]["drone"]["frame_id"]
        assert (rt.frames_dir / f"{fid}.jpg").is_file()
    finally:
        await rt.stop()


# --- HTTP / WebSocket -------------------------------------------------------------------
def test_api_state_and_command(client: TestClient) -> None:
    snap = client.get("/api/state").json()
    check_snapshot(snap)
    r = client.post("/api/command", json={"type": "command", "intent": "aerial_survey"})
    assert r.status_code == 200 and r.json() == {"accepted": True}
    assert client.get("/api/state").json()["decisions"][0]["plan"]["intent"] == "aerial_survey"
    r = client.post("/api/command", json={"type": "confirm", "plan_id": "P-404"})
    assert r.status_code == 422 and r.json()["accepted"] is False
    assert client.post("/api/command", content=b"[]").status_code == 400


def test_api_command_requires_lead_token_when_set(settings: Settings) -> None:
    s = settings.model_copy(update={"skypaw_lead_token": SecretStr("s3cret")})
    with TestClient(create_app(s)) as c:
        assert c.post("/api/command", json={"type": "ping"}).status_code == 401
        ok = c.post(
            "/api/command", json={"type": "ping"}, headers={"Authorization": "Bearer s3cret"}
        )
        assert ok.status_code == 200


def test_frames_route_is_path_safe(client: TestClient, settings: Settings) -> None:
    frames = settings.data_dir / "frames"
    frames.mkdir(parents=True, exist_ok=True)
    (frames / "f_000007.jpg").write_bytes(b"\xff\xd8jpeg")
    r = client.get("/frames/f_000007.jpg")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    for bad in ("f_000008", "..%2Fsecret", "f_1", "x"):
        assert client.get(f"/frames/{bad}.jpg").status_code == 404


def test_ws_snapshot_stream(client: TestClient) -> None:
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "hello", "role": "team", "member": "t2"})
        first = ws.receive_json()
        check_snapshot(first)
        nxt = ws.receive_json()  # the 5 Hz loop delivers more
        assert nxt["type"] in ("snapshot", "event")
        ws.send_json({"type": "confirm", "plan_id": "P-1"})  # lead-only
        for _ in range(20):
            msg = ws.receive_json()
            if msg["type"] == "event":
                assert msg["name"] == "error" and "lead" in msg["data"]["reason"]
                break
        else:
            raise AssertionError("no error event")


def test_ws_events_reach_clients(client: TestClient) -> None:
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "hello", "role": "lead"})
        ws.send_json({"type": "command", "intent": "land"})
        for _ in range(30):
            msg = ws.receive_json()
            if msg["type"] == "event" and msg["name"] == "plan.proposed":
                assert msg["data"]["plan"]["intent"] == "land"
                break
        else:
            raise AssertionError("no plan.proposed event")


def test_ws_refuses_foreign_origin(client: TestClient) -> None:
    with client.websocket_connect("/ws", headers={"Origin": "https://evil.example"}) as ws:
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 1008


def test_ws_allows_listed_origin(client: TestClient) -> None:
    origin = "https://deviloffersservice-hub.github.io"
    with client.websocket_connect("/ws", headers={"Origin": origin}) as ws:
        ws.send_json({"type": "hello", "role": "lead"})
        assert ws.receive_json()["type"] == "snapshot"


def test_ws_requires_hello_first(client: TestClient) -> None:
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "ping"})
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 1008


def test_ws_token_checked_when_configured(settings: Settings) -> None:
    s = settings.model_copy(update={"skypaw_team_token": SecretStr("team-tok")})
    with TestClient(create_app(s)) as c:
        with c.websocket_connect("/ws") as ws:
            ws.send_json({"type": "hello", "role": "team", "token": "wrong"})
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_json()
            assert exc.value.code == 1008
        with c.websocket_connect("/ws") as ws:
            ws.send_json({"type": "hello", "role": "team", "token": "team-tok"})
            assert ws.receive_json()["type"] == "snapshot"
        with c.websocket_connect("/ws") as ws:  # lead token unset in dev: accepted
            ws.send_json({"type": "hello", "role": "lead"})
            assert ws.receive_json()["type"] == "snapshot"


def test_ws_binary_audio_only_while_mic_open(client: TestClient) -> None:
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "hello", "role": "lead"})
        ws.send_bytes(b"\x00" * 3200)  # ignored: mic closed
        ws.send_json({"type": "mic", "state": "start", "lang": "hi-IN"})
        for _ in range(10):
            ws.send_bytes(b"\x00" * 3200)
        ws.send_json({"type": "mic", "state": "stop"})
        for _ in range(60):
            msg = ws.receive_json()
            if msg["type"] == "event" and msg["name"] == "stt.final":
                assert msg["data"]["text"]  # stream C FakeVoice: its scripted lead sample
                break
        else:
            raise AssertionError("no stt.final")


# --- integration with streams C and E (fakes selected by settings flags) ----------------
async def test_default_runtime_uses_stream_fakes_go2_progress_and_phrases(
    settings: Settings,
) -> None:
    from skypaw.go2.controller import Go2Controller
    from skypaw.voice.service import FakeVoice as StreamCFakeVoice

    rt = MissionRuntime(settings, detector=FakeDetector())
    assert isinstance(rt.go2, Go2Controller) and isinstance(rt.voice, StreamCFakeVoice)
    await rt.start()
    try:
        await rt.drone_frame(Frame("f_000001", "x.jpg", 1.0, "cam", [3.5, 2.0, 6.0], [1, 0, 0, 0]))
        await rt.apply({"type": "command", "intent": "navigate_go2", "target": "V-01"})
        await rt.apply({"type": "confirm", "plan_id": "P-1"})
        for _ in range(300):
            await asyncio.sleep(0.01)
            if rt.state.go2.mode in ("arrived", "blocked"):
                break
        snap = rt.snapshot()["robots"]["go2"]
        assert snap["mode"] == "arrived", snap
        assert snap["pos"] is not None and snap["target"] == "V-01"
        # tts with a phrase id uses the console's language and stream C's phrase table
        assert (await rt.apply({"type": "tts", "phrase_id": "p_help", "lang": "ta-IN"}))["accepted"]
        await settle(10)
        playing = rt.snapshot()["tts"]["playing"]
        assert playing["lang"] == "ta-IN" and playing["url"].startswith("/tts/")
        assert playing["text"] != "Help is on the way. Stay calm."  # the Tamil line
    finally:
        await rt.stop()


async def test_stop_all_target(settings: Settings) -> None:
    from skypaw.mission.fakes import FakeDroneExecutor

    rt = runtime(settings)
    rt.drone = FakeDroneExecutor()
    await rt.start()
    try:
        await rt.apply({"type": "command", "intent": "stop_all", "target": "drone"})
        await settle()
        assert rt.drone.calls == ["hold"]  # type: ignore[attr-defined]
        assert ("stop", None) not in rt.go2.calls  # type: ignore[union-attr]
        await rt.apply({"type": "command", "intent": "stop_all", "target": "go2"})
        await settle()
        assert rt.drone.calls == ["hold"]  # type: ignore[attr-defined]
        assert ("stop", None) in rt.go2.calls  # type: ignore[union-attr]
    finally:
        await rt.stop()


def test_ws_snapshot_right_after_hello_and_tts_route(client: TestClient) -> None:
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "hello", "role": "lead", "member": "t1"})
        assert ws.receive_json()["type"] == "snapshot"
    assert client.get("/tts/" + "0" * 64 + ".wav").status_code == 404
