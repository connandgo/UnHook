"""Un Hook 시연 화면 (Streamlit). 대화 실행과 신고 승인을 Agent Core에 연결한다.

실행: streamlit run app.py  (OPENAI_API_KEY 필요, pip install streamlit)
"""

from __future__ import annotations

import html
import json
import os
import uuid
from typing import Any

import streamlit as st
from langchain_core.messages import ToolMessage

from agent import AgentRunResult, AgentTurnInput, StateSnapshot, build_unhook_agent

REPORT_APPROVAL_STATEMENT = "신고 접수를 승인합니다. report_to_authority로 접수해 주세요."
RISK_LABEL = {
    "critical": "매우 위험", "high": "위험", "medium": "주의", "low": "낮음", "insufficient_info": "정보 부족",
}
# 위험도 색은 디자인 토큰의 chart/destructive 색을 쓴다 (.streamlit/config.toml과 같은 팔레트).
RISK_COLOR = {
    "critical": "#F4212E", "high": "#E0245E", "medium": "#F7B928", "low": "#00B87A", "insufficient_info": "#72767A",
}
RISK_TEXT = {"medium": "#0F1419"}  # 노란 배경은 흰 글자가 안 보여 어두운 글자를 쓴다.
SCAM_LABEL = {
    "smishing": "스미싱(문자 사기)", "voice_phishing": "보이스피싱", "messenger_phishing": "메신저 피싱",
    "loan_scam": "대출 사기", "gov_impersonation": "기관 사칭", "investment_scam": "투자 사기", "unknown": "유형 미확정",
}
STAGE_STEPS = [
    ("none", "피해 없음"), ("link_clicked", "링크 클릭"), ("info_exposed", "정보 노출"),
    ("app_installed", "앱 설치"), ("money_sent", "송금"),
]
QUICK_STARTS = ["택배 문자 링크를 눌렀어요", "앱을 설치하라고 해요", "이미 돈을 보냈어요"]
# 분석 중 말풍선. 모델 호출은 스트리밍이 아니라 단계 문구를 CSS로 순환시킨다.
THINKING_STEPS = ["상황을 읽고 있어요", "링크와 번호를 조회하고 있어요", "대응 절차를 찾고 있어요", "답변을 정리하고 있어요"]
THINKING_HTML = (
    "<div class='uh-thinking'><div class='uh-dots'><span></span><span></span><span></span></div>"
    "<div class='uh-thinking-text'>" + "".join(f"<span>{t}</span>" for t in THINKING_STEPS) + "</div></div>"
    "<div class='uh-skeleton'><i style='width:38%'></i><i style='width:92%'></i><i style='width:80%'></i></div>"
)
DEFAULT_USER_ID = "demo-user"
DEFAULT_AGE_GROUP = "general"
# 어시스턴트 아바타: 하늘색 유리구슬 얼굴. 테마 primary(#1E9DF1) 계열 그라데이션.
ASSISTANT_AVATAR = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
<defs>
<radialGradient id="b" cx="42%" cy="34%" r="64%">
<stop offset="0" stop-color="#9ED8FF"/><stop offset="0.45" stop-color="#3DB0F7"/>
<stop offset="0.8" stop-color="#1E9DF1"/><stop offset="1" stop-color="#1279CC"/></radialGradient>
<radialGradient id="g" cx="50%" cy="100%" r="55%">
<stop offset="0" stop-color="#B9F3FF" stop-opacity="0.95"/><stop offset="0.7" stop-color="#B9F3FF" stop-opacity="0"/></radialGradient>
<radialGradient id="h" cx="50%" cy="50%" r="50%">
<stop offset="0" stop-color="#fff" stop-opacity="0.9"/><stop offset="1" stop-color="#fff" stop-opacity="0"/></radialGradient>
</defs>
<circle cx="32" cy="32" r="30" fill="url(#b)"/>
<circle cx="32" cy="32" r="30" fill="url(#g)"/>
<ellipse cx="27" cy="15" rx="15" ry="9" fill="url(#h)"/>
<rect x="23.5" y="25" width="5" height="14" rx="2.5" fill="#fff"/>
<rect x="35.5" y="25" width="5" height="14" rx="2.5" fill="#fff"/>
</svg>"""


def inject_theme_css() -> None:
    """config.toml이 못 미치는 세부(말풍선·단계 레일·할 일 카드)를 같은 팔레트로 맞춘다.

    Streamlit이 .stApp에 color-scheme을 붙이므로 light-dark()로 라이트·다크 값을 고르면
    테마를 바꿔도 스크립트 재실행 없이 바로 따라간다.
    """
    st.markdown(
        """
        <style>
        /* 글꼴: Pretendard. config.toml의 font 소스 URL이 헤더에 주입되지 않아 여기서 직접 불러온다. */
        @import url('https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/static/pretendard.min.css');
        :root {
          --uh-fg: light-dark(#0F1419, #E7E9EA);
          --uh-card: light-dark(#F7F8F8, #17181C);
          --uh-border: light-dark(#E1EAEF, #242628);
          --uh-primary: light-dark(#1E9DF1, #1C9CF0);
          --uh-accent: light-dark(#E3ECF6, #061622);
          --uh-muted: light-dark(#E5E5E6, #2A2C30);
          --uh-muted-fg: #72767A;
          --uh-radius: 1.3rem;
        }
        /* 본문 폭: 채팅 열 하나에 집중한다. */
        .block-container { max-width: 46rem; padding-top: 3.25rem; padding-bottom: 2rem; }
        [data-testid="stBottomBlockContainer"] { max-width: 46rem; padding-top: 0.4rem; padding-bottom: 1.25rem; }
        [data-testid="stHeader"] { background: transparent; }

        /* ── 사건 상태 줄: 위험도 배지 + 피해 단계 레일 (이 화면의 서명 요소) ── */
        .uh-case {
          display: flex; align-items: center; gap: 1.25rem; flex-wrap: wrap;
          padding: 0.85rem 1.1rem 0.85rem 1.25rem; margin-bottom: 1.25rem;
          background: var(--uh-card); border: 1px solid var(--uh-border); border-radius: var(--uh-radius);
        }
        .uh-case-left { display: flex; flex-direction: column; gap: 0.15rem; }
        .uh-eyebrow { color: var(--uh-muted-fg); font-size: 0.7rem; font-weight: 700; letter-spacing: 0.08em; }
        .uh-verdict {
          display: inline-flex; align-items: center; gap: 0.45rem; white-space: nowrap; align-self: flex-start;
          padding: 0.3rem 0.85rem; border-radius: 999px; font-weight: 700; font-size: 0.95rem;
          background: var(--uh-risk); color: var(--uh-risk-text, #fff);
        }
        .uh-rail { display: flex; flex: 1 1 18rem; align-items: center; min-width: 0; padding-top: 0.2rem; }
        .uh-rail-step {
          position: relative; flex: 1; text-align: center; font-size: 0.8rem; color: var(--uh-muted-fg);
          padding-top: 1.15rem; white-space: nowrap;
        }
        .uh-rail-step::before {
          content: ""; position: absolute; top: 0.25rem; left: 50%; width: 0.7rem; height: 0.7rem; z-index: 1;
          transform: translateX(-50%); border-radius: 50%;
          background: var(--uh-card); border: 2px solid var(--uh-border); box-sizing: border-box;
        }
        .uh-rail-step:not(:first-child)::after {
          content: ""; position: absolute; top: 0.52rem; right: 50%; width: 100%; height: 2px;
          background: var(--uh-border);
        }
        .uh-rail-step.done { color: var(--uh-fg); }
        .uh-rail-step.done::before, .uh-rail-step.done::after,
        .uh-rail-step.now::before, .uh-rail-step.now::after { background: var(--uh-risk); border-color: var(--uh-risk); }
        .uh-rail-step.now { color: var(--uh-risk); font-weight: 700; }
        .uh-rail-step.now::before { box-shadow: 0 0 0 4px color-mix(in srgb, var(--uh-risk) 22%, transparent); }

        /* ── 첫 화면 ── */
        .uh-hero { padding: 2.5rem 0 1.25rem; }
        .uh-hero h1 { font-size: 2rem; font-weight: 700; letter-spacing: -0.02em; line-height: 1.25; margin: 0 0 0.6rem; }
        .uh-hero p { color: var(--uh-muted-fg); font-size: 1.02rem; line-height: 1.6; margin: 0; max-width: 34rem; }
        .uh-hero-label { color: var(--uh-muted-fg); font-size: 0.82rem; margin: 1.75rem 0 0.35rem; }

        /* ── 말풍선 ── */
        [data-testid="stChatMessage"] { padding: 0.35rem 0; background: transparent; gap: 0.75rem; }
        [data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarUser"]) {
          flex-direction: row-reverse; width: 86% !important; margin-left: auto;
        }
        [data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarUser"]) [data-testid="stChatMessageContent"] {
          background: var(--uh-accent);
          border-radius: var(--uh-radius) 0.4rem var(--uh-radius) var(--uh-radius);
          padding: 0.75rem 1.05rem;
        }
        [data-testid="stChatMessageAvatarUser"] { background: var(--uh-muted) !important; color: var(--uh-fg) !important; }
        [data-testid="stChatMessageAvatarCustom"] {
          width: 2.1rem; height: 2.1rem; border-radius: 50%; border: none; background: transparent; padding: 0;
          filter: drop-shadow(0 3px 5px color-mix(in srgb, var(--uh-primary) 35%, transparent));
        }
        [data-testid="stChatMessageAvatarCustom"] img { width: 100%; height: 100%; object-fit: contain; }
        .uh-hero-face { width: 4.5rem; height: 4.5rem; margin-bottom: 1rem;
          filter: drop-shadow(0 8px 14px color-mix(in srgb, var(--uh-primary) 35%, transparent)); }
        .uh-quote {
          margin-top: 0.6rem; padding: 0.6rem 0.85rem;
          background: light-dark(rgba(255,255,255,0.7), rgba(255,255,255,0.05));
          border-left: 3px solid var(--uh-primary); border-radius: 0.5rem;
          white-space: pre-wrap; word-break: break-word; font-size: 0.9rem; line-height: 1.5;
        }
        .uh-quote-label { color: var(--uh-primary); font-size: 0.72rem; font-weight: 700; letter-spacing: 0.06em; margin-bottom: 0.2rem; }

        /* ── 어시스턴트 답변 ── */
        .uh-answer-head { display: flex; align-items: center; gap: 0.6rem; flex-wrap: wrap; min-height: 2rem; margin: 0 0 0.75rem; }
        .uh-answer-head .uh-verdict { font-size: 0.85rem; padding: 0.22rem 0.7rem; }
        .uh-answer-meta { color: var(--uh-muted-fg); font-size: 0.85rem; }
        .uh-h { font-weight: 700; font-size: 0.95rem; margin: 0.9rem 0 0.5rem; }
        .uh-steps { display: flex; flex-direction: column; gap: 0.4rem; }
        .uh-step {
          display: flex; gap: 0.8rem; align-items: flex-start;
          background: var(--uh-card); border: 1px solid var(--uh-border);
          border-radius: 0.9rem; padding: 0.75rem 0.95rem;
        }
        .uh-step-n {
          flex: none; width: 1.55rem; height: 1.55rem; border-radius: 50%; display: grid; place-items: center;
          background: var(--uh-risk); color: var(--uh-risk-text, #fff); font-weight: 700; font-size: 0.78rem;
        }
        .uh-step-body { line-height: 1.5; font-size: 0.98rem; }
        .uh-step-contact {
          display: inline-block; margin-left: 0.45rem; padding: 0.05rem 0.55rem; border-radius: 999px;
          background: var(--uh-accent); color: var(--uh-primary); font-size: 0.8rem; font-weight: 600; vertical-align: 1px;
          text-decoration: none !important; white-space: nowrap;
        }
        a.uh-step-contact:hover { background: var(--uh-primary); color: #fff; }
        .uh-ask { padding: 0.9rem 0 0.2rem; font-size: 1.05rem; line-height: 1.6; font-weight: 500; }
        .uh-quiet { color: var(--uh-muted-fg); font-size: 0.85rem; }
        /* 근거·실행 정보는 테두리 없는 작은 토글로. 답변의 무게를 할 일에 둔다. */
        [data-testid="stChatMessage"] [data-testid="stExpander"] details {
          border: none; background: transparent; border-radius: 0;
        }
        [data-testid="stChatMessage"] [data-testid="stExpander"] summary {
          padding: 0.25rem 0; font-size: 0.85rem; color: var(--uh-muted-fg); background: transparent !important;
        }
        [data-testid="stChatMessage"] [data-testid="stExpander"] summary:hover { color: var(--uh-primary); }
        [data-testid="stChatMessage"] [data-testid="stExpanderDetails"] { padding: 0.2rem 0 0.4rem; }
        .uh-detail {
          background: var(--uh-card); border: 1px solid var(--uh-border); border-radius: 0.9rem;
          padding: 0.25rem 1.1rem; font-size: 0.9rem;
        }
        .uh-detail section { padding: 0.85rem 0; }
        .uh-detail section + section { border-top: 1px dashed var(--uh-border); }
        .uh-detail h4 {
          margin: 0 0 0.45rem; font-size: 0.72rem; font-weight: 700; letter-spacing: 0.08em; color: var(--uh-muted-fg);
        }
        .uh-detail ul { list-style: none; margin: 0 !important; padding: 0 !important; display: flex; flex-direction: column; gap: 0.35rem; }
        .uh-detail li { padding: 0 !important; }
        .uh-detail li { display: flex; gap: 0.6rem; align-items: flex-start; line-height: 1.5; }
        .uh-mark { flex: none; width: 0.5rem; height: 0.5rem; border-radius: 50%; margin-top: 0.5rem; }
        .uh-mark.yes { background: var(--uh-primary); }
        .uh-mark.ask { background: transparent; border: 2px solid var(--uh-muted-fg); box-sizing: border-box; }
        .uh-chips { display: flex; flex-wrap: wrap; gap: 0.4rem; }
        .uh-chip {
          display: inline-flex; align-items: center; gap: 0.3rem; padding: 0.15rem 0.6rem; border-radius: 999px;
          background: light-dark(#fff, #000); border: 1px solid var(--uh-border); color: var(--uh-muted-fg); font-size: 0.78rem;
        }
        .uh-chip b { color: var(--uh-fg); font-weight: 600; font-family: var(--font-mono, Menlo, monospace); font-size: 0.74rem; }
        .uh-chip.warn { border-color: color-mix(in srgb, #F7B928 60%, transparent); background: color-mix(in srgb, #F7B928 14%, transparent); color: light-dark(#7A5A00, #F7B928); }
        .uh-tool {
          margin-top: 0.5rem; padding: 0.45rem 0.7rem; border-radius: 0.5rem;
          background: light-dark(#fff, #000); border: 1px solid var(--uh-border);
          font-family: var(--font-mono, Menlo, monospace); font-size: 0.76rem; line-height: 1.5; word-break: break-all;
        }

        /* ── 사이드바: 로고 + 사건 기록 ── */
        .uh-brand { display: flex; align-items: center; gap: 0.75rem; padding: 0.25rem 0 0.9rem; }
        .uh-brand-face { flex: none; width: 2.6rem; height: 2.6rem;
          filter: drop-shadow(0 4px 8px color-mix(in srgb, var(--uh-primary) 35%, transparent)); }
        .uh-brand-name { font-size: 1.35rem; font-weight: 700; letter-spacing: -0.02em; line-height: 1.15; }
        .uh-brand-sub { color: var(--uh-muted-fg); font-size: 0.78rem; margin-top: 0.15rem; }
        [data-testid="stSidebar"] .uh-section {
          color: var(--uh-muted-fg); font-size: 0.72rem; font-weight: 700; letter-spacing: 0.06em;
          text-transform: uppercase; margin: 1.1rem 0 0.4rem;
        }
        .uh-fact { display: flex; justify-content: space-between; align-items: center; padding: 0.4rem 0; font-size: 0.9rem; }
        .uh-fact + .uh-fact { border-top: 1px solid var(--uh-border); }
        .uh-tag { padding: 0.1rem 0.6rem; border-radius: 999px; font-size: 0.78rem; font-weight: 600; text-align: right; max-width: 60%; }
        .uh-tag.bad { background: color-mix(in srgb, #F4212E 14%, transparent); color: #F4212E; }
        .uh-tag.ok { background: color-mix(in srgb, #00B87A 16%, transparent); color: #00B87A; }
        .uh-tag.na { background: var(--uh-muted); color: var(--uh-muted-fg); }
        .uh-check { display: flex; gap: 0.5rem; align-items: center; padding: 0.2rem 0; font-size: 0.9rem; }
        .uh-check.done { color: var(--uh-muted-fg); text-decoration: line-through; }

        /* ── 분석 중 표시 ── */
        .uh-thinking { display: flex; align-items: center; gap: 0.7rem; min-height: 2rem; color: var(--uh-muted-fg); font-size: 0.95rem; }
        .uh-dots { display: inline-flex; gap: 0.28rem; }
        .uh-dots span { width: 0.45rem; height: 0.45rem; border-radius: 50%; background: var(--uh-primary); opacity: 0.35; }
        .uh-thinking-text { position: relative; height: 1.4rem; flex: 1; }
        .uh-thinking-text span { position: absolute; left: 0; top: 0; line-height: 1.4rem; opacity: 0; white-space: nowrap; }
        .uh-thinking-text span:first-child { opacity: 1; }
        .uh-skeleton { display: flex; flex-direction: column; gap: 0.55rem; margin-top: 0.9rem; }
        .uh-skeleton i {
          display: block; height: 0.85rem; border-radius: 999px;
          background: linear-gradient(90deg, var(--uh-card) 25%, var(--uh-muted) 50%, var(--uh-card) 75%);
          background-size: 200% 100%;
        }
        @media (prefers-reduced-motion: no-preference) {
          .uh-dots span { animation: uh-dot 1.2s ease-in-out infinite; }
          .uh-dots span:nth-child(2) { animation-delay: 0.15s; }
          .uh-dots span:nth-child(3) { animation-delay: 0.3s; }
          @keyframes uh-dot { 0%, 80%, 100% { opacity: 0.35; transform: translateY(0); } 40% { opacity: 1; transform: translateY(-3px); } }
          .uh-thinking-text span { animation: uh-cycle 12s linear infinite; }
          .uh-thinking-text span:first-child { opacity: 0; }
          .uh-thinking-text span:nth-child(2) { animation-delay: 3s; }
          .uh-thinking-text span:nth-child(3) { animation-delay: 6s; }
          .uh-thinking-text span:nth-child(4) { animation-delay: 9s; }
          @keyframes uh-cycle {
            0% { opacity: 0; transform: translateY(4px); } 3% { opacity: 1; transform: none; }
            22% { opacity: 1; transform: none; } 25%, 100% { opacity: 0; transform: translateY(-4px); }
          }
          .uh-skeleton i { animation: uh-shimmer 1.6s linear infinite; }
          @keyframes uh-shimmer { from { background-position: 200% 0; } to { background-position: -200% 0; } }
        }

        /* ── 하단 입력 줄 ── */
        [data-testid="stBottom"] > div { background: transparent; }
        [data-testid="stChatInput"] { border-radius: var(--uh-radius); }
        [data-testid="stPopoverBody"] { width: min(34rem, 92vw); }
        @media (prefers-reduced-motion: no-preference) {
          [data-testid="stChatMessage"] { animation: uh-in 0.25s ease-out; }
          @keyframes uh-in { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: none; } }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def risk_vars(risk_level: str) -> str:
    """위험도 색을 CSS 변수로 넘기는 inline style 문자열."""
    color = RISK_COLOR.get(risk_level, RISK_COLOR["insufficient_info"])
    text = RISK_TEXT.get(risk_level, "#FFFFFF")
    return f"--uh-risk:{color};--uh-risk-text:{text};"


def verdict_html(risk_level: str) -> str:
    return (
        f"<span class='uh-verdict'>{html.escape(RISK_LABEL.get(risk_level, risk_level))}</span>"
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
    st.session_state.pending_turn = None   # 화면에는 올렸지만 아직 모델 호출 전인 턴


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


def queue_turn(statement: str, quoted: str, *, report_approved: bool = False) -> None:
    """사용자 메시지를 먼저 화면에 올리고, 모델 호출은 다음 실행(complete_turn)으로 넘긴다."""
    st.session_state.history.append({"role": "user", "statement": statement, "quoted": quoted})
    st.session_state.pending_turn = {"statement": statement, "quoted": quoted, "report_approved": report_approved}
    st.session_state.clear_quoted_draft = True
    st.rerun()


def complete_turn(statement: str, quoted: str, *, report_approved: bool = False) -> None:
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
    try:
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
st.set_page_config(page_title="Un Hook", page_icon="🪝", layout="centered")

if "thread_id" not in st.session_state:
    reset_conversation()
# 문자 원문 첨부는 전송 후 비운다. 위젯이 그려지기 전에만 값을 바꿀 수 있어 플래그로 미룬다.
if st.session_state.pop("clear_quoted_draft", False):
    st.session_state.quoted_draft = ""

inject_theme_css()


def render_case_strip(snap: StateSnapshot) -> None:
    """현재 위험도 배지와 피해 단계 레일. 대화 위에 항상 보인다."""
    current = next((i for i, (key, _) in enumerate(STAGE_STEPS) if key == snap.damage_stage), 0)
    steps = []
    for i, (_, label) in enumerate(STAGE_STEPS):
        cls = "now" if i == current else "done" if i < current else ""
        steps.append(f"<div class='uh-rail-step {cls}'>{label}</div>")
    st.markdown(
        f"<div class='uh-case' style='{risk_vars(snap.risk_level)}'>"
        f"<div class='uh-case-left'><span class='uh-eyebrow'>현재 위험도</span>{verdict_html(snap.risk_level)}</div>"
        f"<div class='uh-rail'>{''.join(steps)}</div></div>",
        unsafe_allow_html=True,
    )


def render_assessment(entry: dict[str, Any]) -> None:
    a = entry["assessment"]
    style = risk_vars(a["risk_level"])
    scam = SCAM_LABEL.get(a["scam_type"], a["scam_type"])
    st.markdown(
        f"<div class='uh-answer-head' style='{style}'>{verdict_html(a['risk_level'])}"
        f"<span class='uh-answer-meta'>{html.escape(scam)} · 확신도 {a['confidence'] * 100:.0f}%</span></div>",
        unsafe_allow_html=True,
    )
    if a.get("injection_detected"):
        st.warning("보내주신 문자 안에 상담 결과를 조작하려는 문구가 있었습니다. 그 지시는 무시하고 판단했습니다.")
    if a["immediate_actions"]:
        cards = []
        for step in a["immediate_actions"]:
            contact = ""
            if step.get("contact"):
                raw = str(step["contact"]).strip()
                if raw.startswith(("http://", "https://")):
                    label = html.escape(raw.split("://", 1)[1].rstrip("/"))
                    contact = f"<a class='uh-step-contact' href='{html.escape(raw)}' target='_blank' rel='noopener'>🔗 {label}</a>"
                else:
                    contact = f"<span class='uh-step-contact'>☎ {html.escape(raw)}</span>"
            cards.append(
                f"<div class='uh-step'><span class='uh-step-n'>{step['priority']}</span>"
                f"<div class='uh-step-body'>{html.escape(step['action'])}{contact}</div></div>"
            )
        st.markdown(
            f"<div class='uh-h'>지금 바로 할 일</div><div class='uh-steps' style='{style}'>{''.join(cards)}</div>",
            unsafe_allow_html=True,
        )
    if a.get("next_question"):
        st.markdown(f"<div class='uh-ask'>{html.escape(a['next_question'])}</div>", unsafe_allow_html=True)
    with st.expander("왜 이렇게 판단했나요?"):
        st.markdown(detail_html(entry), unsafe_allow_html=True)


def detail_html(entry: dict[str, Any]) -> str:
    """판단 근거·미확인 사항·실행 정보를 구획이 분명한 카드 하나로 그린다."""
    a = entry["assessment"]

    def items(values: list[str], mark: str) -> str:
        return "".join(f"<li><span class='uh-mark {mark}'></span>{html.escape(v)}</li>" for v in values)

    sections = [
        f"<section><h4>판단 근거</h4><ul>{items(a['evidence'], 'yes')}</ul></section>",
    ]
    if a["unverified"]:
        sections.append(f"<section><h4>아직 확인되지 않은 것</h4><ul>{items(a['unverified'], 'ask')}</ul></section>")

    chips = [f"<span class='uh-chip'>모델 <b>{html.escape(entry['model_used'])}</b></span>"]
    if entry["escalated"]:
        chips.append(f"<span class='uh-chip warn'>재검토 · {html.escape(', '.join(entry['escalation_reasons']))}</span>")
    tools = []
    for line in entry["tool_lines"]:
        text = html.escape(line.replace("`", ""))
        tools.append(f"<div class='uh-tool'>{text}</div>")
    if not tools:
        chips.append("<span class='uh-chip'>Tool 호출 없음</span>")
    sections.append(
        f"<section><h4>실행 정보</h4><div class='uh-chips'>{''.join(chips)}</div>{''.join(tools)}</section>"
    )
    return f"<div class='uh-detail'>{''.join(sections)}</div>"


def render_case_file(snap: StateSnapshot) -> None:
    """사이드바: 대화에서 확인된 사실과 대응 진행 상황."""
    st.markdown("<div class='uh-section'>확인된 사실</div>", unsafe_allow_html=True)
    rows = []
    for label, value in (("링크 클릭", snap.link_clicked), ("앱 설치", snap.app_installed), ("송금", snap.money_sent)):
        if value is True:
            tag = "<span class='uh-tag bad'>있음</span>"
        elif value is False:
            tag = "<span class='uh-tag ok'>없음</span>"
        else:
            tag = "<span class='uh-tag na'>미확인</span>"
        rows.append(f"<div class='uh-fact'><span>{label}</span>{tag}</div>")
    if snap.info_exposed:
        exposed = html.escape(", ".join(snap.info_exposed))
        rows.append(f"<div class='uh-fact'><span>노출 정보</span><span class='uh-tag bad'>{exposed}</span></div>")
    if snap.sent_amount:
        rows.append(f"<div class='uh-fact'><span>송금액</span><b>{snap.sent_amount:,}원</b></div>")
    if snap.elapsed_minutes is not None:
        rows.append(f"<div class='uh-fact'><span>송금 후 경과</span><b>{snap.elapsed_minutes}분</b></div>")
    st.markdown("".join(rows), unsafe_allow_html=True)

    if snap.checklist:
        st.markdown("<div class='uh-section'>대응 진행</div>", unsafe_allow_html=True)
        done = sum(1 for v in snap.checklist.values() if v)
        st.progress(done / len(snap.checklist), text=f"{done}/{len(snap.checklist)} 완료")
        items = [
            f"<div class='uh-check {'done' if ok else ''}'><span>{'✅' if ok else '⬜'}</span><span>{html.escape(key)}</span></div>"
            for key, ok in snap.checklist.items()
        ]
        st.markdown("".join(items), unsafe_allow_html=True)


snapshot = StateSnapshot.model_validate(st.session_state.snapshot)

with st.sidebar:
    st.markdown(
        f"<div class='uh-brand'><div class='uh-brand-face'>{ASSISTANT_AVATAR}</div>"
        "<div><div class='uh-brand-name'>Un Hook</div>"
        "<div class='uh-brand-sub'>금융사기 피해 상태 확인·대응 안내</div></div></div>",
        unsafe_allow_html=True,
    )
    if st.button("새 상담 시작", width="stretch"):
        reset_conversation()
        st.rerun()
    render_case_file(snapshot)

    # 신고 접수(HITL): 사용자가 버튼을 눌러야만 report_approved=True로 Tool이 모델에 제공된다.
    has_answer = any(e["role"] == "assistant" for e in st.session_state.history)
    if has_answer and not st.session_state.get("pending_turn") and not st.session_state.report_done:
        st.markdown("<div class='uh-section'>신고</div>", unsafe_allow_html=True)
        with st.container(border=True):
            st.markdown("지금까지 확인된 내용으로 신고를 접수할 수 있어요. 버튼을 누르기 전에는 접수되지 않습니다.")
            if st.button("신고 접수하기", type="primary", width="stretch"):
                queue_turn(REPORT_APPROVAL_STATEMENT, "", report_approved=True)
    elif st.session_state.report_done:
        st.success("신고가 접수되었습니다. 접수 결과는 답변의 '왜 이렇게 판단했나요?'에서 확인하세요.")
    st.caption(f"{st.session_state.turn_count}턴 · `{st.session_state.thread_id}`")

if not os.getenv("OPENAI_API_KEY"):
    st.error("OPENAI_API_KEY 환경변수가 없습니다. 설정 후 다시 실행하세요.")
    st.stop()

quick_pick: str | None = None
if st.session_state.history:
    render_case_strip(snapshot)
else:
    st.markdown(
        f"<div class='uh-hero'><div class='uh-hero-face'>{ASSISTANT_AVATAR}</div><h1>무슨 일이 있었나요?</h1>"
        "<p>의심스러운 문자나 전화를 받았다면 지금 상황을 편하게 말해 주세요. "
        "얼마나 위험한지, 지금 당장 무엇을 해야 하는지 알려드립니다. "
        "받은 문자가 있으면 아래 <b>📎 받은 문자 붙여넣기</b>로 원문도 함께 보내 주세요.</p>"
        "<div class='uh-hero-label'>이런 상황이면 눌러서 바로 시작하세요</div></div>",
        unsafe_allow_html=True,
    )
    for col, text in zip(st.columns(len(QUICK_STARTS)), QUICK_STARTS):
        if col.button(text, width="stretch"):
            quick_pick = text

for entry in st.session_state.history:
    if entry["role"] == "user":
        with st.chat_message("user"):
            st.markdown(html.escape(entry["statement"]))
            if entry["quoted"]:
                st.markdown(
                    f"<div class='uh-quote'><div class='uh-quote-label'>받은 문자</div>{html.escape(entry['quoted'])}</div>",
                    unsafe_allow_html=True,
                )
    elif entry["role"] == "assistant":
        with st.chat_message("assistant", avatar=ASSISTANT_AVATAR):
            render_assessment(entry)
    else:
        with st.chat_message("assistant", avatar=ASSISTANT_AVATAR):
            st.error(f"답변을 만들지 못했습니다. {entry['text']}")

pending = st.session_state.get("pending_turn")

# 하단 고정 입력 줄: 문자 원문 첨부(팝오버) + 채팅 입력.
# 사용자 진술과 외부 원문을 분리해 넘기기 위해 원문은 별도 칸에 받는다 (AGENTS.md 입력 보안 연결).
# 분석 중에는 입력을 잠그므로, 모델 호출(아래)보다 먼저 그린다.
with st.bottom:
    attach_col, hint_col = st.columns([1.2, 2.8], vertical_alignment="center")
    has_draft = bool(st.session_state.get("quoted_draft", "").strip())
    with attach_col.popover("📎 문자 원문 첨부됨 · 수정" if has_draft else "📎 받은 문자 붙여넣기", width="stretch"):
        st.text_area(
            "받은 문자·통화 내용을 그대로 붙여넣어 주세요", key="quoted_draft", height=150,
            placeholder="예: [택배] 주소 불일치로 반송. 아래 링크에서 확인하세요 http://...",
        )
        st.caption("전화번호·계좌번호 같은 개인정보는 보내기 전에 자동으로 가려집니다.")
    if has_draft:
        hint_col.markdown("<span class='uh-quiet'>첨부한 원문은 다음 메시지와 함께 전송됩니다.</span>", unsafe_allow_html=True)
    statement = st.chat_input(
        "분석이 끝나면 이어서 말할 수 있어요" if pending else "지금 상황을 말해 주세요 (예: 링크는 눌렀는데 송금은 안 했어요)",
        disabled=bool(pending),
    )

# 직전 실행에서 올린 사용자 메시지에 대한 답변을 만든다. 그동안 어시스턴트 자리에 진행 상태를 보여준다.
if pending:
    with st.chat_message("assistant", avatar=ASSISTANT_AVATAR):
        st.markdown(THINKING_HTML, unsafe_allow_html=True)
        complete_turn(pending["statement"], pending["quoted"], report_approved=pending["report_approved"])
    st.session_state.pending_turn = None
    st.rerun()

statement = (statement or quick_pick or "").strip()
if statement:
    queue_turn(statement, st.session_state.get("quoted_draft", "").strip())
