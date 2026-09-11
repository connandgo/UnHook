"""Agent-facing adapters around the repository's shared contracts.

The canonical output schema and LangGraph state live at repository root in
``schemas.py`` and ``state.py``. This module keeps the previous Agent imports
working while avoiding duplicate Pydantic and TypedDict classes.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from schemas import (
    ActionStep,
    CallerVerificationResult,
    DamageFlags,
    DamageStage,
    PlaybookResult,
    RiskLevel,
    ScamAssessment,
    ScamType,
    URLRiskResult,
)
from state import Channel, RuntimeContext, UnHookState

# Backward-compatible Agent name; both names now refer to the same class.
UnHookRuntimeContext = RuntimeContext


class StateSnapshot(BaseModel):
    """Model-safe subset of the shared state.

    ``pii_vault``, messages, reports and history are deliberately excluded from
    the prompt payload. Use :meth:`from_state` when adapting ``UnHookState``.
    """

    model_config = ConfigDict(extra="forbid")

    channel: Channel = "unknown"
    link_clicked: bool | None = None
    info_exposed: list[str] = Field(default_factory=list, max_length=10)
    app_installed: bool | None = None
    money_sent: bool | None = None
    sent_amount: int | None = None
    elapsed_minutes: int | None = None
    damage_stage: DamageStage = "none"
    risk_level: RiskLevel = "insufficient_info"
    checklist: dict[str, bool] = Field(default_factory=dict)

    @classmethod
    def from_state(cls, state: Mapping[str, Any]) -> "StateSnapshot":
        """Copy only fields that are safe and useful for model reasoning."""
        return cls.model_validate(
            {name: state[name] for name in cls.model_fields if name in state}
        )

    def prompt_dict(self) -> dict[str, Any]:
        return self.model_dump()


__all__ = [
    "ActionStep",
    "CallerVerificationResult",
    "Channel",
    "DamageFlags",
    "DamageStage",
    "PlaybookResult",
    "RiskLevel",
    "ScamAssessment",
    "ScamType",
    "StateSnapshot",
    "URLRiskResult",
    "UnHookRuntimeContext",
    "UnHookState",
]
