from __future__ import annotations

import json
import logging

from skypaw.logging import JsonFormatter


def _format(msg: str, **extra: object) -> dict[str, object]:
    record = logging.LogRecord("skypaw.test", logging.WARNING, __file__, 1, msg, None, None)
    for key, value in extra.items():
        setattr(record, key, value)
    return json.loads(JsonFormatter().format(record))


def test_core_keys_and_fields() -> None:
    out = _format("startup", fields={"version": "0.1.0", "port": 7860})
    assert out["msg"] == "startup"
    assert out["level"] == "warning"
    assert out["logger"] == "skypaw.test"
    assert "ts" in out
    assert out["version"] == "0.1.0" and out["port"] == 7860


def test_fields_cannot_overwrite_core_keys() -> None:
    out = _format(
        "real event",
        fields={"msg": "spoofed", "level": "debug", "ts": "1970", "logger": "x", "exc": "boom"},
    )
    assert out["msg"] == "real event"
    assert out["level"] == "warning"
    assert out["ts"] != "1970"
    assert out["logger"] == "skypaw.test"
    assert "exc" not in out
    assert out["x_msg"] == "spoofed"
    assert out["x_level"] == "debug"
    assert out["x_ts"] == "1970"
    assert out["x_logger"] == "x"
    assert out["x_exc"] == "boom"


def test_record_extras_cannot_overwrite_core_keys() -> None:
    out = _format("real event", level="spoofed", fields={"level": "also spoofed"})
    assert out["level"] == "warning"
    assert {out["x_level"], out["x_x_level"]} == {"spoofed", "also spoofed"}


def test_uvicorn_noise_is_dropped() -> None:
    out = _format("conn", color_message="\x1b[1mconn\x1b[0m", websocket=object())
    assert "color_message" not in out and "websocket" not in out
