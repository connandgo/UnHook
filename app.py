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
RISK_LABEL = {
    "critical": "매우 위험", "high": "위험", "medium": "주의", "low": "낮음", "insufficient_info": "정보 부족",
}
# 위험도 색은 디자인 토큰의 chart/destructive 색을 쓴다 (.streamlit/config.toml과 같은 팔레트).
RISK_COLOR = {
    "critical": "#F4212E", "high": "#E0245E", "medium": "#F7B928", "low": "#00B87A", "insufficient_info": "#72767A",
}
RISK_TEXT = {"medium": "#0F1419"}  # 노란 배경은 흰 글자가 안 보여 어두운 글자를 쓴다.
STAGE_STEPS = [
    ("none", "피해 없음"), ("link_clicked", "링크 클릭"), ("info_exposed", "정보 노출"),
    ("app_installed", "앱 설치"), ("money_sent", "송금"),
]
DEFAULT_USER_ID = "demo-user"
DEFAULT_AGE_GROUP = "general"


def inject_theme_css() -> None:
    """config.toml이 못 미치는 세부(채팅 말풍선·폼·확장 패널)를 같은 팔레트로 맞춘다.

    Streamlit이 .stApp에 color-scheme을 붙이므로 light-dark()로 라이트·다크 값을 고르면
    테마를 바꿔도 스크립트 재실행 없이 바로 따라간다.
    """
    st.markdown(
        """
        <style>
        :root {
          --uh-card: light-dark(#F7F8F8, #17181C);
          --uh-border: light-dark(#E1EAEF, #242628);
          --uh-primary: light-dark(#1E9DF1, #1C9CF0);
          --uh-accent: light-dark(#E3ECF6, #061622);
          --uh-muted-fg: #72767A;
          --uh-radius: 1.3rem;
        }
        [data-testid="stChatMessage"] {
          background: var(--uh-card);
          border: 1px solid var(--uh-border);
          border-radius: var(--uh-radius);
          padding: 1rem 1.25rem;
        }
        [data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarUser"]) {
          background: var(--uh-accent);
          border-color: transparent;
        }
        [data-testid="stForm"] {
          background: var(--uh-card);
          border: 1px solid var(--uh-border);
          border-radius: var(--uh-radius);
          padding: 1.25rem 1.25rem 1rem;
        }
        [data-testid="stExpander"] details {
          border-radius: calc(var(--uh-radius) - 4px);
          border-color: var(--uh-border);
        }
        .uh-risk-card {
          border-radius: var(--uh-radius);
          padding: 16px 18px;
          margin-bottom: 14px;
        }
        .uh-risk-card .uh-risk-label { font-size: 0.8rem; opacity: 0.85; }
        .uh-risk-card .uh-risk-value { font-size: 1.6rem; font-weight: 700; line-height: 1.2; }
        .uh-stages { line-height: 1.9; margin-bottom: 12px; }
        .uh-stage-done, .uh-stage-todo, .uh-unknown { color: var(--uh-muted-fg); }
        .uh-stage-todo { opacity: 0.7; }
        .uh-section { color: var(--uh-muted-fg); font-size: 0.78rem; font-weight: 600;
          letter-spacing: 0.04em; text-transform: uppercase; margin: 10px 0 4px; }
        </style>
        """,
        unsafe_allow_html=True,
    )


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


def thread_messages(agent: Any, thread_id: str) -> list[Any]:
    """본 스레드 체크포인트에 저장된 메시지 목록. 없으면 빈 목록."""
    try:
        saved = agent.checkpointer.get_tuple({"configurable": {"thread_id": thread_id}})
    except Exception:
        return []
    if saved is None:
        return []
    return list((saved.checkpoint.get("channel_values") or {}).get("messages") or [])


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
        user_id=DEFAULT_USER_ID,
        age_group=DEFAULT_AGE_GROUP,
        user_statement=statement,
        quoted_content=quoted,
        state=st.session_state.snapshot,
        multiple_messages=len([b for b in quoted.split("\n\n") if b.strip()]) > 1,
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

    # 재검토(승격)가 일어나면 result.raw_state는 재검토용 별도 스레드의 상태라
    # 1차 nano 실행에서 부른 Tool이 빠진다. 본 스레드의 체크포인트도 함께 읽는다.
    tool_lines = collect_tool_activity(
        {"messages": thread_messages(agent, st.session_state.thread_id) + list(result.raw_state.get("messages", []))},
        st.session_state.seen_tool_events,
    )
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
    reset_conversation()

