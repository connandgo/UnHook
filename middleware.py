"""Middleware from design section 3.2.

State transition rules live here rather than in `state.py` because section 5
assigns the derivation of `damage_stage` / `risk_level` and the prevention of
regression to `DamageStateMiddleware`.
"""

import logging
import re

from langchain.agents.middleware import (
    after_agent,
    after_model,
    before_agent,
    wrap_model_call,
)
from langchain_core.messages import HumanMessage

import memory
from pii import restore_tokens
from schemas import ActionStep, DamageFlags, DamageStage, RiskLevel, ScamAssessment
from state import UnHookState
from tools import LOOKUP_TOOL_NAMES

logger = logging.getLogger(__name__)

RISK_ORDER: dict[RiskLevel, int] = {
    "insufficient_info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4,
}
STAGE_ORDER: dict[DamageStage, int] = {
    "none": 0, "link_clicked": 1, "info_exposed": 2, "app_installed": 3, "money_sent": 4,
}

# Floor only: the confirmed damage stage cannot imply less risk than this.
# The model supplies the situational judgement on top (TS-04-C001 reaches
# critical with no tool call and no damage stage).
# TODO(강준모 확정): 설계서 3.1에 표로 올릴 것. money_sent는 TS-02-C002·TS-03-C001,
# app_installed는 TS-02-C001("Tool 호출 0회"이므로 조회 근거 없이 critical)로 값이
# 강제되고, link_clicked·info_exposed는 테스트 근거가 없는 제안값이다.
STAGE_RISK_FLOOR: dict[DamageStage, RiskLevel] = {
    "none": "insufficient_info",
    "link_clicked": "medium",
    "info_exposed": "high",
    "app_installed": "critical",
    "money_sent": "critical",
}

MAX_INFO_EXPOSED = 10


def merge_flag(current: bool | None, incoming: bool | None) -> bool | None:
    """확인된 피해는 되돌릴 수 없다. 미확인(None)만 False로 확정할 수 있다."""
    if incoming is None:
        return current
    if current is True and incoming is False:
        return current
    return incoming


def merge_info_exposed(current: list[str], incoming: list[str] | None) -> list[str]:
    if not incoming:
        return current
    return list(dict.fromkeys([*current, *incoming]))[:MAX_INFO_EXPOSED]


def compute_damage_stage(flags: dict) -> DamageStage:
    """여러 피해가 동시에 성립하면 가장 높은 단계를 대표값으로 삼는다."""
    if flags.get("money_sent"):
        return "money_sent"
    if flags.get("app_installed"):
        return "app_installed"
    if flags.get("info_exposed"):
        return "info_exposed"
    if flags.get("link_clicked"):
        return "link_clicked"
    return "none"


def escalate_stage(current: DamageStage, incoming: DamageStage) -> DamageStage:
    return incoming if STAGE_ORDER[incoming] >= STAGE_ORDER[current] else current


def escalate_risk(*levels: RiskLevel) -> RiskLevel:
    """위험도는 단조 증가한다. 하향은 G6(판정 보류)만 예외다."""
    return max(levels, key=lambda level: RISK_ORDER[level])


def apply_checklist_done(checklist: dict[str, bool], done: list[str] | None) -> dict[str, bool]:
    """사용자가 완료했다고 말한 항목만 True로 바꾼다. 없는 항목은 만들지 않는다.

    체크리스트 항목은 `get_scam_playbook`의 steps로 생성되므로, 모델이 옮겨 적은
    문구가 항목명과 정확히 일치할 때만 반영한다.
    """
    if not done:
        return checklist
    updated = dict(checklist)
    for item in done:
        if item in updated:
            updated[item] = True
    return updated


