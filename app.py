"""Un Hook 시연 화면 (Streamlit). 대화 실행과 신고 승인을 Agent Core에 연결한다.

실행: streamlit run app.py  (OPENAI_API_KEY 필요, pip install streamlit)
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any

import streamlit as st
from langchain_core.messages import ToolMessage

from agent import AgentRunResult, AgentTurnInput, StateSnapshot, build_unhook_agent

REPORT_APPROVAL_STATEMENT = "신고 접수를 승인합니다. report_to_authority로 접수해 주세요."
RISK_ICON = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🟢", "insufficient_info": "⚪"}


@st.cache_resource(show_spinner="Agent를 준비하는 중...")
def get_agent():
    # Checkpointer·Store를 세션 재실행 간에 유지하기 위해 프로세스당 한 번만 만든다.
    return build_unhook_agent()


def reset_conversation() -> None:
    st.session_state.thread_id = f"web-{uuid.uuid4().hex[:12]}"
    # 세션에는 dict만 둔다. Streamlit이 모듈을 다시 임포트하면 클래스 객체가 바뀌어
    # 이전 인스턴스가 Pydantic 검증에서 거부되기 때문이다.
    st.session_state.snapshot = StateSnapshot().model_dump(mode="json")
    st.session_state.turn_count = 0
    st.session_state.history = []          # [{"role": "user"|"assistant", ...}]
    st.session_state.seen_tool_events = set()
    st.session_state.report_done = False


def collect_tool_activity(raw_state: dict[str, Any], seen: set[str]) -> list[str]:
    lines: list[str] = []
    for message in raw_state.get("messages", []):
        for call in getattr(message, "tool_calls", None) or []:
            name = str(call.get("name", "unknown"))
            if name == "ScamAssessment":
                continue
            key = f"call:{call.get('id', name)}"
            if key not in seen:
                seen.add(key)
                lines.append(f"🔧 Tool 호출: `{name}`")
        if isinstance(message, ToolMessage):
            key = f"result:{message.id or message.tool_call_id}"
            if key in seen:
                continue
            seen.add(key)
            content = message.content
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False, default=str)
            lines.append(f"📄 Tool 결과 `{getattr(message, 'name', None) or 'unknown'}`: {content}")
    return lines


def render_assessment(entry: dict[str, Any]) -> None:
    a = entry["assessment"]
    st.markdown(
        f"**{RISK_ICON.get(a['risk_level'], '⚪')} 위험도 `{a['risk_level']}` · 유형 `{a['scam_type']}` · "
        f"피해 단계 `{a['damage_stage']}` · 신뢰도 {a['confidence']:.2f}**"
    )
    if a.get("injection_detected"):
        st.warning("입력 안의 지시문 공격 신호가 감지되었습니다.")
    if a["immediate_actions"]:
        st.markdown("**지금 할 일**")
        for step in a["immediate_actions"]:
            contact = f" — {step['contact']}" if step.get("contact") else ""
            st.markdown(f"{step['priority']}. {step['action']}{contact}")
    if a["evidence"]:
        st.markdown("**판단 근거**")
        for item in a["evidence"]:
            st.markdown(f"- {item}")
    if a["unverified"]:
        st.markdown("**확인 필요**")
        for item in a["unverified"]:
            st.markdown(f"- {item}")
    if a.get("next_question"):
        st.info(f"❓ {a['next_question']}")
    with st.expander("실행 정보", expanded=False):
        model_line = f"모델: `{entry['model_used']}`"
        if entry["escalated"]:
            model_line += f" (재검토: {', '.join(entry['escalation_reasons'])})"
        st.markdown(model_line)
        for line in entry["tool_lines"]:
            st.markdown(line)
        if not entry["tool_lines"]:
            st.caption("Tool 호출 없음")


def run_turn(statement: str, quoted: str, *, report_approved: bool = False) -> None:
    agent = get_agent()
    turn = AgentTurnInput(
        thread_id=st.session_state.thread_id,
        user_id=st.session_state.user_id,
        age_group=st.session_state.age_group,
        user_statement=statement,
        quoted_content=quoted,
        state=st.session_state.snapshot,
        multiple_messages="\n" in quoted,
        conversation_turns=st.session_state.turn_count,
        report_approved=report_approved,
    )
    st.session_state.history.append({"role": "user", "statement": statement, "quoted": quoted})
    try:
        with st.spinner("분석 중..."):
            result: AgentRunResult = agent.invoke(turn)
    except Exception as exc:  # 시연 화면이므로 오류를 대화에 그대로 보여준다.
        detail = f"{type(exc).__name__}: {exc}"
        if type(exc).__name__ == "LengthFinishReasonError":
            detail = "모델이 추론 중 출력 토큰 한도를 모두 사용했습니다. 입력을 줄여 다시 시도하세요."
        st.session_state.history.append({"role": "error", "text": detail})
        return

    tool_lines = collect_tool_activity(result.raw_state, st.session_state.seen_tool_events)
    st.session_state.history.append({
        "role": "assistant",
        "assessment": result.assessment.model_dump(mode="json"),
        "model_used": result.model_used,
        "escalated": result.escalated,
        "escalation_reasons": list(result.escalation_reasons),
        "tool_lines": tool_lines,
    })
    st.session_state.snapshot = StateSnapshot.from_state(result.raw_state).model_dump(mode="json")
    st.session_state.turn_count += 1
    if report_approved and any("report_to_authority" in line for line in tool_lines):
        st.session_state.report_done = True


# ── 화면 ────────────────────────────────────────────────────────────────
st.set_page_config(page_title="Un Hook", page_icon="🪝", layout="wide")

if "thread_id" not in st.session_state:
    st.session_state.user_id = "demo-user"
    st.session_state.age_group = "general"
    reset_conversation()

with st.sidebar:
    st.title("🪝 Un Hook")
    st.caption("금융사기 피해 상태 확인·대응 안내 Agent")
    st.session_state.user_id = st.text_input("사용자 ID", st.session_state.user_id)
    st.session_state.age_group = st.selectbox(
        "연령대", ["general", "senior"],
        index=["general", "senior"].index(st.session_state.age_group),
    )
    if st.button("새 대화 시작", use_container_width=True):
        reset_conversation()
        st.rerun()

    st.divider()
    st.subheader("현재 피해 상태")
    snap = StateSnapshot.model_validate(st.session_state.snapshot)
    st.markdown(
        f"{RISK_ICON.get(snap.risk_level, '⚪')} 위험도 `{snap.risk_level}`  \n"
        f"피해 단계 `{snap.damage_stage}`  \n"
        f"채널 `{snap.channel}`"
    )
    flags = {
        "링크 클릭": snap.link_clicked,
        "앱 설치": snap.app_installed,
        "송금": snap.money_sent,
    }
    for label, value in flags.items():
        mark = "✅" if value else ("❌" if value is False else "➖")
        st.markdown(f"{mark} {label}")
    if snap.info_exposed:
        st.markdown("노출 정보: " + ", ".join(snap.info_exposed))
    if snap.sent_amount:
        st.markdown(f"송금액: {snap.sent_amount:,}원")
    if snap.elapsed_minutes is not None:
        st.markdown(f"경과: {snap.elapsed_minutes}분")
    if snap.checklist:
        st.markdown("**체크리스트**")
        for key, done in snap.checklist.items():
            st.markdown(f"{'☑' if done else '☐'} {key}")
    st.caption(f"thread: `{st.session_state.thread_id}` · {st.session_state.turn_count}턴")

if not os.getenv("OPENAI_API_KEY"):
    st.error("OPENAI_API_KEY 환경변수가 없습니다. 설정 후 다시 실행하세요.")
    st.stop()

st.header("금융사기 상담")

for entry in st.session_state.history:
    if entry["role"] == "user":
        with st.chat_message("user"):
            st.markdown(entry["statement"])
            if entry["quoted"]:
                st.code(entry["quoted"], language=None)
    elif entry["role"] == "assistant":
        with st.chat_message("assistant"):
            render_assessment(entry)
    else:
        with st.chat_message("assistant"):
            st.error(f"실행 오류: {entry['text']}")

with st.form("turn_form", clear_on_submit=True):
    statement = st.text_input("내 상황 / 질문", placeholder="예: 링크는 눌렀는데 송금은 안 했어요")
    quoted = st.text_area(
        "받은 문자·통화 원문 (없으면 비워두세요)", height=100,
        placeholder="예: [택배] 주소 불일치로 반송. 아래 링크에서 확인하세요 http://...",
    )
    submitted = st.form_submit_button("보내기", type="primary", use_container_width=True)

if submitted:
    if not statement.strip():
        st.warning("내 상황 / 질문을 입력하세요.")
    else:
        run_turn(statement.strip(), quoted.strip())
        st.rerun()

# 신고 접수(HITL): 사용자가 버튼을 눌러야만 report_approved=True로 Tool이 모델에 제공된다.
if st.session_state.history and not st.session_state.report_done:
    st.divider()
    col1, col2 = st.columns([3, 1])
    col1.markdown("**신고 접수** — 버튼을 누르기 전에는 신고 Tool이 실행되지 않습니다.")
    if col2.button("신고 접수 승인", type="secondary", use_container_width=True):
        run_turn(REPORT_APPROVAL_STATEMENT, "", report_approved=True)
        st.rerun()
elif st.session_state.report_done:
    st.success("신고 접수가 완료되었습니다. 접수 결과는 위 실행 정보에서 확인하세요.")
