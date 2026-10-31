"""Command → mission plan with Sarvam chat (strict JSON schema), plus a rule-based fallback.

Call (SPEC §3.1, RESEARCH §2.3 and §6): ``POST /v1/chat/completions`` with ``response_format``
``json_schema`` and ``strict: true``. Live 2026-10-31: ``sarvam-105b`` at
``reasoning_effort=low``/``max_tokens=512`` returned ``finish_reason=length``, ``content=null``
(511 reasoning tokens). So the planner asks ``sarvam-105b-conversations`` first (no reasoning,
no ``reasoning_effort`` sent, ``max_tokens=1024``) and ``sarvam-105b`` (``reasoning_effort=low``,
``max_tokens=2048``) only when the first attempt returns no content; then the rule-based plan.
Models come from ``SARVAM_PLAN_MODEL`` / ``SARVAM_PLAN_FALLBACK_MODEL`` (skypaw.voice.settings).

Plan schema: docs/PROTOCOL.md "Mission plan JSON".
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ValidationError, field_validator

from skypaw.logging import get_logger, log_event
from skypaw.mission.ports import Plan
from skypaw.voice.client import (
    MODEL_PLAN,
    MODEL_PLAN_REASONING,
    REASONING_MODELS,
    QuotaExhausted,
    SarvamError,
    SarvamUnavailable,
)
from skypaw.voice.phrases import PHRASES, TEAM_LANGS

log = get_logger(__name__)

INTENTS = ("aerial_survey", "navigate_go2", "speak", "stop_all", "land", "unknown")
PLAN_MAX_TOKENS = 1024
PLAN_FALLBACK_MAX_TOKENS = 2048  # the reasoning model needs room for reasoning + JSON
DEFAULT_PLAN_MODEL = MODEL_PLAN
DEFAULT_PLAN_FALLBACK_MODEL = MODEL_PLAN_REASONING
FALLBACK_CONFIDENCE = 0.3

PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": list(INTENTS)},
        "target": {"type": ["string", "null"]},
        "language": {"type": "string", "enum": list(TEAM_LANGS)},
        "text_to_speak": {"type": ["string", "null"]},
        "confidence": {"type": "number"},
    },
    "required": ["intent", "target", "language", "text_to_speak", "confidence"],
    "additionalProperties": False,
}

RESPONSE_FORMAT: dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {"name": "mission_plan", "strict": True, "schema": PLAN_SCHEMA},
}

SYSTEM_PROMPT = """\
You are the mission planner of Sky & Paw, a search-and-rescue console at a collapsed building.
The rescue lead speaks short commands in Hindi, Marathi, Tamil or English, often code-mixed
(for example "dog को V-01 के पास भेजो"). Turn each command into exactly one JSON plan.

Robots:
- the drone ("drone", "ड्रोन", "ட்ரோன்") flies an aerial survey that maps the site and
  finds people, and can land;
- the Go2 robot dog ("dog", "Go2", "robot", "कुत्ता", "कुत्र्याला", "நாய்") walks to a
  victim and carries a speaker.
Victims are named V-01, V-02, V-03 ... (spoken "V one", "वी वन", "victim 2"). Always write a
target as "V-" followed by two digits.

Intents:
- aerial_survey: map, scan or search the site with the drone. target null.
- navigate_go2: send the Go2 to a victim. target is the victim id, or null when none is named.
- speak: the Go2 speaker should say something to a survivor. text_to_speak is the short sentence
  to say, written in the language the lead asked for, and language is that language; when no
  language is named, use the language of the command.
- stop_all: stop, hold, abort, freeze everything; "everyone out"; evacuate. target null.
- land: land the drone. target null.
- unknown: anything else, such as messages for the team, questions or chatter. target null.

