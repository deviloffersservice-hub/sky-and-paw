"""Decision queue: plan / speak / alert / warn cards shown to the lead (PROTOCOL ``decisions``).

A *plan* decision wraps a :class:`skypaw.mission.ports.Plan` and is resolved by
``confirm(plan_id)`` / ``cancel(plan_id)`` or ``decide(decision_id, "go" | "cancel")``.
Other kinds carry their own actions and are resolved by ``decide``. Resolved decisions
leave the queue. Ids: decisions ``D-n``, plans ``P-n`` (independent counters, from 1).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from skypaw.mission.ports import Plan

DECISION_KINDS = frozenset({"plan", "speak", "alert", "warn"})
PLAN_ACTIONS: tuple[dict[str, Any], ...] = (
    {"id": "go", "label": "Confirm · Go", "primary": True},
    {"id": "cancel", "label": "Cancel"},
)
#: Steps and estimates shown on plan cards, per intent.
PLAN_TEMPLATES: dict[str, dict[str, Any]] = {
    "aerial_survey": {
        "title": "Drone: map the site, search for people",
        "steps": ["Take off to 6 m", "Fly 5 passes 2.5 m apart", "Capture frames", "Hold"],
        "risk": "low",
        "est_s": 90,
    },
    "navigate_go2": {
        "title": "Go2: go to {target}",
        "steps": ["Plan route on the grid", "Walk to the target", "Report arrival"],
        "risk": "medium",
        "est_s": 60,
    },
    "speak": {
        "title": "Go2 speaker: talk to {target}",
        "steps": ["Synthesise the phrase", "Play on the Go2 speaker"],
        "risk": "low",
        "est_s": 5,
    },
    "land": {
        "title": "Drone: land",
        "steps": ["Descend and land"],
        "risk": "low",
        "est_s": 15,
    },
    "unknown": {
        "title": "Unclear command",
        "steps": ["Nothing will be executed"],
        "risk": "low",
        "est_s": 0,
    },
}


class DecisionError(ValueError):
    """Unknown id or action."""


@dataclass
class Decision:
    """One decision card; ``plan`` is the domain object behind a plan card."""

    id: str
    kind: str
    title: str
    body: str
    created_t: float
    actions: list[dict[str, Any]]
    plan_id: str | None = None
    plan: Plan | None = None
    plan_meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """The PROTOCOL decision object."""
        out: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "body": self.body,
            "created_t": self.created_t,
            "actions": [dict(a) for a in self.actions],
        }
        if self.plan is not None:
            out["plan"] = {
                "id": self.plan_id,
                "intent": self.plan.intent,
                "target": self.plan.target,
                **self.plan_meta,
            }
        else:
            out["plan"] = None
        return out


@dataclass
class Resolution:
    """What resolving a decision means for the runtime."""

    decision: Decision
    action: str
    plan: Plan | None  # set when a plan was confirmed (execute it)


class DecisionQueue:
    """Open decisions in creation order."""

    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._open: list[Decision] = []
        self._n_dec = 0
        self._n_plan = 0

    # creation -----------------------------------------------------------------------
    def add(
        self,
        kind: str,
        title: str,
        body: str = "",
        actions: list[dict[str, Any]] | None = None,
    ) -> Decision:
        """A non-plan card (speak / alert / warn); default action ``ok``."""
        if kind not in DECISION_KINDS:
            raise DecisionError(f"unknown decision kind {kind!r}")
        self._n_dec += 1
        d = Decision(
            id=f"D-{self._n_dec}",
            kind=kind,
            title=title,
            body=body,
            created_t=round(self._clock(), 2),
            actions=actions or [{"id": "ok", "label": "OK", "primary": True}],
        )
        self._open.append(d)
        return d

    def add_plan(
        self,
        plan: Plan,
        *,
        source_text: str = "",
        source_lang: str | None = None,
        model: str = "ui",
        body: str = "",
    ) -> Decision:
        """A plan card that must be confirmed before the plan executes."""
        tpl = PLAN_TEMPLATES.get(plan.intent, PLAN_TEMPLATES["unknown"])
        self._n_dec += 1
        self._n_plan += 1
        target = plan.target or "nearest"
        d = Decision(
            id=f"D-{self._n_dec}",
            kind="plan",
            title=str(tpl["title"]).format(target=target),
            body=body or (plan.text_to_speak or ""),
            created_t=round(self._clock(), 2),
            actions=[dict(a) for a in PLAN_ACTIONS],
            plan_id=f"P-{self._n_plan}",
            plan=plan,
            plan_meta={
                "steps": list(tpl["steps"]),
                "risk": tpl["risk"],
                "est_s": tpl["est_s"],
                "source_text": source_text,
                "source_lang": source_lang or plan.language,
                "model": model,
            },
        )
        self._open.append(d)
        return d

    # resolution ---------------------------------------------------------------------
    def _by_plan(self, plan_id: str) -> Decision:
        for d in self._open:
            if d.plan_id == plan_id:
                return d
        raise DecisionError(f"no open plan {plan_id!r}")

    def _by_id(self, decision_id: str) -> Decision:
        for d in self._open:
            if d.id == decision_id:
                return d
        raise DecisionError(f"no open decision {decision_id!r}")

    def confirm(self, plan_id: str) -> Resolution:
        """Confirm a plan card: it leaves the queue and its plan is returned for execution."""
        d = self._by_plan(plan_id)
        self._open.remove(d)
        return Resolution(d, "go", d.plan)

    def cancel(self, plan_id: str) -> Resolution:
        """Cancel a plan card."""
        d = self._by_plan(plan_id)
        self._open.remove(d)
        return Resolution(d, "cancel", None)

    def decide(self, decision_id: str, action: str) -> Resolution:
        """Apply ``action`` (one of the card's action ids, or approve/confirm/go for plans)."""
        d = self._by_id(decision_id)
        ids = {a["id"] for a in d.actions}
        if d.kind == "plan":
            if action in ("go", "approve", "confirm"):
                assert d.plan_id is not None  # noqa: S101 - plan decisions always have one
                return self.confirm(d.plan_id)
            if action in ("cancel", "reject"):
                assert d.plan_id is not None  # noqa: S101
                return self.cancel(d.plan_id)
        if action not in ids and action not in ("approve", "dismiss"):
            raise DecisionError(f"decision {decision_id} has no action {action!r}")
        self._open.remove(d)
        return Resolution(d, action, None)

    def to_list(self) -> list[dict[str, Any]]:
        """Open decisions as PROTOCOL dicts."""
        return [d.to_dict() for d in self._open]

    def __len__(self) -> int:
        return len(self._open)


__all__ = [
    "DECISION_KINDS",
    "PLAN_ACTIONS",
    "PLAN_TEMPLATES",
    "Decision",
    "DecisionError",
    "DecisionQueue",
    "Resolution",
]
