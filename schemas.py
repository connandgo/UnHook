"""Shared output contracts from agent-design.md sections 2.4 and 2.5."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from typing_extensions import TypedDict

ScamType = Literal[
    "smishing", "voice_phishing", "messenger_phishing", "loan_scam",
    "gov_impersonation", "investment_scam", "unknown",
]
RiskLevel = Literal["critical", "high", "medium", "low", "insufficient_info"]
DamageStage = Literal[
    "none", "link_clicked", "info_exposed", "app_installed", "money_sent",
]


class DamageFlags(BaseModel):
    """Facts explicitly stated this turn; None means not mentioned."""

    link_clicked: bool | None = None
    info_exposed: list[str] | None = Field(default=None, max_length=10)
    app_installed: bool | None = None
    money_sent: bool | None = None
    sent_amount: int | None = None
    elapsed_minutes: int | None = None
    checklist_done: list[str] | None = None


class ActionStep(BaseModel):
    priority: int = Field(ge=1)
    action: str
    contact: str | None


class ScamAssessment(BaseModel):
    scam_type: ScamType
    risk_level: RiskLevel
    damage_stage: DamageStage
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[str] = Field(min_length=1)
    unverified: list[str]
    immediate_actions: list[ActionStep] = Field(max_length=5)
    next_question: str | None = None
    damage_flags: DamageFlags
    injection_detected: bool

    @model_validator(mode="after")
    def validate_action_order(self) -> "ScamAssessment":
        priorities = [step.priority for step in self.immediate_actions]
        if priorities != sorted(priorities):
            raise ValueError("immediate_actions must be ordered by priority")
        return self


class URLRiskResult(TypedDict):
    blacklisted: bool
    risk_score: int
    signals: list[str]


class CallerVerificationResult(TypedDict):
    # None is the documented fallback when verification fails.
    is_official: bool | None
    official_numbers: list[str]
    company: str


class PlaybookResult(TypedDict):
    steps: list[str]
    contacts: list[str]


InjectionReason = Literal[
    "instruction_override", "authority_spoofing", "verdict_manipulation",
    "boundary_spoofing", "output_manipulation", "secret_request",
]


class InjectionDecision(BaseModel):
    """Detector output; never contains untrusted free-form explanations."""

    model_config = ConfigDict(extra="forbid", strict=True)
    injection_detected: bool
    reason_codes: list[InjectionReason] = Field(max_length=6)

    @model_validator(mode="after")
    def validate_reasons(self) -> "InjectionDecision":
        if self.injection_detected != bool(self.reason_codes):
            raise ValueError("Detection and reason codes must agree")
        return self


class InputGuardResult(TypedDict):
    message_id: str
    status: Literal["not_checked", "not_detected", "detected", "unavailable"]
    reason_codes: list[InjectionReason]
