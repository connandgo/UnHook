"""Middleware from design section 3.2.

State transition rules live here rather than in `state.py` because section 5
assigns the derivation of `damage_stage` / `risk_level` and the prevention of
regression to `DamageStateMiddleware`.
"""

import json
import logging
import re
from functools import lru_cache

from langchain.agents.middleware import (
    after_agent,
    after_model,
    before_agent,
    wrap_model_call,
)
from langchain_core.messages import HumanMessage, ToolMessage

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

# 하한만 정한다. 상황 판단은 모델이 얹는다 — TS-04-C001은 Tool 호출 0회에
# damage_stage=none인데도 critical이다. 값의 근거는 설계서 2.5 STAGE_RISK_FLOOR 표.
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


STEP_SEPARATOR = " — "
PLAYBOOK_TOOL_NAME = "get_scam_playbook"


@lru_cache(maxsize=1)
def _step_order() -> dict[str, list[str]]:
    """단계별 정규 조치 순서. rag.py와 같은 파일을 읽어 키가 어긋나지 않게 한다."""
    from rag import PLAYBOOK_FALLBACK_PATH

    return json.loads(PLAYBOOK_FALLBACK_PATH.read_text(encoding="utf-8"))["step_order"]


def _playbook_steps(messages: list) -> list[str] | None:
    """가장 최근 get_scam_playbook 결과의 steps. 없으면 None."""
    for message in reversed(messages or []):
        if not isinstance(message, ToolMessage):
            continue
        if getattr(message, "name", None) != PLAYBOOK_TOOL_NAME:
            continue
        content = message.content
        if isinstance(content, str):
            try:
                content = json.loads(content)
            except ValueError:
                return None
        steps = content.get("steps") if isinstance(content, dict) else None
        return steps or None
    return None


def build_checklist(damage_stage: DamageStage, playbook_steps: list[str] | None) -> dict[str, bool]:
    """조치 목록을 만든다.

    steps 각 항목은 `"<step_key> — <설명>"` 형식이고 앞부분이 checklist 키다
    (설계서 2.5 출력 형식). Tool 결과가 없으면 로컬 JSON의 단계별 순서를 쓴다.
    """
    if playbook_steps:
        keys = [step.split(STEP_SEPARATOR, 1)[0].strip() for step in playbook_steps]
    else:
        keys = _step_order().get(damage_stage, [])
    return {key: False for key in keys if key}


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

    current = state.get("checklist") or {}
    checklist = current
    # 송금 피해가 처음 확인될 때 조치 목록을 만든다 (설계서 3.1 checklist).
    if stage == "money_sent" and not current:
        checklist = build_checklist(stage, _playbook_steps(state.get("messages")))
    checklist = apply_checklist_done(checklist, flags.checklist_done)
    if checklist != current:
        update["checklist"] = checklist

    return update


GOLDEN_TIME_MINUTES = 30
PAYMENT_STOP_ITEM = "지급정지 요청"
MAX_ACTIONS = 5

# 4.2 TS-02-C003이 immediate_actions[0]으로 기대하는 문구.
PAYMENT_STOP_ACTION = "송금한 은행 콜센터에 즉시 전화해 지급정지 요청(다른 사람 휴대폰 사용)"
# data/contacts.json의 bank_callcenter label. 연락처 테이블 밖의 값을 쓰지 않는다 (4.2).
BANK_CALLCENTER_LABEL = "송금한 은행 콜센터"

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
        contact=BANK_CALLCENTER_LABEL,
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


# ── 조립 (설계서 5절 "미들웨어 조립", AGENTS.md ② 담당) ────────────────
#
# 실행 순서 규칙 (실측 확인):
#   before_agent·before_model : 목록 순서
#   wrap_model_call           : 목록 순서로 중첩 (앞이 바깥)
#   after_model·after_agent   : 목록 역순
#
# 그래서 목록 순서와 실행 순서가 다르다. 아래 배치는 설계서 3.2·5.1의
# 순서 요구를 실행 순서 기준으로 만족시킨 결과다.

REPORT_TOOL_NAME = "report_to_authority"
TOOL_RETRY_ATTEMPTS = 3