@after_model(state_schema=UnHookState)
def damage_state_middleware(state: UnHookState, runtime) -> dict | None:
    """모델이 추출한 피해 사실을 검증해 State에 반영하고 단계·위험도를 산출한다.

    모델이 낸 `damage_stage`·`risk_level`은 여기서 산출한 값으로 덮어쓴다.
    """
    assessment: ScamAssessment | None = state.get("structured_response")
    if assessment is None:
        return None

    flags: DamageFlags = assessment.damage_flags

    merged = {
        "link_clicked": merge_flag(state.get("link_clicked"), flags.link_clicked),
        "app_installed": merge_flag(state.get("app_installed"), flags.app_installed),
        "money_sent": merge_flag(state.get("money_sent"), flags.money_sent),
        "info_exposed": merge_info_exposed(state.get("info_exposed") or [], flags.info_exposed),
    }

    stage = escalate_stage(state.get("damage_stage") or "none", compute_damage_stage(merged))
    risk = escalate_risk(
        state.get("risk_level") or "insufficient_info",
        assessment.risk_level,
        STAGE_RISK_FLOOR[stage],
    )

    update: dict = {
        **merged,
        "damage_stage": stage,
        "risk_level": risk,
        "structured_response": assessment.model_copy(
            update={"damage_stage": stage, "risk_level": risk}
        ),
    }

    for field in ("sent_amount", "elapsed_minutes"):
        value = getattr(flags, field)
        if value is not None:
            update[field] = value

    checklist = apply_checklist_done(state.get("checklist") or {}, flags.checklist_done)
    if checklist != (state.get("checklist") or {}):
        update["checklist"] = checklist

    return update


GOLDEN_TIME_MINUTES = 30
PAYMENT_STOP_ITEM = "지급정지 요청"
MAX_ACTIONS = 5

# 4.2 TS-02-C003이 immediate_actions[0]으로 기대하는 문구.
PAYMENT_STOP_ACTION = "송금한 은행 콜센터에 즉시 전화해 지급정지 요청(다른 사람 휴대폰 사용)"

# 완료형 표현만 본다. "송금하면 안 되나요?" 같은 질문까지 긴급으로 보면
# 평범한 턴의 조회 Tool이 꺼져 TS-01이 깨진다.
_MONEY_SENT_RE = re.compile(
    r"보냈|부쳤|넘겼|송금\s*했|이체\s*했|입금\s*했|송금해\s*버|이체해\s*버"
)


def detect_money_sent(text: str) -> bool:
    """입력에 이미 송금이 끝났다는 표현이 있는지 본다."""
    return bool(_MONEY_SENT_RE.search(text))


def _last_human_text(messages: list) -> str:
    for message in reversed(messages or []):
        if isinstance(message, HumanMessage) and isinstance(message.content, str):
            return message.content
    return ""


def build_payment_stop_action(elapsed_minutes: int | None) -> str:
    """경과 시간은 문구의 긴급도만 조절한다. 지급정지 안내 자체는 항상 나간다."""
    if elapsed_minutes is None:
        return PAYMENT_STOP_ACTION
    if elapsed_minutes <= GOLDEN_TIME_MINUTES:
        return f"{PAYMENT_STOP_ACTION} — 송금 후 {elapsed_minutes}분, 지급정지 골든타임입니다"
    return f"{PAYMENT_STOP_ACTION} — {elapsed_minutes}분이 지났어도 반드시 신청하세요"


@before_agent(state_schema=UnHookState)
def emergency_route_detect(state: UnHookState, runtime) -> dict:
    """이번 턴이 긴급 턴인지 판정해 표시만 남긴다.

    Tool 목록은 `ModelRequest`에만 있고 `before_agent`는 State 갱신만 반환할 수
    있으므로, 실제 Tool 차단은 `emergency_route_restrict`가 맡는다.
    """
    if state.get("money_sent"):
        return {"emergency_mode": True}
    return {"emergency_mode": detect_money_sent(_last_human_text(state.get("messages")))}


