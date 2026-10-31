"""Planner: strict schema request, pydantic validation, rule-based fallback."""

from __future__ import annotations

import json

import httpx
import pytest
from voice_helpers import fixture_response

from skypaw.mission.ports import Plan
from skypaw.voice import planner
from skypaw.voice.client import FakeSarvam, QuotaExhausted, SarvamUnavailable
from skypaw.voice.phrases import LEAD_SAMPLES, phrase

DEPRECATED = "sarvam-" + "30b"  # spelled in two parts so the id never appears in the repo

# The mockup's LEAD_SAMPLES + "everyone out" + three more commands across the four languages.
SAMPLES = [
    (LEAD_SAMPLES[0].text, "hi-IN", ("aerial_survey", None, "hi-IN")),
    (LEAD_SAMPLES[1].text, "hi-IN", ("navigate_go2", "V-01", "hi-IN")),
    (LEAD_SAMPLES[2].text, "en-IN", ("unknown", None, "en-IN")),  # mockup "broadcast"
    (LEAD_SAMPLES[3].text, "en-IN", ("speak", None, "mr-IN")),
    ("everyone out", "en-IN", ("stop_all", None, "en-IN")),
    ("land the drone", "en-IN", ("land", None, "en-IN")),
    ("कुत्र्याला V-02 कडे पाठवा", "mr-IN", ("navigate_go2", "V-02", "mr-IN")),
    ("நாயை V-03 க்கு அனுப்பு", "ta-IN", ("navigate_go2", "V-03", "ta-IN")),
    ("सब रुको", "auto", ("stop_all", None, "hi-IN")),
    ("Go2 को victim 3 के पास भेजो", "hi-IN", ("navigate_go2", "V-03", "hi-IN")),
]


@pytest.mark.parametrize(("text", "lang", "want"), SAMPLES)
def test_rule_based_plan(text, lang, want):
    p = planner.rule_based_plan(text, lang)
    assert (p.intent, p.target, p.language) == want
    assert p.confidence == planner.FALLBACK_CONFIDENCE
    if p.intent == "speak":
        assert p.text_to_speak == phrase("p_medic").text["mr-IN"]
    else:
        assert p.text_to_speak is None


def test_schema_is_strict_and_matches_protocol():
    s = planner.PLAN_SCHEMA
    assert (
        set(s["required"])
        == set(s["properties"])
        == {
            "intent",
            "target",
            "language",
            "text_to_speak",
            "confidence",
        }
    )
    assert s["additionalProperties"] is False
    assert s["properties"]["intent"]["enum"] == [
        "aerial_survey",
        "navigate_go2",
        "speak",
        "stop_all",
        "land",
        "unknown",
    ]
    rf = planner.RESPONSE_FORMAT
    assert rf["type"] == "json_schema" and rf["json_schema"]["strict"] is True


def test_messages_have_system_prompt_and_six_shots():
    msgs = planner.build_messages("dog को V-02 के पास भेजो", "hi-IN")
    assert msgs[0]["role"] == "system" and "V-01" in msgs[0]["content"]
    assert len(msgs) == 1 + 6 * 2 + 1
    for m in msgs[2:-1:2]:
        data = json.loads(m["content"])
        planner.PlanModel.model_validate(data)  # every few-shot answer is schema-valid
        assert list(data) == planner.PLAN_SCHEMA["required"]
    assert msgs[-1] == {"role": "user", "content": "[hi-IN] dog को V-02 के पास भेजो"}


async def test_plan_via_api_request_shape(make_client):
    client, rec = make_client([fixture_response("doc_chat_completion.json")])
    p = await planner.plan(client, "dog को V-01 के पास भेजो", "hi-IN")
    assert p == Plan("navigate_go2", "V-01", "hi-IN", None, 0.94)
    body = rec.json_bodies()[0]
    assert body["model"] == "sarvam-105b"
    assert body["reasoning_effort"] == "low"
    assert body["max_tokens"] == planner.PLAN_MAX_TOKENS
    assert body["response_format"] == planner.RESPONSE_FORMAT
    assert DEPRECATED not in json.dumps(body)


