"""Voice-only settings, read from the environment (optionally .env) like skypaw.config.

Kept inside skypaw/voice (stream C) so the shared Settings class is untouched.

- ``SARVAM_PLAN_MODEL`` (default ``sarvam-105b-conversations``): first planning attempt, no
  reasoning. Live 2026-10-31: ``sarvam-105b`` at ``reasoning_effort=low`` spent all 511 of 512
  tokens on reasoning and returned ``content=null`` (docs/RESEARCH.md, last paragraph).
- ``SARVAM_PLAN_FALLBACK_MODEL`` (default ``sarvam-105b``): second attempt, only when the first
  returns no content; sent with ``reasoning_effort=low`` and ``max_tokens=2048``.

Only the two current chat models are accepted (the deprecated one is rejected at startup).
"""

from __future__ import annotations

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

PlanModel = Literal["sarvam-105b-conversations", "sarvam-105b"]


class VoiceSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        env_ignore_empty=True,
    )

    sarvam_plan_model: PlanModel = "sarvam-105b-conversations"
    sarvam_plan_fallback_model: PlanModel = "sarvam-105b"


__all__ = ["PlanModel", "VoiceSettings"]
