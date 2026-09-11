"""Shared model settings fixed by agent-design.md section 2.3.

This module reads environment variables only. It does not load ``.env`` files,
make API calls, or contain secrets.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Final

DEFAULT_MODEL: Final[str] = "gpt-5-nano"
ESCALATION_MODEL: Final[str] = DEFAULT_MODEL
LOW_CONFIDENCE_THRESHOLD: Final[float] = 0.7
LONG_INPUT_CHAR_THRESHOLD: Final[int] = 4_000
LONG_CONVERSATION_TURN_THRESHOLD: Final[int] = 6
DEFAULT_MODEL_TIMEOUT_SECONDS: Final[int] = 60
# gpt-5 counts hidden reasoning tokens here. 800 was enough only for
# reasoning_effort="minimal"; "low" plus a tool call needs more headroom.
DEFAULT_MODEL_MAX_OUTPUT_TOKENS: Final[int] = 4000
REVIEW_MODEL_MAX_OUTPUT_TOKENS: Final[int] = 8000
GUARD_MAX_INPUT_CHARS: Final[int] = 12_000


@dataclass(frozen=True, slots=True)
class AgentSettings:
    """Runtime settings shared by Agent Core and the integration layer."""

    api_key: str | None
    nano_model: str = DEFAULT_MODEL
    review_model: str = ESCALATION_MODEL
    nano_timeout_seconds: float = DEFAULT_MODEL_TIMEOUT_SECONDS
    review_timeout_seconds: float = 60.0
    nano_max_output_tokens: int = DEFAULT_MODEL_MAX_OUTPUT_TOKENS
    # gpt-5 counts hidden reasoning tokens against max_completion_tokens.
    # Medium reasoning can consume the old 1,200-token budget before emitting JSON.
    review_max_output_tokens: int = REVIEW_MODEL_MAX_OUTPUT_TOKENS
    confidence_threshold: float = LOW_CONFIDENCE_THRESHOLD
    # A no-tool turn traverses about 18 graph nodes after the full middleware
    # stack is assembled. Leave room for the bounded model/tool loop below.
    recursion_limit: int = 50
    model_call_limit: int = 4
    tool_call_limit: int = 6

    @classmethod
    def from_env(cls) -> "AgentSettings":
        nano_model = os.getenv("UNHOOK_NANO_MODEL") or DEFAULT_MODEL
        return cls(
            api_key=os.getenv("OPENAI_API_KEY") or None,
            nano_model=nano_model,
            review_model=nano_model,
        )

    def require_api_key(self) -> str:
        if not self.api_key:
            raise RuntimeError(
                "OPENAI_API_KEY가 설정되지 않았습니다. 루트 .env.example을 참고해 "
                "실행 환경의 비밀 변수로 주입하세요."
            )
        return self.api_key


__all__ = [
    "AgentSettings",
    "DEFAULT_MODEL_MAX_OUTPUT_TOKENS",
    "DEFAULT_MODEL_TIMEOUT_SECONDS",
    "DEFAULT_MODEL",
    "ESCALATION_MODEL",
    "GUARD_MAX_INPUT_CHARS",
    "LONG_CONVERSATION_TURN_THRESHOLD",
    "LONG_INPUT_CHAR_THRESHOLD",
    "LOW_CONFIDENCE_THRESHOLD",
    "REVIEW_MODEL_MAX_OUTPUT_TOKENS",
]
