"""Command → mission plan with sarvam-105b (strict JSON schema), plus a rule-based fallback.

Call (SPEC §3.1, RESEARCH §2.3): ``POST /v1/chat/completions``, ``model=sarvam-105b``,
``reasoning_effort="low"`` (the server accepts only low|medium|high), ``response_format``
``json_schema`` with ``strict: true``. Reasoning tokens count toward ``max_tokens``; SPEC §3.1
asks for ``max_tokens ≥ 1024`` so a low-effort reasoning pass cannot starve the JSON.

Plan schema: docs/PROTOCOL.md "Mission plan JSON".
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ValidationError, field_validator

from skypaw.logging import get_logger, log_event
from skypaw.mission.ports import Plan
from skypaw.voice.client import SarvamError, SarvamUnavailable
from skypaw.voice.phrases import PHRASES, TEAM_LANGS

log = get_logger(__name__)

INTENTS = ("aerial_survey", "navigate_go2", "speak", "stop_all", "land", "unknown")
PLAN_MAX_TOKENS = 1024
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


async def plan(
    client: Any, text: str, lang: str | None, *, max_tokens: int = PLAN_MAX_TOKENS
) -> Plan:
    """Plan with sarvam-105b; fall back to ``rule_based_plan`` (confidence 0.3) when the API
    is unavailable (no key, no credits, network). A malformed reply is intent "unknown"."""
    src = normalise_lang(lang) if lang and lang != "auto" else detect_lang(text)
    if client is None or not getattr(client, "available", False):
        return rule_based_plan(text, src)
    try:
        resp = await client.chat(
            build_messages(text, src),
            response_format=RESPONSE_FORMAT,
            reasoning_effort="low",
            max_tokens=max_tokens,
        )
    except (SarvamError, SarvamUnavailable) as exc:
        log_event(log, "plan_fallback", reason=type(exc).__name__, status=exc.status)
        return rule_based_plan(text, src)
    try:
        content = resp["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        content = None
    return parse_plan(content, src)
