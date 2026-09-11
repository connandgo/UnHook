"""Shared settings fixed by agent-design.md section 2.3.

These are decision thresholds, not a complete model-routing policy.
Emergency routing and the other escalation conditions belong to the agent
and middleware implementations.
"""

from typing import Final

DEFAULT_MODEL: Final[str] = "gpt-5-nano"
ESCALATION_MODEL: Final[str] = "gpt-5"
LOW_CONFIDENCE_THRESHOLD: Final[float] = 0.7  # Escalate below this value.
LONG_INPUT_CHAR_THRESHOLD: Final[int] = 4_000  # Inclusive; character count.
LONG_CONVERSATION_TURN_THRESHOLD: Final[int] = 6  # Inclusive; dialogue turns.

DEFAULT_MODEL_TIMEOUT_SECONDS: Final[int] = 20
DEFAULT_MODEL_MAX_OUTPUT_TOKENS: Final[int] = 800
GUARD_MAX_INPUT_CHARS: Final[int] = 12_000  # Input-security operating default.