language is the BCP-47 code of the command (hi-IN, mr-IN, ta-IN or en-IN), except for speak.
text_to_speak is null unless the intent is speak. confidence is your certainty from 0 to 1.
Reply with the JSON object only."""

_FEW_SHOT: list[tuple[str, str, dict[str, Any]]] = [
    (
        "hi-IN",
        "ड्रोन भेजो, पूरी इमारत मैप करो और लोगों को ढूंढो",
        {"intent": "aerial_survey", "target": None, "language": "hi-IN"},
    ),
    (
        "hi-IN",
        "dog को V-01 के पास भेजो",
        {"intent": "navigate_go2", "target": "V-01", "language": "hi-IN"},
    ),
    (
        "en-IN",
        "Tell them a medic is close, in Marathi",
        {
            "intent": "speak",
            "target": None,
            "language": "mr-IN",
            "text_to_speak": "मेडिक दोन मिनिटांवर आहे. हळू श्वास घ्या.",
        },
    ),
    (
        "ta-IN",
        "நாயை V-03 க்கு அனுப்பு",
        {"intent": "navigate_go2", "target": "V-03", "language": "ta-IN"},
    ),
    (
        "mr-IN",
        "सगळं थांबवा, सर्वांनी बाहेर पडा",
        {"intent": "stop_all", "target": None, "language": "mr-IN"},
    ),
    (
        "en-IN",
        "Everyone, V-01 is the priority. Priya goes first.",
        {"intent": "unknown", "target": None, "language": "en-IN", "confidence": 0.8},
    ),
]


def _shot(plan: dict[str, Any]) -> str:
    full = {"text_to_speak": None, "confidence": 0.95} | plan
    ordered = {k: full[k] for k in PLAN_SCHEMA["required"]}
    return json.dumps(ordered, ensure_ascii=False)


def build_messages(text: str, lang: str) -> list[dict[str, str]]:
    msgs = [{"role": "system", "content": SYSTEM_PROMPT}]
    for shot_lang, shot_text, shot_plan in _FEW_SHOT:
        msgs.append({"role": "user", "content": f"[{shot_lang}] {shot_text}"})
        msgs.append({"role": "assistant", "content": _shot(shot_plan)})
    msgs.append({"role": "user", "content": f"[{lang}] {text}"})
    return msgs


# ---------------------------------------------------------------- validation

_TARGET_RE = re.compile(r"^\s*(?:v|victim)\s*-?\s*0*(\d{1,2})\s*$", re.IGNORECASE)


def normalise_target(target: Any) -> str | None:
    if target in (None, "", "null", "none"):
        return None
    m = _TARGET_RE.match(str(target))
    return f"V-{int(m.group(1)):02d}" if m else None


def normalise_lang(lang: str | None, default: str = "en-IN") -> str:
    if not lang:
        return default
    lang = lang.strip()
    if lang in TEAM_LANGS:
        return lang
    short = lang.split("-")[0].lower()
    for full in TEAM_LANGS:
        if full.startswith(short + "-"):
            return full
    return default


class PlanModel(BaseModel):
    intent: Literal["aerial_survey", "navigate_go2", "speak", "stop_all", "land", "unknown"]
    target: str | None = None
    language: str
    text_to_speak: str | None = None
    confidence: float = 0.0

    @field_validator("target", mode="before")
    @classmethod
    def _target(cls, v: Any) -> str | None:
        return normalise_target(v)

    @field_validator("language", mode="before")
    @classmethod
    def _language(cls, v: Any) -> str:
        return normalise_lang(str(v) if v else None)

    @field_validator("confidence", mode="before")
    @classmethod
    def _confidence(cls, v: Any) -> float:
        try:
            return min(1.0, max(0.0, float(v)))
        except (TypeError, ValueError):
            return 0.0

    def to_plan(self) -> Plan:
        speak = self.text_to_speak if self.intent == "speak" else None
        return Plan(self.intent, self.target, self.language, speak or None, self.confidence)


def _extract_json(content: str) -> Any:
    content = content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?|```$", "", content, flags=re.MULTILINE).strip()
    try:
        return json.loads(content)
    except ValueError:
        start, end = content.find("{"), content.rfind("}")
        if start >= 0 and end > start:
            return json.loads(content[start : end + 1])
        raise


def parse_plan(content: str | None, lang: str) -> Plan:
    """Validate the model's JSON; anything malformed becomes intent "unknown"."""
    try:
        data = _extract_json(content or "")
        if not isinstance(data, dict):
            raise ValueError("plan is not an object")
        return PlanModel.model_validate(data).to_plan()
    except (ValueError, ValidationError) as exc:
        log_event(log, "plan_parse_failed", error=type(exc).__name__)
        return Plan("unknown", None, normalise_lang(lang), None, 0.0)


# ---------------------------------------------------------------- rule-based fallback

