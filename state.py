"""Shared conversation state and runtime context from design section 3.1."""

from dataclasses import dataclass
from typing import Any, Literal

from langchain.agents import AgentState

from schemas import DamageStage, InputGuardResult, RiskLevel, ScamAssessment

Channel = Literal["sms", "call", "messenger", "unknown"]
AgeGroup = Literal["general", "senior"]


@dataclass(frozen=True)
class RuntimeContext:
    user_id: str
    age_group: AgeGroup = "general"


class UnHookState(AgentState[ScamAssessment], total=False):
    channel: Channel
    link_clicked: bool | None
    info_exposed: list[str]
    app_installed: bool | None
    money_sent: bool | None
    sent_amount: int | None
    elapsed_minutes: int | None
    damage_stage: DamageStage
    risk_level: RiskLevel
    tool_results: dict[str, Any]
    checklist: dict[str, bool]
    pii_vault: dict[str, str]
    incident_report: dict[str, Any] | None
    history_matches: list[dict[str, Any]]
    input_guard: InputGuardResult | None
    emergency_mode: bool


def create_initial_state() -> UnHookState:
    """Create a fresh conversation; use only once for each new thread."""
    return {
        "messages": [],
        "channel": "unknown",
        "link_clicked": None,
        "info_exposed": [],
        "app_installed": None,
        "money_sent": None,
        "sent_amount": None,
        "elapsed_minutes": None,
        "damage_stage": "none",
        "risk_level": "insufficient_info",
        "tool_results": {},
        "checklist": {},
        "pii_vault": {},
        "incident_report": None,
        "history_matches": [],
        "input_guard": None,
        "emergency_mode": False,
    }
