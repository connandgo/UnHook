"""Shared model settings fixed by agent-design.md section 2.3.

This module reads environment variables only. It does not load ``.env`` files,
make API calls, or contain secrets.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Final

DEFAULT_MODEL: Final[str] = "gpt-5-nano"
ESCALATION_MODEL: Final[str] = "gpt-5"
LOW_CONFIDENCE_THRESHOLD: Final[float] = 0.7
LONG_INPUT_CHAR_THRESHOLD: Final[int] = 4_000
LONG_CONVERSATION_TURN_THRESHOLD: Final[int] = 6
DEFAULT_MODEL_TIMEOUT_SECONDS: Final[int] = 20
DEFAULT_MODEL_MAX_OUTPUT_TOKENS: Final[int] = 800
GUARD_MAX_INPUT_CHARS: Final[int] = 12_000


@dataclass(frozen=True, slots=True)
class AgentSettings:
    """Runtime settings shared by Agent Core and the integration layer."""

    api_key: str | None
    nano_model: str = DEFAULT_MODEL
    review_model: str = ESCALATION_MODEL
    nano_timeout_seconds: float = DEFAULT_MODEL_TIMEOUT_SECONDS
    review_timeout_seconds: float = 30.0
    nano_max_output_tokens: int = DEFAULT_MODEL_MAX_OUTPUT_TOKENS
    review_max_output_tokens: int = 1200
    confidence_threshold: float = LOW_CONFIDENCE_THRESHOLD
    recursion_limit: int = 12
    model_call_limit: int = 4
    tool_call_limit: int = 6

    @classmethod
    def from_env(cls) -> "AgentSettings":
        return cls(
            api_key=os.getenv("OPENAI_API_KEY") or None,
            nano_model=os.getenv("UNHOOK_NANO_MODEL") or DEFAULT_MODEL,
            review_model=os.getenv("UNHOOK_REVIEW_MODEL") or ESCALATION_MODEL,
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
]
