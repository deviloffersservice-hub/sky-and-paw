"""Decision queue: plan cards, confirm / cancel / decide."""

from __future__ import annotations

import pytest

from skypaw.mission.decisions import DecisionError, DecisionQueue
from skypaw.mission.ports import Plan


def queue() -> DecisionQueue:
    return DecisionQueue(clock=lambda: 12.5)


def test_plan_card_shape() -> None:
    q = queue()
    d = q.add_plan(Plan("navigate_go2", "V-01", "hi-IN"), source_text="dog V-01", model="m")
    out = d.to_dict()
    assert out["id"] == "D-1" and out["kind"] == "plan" and out["created_t"] == 12.5
    plan = out["plan"]
    assert plan["id"] == "P-1" and plan["intent"] == "navigate_go2" and plan["target"] == "V-01"
    for key in ("steps", "risk", "est_s", "source_text", "source_lang", "model"):
        assert key in plan
    assert plan["source_lang"] == "hi-IN"
    assert [a["id"] for a in out["actions"]] == ["go", "cancel"]
    assert "V-01" in out["title"]


def test_confirm_returns_plan_and_removes() -> None:
    q = queue()
    q.add_plan(Plan("aerial_survey", None, "en-IN"))
    res = q.confirm("P-1")
    assert res.plan is not None and res.plan.intent == "aerial_survey"
    assert len(q) == 0
    with pytest.raises(DecisionError):
        q.confirm("P-1")


def test_cancel() -> None:
    q = queue()
    q.add_plan(Plan("land", None, "en-IN"))
    res = q.cancel("P-1")
    assert res.plan is None and res.action == "cancel" and len(q) == 0


def test_decide_on_plan_and_other_kinds() -> None:
    q = queue()
    q.add_plan(Plan("land", None, "en-IN"))
    warn = q.add("warn", "Careful", "body")
    assert warn.id == "D-2" and warn.to_dict()["plan"] is None
    res = q.decide("D-1", "approve")
    assert res.plan is not None and res.plan.intent == "land"
    with pytest.raises(DecisionError):
        q.decide("D-2", "launch")
    res = q.decide("D-2", "ok")
    assert res.action == "ok" and len(q) == 0


def test_ids_increment_and_list_order() -> None:
    q = queue()
    q.add("alert", "a")
    q.add_plan(Plan("speak", "V-02", "mr-IN", "hello"))
    q.add_plan(Plan("land", None, "en-IN"))
    items = q.to_list()
    assert [d["id"] for d in items] == ["D-1", "D-2", "D-3"]
    assert [d["plan"]["id"] if d["plan"] else None for d in items] == [None, "P-1", "P-2"]


def test_unknown_kind_rejected() -> None:
    with pytest.raises(DecisionError):
        queue().add("party", "x")
