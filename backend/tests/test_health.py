from __future__ import annotations

import socket
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from skypaw import __version__
from skypaw.app import PLACEHOLDER_HTML, create_app
from skypaw.config import Settings


def test_healthz(client: TestClient) -> None:
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "version": __version__}


def test_version(client: TestClient) -> None:
    resp = client.get("/version")
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "skypaw"
    assert body["version"] == __version__
    assert body["git_sha"] == "test"
    assert body["state_source"] in {"rest", "mqtt"}


def test_root_placeholder_when_frontend_not_built(client: TestClient) -> None:
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.text == PLACEHOLDER_HTML
    assert "<title>Sky &amp; Paw Ops</title>" in resp.text


def test_root_serves_built_frontend(settings: Settings, tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>Sky &amp; Paw Ops</title>BUILT")
    (dist / "assets" / "app.js").write_text("console.log('ok')")
    app = create_app(settings.model_copy(update={"frontend_dist": dist}))
    with TestClient(app) as c:
        root = c.get("/")
        assert root.status_code == 200
        assert "BUILT" in root.text
        assert c.get("/assets/app.js").status_code == 200
        # API routes still win over the static mount.
        assert c.get("/healthz").json()["status"] == "ok"


def test_ws_echoes_hello(client: TestClient) -> None:
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "hello"})
        assert ws.receive_json() == {"type": "hello"}
        ws.send_json({"type": "hello", "client": "ops", "n": 2})
        assert ws.receive_json() == {"type": "hello", "client": "ops", "n": 2}


def test_ws_rejects_invalid_json(client: TestClient) -> None:
    with client.websocket_connect("/ws") as ws:
        ws.send_text("not json")
        assert ws.receive_json() == {"type": "error", "error": "invalid_json"}


@pytest.mark.parametrize(
    "origin",
    [
        "https://deviloffersservice-hub.github.io",
        "https://fuzzy-space-abc123-7860.app.github.dev",
        "http://localhost:5173",
        "http://127.0.0.1:7860",
    ],
)
def test_cors_allows_known_origins(client: TestClient, origin: str) -> None:
    resp = client.get("/healthz", headers={"Origin": origin})
    assert resp.headers.get("access-control-allow-origin") == origin


@pytest.mark.parametrize(
    "origin",
    [
        "https://evil.example.com",
        "https://deviloffersservice-hub.github.io.evil.com",
        "https://app.github.dev.evil.com",
        "http://evil.app.github.dev",
        "https://evil.com/.app.github.dev",
        "https://evil.com#.app.github.dev",
        "https://.app.github.dev",
        "https://evil.com?.app.github.dev",
        "https://a.b.app.github.dev",
        "https://-x.app.github.dev",
        "https://X-7860.app.github.dev.evil.com",
    ],
)
def test_cors_rejects_other_origins(client: TestClient, origin: str) -> None:
    resp = client.get("/healthz", headers={"Origin": origin})
    assert "access-control-allow-origin" not in resp.headers


def test_cors_localhost_only_in_dev(settings: Settings) -> None:
    app = create_app(settings.model_copy(update={"skypaw_env": "prod"}))
    with TestClient(app) as c:
        local = c.get("/healthz", headers={"Origin": "http://localhost:5173"})
        assert "access-control-allow-origin" not in local.headers
        pages = c.get("/healthz", headers={"Origin": "https://deviloffersservice-hub.github.io"})
        assert (
            pages.headers["access-control-allow-origin"]
            == "https://deviloffersservice-hub.github.io"
        )


def test_settings_defaults_match_spec(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "DATA_DIR",
        "PORT",
        "SIM_MAX_DURATION_S",
        "CREDIT_FLOOR",
        "CW_STATE_SOURCE",
        "CYBERWAVE_API_KEY",
        "SARVAM_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    s = Settings(_env_file=None)
    assert s.port == 7860
    assert str(s.data_dir) == "/data"
    assert s.sim_max_duration_s == 600
    assert s.credit_floor == 15
    assert s.cw_state_source == "rest"
    assert s.cyberwave_api_key is None and s.sarvam_api_key is None


def test_secrets_are_not_exposed_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SARVAM_API_KEY", "sk-not-a-real-key")
    s = Settings(_env_file=None)
    assert "sk-not-a-real-key" not in repr(s)
    assert s.sarvam_api_key is not None
    assert s.sarvam_api_key.get_secret_value() == "sk-not-a-real-key"


def test_network_is_blocked_in_tests() -> None:
    with pytest.raises(RuntimeError, match="must not open network connections"):
        socket.create_connection(("203.0.113.1", 443), timeout=1)


def test_env_example_copied_verbatim_loads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A5: `cp .env.example .env` must not break startup (empty values mean defaults)."""
    example = Path(__file__).resolve().parents[2] / ".env.example"
    keys = [
        line.split("=", 1)[0]
        for line in example.read_text().splitlines()
        if line and not line.startswith("#")
    ]
    for key in keys:
        monkeypatch.delenv(key, raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(example.read_text())
    s = Settings(_env_file=env_file)
    assert s.port == 7860
    assert s.cw_state_source == "rest"
    assert s.cyberwave_api_key is None
    # Every settings field is documented in .env.example.
    documented = {k.upper() for k in keys}
    for name, field in Settings.model_fields.items():
        assert (field.alias or name).upper() in documented, name


def test_docs_only_in_dev(settings: Settings) -> None:
    with TestClient(create_app(settings)) as dev:
        assert dev.get("/docs").status_code == 200
        assert dev.get("/openapi.json").status_code == 200
    prod_app = create_app(settings.model_copy(update={"skypaw_env": "prod"}))
    with TestClient(prod_app) as prod:
        # No docs routes: / is the placeholder route only, so these 404.
        assert prod.get("/docs").status_code == 404
        assert prod.get("/redoc").status_code == 404
        assert prod.get("/openapi.json").status_code == 404


def test_ws_survives_deeply_nested_json(client: TestClient) -> None:
    """json.loads raises RecursionError on very deep nesting; the socket must stay usable."""
    with client.websocket_connect("/ws") as ws:
        ws.send_text("[" * 200_000 + "]" * 200_000)
        assert ws.receive_json() == {"type": "error", "error": "invalid_json"}
        ws.send_json({"type": "hello"})
        assert ws.receive_json() == {"type": "hello"}