@lru_cache(maxsize=1)
def contact_labels() -> tuple[str, ...]:
    """출력 감사가 허용할 연락처. get_scam_playbook이 내보내는 label과 같은 출처다."""
    from rag import CONTACTS_PATH

    data = json.loads(CONTACTS_PATH.read_text(encoding="utf-8"))
    return tuple(a["label"] for a in data.get("agencies", []) if a.get("label"))


def build_middleware(*, classifier=None, allowed_contacts=None) -> list:
    """Agent에 넘길 미들웨어를 실행 순서가 맞게 배치해 돌려준다.

    classifier: 인젝션 판별 Runnable. None이면 guards.py 기본값(nano).
    allowed_contacts: 출력 감사가 허용할 연락처. None이면 data/contacts.json의 label.
    """
    from audit import OutputAuditMiddleware
    from guards import (
        ContentIsolationMiddleware,
        InjectionGuardMiddleware,
        TopicFilterMiddleware,
        build_input_middlewares,
    )
    from langchain.agents.middleware import (
        HumanInTheLoopMiddleware,
        ToolRetryMiddleware,
    )
    from pii import PIIMiddleware

    by_type = {type(m).__name__: m for m in build_input_middlewares(classifier=classifier)}
    topic_filter: TopicFilterMiddleware = by_type["TopicFilterMiddleware"]
    injection_guard: InjectionGuardMiddleware = by_type["InjectionGuardMiddleware"]
    content_isolation: ContentIsolationMiddleware = by_type["ContentIsolationMiddleware"]

    return [
        # 1. 마스킹이 가장 먼저. 인젝션 판별 모델·로그·Checkpointer 모두
        #    가려진 텍스트만 보게 한다 (설계서 2.1 "보조 모델에도 마스킹된 텍스트만").
        PIIMiddleware(),

        # 2. 최종 검사. after_* 는 역순이라 목록 맨 앞이 곧 가장 마지막 실행이다.
        #    after_model은 DamageState 뒤에, after_agent는 보안 보강·긴급 안내가
        #    모두 반영된 뒤에 돈다 (설계서 608줄).
        OutputAuditMiddleware(
            allowed_contacts=contact_labels() if allowed_contacts is None else allowed_contacts
        ),

        # 3. after_agent 전용. InjectionGuard 뒤, 최종 검사 앞에 와야 하므로
        #    목록에서는 그 둘 사이에 둔다.
        emergency_route_notice,

        # 4~5. 입력 가드. 마스킹 뒤에 온다.
        topic_filter,
        injection_guard,

        # 6~7. 긴급 판정과 이력 대조. 이력 대조는 pii_vault가 채워진 뒤라야 한다.
        emergency_route_detect,
        memory_inject_lookup,

        # 8. 피해 상태 갱신. after_model 중 가장 먼저 실행된다.
        damage_state_middleware,

        # 9~11. 모델 호출 감싸기. ContentIsolation은 프롬프트를 더하는 wrapper보다
        #       안쪽이어야 한다 (설계서 609줄).
        emergency_route_restrict,
        memory_inject_prompt,
        content_isolation,

        # 12~13. Tool 호출 감싸기. 승인이 재시도보다 바깥이라 거절된 호출은
        #        재시도되지 않는다.
        HumanInTheLoopMiddleware(interrupt_on={REPORT_TOOL_NAME: True}),
        ToolRetryMiddleware(max_retries=TOOL_RETRY_ATTEMPTS),
    ]


def build_checkpointer():
    """대화별 State 저장·복원. 없으면 멀티턴과 승인 재개가 동작하지 않는다.

    Colab 단일 세션 기준이라 인메모리를 쓴다 (설계서 1.5). 세션이 끝나면 사라진다.
    """
    from langgraph.checkpoint.memory import InMemorySaver

    return InMemorySaver()


def build_store():
    """대화 간 공유하는 과거 신고 이력 보관. 없으면 TS-05 재방문 경고가 빠진다."""
    from langgraph.store.memory import InMemoryStore

    return InMemoryStore()
