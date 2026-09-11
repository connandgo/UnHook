"""Compatibility imports for the repository-wide configuration."""

from config import (
    AgentSettings,
    DEFAULT_MODEL,
    ESCALATION_MODEL,
    LONG_CONVERSATION_TURN_THRESHOLD,
    LONG_INPUT_CHAR_THRESHOLD,
    LOW_CONFIDENCE_THRESHOLD,
)

__all__ = [
    "AgentSettings",
    "DEFAULT_MODEL",
    "ESCALATION_MODEL",
    "LONG_CONVERSATION_TURN_THRESHOLD",
    "LONG_INPUT_CHAR_THRESHOLD",
    "LOW_CONFIDENCE_THRESHOLD",
]