inject_theme_css()

def render_state_panel(snap: StateSnapshot) -> None:
    """현재 피해 상태를 한눈에 보이게 그린다."""
    color = RISK_COLOR.get(snap.risk_level, RISK_COLOR["insufficient_info"])
    text = RISK_TEXT.get(snap.risk_level, "#FFFFFF")
    st.markdown(
        f"""
        <div class="uh-risk-card" style="background:{color};color:{text};">
          <div class="uh-risk-label">현재 위험도</div>
          <div class="uh-risk-value">
            {RISK_ICON.get(snap.risk_level, "⚪")} {RISK_LABEL.get(snap.risk_level, snap.risk_level)}
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.markdown("<div class='uh-section'>피해 단계</div>", unsafe_allow_html=True)
    current = next((i for i, (key, _) in enumerate(STAGE_STEPS) if key == snap.damage_stage), 0)
    rows = []
    for i, (_, label) in enumerate(STAGE_STEPS):
        if i == current:
            rows.append(f"<div style='font-weight:700;color:{color};'>▶ {label}</div>")
        elif i < current:
            rows.append(f"<div class='uh-stage-done'>✓ {label}</div>")
        else:
            rows.append(f"<div class='uh-stage-todo'>○ {label}</div>")
    st.markdown("<div class='uh-stages'>" + "".join(rows) + "</div>", unsafe_allow_html=True)

    st.markdown("<div class='uh-section'>확인된 사실</div>", unsafe_allow_html=True)
    flags = [
        ("링크 클릭", snap.link_clicked),
        ("앱 설치", snap.app_installed),
        ("송금", snap.money_sent),
    ]
    for label, value in flags:
        if value is True:
            st.markdown(f"🔴 {label} **있음**")
        elif value is False:
            st.markdown(f"🟢 {label} 없음")
        else:
            st.markdown(f"<span class='uh-unknown'>➖ {label} 미확인</span>", unsafe_allow_html=True)
    if snap.info_exposed:
        st.markdown("🔴 노출 정보: **" + ", ".join(snap.info_exposed) + "**")
    if snap.sent_amount:
        st.markdown(f"💸 송금액 **{snap.sent_amount:,}원**")
    if snap.elapsed_minutes is not None:
        st.markdown(f"⏱ 송금 후 **{snap.elapsed_minutes}분** 경과")

    if snap.checklist:
        st.markdown("<div class='uh-section'>대응 체크리스트</div>", unsafe_allow_html=True)
        done = sum(1 for v in snap.checklist.values() if v)
        st.progress(done / len(snap.checklist), text=f"{done}/{len(snap.checklist)} 완료")
        for key, ok in snap.checklist.items():
            st.markdown(f"{'✅' if ok else '⬜'} {key}")


with st.sidebar:
    st.title("🪝 Un Hook")
    st.caption("금융사기 피해 상태 확인·대응 안내 Agent")
    if st.button("새 대화 시작", use_container_width=True):
        reset_conversation()
        st.rerun()
    st.divider()
    render_state_panel(StateSnapshot.model_validate(st.session_state.snapshot))
    st.divider()
    st.caption(f"{st.session_state.turn_count}턴 · thread `{st.session_state.thread_id}`")

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
    with st.container(border=True):
        col1, col2 = st.columns([3, 1], vertical_alignment="center")
        col1.markdown("**신고 접수** — 버튼을 누르기 전에는 신고 Tool이 실행되지 않습니다.")
        if col2.button("신고 접수 승인", type="secondary", use_container_width=True):
            run_turn(REPORT_APPROVAL_STATEMENT, "", report_approved=True)
            st.rerun()
elif st.session_state.report_done:
    st.success("신고 접수가 완료되었습니다. 접수 결과는 위 실행 정보에서 확인하세요.")
