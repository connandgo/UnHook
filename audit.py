"""audit.py — 출력 검증 (설계서 2.2 8단계, 3.2 OutputAuditMiddleware, 3.3 G5·G6)

검사 항목
- 단정 표현 (G5): "100% 사기", "확실한 사기" 같은 확정 판정과 "안전합니다", "사기가 아닙니다" 같은 안심 단정.
  규칙으로 찾은 표현은 완화 문구로 바꾼다. 규칙에 없는 표현은 선택적으로 넘기는 분류기(nano)로 잡는다.
  "무조건 지급정지부터 하세요"처럼 행동을 재촉하는 말은 판정이 아니므로 건드리지 않는다.
- 근거 없는 판정 (G6): evidence가 "없음" 같은 빈 내용뿐이면 risk_level을 insufficient_info로 보류한다.
  단, 송금 피해가 확인된 상태(money_sent=True)는 긴급 안내를 유지해야 하므로 보류하지 않는다 (2.2).
- 개인정보 노출: 응답에 원문 번호가 남아 있으면 토큰(사기범 측) 또는 라벨로 가린다.
- 질문 개수: next_question에 질문이 여러 개면 첫 질문만 남긴다 (2.4 "한 번에 1개만").
- 연락처 대조 (선택): allowed_contacts를 넘기면 목록에 없는 전화번호를 조치에서 뺀다.

스키마 자체가 깨진 결과는 build_safe_fallback()으로 만든 안전 응답으로 바꾼다 (2.2 8단계).
의미 규칙 위반은 고칠 수 있는 만큼 고치고, 고칠 수 없으면 원문 유지 + 경고 로그 (3.2).

검사 시점 (5.1)
- after_model: 모델이 최종 판단을 낸 직후. AI 메시지와 structured_response를 함께 고친다.
- after_agent: 입력 보안의 after_agent 보강(injection_detected·근거 추가) 등 다른 미들웨어가 바꾼
  최종 structured_response를 한 번 더 검사한다. TopicFilter처럼 모델 없이 끝난 턴도 여기서 검사된다.
  after_* 훅은 등록 역순으로 실행되므로 이 미들웨어는 입력 보안·DamageState·EmergencyRoute보다 앞에 등록한다.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Literal

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, ToolMessage
from pydantic import ValidationError

from pii import find_pii
from schemas import ActionStep, DamageFlags, ScamAssessment
from state import RuntimeContext, UnHookState

logger = logging.getLogger("unhook.audit")

IssueCode = Literal[
    "assertive", "false_safety", "no_evidence", "pii_exposed",
    "multi_question", "unknown_contact", "schema_invalid", "classifier_flag",
]

# ---------------------------------------------------------------------------
# 단정 표현 규칙 (패턴, 바꿀 문구)
# ---------------------------------------------------------------------------
_SCAM_WORD = r"(?:사기|보이스\s*피싱|스미싱|피싱|메신저\s*피싱|사칭)"
_END = r"(?:입니다|이에요|예요|이다|야|임|네요|이네요|인\s*것\s*같아요)?"
_LIKELY = "사기일 가능성이 매우 높아요"

ASSERTIVE_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(rf"(?:100|백)\s*(?:%|퍼센트|프로)\s*{_SCAM_WORD}{_END}"), _LIKELY),
    (re.compile(rf"(?:확실한|명백한|틀림없는|완벽한)\s*{_SCAM_WORD}{_END}"), _LIKELY),
    (re.compile(rf"(?:무조건|틀림없이|분명히|확실히)\s*{_SCAM_WORD}{_END}"), _LIKELY),
    (re.compile(rf"{_SCAM_WORD}(?:가|이|임이|인\s*게|인\s*것이)\s*(?:확실|분명|틀림없|명백)(?:합니다|해요|하다|어요|습니다|하네요)?"), _LIKELY),
]
FALSE_SAFETY_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"(?:완전히\s*|100%\s*|전혀\s*문제\s*없이\s*)?안전(?:합니다|해요|한\s*(?:링크|문자|사이트|번호)(?:입니다|예요|이에요))"),
     "안전하다고 단정할 수는 없어요"),
    (re.compile(rf"{_SCAM_WORD}(?:가|는)?\s*(?:아닙니다|아니에요|아니네요|아니야)"),
     "사기가 아니라고 단정할 수는 없어요"),
    (re.compile(r"(?:걱정|신경)\s*(?:안\s*하셔도|하지\s*않으셔도|안\s*하셔도\s*)\s*(?:됩니다|돼요|되세요)"),
     "주의는 계속 필요해요"),
]

# 응답 쪽에서는 번호 주인을 알 수 없으므로 '본인' 없이 가린다
OUTPUT_MASK_LABELS: dict[str, str] = {
    "rrn": "[주민등록번호 가림]",
    "card": "[카드번호 가림]",
    "account": "[계좌번호 가림]",
    "phone": "[전화번호 가림]",
}

_PLACEHOLDER_EVIDENCE = re.compile(
    r"^\s*(?:없음|없습니다|근거\s*없음|해당\s*없음|모름|알\s*수\s*없음|확인\s*불가|n/?a|none|null|-+|\.+)?\s*$",
    re.IGNORECASE,
)
_HELD_EVIDENCE = "판단 근거가 확인되지 않아 판정을 보류합니다"
_MONEY_SENT_EVIDENCE = "사용자가 송금 사실을 진술함"


@dataclass(frozen=True)
class AuditIssue:
    code: IssueCode
    detail: str  # 원문 PII를 담지 않는다


@dataclass
class AuditResult:
    assessment: ScamAssessment
    text: str | None
    issues: list[AuditIssue] = field(default_factory=list)
    changed: bool = False
    failed: bool = False  # 고치지 못한 위반이 남음 → Agent가 gpt-5 재검토 조건으로 쓸 수 있음

    def summary(self) -> dict[str, Any]:
        return {
            "changed": self.changed,
            "failed": self.failed,
            "issues": [issue.code for issue in self.issues],
        }


# ---------------------------------------------------------------------------
# 순수 함수
# ---------------------------------------------------------------------------
def find_assertive_phrases(text: str) -> list[str]:
    """단정 표현(확정 판정 + 안심 단정) 목록."""
    if not text:
        return []
    found: list[str] = []
    for pattern, _ in ASSERTIVE_RULES + FALSE_SAFETY_RULES:
        found.extend(m.group(0) for m in pattern.finditer(text))
    return list(dict.fromkeys(found))


def soften_text(text: str) -> tuple[str, list[AuditIssue]]:
    """단정 표현을 완화 문구로 바꾼다."""
    if not text:
        return text, []
    issues: list[AuditIssue] = []
    for code, rules in (("assertive", ASSERTIVE_RULES), ("false_safety", FALSE_SAFETY_RULES)):
        for pattern, replacement in rules:
            text, count = pattern.subn(replacement, text)
            if count:
                issues.append(AuditIssue(code, f"{count}건 완화"))
    return text, issues


def mask_output_pii(text: str, vault: dict[str, str] | None, allowed_contacts: set[str] | None = None) -> tuple[str, int]:
    """응답에 남은 원문 PII를 가린다. vault에 있는 사기범 번호는 토큰으로, 나머지는 라벨로."""
    if not text:
        return text, 0
    reverse = {re.sub(r"\D", "", raw): token for token, raw in (vault or {}).items()}
    allowed = allowed_contacts or set()
    out, cursor, count = [], 0, 0
    for m in find_pii(text):
        digits = re.sub(r"\D", "", m.value)
        if m.kind == "phone" and digits in allowed:
            continue  # 공식 연락처 목록에 있는 번호는 그대로 둔다
        out.append(text[cursor:m.start])
        out.append(reverse.get(digits) or OUTPUT_MASK_LABELS[m.kind])
        cursor = m.end
        count += 1
    out.append(text[cursor:])
    return "".join(out), count


def _first_question(question: str | None) -> tuple[str | None, bool]:
    if not question:
        return question, False
    parts = re.split(r"(?<=\?)\s*", question.strip())
    questions = [p for p in parts if p.endswith("?")]
    if len(questions) <= 1:
        return question, False
    return questions[0], True


def _digits_set(values: Iterable[str] | None) -> set[str] | None:
    if values is None:
        return None
    return {re.sub(r"\D", "", v) for v in values if re.sub(r"\D", "", v)}


def validate_payload(payload: Any) -> tuple[ScamAssessment | None, list[str]]:
    """구조화 출력 원본(dict·모델 객체)을 검증한다. 실패하면 (None, 오류 목록)."""
    if isinstance(payload, ScamAssessment):
        return payload, []
    try:
        return ScamAssessment.model_validate(payload), []
    except ValidationError as exc:
        return None, [f"{'.'.join(map(str, e['loc']))}: {e['type']}" for e in exc.errors()]


def build_safe_fallback(state: dict[str, Any] | None) -> ScamAssessment:
    """검증을 통과하지 못했을 때 쓰는 안전 응답. 확인된 State만 근거로 쓴다 (2.2 8단계)."""
    state = state or {}
    money_sent = state.get("money_sent") is True
    stop_done = bool((state.get("checklist") or {}).get("지급정지 요청"))
    if money_sent and not stop_done:
        actions = [
            ActionStep(priority=1, action="송금한 은행 콜센터에 전화해 지급정지를 요청하세요", contact=None),
            ActionStep(priority=2, action="경찰에 사기 피해를 신고하세요", contact="112"),
        ]
    else:
        actions = [
            ActionStep(priority=1, action="받은 문자나 전화의 링크·번호로 연락하지 마세요", contact=None),
            ActionStep(priority=2, action="의심되면 경찰에 상담하세요", contact="112"),
        ]
    return ScamAssessment(
        scam_type="unknown",
        risk_level=state.get("risk_level") or "insufficient_info",
        damage_stage=state.get("damage_stage") or "none",
        confidence=0.0,
        evidence=[_MONEY_SENT_EVIDENCE if money_sent else "자동 판단 결과를 검증하지 못해 확인된 상태만 안내합니다"],
        unverified=["사기 유형", "위험 신호 분석 결과"],
        immediate_actions=actions,
        next_question=None,
        damage_flags=DamageFlags(),
        injection_detected=False,
    )


def audit_assessment(
    assessment: ScamAssessment,
    *,
    text: str | None = None,
    state: dict[str, Any] | None = None,
    allowed_contacts: Iterable[str] | None = None,
    classifier: Callable[[str], bool] | None = None,
) -> AuditResult:
    """ScamAssessment와 응답 텍스트를 검사하고, 고친 결과를 돌려준다. 입력 객체는 바꾸지 않는다."""
    state = state or {}
    vault = state.get("pii_vault") or {}
    allowed = _digits_set(allowed_contacts)
    data = assessment.model_dump()
    issues: list[AuditIssue] = []

    def clean(value: str | None) -> str | None:
        if value is None:
            return None
        value, found = soften_text(value)
        issues.extend(found)
        value, pii_count = mask_output_pii(value, vault, allowed)
        if pii_count:
            issues.append(AuditIssue("pii_exposed", f"{pii_count}건 가림"))
        return value

    data["evidence"] = [clean(e) for e in data["evidence"]]
    data["unverified"] = [clean(u) for u in data["unverified"]]
    for step in data["immediate_actions"]:
        step["action"] = clean(step["action"])
        contact = step.get("contact")
        if contact and allowed is not None:
            digits = re.sub(r"\D", "", contact)
            if len(digits) >= 3 and digits not in allowed:
                issues.append(AuditIssue("unknown_contact", "연락처 목록에 없는 번호 제거"))
                step["contact"] = None
        if step.get("contact"):
            step["contact"], pii_count = mask_output_pii(step["contact"], vault, allowed)
            if pii_count:
                issues.append(AuditIssue("pii_exposed", f"{pii_count}건 가림"))

    question, trimmed = _first_question(clean(data.get("next_question")))
    data["next_question"] = question
    if trimmed:
        issues.append(AuditIssue("multi_question", "첫 질문만 남김"))

    # G6: 의미 있는 근거가 없으면 판정 보류 (송금 확인 상태는 예외)
    meaningful = [e for e in data["evidence"] if e and not _PLACEHOLDER_EVIDENCE.match(e)]
    if not meaningful:
        if state.get("money_sent") is True:
            data["evidence"] = [_MONEY_SENT_EVIDENCE]
        else:
            data["evidence"] = [_HELD_EVIDENCE]
            if data["risk_level"] != "insufficient_info":
                data["risk_level"] = "insufficient_info"
            issues.append(AuditIssue("no_evidence", "근거 부족으로 판정 보류"))
    else:
        data["evidence"] = meaningful

    new_text = clean(text) if isinstance(text, str) else text

    failed = False
    if classifier is not None:
        combined = "\n".join(filter(None, [new_text, *data["evidence"], data.get("next_question")]))
        try:
            if classifier(combined):
                issues.append(AuditIssue("classifier_flag", "규칙으로 고치지 못한 단정 표현 의심"))
                failed = True
        except Exception:
            logger.exception("출력 감사 분류기 호출 실패 — 규칙 결과만 사용")

    fixed, errors = validate_payload(data)
    if fixed is None:  # 수정 과정에서 제약을 깨면 원본 유지
        logger.warning("감사 수정본이 스키마를 통과하지 못해 원본을 유지합니다: %s", errors)
        return AuditResult(assessment=assessment, text=text, issues=issues, changed=False, failed=True)

    changed = fixed.model_dump() != assessment.model_dump() or new_text != text
    return AuditResult(assessment=fixed, text=new_text, issues=issues, changed=changed, failed=failed)


# ---------------------------------------------------------------------------
# 미들웨어
# ---------------------------------------------------------------------------
def _structured_tool_messages(messages: list[Any], last: AIMessage, names: set[str]) -> list[ToolMessage]:
    """ToolStrategy가 남긴 'Returning structured response' 기록 메시지."""
    ids = {c.get("id") for c in (last.tool_calls or []) if c.get("name") in names}
    return [m for m in messages if isinstance(m, ToolMessage) and m.tool_call_id in ids]


def _last_ai(messages: list[Any]) -> AIMessage | None:
    for message in reversed(messages):
        if isinstance(message, AIMessage):
            return message
    return None


class OutputAuditMiddleware(AgentMiddleware[UnHookState, RuntimeContext]):
    """G5·G6. 모델이 최종 판단(structured_response)을 낸 호출 직후 검사한다.

    - Tool을 부르는 중간 호출은 건너뛴다.
    - 고친 structured_response와 AI 메시지를 State에 되돌려 쓴다.
    - 감사 결과 요약은 마지막 AI 메시지의 response_metadata["unhook_audit"]에 남긴다
      (State 필드를 새로 만들지 않기 위함). failed=True면 Agent가 재검토 판단에 쓸 수 있다.

    allowed_contacts: data/contacts.json의 전화번호 목록 (선택)
    classifier: 규칙에 없는 단정 표현을 판별하는 함수 text -> bool (선택, nano 연결용)
    """

    state_schema = UnHookState

    def __init__(
        self,
        allowed_contacts: Iterable[str] | None = None,
        classifier: Callable[[str], bool] | None = None,
        structured_tool_names: Iterable[str] = ("ScamAssessment",),
    ) -> None:
        super().__init__()
        self.allowed_contacts = list(allowed_contacts) if allowed_contacts is not None else None
        self.classifier = classifier
        self.structured_tool_names = set(structured_tool_names)

    def after_model(self, state: UnHookState, runtime) -> dict[str, Any] | None:
        raw = state.get("structured_response")
        if raw is None:
            return None
        last = _last_ai(state.get("messages", []))
        if last is None:
            return None
        pending_tools = [c["name"] for c in (last.tool_calls or []) if c.get("name") not in self.structured_tool_names]
        if pending_tools:
            return None  # 아직 Tool 호출 중인 중간 단계

        assessment, errors = validate_payload(raw)
        if assessment is None:
            logger.warning("구조화 출력 검증 실패 → 안전 응답으로 전환: %s", errors)
            fallback = build_safe_fallback(state)
            meta = {**(last.response_metadata or {}), "unhook_audit": {"changed": True, "failed": True, "issues": ["schema_invalid"]}}
            return {"structured_response": fallback, "messages": [last.model_copy(update={"response_metadata": meta})]}

        text = last.content if isinstance(last.content, str) else None
        result = audit_assessment(
            assessment, text=text, state=state,
            allowed_contacts=self.allowed_contacts, classifier=self.classifier,
        )
        if not result.issues:
            return None
        if result.failed:
            logger.warning("출력 감사에서 고치지 못한 위반: %s", [i.code for i in result.issues])
        else:
            logger.info("출력 감사 수정: %s", [i.code for i in result.issues])

        update_fields: dict[str, Any] = {"response_metadata": {**(last.response_metadata or {}), "unhook_audit": result.summary()}}
        if text is not None and result.text != text:
            update_fields["content"] = result.text
        replaced: list[Any] = [last.model_copy(update=update_fields)]  # 표시용 AI 메시지도 맞춰 둔다
        update: dict[str, Any] = {"messages": replaced}
        if result.changed:
            update["structured_response"] = result.assessment
            # 다음 턴에 모델이 고치기 전 문장을 다시 보지 않도록 기록 메시지도 맞춘다
            for tool_message in _structured_tool_messages(state.get("messages", []), last, self.structured_tool_names):
                replaced.append(tool_message.model_copy(
                    update={"content": f"Returning structured response: {result.assessment}"}))
        return update

    def after_agent(self, state: UnHookState, runtime) -> dict[str, Any] | None:
        """최종 structured_response 검사. UI는 이 값을 사용한다 (5.1)."""
        raw = state.get("structured_response")
        if raw is None:
            return None
        assessment, errors = validate_payload(raw)
        if assessment is None:
            logger.warning("최종 구조화 출력 검증 실패 → 안전 응답으로 전환: %s", errors)
            return {"structured_response": build_safe_fallback(state)}
        # 분류기는 after_model에서 이미 호출했으므로 여기서는 규칙 검사만 한다 (모델 호출 중복 방지)
        result = audit_assessment(assessment, text=None, state=state, allowed_contacts=self.allowed_contacts)
        if not result.changed:
            return None
        logger.info("최종 출력 감사 수정: %s", [i.code for i in result.issues])
        return {"structured_response": result.assessment}


__all__ = [
    "AuditIssue", "AuditResult", "ASSERTIVE_RULES", "FALSE_SAFETY_RULES",
    "find_assertive_phrases", "soften_text", "mask_output_pii", "validate_payload",
    "build_safe_fallback", "audit_assessment", "OutputAuditMiddleware",
]