_I = re.IGNORECASE
_STOP = re.compile(
    r"\b(stop|halt|freeze|abort|evacuat\w*)\b|रुको|रोको|रोक दो|थांब|निकलो|बाहर निकल|बाहेर पड|"
    r"நிறுத்து|வெளியேறு",
    _I,
)
_EVERYONE = re.compile(r"\b(everyone|everybody|all)\b|सब|सगळ|सर्व|எல்லோர", _I)
_OUT = re.compile(r"\bout\b|बाहर|बाहेर|निकल|வெளியே", _I)
_LAND = re.compile(r"\bland(ing)?\b|उतारो|उतार दो|लैंड|उतरव|தரையிறக்கு", _I)
_SURVEY = re.compile(r"drone|ड्रोन|ட்ரோன|\bscan\b|\bsurvey\b|\bmap\b|mapping|मैप|मॅप|स्कैन|வரைபட", _I)
_NAV = re.compile(r"\bdog\b|कुत्त|कुत्र|\bgo2\b|\brobot\b|रोबोट|bhej|भेज|पाठव|\bsend\b|அனுப்பு|நாய", _I)
_SPEAK = re.compile(r"\b(tell|say|speak)\b|बोल|कह|बताओ|सांग|சொல்", _I)
_VICTIM = re.compile(r"(?:\bv|\bvictim|वी|विक्टिम)\s*-?\s*0*(\d{1,2})\b", _I)
_SPEAK_LANG = [
    ("mr-IN", re.compile(r"marathi|मराठी|மராத்தி", _I)),
    ("ta-IN", re.compile(r"tamil|तमिल|तामिळ|தமிழ்", _I)),
    ("hi-IN", re.compile(r"hindi|हिंदी|हिन्दी|ஹிந்தி", _I)),
    ("en-IN", re.compile(r"english|अंग्रेज़ी|अंग्रेजी|इंग्रजी|ஆங்கில", _I)),
]
_PHRASE_PICK = [
    ("p_hear", re.compile(r"hear|सुन|ऐक|கேட்", _I)),
    ("p_still", re.compile(r"move|हिल|हल|அசை", _I)),
    ("p_medic", re.compile(r"medic|मेडिक|डॉक्टर|மருத்துவ", _I)),
]
_TAMIL = re.compile(r"[஀-௿]")
_DEVANAGARI = re.compile(r"[ऀ-ॿ]")
_MARATHI_HINTS = re.compile(r"आहे|करा|पाठवा|च्या|ला(?:\s|$)|नका|थांब|सगळ|बाहेर|तुम्ही|त्यांना")


def detect_lang(text: str, default: str = "en-IN") -> str:
    if _TAMIL.search(text):
        return "ta-IN"
    if _DEVANAGARI.search(text):
        return "mr-IN" if _MARATHI_HINTS.search(text) else "hi-IN"
    return default


def rule_based_plan(text: str, lang: str | None = None) -> Plan:
    """Deterministic fallback (port of the mockup's ``proposeFromText`` to PROTOCOL intents).

    Order: stop/everyone-out → land → survey → Go2 → speak → unknown. A mockup "broadcast"
    (a message for the team) has no robot intent in PROTOCOL and maps to "unknown".
    """
    src = normalise_lang(lang) if lang and lang != "auto" else detect_lang(text)
    t = text.casefold()
    conf = FALLBACK_CONFIDENCE
    if _STOP.search(t) or (_EVERYONE.search(t) and _OUT.search(t)):
        return Plan("stop_all", None, src, None, conf)
    if _LAND.search(t):
        return Plan("land", None, src, None, conf)
    if _SURVEY.search(t):
        return Plan("aerial_survey", None, src, None, conf)
    if _NAV.search(t):
        m = _VICTIM.search(t)
        target = f"V-{int(m.group(1)):02d}" if m else None
        return Plan("navigate_go2", target, src, None, conf)
    if _SPEAK.search(t):
        say_lang = next((code for code, rx in _SPEAK_LANG if rx.search(t)), src)
        pid = next((p for p, rx in _PHRASE_PICK if rx.search(t)), "p_help")
        phrase = next(p for p in PHRASES if p.id == pid)
        return Plan("speak", None, say_lang, phrase.text[say_lang], conf)
    return Plan("unknown", None, src, None, conf)


# ---------------------------------------------------------------- the planner


