"""Middleware from design section 3.2.

State transition rules live here rather than in `state.py` because section 5
assigns the derivation of `damage_stage` / `risk_level` and the prevention of
regression to `DamageStateMiddleware`.
"""

from langchain.agents.middleware import after_model

from schemas import DamageFlags, DamageStage, RiskLevel, ScamAssessment
from state import UnHookState

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
