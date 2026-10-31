#!/usr/bin/env python3
"""Drive a running mission (``scripts/mission.py --serve --stop-file ...``) like the console.

    scripts/drive_mission.py --base http://127.0.0.1:7865 --evidence-dir tests/evidence/V1-INT \
        --stop-file tests/evidence/V1-INT/STOP

Scenario (PROTOCOL client messages over ``POST /api/command``, state from ``GET /api/state``,
plus one ``/ws`` hello to record the first snapshot):
1. wait for ``sim.ready``;
2. ``text_command`` "ड्रोन भेजो, पूरी इमारत मैप करो और लोगों को ढूंढो" -> ``confirm`` the plan card;
3. wait for the survey to finish (phase ``search``) -> victims list;
4. ``text_command`` "dog को V-01 के पास भेजो" -> ``confirm`` -> wait for ``arrived``/``blocked``
   (Go2 time to arrival or final distance);
5. the speaker card -> ``decide`` p_help -> ``tts.playing.url`` -> ``GET`` it.
Snapshots of each phase go to ``<evidence>/phases.json``, the outcome to ``drive.json``.
Writing the stop file ends the mission process (which stops the simulation).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "backend"))

import httpx  # noqa: E402
from skypaw.logging import configure_logging, get_logger, log_event  # noqa: E402

log = get_logger("skypaw.scripts.drive_mission")
SURVEY_TEXT = "ड्रोन भेजो, पूरी इमारत मैप करो और लोगों को ढूंढो"
GO2_TEXT = "dog को V-01 के पास भेजो"


class Driver:
    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        self.http = httpx.AsyncClient(base_url=self.base, timeout=10.0, trust_env=False)
        self.phases: dict[str, Any] = {}
        self.out: dict[str, Any] = {"steps": []}
        self.t0 = time.monotonic()

    def step(self, name: str, **data: Any) -> None:
        entry = {"name": name, "t": round(time.monotonic() - self.t0, 1), **data}
        self.out["steps"].append(entry)
        log_event(log, "drive_step", **entry)

    async def state(self) -> dict[str, Any]:
        r = await self.http.get("/api/state")
        r.raise_for_status()
        return r.json()

    async def command(self, msg: dict[str, Any]) -> dict[str, Any]:
        r = await self.http.post("/api/command", json=msg)
        body = r.json()
        self.step("command", msg=msg, status=r.status_code, result=body)
        return body

    async def wait(self, pred: Any, timeout_s: float, poll_s: float = 1.0) -> dict[str, Any] | None:
        end = time.monotonic() + timeout_s
        while time.monotonic() < end:
            try:
                s = await self.state()
            except httpx.HTTPError:
                await asyncio.sleep(poll_s)
                continue
            if pred(s):
                return s
            await asyncio.sleep(poll_s)
        return None

    async def plan_card(self, intent: str, timeout_s: float = 15) -> dict[str, Any] | None:
        s = await self.wait(
            lambda s: any((d.get("plan") or {}).get("intent") == intent for d in s["decisions"]),
            timeout_s,
            0.5,
        )
        if s is None:
            return None
        return next(d for d in s["decisions"] if (d.get("plan") or {}).get("intent") == intent)

    async def ws_snapshot(self) -> dict[str, Any] | None:
        import websockets

        url = self.base.replace("http", "ws", 1) + "/ws"
        async with websockets.connect(url, open_timeout=10) as ws:
            await ws.send(json.dumps({"type": "hello", "role": "lead", "member": "t1"}))
            first = json.loads(await asyncio.wait_for(ws.recv(), 10))
        return first


async def drive(args: argparse.Namespace) -> int:
    d = Driver(args.base)
    code = 0
    try:
        s = await d.wait(
            lambda s: args.offline or s["sim"]["ready"] or s["sim"]["status"] == "error", 240, 2
        )
        if s is None or not (args.offline or s["sim"]["ready"]):
            d.step("sim_not_ready", sim=(s or {}).get("sim"))
            return 4
        d.phases["1_ready"] = s
        first = await d.ws_snapshot()
        d.step("ws_hello", first_type=(first or {}).get("type"))

        await d.command({"type": "text_command", "text": SURVEY_TEXT, "lang": "hi-IN"})
        card = await d.plan_card("aerial_survey")
        if card is None:
            d.step("no_survey_card")
            return 5
        d.phases["2_survey_card"] = await d.state()
        await d.command({"type": "confirm", "plan_id": card["plan"]["id"]})
        s = await d.wait(lambda s: s["mission"]["phase"] == "mapping", 20, 0.5)
        d.phases["3_mapping"] = s
        s = await d.wait(lambda s: s["mission"]["phase"] == "search", args.survey_timeout, 2)
        if s is None:
            d.step("survey_not_finished")
            d.phases["4_survey_timeout"] = await d.state()
            return 5
        await asyncio.sleep(3)  # let the last detections and the publish land
        s = await d.state()
        d.phases["4_survey_done"] = s
        d.out["victims_after_survey"] = [
            {k: v.get(k) for k in ("id", "pos", "fused", "life_sign", "suggested_triage")}
            for v in s["victims"]
        ]
        d.step("survey_done", pct=s["survey"]["pct_mapped"], frames=s["survey"]["frames"])

        await d.command({"type": "text_command", "text": GO2_TEXT, "lang": "hi-IN"})
        card = await d.plan_card("navigate_go2")
        if card is None:
            d.step("no_go2_card")
            return 5
        await d.command({"type": "confirm", "plan_id": card["plan"]["id"]})
        t_go = time.monotonic()
        s = await d.wait(
            lambda s: s["robots"]["go2"]["mode"] in ("arrived", "blocked"), args.go2_timeout, 1
        )
        s = s or await d.state()
        g = s["robots"]["go2"]
        v1 = next((v for v in s["victims"] if v["id"] == "V-01"), None)
        dist = (
            math.dist(g["pos"][:2], v1["pos"][:2])
            if g.get("pos") and v1 and v1.get("pos")
            else None
        )
        d.out["go2"] = {
            "mode": g["mode"],
            "seconds": round(time.monotonic() - t_go, 1),
            "final_distance_m": round(dist, 2) if dist is not None else None,
            "pos": g.get("pos"),
        }
        d.phases["5_go2_end"] = s
        d.step("go2_end", **d.out["go2"])

        speak = next((x for x in s["decisions"] if x["kind"] == "speak"), None)
        if speak is None:
            s2 = await d.wait(lambda s: any(x["kind"] == "speak" for x in s["decisions"]), 5, 0.5)
            speak = next((x for x in (s2 or {}).get("decisions", []) if x["kind"] == "speak"), None)
        if speak is None:
            d.step("no_speaker_card")
            # fall back to a direct tts so the TTS route is still exercised
            await d.command({"type": "tts", "phrase_id": "p_help", "lang": "mr-IN"})
        else:
            await d.command({"type": "decide", "decision_id": speak["id"], "action": "p_help"})
        s = await d.wait(lambda s: s["tts"]["playing"] is not None, 20, 0.5)
        if s is None:
            d.step("no_tts")
            return 5
        playing = s["tts"]["playing"]
        r = await d.http.get(playing["url"])
        d.out["tts"] = {
            **playing,
            "http_status": r.status_code,
            "bytes": len(r.content),
            "content_type": r.headers.get("content-type"),
        }
        d.phases["6_tts"] = s
        d.step("tts", status=r.status_code, bytes=len(r.content))
        if g["mode"] != "arrived":
            code = 6
    except Exception as exc:  # noqa: BLE001
        log.exception("drive_error")
        d.out["error"] = f"{type(exc).__name__}: {exc}"
        code = 1
    finally:
        try:
            d.phases["7_final"] = await d.state()
        except Exception as exc:  # noqa: BLE001 - the mission may already be gone
            log_event(log, "final_state_unavailable", level=30, error=str(exc))
        ev: Path = args.evidence_dir
        ev.mkdir(parents=True, exist_ok=True)
        (ev / "phases.json").write_text(json.dumps(d.phases, indent=1) + "\n", "utf-8")
        d.out["exit_code"] = code
        (ev / "drive.json").write_text(json.dumps(d.out, indent=2) + "\n", "utf-8")
        if args.stop_file is not None:
            args.stop_file.write_text("done\n", "utf-8")
        await d.http.aclose()
    return code


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Drive a running Sky & Paw mission over HTTP.")
    ap.add_argument("--base", default="http://127.0.0.1:7865")
    ap.add_argument("--evidence-dir", type=Path, default=REPO_ROOT / "tests/evidence/V1-INT")
    ap.add_argument("--stop-file", type=Path, default=None)
    ap.add_argument("--offline", action="store_true", help="mission.py --offline (no sim)")
    ap.add_argument("--survey-timeout", type=float, default=200.0)
    ap.add_argument("--go2-timeout", type=float, default=90.0)
    args = ap.parse_args(argv)
    configure_logging("INFO")
    return asyncio.run(drive(args))


if __name__ == "__main__":
    raise SystemExit(main())
