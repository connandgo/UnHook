"""Deterministic gpt-5 escalation policy from design section 2.3."""

from __future__ import annotations

import re
from dataclasses import dataclass

from config import LONG_CONVERSATION_TURN_THRESHOLD, LONG_INPUT_CHAR_THRESHOLD

from .schemas import StateSnapshot

TYPE_PATTERNS = {
    "smishing": re.compile(r"스미싱|문자|택배|배송|https?://", re.I),
    "voice_phishing": re.compile(r"보이스피싱|전화|통화", re.I),
    "messenger_phishing": re.compile(r"메신저|카톡|가족|지인|휴대폰.{0,8}고장", re.I),
    "loan_scam": re.compile(r"대출|저금리|대환|갈아타기|선입금", re.I),
    "gov_impersonation": re.compile(r"검찰|경찰|금감원|수사관|검사", re.I),
    "investment_scam": re.compile(r"투자|리딩방|원금.{0,5}보장|고수익|코인", re.I),
}


@dataclass(frozen=True, slots=True)
class EscalationDecision:
    use_review_model: bool
    reasons: tuple[str, ...] = ()


class ModelEscalationPolicy:
    def __init__(self, confidence_threshold: float = 0.7):
        self.confidence_threshold = confidence_threshold

    def before_call(
        self,
        *,
        user_statement: str,
        quoted_content: str,
        multiple_messages: bool,
        conversation_turns: int,
        tool_conflict: bool,
        output_audit_failed: bool,
        state: StateSnapshot,
        emergency_detected: bool = False,
    ) -> EscalationDecision:
        # EmergencyRoute suppresses gpt-5 escalation but keeps the nano call.
        if emergency_detected or state.money_sent is True:
            return EscalationDecision(False, ("money_sent: review model escalation suppressed",))

        combined = user_statement + "\n" + quoted_content
        reasons: list[str] = []
        if len(combined) >= LONG_INPUT_CHAR_THRESHOLD:
            reasons.append(f"input_length>={LONG_INPUT_CHAR_THRESHOLD}")
        if multiple_messages:
            reasons.append("multiple_messages")
        if conversation_turns >= LONG_CONVERSATION_TURN_THRESHOLD:
            reasons.append(
                f"conversation_turns>={LONG_CONVERSATION_TURN_THRESHOLD}"
            )
        detected_types = [name for name, pattern in TYPE_PATTERNS.items() if pattern.search(combined)]
        if len(detected_types) >= 2:
            reasons.append("multiple_scam_types")
        if tool_conflict:
            reasons.append("tool_results_conflict")
        if output_audit_failed:
            reasons.append("previous_output_audit_failed")
        return EscalationDecision(bool(reasons), tuple(reasons))

    def after_nano(
        self,
        confidence: float,
        state: StateSnapshot,
        emergency_detected: bool = False,
        output_audit_failed: bool = False,
    ) -> EscalationDecision:
        if emergency_detected or state.money_sent is True:
            return EscalationDecision(False, ("money_sent: review model escalation suppressed",))
        if output_audit_failed:
            return EscalationDecision(True, ("output_audit_failed",))
        if confidence < self.confidence_threshold:
            return EscalationDecision(True, (f"confidence<{self.confidence_threshold}",))
        return EscalationDecision(False)
