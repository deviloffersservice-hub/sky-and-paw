"""Sky & Paw backend package.

Mission sub-packages (filled in by later milestones):
    skypaw.sim         Cyberwave simulation access, always behind skypaw.sim.guard
    skypaw.perception  YOLOv8n on simulator frames + synthetic sensors
    skypaw.voice       Sarvam STT / planning LLM / TTS / translation
    skypaw.mission     grid, planner, triage, allocation, decisions
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