@dataclass
class PlanAttempt:
    """One chat call made while planning (for logs and scripts/voice_check.py)."""

    model: str
    max_tokens: int
    reasoning_effort: str | None
    status: str = "ok"  # ok | empty | error
    finish_reason: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    content: str | None = None
    latency_ms: float | None = None
    error: str | None = None


@dataclass
class PlanTrace:
    attempts: list[PlanAttempt] = field(default_factory=list)
    source: str = "rules"  # llm | rules

    @property
    def got_content(self) -> bool:
        return any(a.status == "ok" for a in self.attempts)


def _attempt_params(model: str, max_tokens: int) -> tuple[int, str | None]:
    """Reasoning models get reasoning_effort=low; others get no reasoning_effort at all."""
    return max_tokens, ("low" if model in REASONING_MODELS else None)


def _content_of(resp: Any) -> tuple[str | None, str | None]:
    try:
        choice = resp["choices"][0]
        content = choice["message"].get("content")
        return (content if isinstance(content, str) else None), choice.get("finish_reason")
    except (KeyError, IndexError, TypeError, AttributeError):
        return None, None


async def plan_with_trace(
    client: Any,
    text: str,
    lang: str | None,
    *,
    model: str | None = None,
    fallback_model: str | None = None,
    max_tokens: int = PLAN_MAX_TOKENS,
    fallback_max_tokens: int = PLAN_FALLBACK_MAX_TOKENS,
) -> tuple[Plan, PlanTrace]:
    """Plan a command; returns the plan and every chat attempt made.

    1. ``model`` (default ``sarvam-105b-conversations``, no reasoning, max_tokens 1024).
    2. Only if that returned no content (or a non-quota API error):
       ``fallback_model`` (default ``sarvam-105b``, reasoning_effort=low, max_tokens 2048).
    3. Still no content, or no key / no credits / network down: ``rule_based_plan`` (0.3).
    Content that is present but not a valid plan is intent "unknown" (confidence 0).
    """
    src = normalise_lang(lang) if lang and lang != "auto" else detect_lang(text)
    trace = PlanTrace()
    if client is None or not getattr(client, "available", False):
        return rule_based_plan(text, src), trace
    model = model or DEFAULT_PLAN_MODEL
    fallback_model = fallback_model or DEFAULT_PLAN_FALLBACK_MODEL
    chain = [(model, max_tokens)]
    if fallback_model and fallback_model != model:
        chain.append((fallback_model, fallback_max_tokens))
    messages = build_messages(text, src)
    for m, tokens in chain:
        tokens, effort = _attempt_params(m, tokens)
        att = PlanAttempt(model=m, max_tokens=tokens, reasoning_effort=effort)
        trace.attempts.append(att)
        t0 = time.perf_counter()
        try:
            resp = await client.chat(
                messages,
                model=m,
                response_format=RESPONSE_FORMAT,
                reasoning_effort=effort,
                max_tokens=tokens,
            )
        except (QuotaExhausted, SarvamUnavailable) as exc:
            att.status, att.error = "error", f"{type(exc).__name__}: {exc}"[:200]
            log_event(log, "plan_fallback", reason=type(exc).__name__, status=exc.status)
            return rule_based_plan(text, src), trace
        except SarvamError as exc:
            att.status, att.error = "error", f"{type(exc).__name__}: {exc}"[:200]
            log_event(log, "plan_attempt_failed", model=m, status=exc.status)
            continue
        finally:
            att.latency_ms = round((time.perf_counter() - t0) * 1000, 1)
        att.content, att.finish_reason = _content_of(resp)
        att.usage = dict(resp.get("usage") or {}) if isinstance(resp, dict) else {}
        if not (att.content or "").strip():
            att.status = "empty"
            log_event(log, "plan_empty", model=m, finish_reason=att.finish_reason)
            continue
        trace.source = "llm"
        return parse_plan(att.content, src), trace
    log_event(log, "plan_fallback", reason="no_content")
    return rule_based_plan(text, src), trace


async def plan(
    client: Any,
    text: str,
    lang: str | None,
    *,
    model: str | None = None,
    fallback_model: str | None = None,
) -> Plan:
    """Plan a command (see ``plan_with_trace``)."""
    p, _ = await plan_with_trace(client, text, lang, model=model, fallback_model=fallback_model)
    return p