def _chat(content: str) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": content}}], "usage": {}}


@pytest.mark.parametrize(
    ("content", "want"),
    [
        (
            '{"intent":"land","target":null,"language":"en-IN","text_to_speak":null,'
            '"confidence":0.9}',
            ("land", None, "en-IN", None, 0.9),
        ),
        # target normalised, language short code expanded, confidence clamped
        (
            '{"intent":"navigate_go2","target":"v2","language":"mr","text_to_speak":null,'
            '"confidence":1.7}',
            ("navigate_go2", "V-02", "mr-IN", None, 1.0),
        ),
        # fenced JSON is tolerated
        (
            '```json\n{"intent":"speak","target":null,"language":"ta-IN",'
            '"text_to_speak":"உதவி வருகிறது.","confidence":0.8}\n```',
            ("speak", None, "ta-IN", "உதவி வருகிறது.", 0.8),
        ),
        # text_to_speak dropped for non-speak intents
        (
            '{"intent":"aerial_survey","target":null,"language":"hi-IN","text_to_speak":"x",'
            '"confidence":0.7}',
            ("aerial_survey", None, "hi-IN", None, 0.7),
        ),
        # bad target id → null
        (
            '{"intent":"navigate_go2","target":"the red one","language":"en-IN",'
            '"text_to_speak":null,"confidence":0.5}',
            ("navigate_go2", None, "en-IN", None, 0.5),
        ),
    ],
)
async def test_plan_validation(content, want):
    fake = FakeSarvam(chat=[_chat(content)])
    p = await planner.plan(fake, "whatever", "en-IN")
    assert (p.intent, p.target, p.language, p.text_to_speak, p.confidence) == want


@pytest.mark.parametrize(
    "content",
    [
        "I will send the dog.",
        '{"intent":"fly_to_moon","target":null,"language":"en-IN","text_to_speak":null,'
        '"confidence":1}',
        "[1, 2]",
        "",
    ],
)
async def test_parse_failure_is_unknown(content):
    p = await planner.plan(FakeSarvam(chat=[_chat(content)]), "dog को V-01 भेजो", "hi-IN")
    assert p == Plan("unknown", None, "hi-IN", None, 0.0)


async def test_missing_choices_is_unknown():
    p = await planner.plan(FakeSarvam(chat=[{"error": "?"}]), "x", "en-IN")
    assert p.intent == "unknown"


@pytest.mark.parametrize(
    "exc",
    [
        QuotaExhausted("402", status=402),
        SarvamUnavailable("down"),
    ],
)
async def test_api_failure_falls_back_to_rules(exc):
    fake = FakeSarvam(chat=[exc])
    p = await planner.plan(fake, "dog को V-01 के पास भेजो", "hi-IN")
    assert p == Plan("navigate_go2", "V-01", "hi-IN", None, 0.3)


async def test_recorded_402_falls_back(make_client):
    client, _ = make_client([fixture_response("recorded_402_chat.json")])
    p = await planner.plan(client, "ड्रोन भेजो, पूरी इमारत मैप करो", "hi-IN")
    assert (p.intent, p.confidence) == ("aerial_survey", 0.3)


async def test_no_key_uses_rules_without_calling(make_client):
    client, rec = make_client([httpx.Response(500)], api_key=None)
    p = await planner.plan(client, "everyone out", "auto")
    assert p.intent == "stop_all" and rec.requests == []


def test_detect_lang():
    assert planner.detect_lang("नाय V-01") == "hi-IN"
    assert planner.detect_lang("त्यांना सांगा") == "mr-IN"
    assert planner.detect_lang("நாயை அனுப்பு") == "ta-IN"
    assert planner.detect_lang("send the dog") == "en-IN"
