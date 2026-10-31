"""Runtime configuration, read from environment variables only (optionally a local .env).

No secrets live in the repository: see .env.example for the full list of variables.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/skypaw/config.py -> repository root is two levels above the package.
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_FRONTEND_DIST = REPO_ROOT / "frontend" / "dist"

#: GitHub Pages origin of the deployed console.
PAGES_ORIGIN = "https://deviloffersservice-hub.github.io"
#: GitHub Codespaces forwarded-port URLs (https://<codespace>-7860.app.github.dev): one DNS
#: label of [a-z0-9-] before .app.github.dev, so no "/", "#", "." or empty label can sneak in.
CODESPACES_ORIGIN_REGEX = r"https://[a-z0-9][a-z0-9-]*\.app\.github\.dev"
#: Local development origins (any port), only allowed when SKYPAW_ENV=dev.
LOCALHOST_ORIGIN_REGEX = r"http://(localhost|127\.0\.0\.1)(:\d+)?"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
        # A copied .env.example has empty values: treat them as unset so defaults apply.
        env_ignore_empty=True,
    )

    # Vendor credentials (never logged; SecretStr hides them in repr).
    cyberwave_api_key: SecretStr | None = None
    cyberwave_environment_id: str | None = None
    sarvam_api_key: SecretStr | None = None

    # Twin-state source: MQTT on 8883 where reachable (Codespaces), REST polling otherwise.
    cw_state_source: Literal["rest", "mqtt"] = "rest"
    # Read by skypaw.sim.guard; anything other than a simulation mode is refused.
    cw_runtime_mode: str = "simulation"

    # Shared bearer tokens for the lead console and team phones.
    skypaw_lead_token: SecretStr | None = None
    skypaw_team_token: SecretStr | None = None

    # dev allows http://localhost:* through CORS; prod does not.
    skypaw_env: Literal["dev", "prod"] = "dev"
    log_level: str = "INFO"

    data_dir: Path = Path("/data")
    frontend_dist: Path = Field(default=DEFAULT_FRONTEND_DIST, alias="SKYPAW_FRONTEND_DIST")
    port: int = 7860

    # Budget guards for live simulator runs.
    sim_max_duration_s: int = Field(default=600, gt=0)
    sim_default_duration_s: int = Field(default=120, gt=0)
    credit_floor: float = Field(default=15, ge=0)

    # Cyberwave scene (M1). CYBERWAVE_WORKSPACE_ID is only needed for multi-workspace keys.
    cyberwave_workspace_id: str | None = None
    cyberwave_base_url: str | None = None
    cw_project_name: str = "skypaw"
    cw_environment_name: str = "skypaw-m1"

    # MQTT over secure WebSocket (RESEARCH §6): wss://mqtt.cyberwave.com:443, path "/".
    # Port 8883 is blocked from cloud sandboxes and is never used as a silent fallback.
    cw_mqtt_host: str = "mqtt.cyberwave.com"
    cw_mqtt_port: int = 443
    cw_mqtt_ws_path: str = "/"
    cw_mqtt_username: str = "mqttcyb"

    # Git commit baked in at build time (Docker ARG / CI env); informational only.
    git_sha: str = "unknown"

    def cors_origins(self) -> list[str]:
        return [PAGES_ORIGIN]

    def cors_origin_regex(self) -> str:
        patterns = [CODESPACES_ORIGIN_REGEX]
        if self.skypaw_env == "dev":
            patterns.append(LOCALHOST_ORIGIN_REGEX)
        return "|".join(f"(?:{p})" for p in patterns)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


__all__ = [
    "CODESPACES_ORIGIN_REGEX",
    "LOCALHOST_ORIGIN_REGEX",
    "PAGES_ORIGIN",
    "Settings",
    "get_settings",
]