@wrap_model_call(state_schema=UnHookState)
def emergency_route_restrict(request, handler):
    """긴급 턴에는 외부 조회 Tool을 빼고 모델을 호출한다.

    이미 송금한 사용자에게 URL 검사·번호 조회를 돌리는 것은 지급정지
    골든타임을 소모하는 행위다. 모델 호출 자체는 유지한다 — 사기 유형 판정과
    `damage_flags` 추출은 모델만 할 수 있기 때문이다.

    gpt-5 승격 억제도 같은 조건이지만 모델 선택은 `agent.py` 책임이므로,
    승격 로직은 State의 `emergency_mode`를 확인해야 한다.
    """
    if request.state.get("emergency_mode"):
        request.tools = [
            tool for tool in request.tools
            if getattr(tool, "name", None) not in LOOKUP_TOOL_NAMES
        ]
    return handler(request)


@after_agent(state_schema=UnHookState)
def emergency_route_notice(state: UnHookState, runtime) -> dict | None:
    """지급정지가 아직이면 이를 1순위 조치로 고정한다.

    구조화 출력을 쓰면 AIMessage 본문이 비어 있고 사용자에게 보이는 것은
    `ScamAssessment`다. 따라서 설계서 3.2의 "응답 첫 줄 고정"은
    `immediate_actions[0]` 삽입으로 구현한다 (4.2 TS-02-C003과 동일).
    """
    if not state.get("money_sent"):
        return None
    if (state.get("checklist") or {}).get(PAYMENT_STOP_ITEM):
        return None

    assessment: ScamAssessment | None = state.get("structured_response")
    if assessment is None:
        return None

    actions = assessment.immediate_actions
    if actions and actions[0].action.startswith(PAYMENT_STOP_ACTION):
        return None

    first = ActionStep(
        priority=1,
        action=build_payment_stop_action(state.get("elapsed_minutes")),
        contact=None,  # TODO: data/contacts.json 확정 후 은행 콜센터 안내 연결
    )
    renumbered = [first] + [
        action.model_copy(update={"priority": index})
        for index, action in enumerate(actions, start=2)
    ]
    return {
        "structured_response": assessment.model_copy(
            update={"immediate_actions": renumbered[:MAX_ACTIONS]}
        )
    }


SENIOR_TONE = (
    "이 사용자는 고령층입니다. 짧고 쉬운 문장을 쓰고, 지금 할 행동을 한 번에 하나씩만 "
    "제시하십시오. 전문용어 대신 일상 표현을 쓰십시오."
)


@before_agent(state_schema=UnHookState)
def memory_inject_lookup(state: UnHookState, runtime) -> dict:
    """Store의 과거 신고 이력을 이번 입력과 대조해 일치 항목을 기록한다.

    PII 미들웨어가 먼저 돌아 전화번호를 토큰으로 바꿔놓으므로, 대조 전에
    `pii_vault`로 원문을 되돌린다. 복원한 텍스트는 대조에만 쓰고 모델에 넘기지
    않는다.
    """
    store = getattr(runtime, "store", None)
    if store is None:
        return {"history_matches": []}

    text = _last_human_text(state.get("messages"))
    if not text:
        return {"history_matches": []}

    try:
        history = memory.load_history(store, runtime.context.user_id)
        matches = memory.match_history(
            history, restore_tokens(text, state.get("pii_vault"))
        )
    except Exception:
        # 조회 실패는 추측으로 채우지 않는다 (설계서 1.5 안정성).
        logger.exception("과거 신고 이력 조회 실패")
        return {"history_matches": []}

    return {"history_matches": matches}


@wrap_model_call(state_schema=UnHookState)
def memory_inject_prompt(request, handler):
    """과거 이력 일치 항목과 연령대별 응답 톤을 시스템 프롬프트에 얹는다."""
    blocks: list[str] = []

    matches = request.state.get("history_matches") or []
    if matches:
        blocks.append(memory.summarize_for_prompt(matches))

    if getattr(request.runtime.context, "age_group", "general") == "senior":
        blocks.append(SENIOR_TONE)

    if blocks:
        base = request.system_prompt or ""
        request.system_prompt = "\n\n".join([base, *blocks]) if base else "\n\n".join(blocks)

    return handler(request)
