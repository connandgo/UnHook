"""Interactive terminal client for the fully assembled UnHook Agent."""

from __future__ import annotations

import json
import os
import sys
import uuid
from typing import Any

from langchain_core.messages import ToolMessage

from .core import AgentTurnInput, build_unhook_agent
from .schemas import StateSnapshot


def _show_tool_activity(raw_state: dict[str, Any], seen: set[str]) -> None:
    for message in raw_state.get("messages", []):
        for call in getattr(message, "tool_calls", None) or []:
            name = str(call.get("name", "unknown"))
            if name == "ScamAssessment":
                continue
            call_id = f"call:{call.get('id', name)}"
            if call_id not in seen:
                print(f"  [Tool 호출] {name}")
                seen.add(call_id)
        if isinstance(message, ToolMessage):
            message_id = f"result:{message.id or message.tool_call_id}"
            if message_id in seen:
                continue
            seen.add(message_id)
            name = getattr(message, "name", None) or "unknown"
            content = message.content
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False, default=str)
            print(f"  [Tool 결과] {name}: {content}")


def _show_assessment(result: Any) -> None:
    assessment = result.assessment
    print("\nUnHook")
    print(f"  모델: {result.model_used}")
    if result.escalated:
        print(f"  검토 모델 승격: {', '.join(result.escalation_reasons)}")
    print(f"  판단: {assessment.scam_type} / 위험도 {assessment.risk_level}")
    print(f"  피해 단계: {assessment.damage_stage} / 신뢰도 {assessment.confidence:.2f}")
    if assessment.evidence:
        print("  근거:")
        for item in assessment.evidence:
            print(f"    - {item}")
    if assessment.unverified:
        print("  확인 필요:")
        for item in assessment.unverified:
            print(f"    - {item}")
    if assessment.immediate_actions:
        print("  지금 할 일:")
        for step in assessment.immediate_actions:
            contact = f" ({step.contact})" if step.contact else ""
            print(f"    {step.priority}. {step.action}{contact}")
    if assessment.next_question:
        print(f"  다음 질문: {assessment.next_question}")
    if assessment.injection_detected:
        print("  입력 안의 지시문 공격 신호가 감지되었습니다.")


def main() -> int:
    if not os.getenv("OPENAI_API_KEY"):
        print('OPENAI_API_KEY가 없습니다. 먼저 export OPENAI_API_KEY="..."를 실행하세요.')
        return 2

    try:
        agent = build_unhook_agent()
    except Exception as exc:
        print(f"Agent 초기화 실패: {type(exc).__name__}: {exc}")
        return 1

    thread_id = f"terminal-{uuid.uuid4().hex[:12]}"
    state = StateSnapshot()
    turn_count = 0
    seen_tool_events: set[str] = set()
    print("UnHook 터미널 대화 테스트")
    print("받은 문자·통화 원문은 두 번째 입력란에 넣으세요. 종료: /quit")

    while True:
        try:
            statement = input("\n내 상황/질문 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n종료합니다.")
            return 0
        if statement.lower() in {"/quit", "/exit"}:
            print("종료합니다.")
            return 0
        if not statement:
            continue
        quoted = input("받은 문자·통화 원문 (없으면 Enter) > ").strip()

        try:
            result = agent.invoke(AgentTurnInput(
                thread_id=thread_id,
                user_id="terminal-user",
                user_statement=statement,
                quoted_content=quoted,
                state=state,
                multiple_messages="\n" in quoted,
                conversation_turns=turn_count,
            ))
        except Exception as exc:
            if type(exc).__name__ == "LengthFinishReasonError":
                print("\n실행 오류: 모델이 추론 중 출력 토큰 한도를 모두 사용했습니다.")
                print("설정의 review_max_output_tokens를 확인한 뒤 다시 시도하세요.")
                continue
            print(f"\n실행 오류: {type(exc).__name__}: {exc}")
            print("입력을 바꿔 다시 시도하거나 /quit로 종료하세요.")
            continue

        _show_tool_activity(result.raw_state, seen_tool_events)
        _show_assessment(result)
        state = StateSnapshot.from_state(result.raw_state)
        turn_count += 1


if __name__ == "__main__":
    sys.exit(main())
