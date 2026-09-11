"""app.py — Un Hook 프론트엔드 (레인 E)

INTERFACES.md B-7 계약을 따른다.
- render_* 함수는 전부 순수 함수다. Gradio 없이 단독 호출할 수 있고, 키가 없거나 None이어도 죽지 않는다.
- 모듈 최상단에서는 contracts만 import한다. agent는 _get_agent() 안에서 지연 import한다.
- agent 호출은 2번 규약만 쓴다: agent.run_turn(text, thread_id) / agent.resume_turn(approved, thread_id)

agent.py를 불러오지 못하면 데모 모드로 동작한다. 화면 확인용 예시 응답이며, 상단에 데모 모드라고 표시된다.
"""
from __future__ import annotations

import html
import inspect
import re
import uuid

# ---------------------------------------------------------------------------
# contracts (동결 모듈). 아직 파일이 없어도 화면 확인은 되도록 폴백 값을 둔다.
# ---------------------------------------------------------------------------
try:
    import contracts as _contracts

    RISK_ORDER = _contracts.RISK_ORDER
    STAGE_ORDER = _contracts.STAGE_ORDER
    STAGE_TO_RISK = _contracts.STAGE_TO_RISK
    GOLDEN_TIME_MINUTES = _contracts.GOLDEN_TIME_MINUTES
    DEFAULT_CHECKLIST_ITEMS = list(_contracts.DEFAULT_CHECKLIST_ITEMS)
    OFFICIAL_CONTACTS = dict(_contracts.OFFICIAL_CONTACTS)
    initial_state = _contracts.initial_state
    CONTRACTS_LOADED = True
except Exception:  # contracts.py가 없을 때만
    RISK_ORDER = {"insufficient_info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
    STAGE_ORDER = {"none": 0, "link_clicked": 1, "info_exposed": 2, "app_installed": 3, "money_sent": 4}
    STAGE_TO_RISK = {"none": "low", "link_clicked": "medium", "info_exposed": "high",
                     "app_installed": "high", "money_sent": "critical"}
    GOLDEN_TIME_MINUTES = 30
    DEFAULT_CHECKLIST_ITEMS = ["지급정지 요청", "112 신고", "개인정보노출자 사실 등록",
                               "휴대폰 악성앱 삭제", "계좌 비밀번호 변경"]
    OFFICIAL_CONTACTS = {"police": "112", "fss": "1332", "kisa": "118", "id_exposure": "pd.fss.or.kr"}

    def initial_state() -> dict:
        return {
            "link_clicked": None, "info_exposed": [], "app_installed": None, "money_sent": None,
            "sent_amount": None, "elapsed_minutes": None, "damage_stage": "none",
            "risk_level": "insufficient_info", "golden_time": False, "channel": None,
            "tool_results": {}, "question_count": 0, "checklist": {}, "incident_report": None,
        }

    CONTRACTS_LOADED = False


# ---------------------------------------------------------------------------
# 표시용 사전
# ---------------------------------------------------------------------------
RISK_LABELS = {
    "critical": ("긴급", "지금 바로 조치가 필요해요"),
    "high": ("높음", "사기일 가능성이 높아요"),
    "medium": ("주의", "의심스러운 신호가 있어요"),
    "low": ("낮음", "뚜렷한 위험 신호는 아직 없어요"),
    "insufficient_info": ("판단 보류", "판단하려면 정보가 더 필요해요"),
}
SCAM_LABELS = {
    "smishing": "스미싱",
    "voice_phishing": "보이스피싱",
    "messenger_phishing": "메신저피싱",
    "loan_scam": "대출 빙자",
    "gov_impersonation": "기관 사칭",
    "investment_scam": "투자 사기",
    "unknown": "유형 확인 중",
}
STAGE_LABELS = [
    ("link_clicked", "링크 클릭"),
    ("info_exposed", "개인정보 입력"),
    ("app_installed", "앱 설치"),
    ("money_sent", "송금"),
]
TOOL_LABELS = {
    "check_url_risk": "링크 검사",
    "verify_caller_number": "발신번호 확인",
    "get_scam_playbook": "대응 절차 조회",
    "report_to_authority": "신고 접수",
    "lookup_history": "지난 상담 이력 조회",
}
CONTACT_LABELS = {
    "police": "경찰",
    "fss": "금융감독원",
    "kisa": "한국인터넷진흥원",
    "id_exposure": "개인정보노출자 등록",
}

_esc = html.escape


def _risk_key(value) -> str:
    return value if isinstance(value, str) and value in RISK_LABELS else "insufficient_info"


def _max_risk(*levels) -> str:
    valid = [lv for lv in levels if isinstance(lv, str) and lv in RISK_ORDER]
    if not valid:
        return "insufficient_info"
    return max(valid, key=lambda lv: RISK_ORDER[lv])


def _contact_text(value) -> str:
    """OFFICIAL_CONTACTS 값이 str이든 dict든 표시용 문자열로 바꾼다."""
    if value is None:
        return ""
    if isinstance(value, dict):
        for key in ("phone", "number", "tel", "url", "site", "value"):
            if value.get(key):
                return str(value[key])
        return " ".join(str(v) for v in value.values() if v)
    return str(value)


def _contact_html(text) -> str:
    """전화번호처럼 생긴 연락처는 tel 링크로 만든다. 사이트 주소는 텍스트로만 둔다."""
    if not text:
        return ""
    raw = str(text)
    digits = re.sub(r"[^0-9]", "", raw)
    if digits and re.fullmatch(r"[0-9\-\s()]+", raw.strip()):
        return f'<a class="uh-tel" href="tel:{digits}">{_esc(raw)}</a>'
    return f'<span class="uh-contact">{_esc(raw)}</span>'


def _format_amount(amount) -> str:
    try:
        n = int(amount)
    except (TypeError, ValueError):
        return ""
    if n >= 10000 and n % 10000 == 0:
        return f"{n // 10000:,}만원"
    return f"{n:,}원"


# ---------------------------------------------------------------------------
# B-7 계약: render_* 순수 함수
# ---------------------------------------------------------------------------
def render_badge(risk_level) -> str:
    key = _risk_key(risk_level)
    label, _ = RISK_LABELS[key]
    return f'<span class="uh-badge uh-badge--{key}">{label}</span>'


def render_damage_flags(state) -> str:
    state = state or {}
    rows = []
    for key, label in STAGE_LABELS:
        value = state.get(key)
        detail = ""
        if key == "info_exposed":
            items = value if isinstance(value, list) else []
            if items:
                tone, word = "yes", "예"
                detail = ", ".join(_esc(str(i)) for i in items)
            else:
                tone, word = "unknown", "확인된 항목 없음"
        elif value is True:
            tone, word = "yes", "예"
            if key == "money_sent":
                bits = []
                if state.get("sent_amount"):
                    bits.append(_format_amount(state.get("sent_amount")))
                if state.get("elapsed_minutes") is not None:
                    bits.append(f"{_esc(str(state.get('elapsed_minutes')))}분 경과")
                detail = " / ".join(bits)
        elif value is False:
            tone, word = "no", "아니오"
        else:
            tone, word = "unknown", "미확인"
        passed = "is-reached" if tone == "yes" else ""
        detail_html = f'<span class="uh-flag__detail">{detail}</span>' if detail else ""
        rows.append(
            f'<li class="uh-flag {passed}">'
            f'<span class="uh-flag__name">{label}</span>'
            f'<span class="uh-flag__value uh-flag__value--{tone}">{word}</span>{detail_html}</li>'
        )
    return f'<ol class="uh-track" aria-label="피해 진행 상태">{"".join(rows)}</ol>'


def render_evidence_block(structured) -> str:
    structured = structured or {}
    evidence = [e for e in (structured.get("evidence") or []) if e]
    unverified = [u for u in (structured.get("unverified") or []) if u]
    if not evidence and not unverified:
        return ""
    parts = ['<section class="uh-sec">']
    if evidence:
        items = "".join(f"<li>{_esc(str(e))}</li>" for e in evidence)
        parts.append(f'<h3>이렇게 판단했어요</h3><ul class="uh-list">{items}</ul>')
    if unverified:
        items = "".join(f"<li>{_esc(str(u))}</li>" for u in unverified)
        parts.append(f'<h3 class="uh-sub">확인하지 못한 것</h3><ul class="uh-list uh-list--muted">{items}</ul>')
    parts.append("</section>")
    return "".join(parts)


def render_next_actions(structured) -> str:
    structured = structured or {}
    raw = structured.get("immediate_actions") or structured.get("next_actions") or []  # 설계서 최신 이름 우선
    steps = [s for s in raw if isinstance(s, dict) and s.get("action")]
    if not steps:
        return ""
    steps.sort(key=lambda s: s.get("priority") if isinstance(s.get("priority"), int) else 99)
    items = []
    for s in steps[:5]:
        contact = _contact_html(s.get("contact"))
        contact_html = f'<span class="uh-action__contact">{contact}</span>' if contact else ""
        items.append(f'<li class="uh-action"><span class="uh-action__text">{_esc(str(s["action"]))}</span>{contact_html}</li>')
    return f'<section class="uh-sec uh-sec--actions"><h3>지금 할 일</h3><ol class="uh-actions">{"".join(items)}</ol></section>'


def render_tools_called(tools_called) -> str:
    tools = [t for t in (tools_called or []) if t]
    if not tools:
        return ""
    chips = "".join(
        f'<li><span>{_esc(TOOL_LABELS.get(t, t))}</span><code>{_esc(t)}</code></li>' for t in tools
    )
    return (
        '<details class="uh-tools"><summary>이번 답변에서 확인한 것</summary>'
        f'<ol class="uh-toollist">{chips}</ol></details>'
    )


def render_checklist(state) -> str:
    checklist = (state or {}).get("checklist") or {}
    if not isinstance(checklist, dict) or not checklist:
        return ""
    done = sum(1 for v in checklist.values() if v)
    items = []
    for name, ok in checklist.items():
        cls = "is-done" if ok else ""
        mark = "완료" if ok else "남음"
        items.append(f'<li class="uh-check {cls}"><span class="uh-check__box" aria-label="{mark}"></span>{_esc(str(name))}</li>')
    return (
        '<section class="uh-sec"><h3>피해 수습 체크리스트 '
        f'<span class="uh-count">{done}/{len(checklist)}</span></h3>'
        f'<ul class="uh-checklist">{"".join(items)}</ul></section>'
    )


def render_golden_time_banner(state) -> str:
    state = state or {}
    elapsed = state.get("elapsed_minutes")
    checklist = state.get("checklist") if isinstance(state.get("checklist"), dict) else {}
    if checklist.get("지급정지 요청"):
        return ""
    if state.get("golden_time"):
        when = f"송금한 지 {_esc(str(elapsed))}분이 지났어요. " if elapsed is not None else ""
        return (
            '<div class="uh-banner uh-banner--now" role="alert">'
            f"<strong>{when}지급정지 요청을 가장 먼저 하세요.</strong>"
            "<span>송금한 은행 콜센터나 112에 전화하면 돼요. 휴대폰에 앱을 설치했다면 다른 사람의 휴대폰으로 거세요.</span></div>"
        )
    if state.get("money_sent"):
        return (
            '<div class="uh-banner" role="alert">'
            "<strong>시간이 지났어도 지급정지는 지금 요청하세요.</strong>"
            "<span>송금한 은행 콜센터나 112에 바로 전화하세요.</span></div>"
        )
    return ""


def _render_risk_band(risk_level, structured) -> str:
    key = _risk_key(risk_level)
    label, message = RISK_LABELS[key]
    structured = structured or {}
    scam = structured.get("scam_type")
    scam_html = f'<span class="uh-risk__type">{_esc(SCAM_LABELS.get(scam, str(scam)))}</span>' if scam else ""
    conf_html = ""
    conf = structured.get("confidence")
    if isinstance(conf, (int, float)) and 0 <= conf <= 1:
        pct = round(conf * 100)
        conf_html = (
            f'<span class="uh-risk__conf">판단 확신도 {pct}%'
            f'<span class="uh-meter" aria-hidden="true"><span style="width:{pct}%"></span></span></span>'
        )
    meta = f'<div class="uh-risk__meta">{scam_html}{conf_html}</div>' if (scam_html or conf_html) else ""
    return (
        f'<div class="uh-risk uh-risk--{key}" role="status" aria-live="polite">'
        f'<span class="uh-risk__eyebrow">위험도</span>'
        f'<span class="uh-risk__level">{label}</span>'
        f'<span class="uh-risk__msg">{message}</span>{meta}</div>'
    )


def render_state_html(state, structured=None, tools_called=None) -> str:
    """상태 패널 전체. B-7 계약상 state만 넘겨도 동작하고, structured/tools_called는 선택이다."""
    state = state or {}
    structured = structured or {}
    started = bool(structured) or any(
        state.get(k) is not None for k in ("link_clicked", "app_installed", "money_sent")
    ) or bool(state.get("info_exposed")) or bool(state.get("checklist"))
    if not started:
        return (
            '<div class="uh-panel uh-panel--empty">'
            + _render_risk_band("insufficient_info", {})
            + '<p class="uh-empty">받은 문자나 전화 내용을 알려주시면, 이곳에 위험도와 지금 해야 할 일이 정리돼요.</p>'
            "</div>"
        )
    risk = _max_risk(structured.get("risk_level"), state.get("risk_level"))
    notice = ""
    if structured.get("injection_detected"):
        notice = '<p class="uh-notice">붙여넣은 내용 속에 AI를 향한 지시문이 있었어요. 따르지 않고 위험 신호로 반영했어요.</p>'
    return (
        '<div class="uh-panel">'
        + _render_risk_band(risk, structured)
        + render_golden_time_banner(state)
        + notice
        + render_next_actions(structured)
        + '<section class="uh-sec"><h3>피해 진행 상태</h3>' + render_damage_flags(state) + "</section>"
        + render_evidence_block(structured)
        + render_checklist(state)
        + render_tools_called(tools_called)
        + "</div>"
    )


def render_pending(pending) -> str:
    """HITL 승인 대기 payload 표시. payload 형태가 확정 전이라 여러 모양을 받아준다."""
    if not pending:
        return ""
    requests = []
    if isinstance(pending, dict) and isinstance(pending.get("action_requests"), list):
        requests = pending["action_requests"]
    elif isinstance(pending, list):
        requests = pending
    elif isinstance(pending, dict):
        requests = [pending]
    blocks = []
    for req in requests:
        if not isinstance(req, dict):
            blocks.append(f"<p>{_esc(str(req))}</p>")
            continue
        name = req.get("tool") or req.get("name") or req.get("action") or "report_to_authority"
        desc = req.get("description") or ""
        args = req.get("args") or req.get("arguments") or {}
        rows = ""
        if isinstance(args, dict):
            rows = "".join(
                f"<div><dt>{_esc(str(k))}</dt><dd>{_esc(str(v))}</dd></div>" for k, v in args.items()
            )
        blocks.append(
            f'<h3>{_esc(TOOL_LABELS.get(name, name))} 전에 확인해 주세요</h3>'
            + (f'<p>{_esc(str(desc))}</p>' if desc else "")
            + (f'<dl class="uh-kv">{rows}</dl>' if rows else "")
        )
    return f'<div class="uh-approve">{"".join(blocks)}</div>'


def render_url_result(result, url="") -> str:
    if not result:
        return ""
    score = result.get("risk_score")
    try:
        score = max(0, min(100, int(score)))
    except (TypeError, ValueError):
        score = None
    if result.get("blacklisted"):
        verdict, tone = "신고된 위험 링크예요", "critical"
    elif score is not None and score >= 70:
        verdict, tone = "위험 신호가 많은 링크예요", "high"
    elif score is not None and score >= 40:
        verdict, tone = "의심스러운 링크예요", "medium"
    else:
        verdict, tone = "알려진 위험 정보는 없어요", "low"
    signals = "".join(f"<li>{_esc(str(s))}</li>" for s in (result.get("signals") or []))
    score_html = ""
    if score is not None:
        score_html = (
            f'<div class="uh-score"><span>위험 점수 <b>{score}</b>/100</span>'
            f'<span class="uh-meter uh-meter--{tone}" aria-hidden="true"><span style="width:{score}%"></span></span></div>'
        )
    safe = _esc(result.get("defanged") or _defang_local(url))
    caution = "" if tone != "low" else '<p class="uh-note">신고 이력이 없다고 안전한 링크라는 뜻은 아니에요. 문자로 받은 링크는 누르지 마세요.</p>'
    demo = '<p class="uh-note">데모 검사 결과예요.</p>' if result.get("demo") else ""
    return (
        f'<div class="uh-url uh-url--{tone}">'
        f'<p class="uh-url__verdict">{verdict}</p>'
        f'<p class="uh-url__addr">{safe}</p>{score_html}'
        + (f'<ul class="uh-list">{signals}</ul>' if signals else "")
        + caution + demo + "</div>"
    )


# ---------------------------------------------------------------------------
# agent 연결 (지연 import) + 데모 모드
# ---------------------------------------------------------------------------
_AGENT = None
_AGENT_MODE = None  # "live" | "demo"
_AGENT_NOTE = ""


def _get_agent():
    global _AGENT, _AGENT_MODE, _AGENT_NOTE
    if _AGENT is not None:
        return _AGENT
    try:
        import agent as agent_module  # noqa: WPS433 (지연 import가 계약)

        if not (hasattr(agent_module, "run_turn") and hasattr(agent_module, "resume_turn")):
            raise ImportError("agent.py에 run_turn/resume_turn이 없음")
        _AGENT, _AGENT_MODE = agent_module, "live"
    except Exception as exc:  # agent.py가 없거나 import 중 실패
        _AGENT, _AGENT_MODE = _DemoAgent(), "demo"
        _AGENT_NOTE = f"{type(exc).__name__}: {exc}"
    return _AGENT


_RESULT_DEFAULTS = {"text": "", "structured": None, "tools_called": [], "state": {},
                    "interrupted": False, "pending": None, "error": None}


def _normalize(result) -> dict:
    out = dict(_RESULT_DEFAULTS)
    if isinstance(result, dict):
        out.update({k: result.get(k, v) for k, v in _RESULT_DEFAULTS.items()})
    else:
        out["error"] = "agent 반환값이 dict가 아니에요"
    if out["structured"] is not None and not isinstance(out["structured"], dict):
        dump = getattr(out["structured"], "model_dump", None)
        out["structured"] = dump() if callable(dump) else None
    out["tools_called"] = list(out["tools_called"] or [])
    out["state"] = out["state"] or {}
    return out


def call_run_turn(text: str, thread_id: str) -> dict:
    agent = _get_agent()
    try:
        return _normalize(agent.run_turn(text, thread_id))
    except Exception as exc:  # run_turn은 예외를 안 던지는 계약이지만 방어
        return _normalize({"error": f"{type(exc).__name__}: {exc}"})


def call_resume_turn(approved: bool, thread_id: str) -> dict:
    agent = _get_agent()
    try:
        return _normalize(agent.resume_turn(approved, thread_id))
    except Exception as exc:
        return _normalize({"error": f"{type(exc).__name__}: {exc}"})


def _defang_local(url: str) -> str:
    if not url:
        return ""
    out = re.sub(r"^http", "hxxp", str(url).strip(), flags=re.IGNORECASE)
    return out.replace(".", "[.]")


_URL_IN_TEXT = re.compile(r"(?:https?://|www\.)[^\s)\]]+|\b[a-z0-9-]+\.(?:top|xyz|icu|click|shop|live|cfd|sbs|ly|kr|com|net)/[^\s)\]]*", re.IGNORECASE)


def defang_text(text: str) -> str:
    """대화창에 보여줄 때만 링크를 누를 수 없게 바꾼다. 에이전트에는 원문을 보낸다."""
    return _URL_IN_TEXT.sub(lambda m: _defang_local(m.group(0)), text or "")


def check_url(url: str) -> dict:
    """URL 검사창 전용. LLM을 거치지 않고 tools_lookup을 직접 부른다."""
    url = (url or "").strip()
    if not url:
        return {}
    try:
        import tools_lookup  # 지연 import

        result = tools_lookup.check_url_risk.invoke({"url": url})
        result = dict(result) if isinstance(result, dict) else {}
        defang = getattr(tools_lookup, "defang", None)
        result["defanged"] = defang(url) if callable(defang) else _defang_local(url)
        return result
    except Exception:
        return _demo_check_url(url)


def _demo_check_url(url: str) -> dict:
    signals, score = [], 10
    host = re.sub(r"^[a-z]+://", "", url.lower()).split("/")[0]
    if re.search(r"\.(top|xyz|icu|click|shop|live|cfd|sbs)$", host):
        signals.append("스미싱에 자주 쓰이는 도메인 끝자리(.top 등)")
        score += 35
    if re.search(r"(bit\.ly|han\.gl|me2\.do|url\.kr|vo\.la)", host):
        signals.append("실제 주소를 숨기는 단축 URL")
        score += 25
    if re.search(r"(cj|kb|nh|shinhan|kakao|naver|coupang|post|epost)[-.]", host) or re.search(r"-(cj|kb|nh|bank)", host):
        signals.append("택배사·은행 이름을 흉내 낸 주소")
        score += 25
    if url.lower().endswith(".apk"):
        signals.append("앱 설치 파일(.apk)로 연결됨")
        score += 30
    if re.search(r"/[a-z0-9]{1,3}$", url.lower()):
        signals.append("짧은 경로로 목적지를 숨긴 형태")
        score += 5
    return {"blacklisted": False, "risk_score": min(score, 100), "signals": signals,
            "defanged": _defang_local(url), "demo": True}


def _contact(key: str) -> str:
    return _contact_text(OFFICIAL_CONTACTS.get(key))


class _DemoAgent:
    """agent.py 없이 화면을 확인하기 위한 예시 응답. 실제 판단 로직이 아니다."""

    def __init__(self):
        self.cases: dict[str, dict] = {}

    def _case(self, thread_id):
        if thread_id not in self.cases:
            self.cases[thread_id] = {"state": initial_state(), "structured": None, "pending": None}
        return self.cases[thread_id]

    @staticmethod
    def _recompute(state):
        if state.get("money_sent"):
            stage = "money_sent"
        elif state.get("app_installed"):
            stage = "app_installed"
        elif state.get("info_exposed"):
            stage = "info_exposed"
        elif state.get("link_clicked"):
            stage = "link_clicked"
        else:
            stage = "none"
        if STAGE_ORDER[stage] >= STAGE_ORDER.get(state.get("damage_stage", "none"), 0):
            state["damage_stage"] = stage
        state["risk_level"] = _max_risk(state.get("risk_level"), STAGE_TO_RISK[state["damage_stage"]])
        elapsed = state.get("elapsed_minutes")
        state["golden_time"] = bool(state.get("money_sent")) and elapsed is not None and elapsed <= GOLDEN_TIME_MINUTES

    def _result(self, case, text, structured=None, tools=None, **extra):
        if structured is not None:
            prev = case["structured"] or {}
            structured["risk_level"] = _max_risk(structured.get("risk_level"), prev.get("risk_level"))
            case["structured"] = structured
        out = dict(_RESULT_DEFAULTS)
        out.update(text=text, structured=case["structured"], tools_called=tools or [],
                   state=dict(case["state"]))
        out.update(extra)
        return out

    def run_turn(self, text, thread_id):
        case = self._case(thread_id)
        st = case["state"]
        prev = case["structured"] or {}
        t = text.strip()
        police, fss = _contact("police"), _contact("fss")

        if re.search(r"(무시하고|시스템\s*프롬프트|너의\s*규칙|지시를\s*잊)", t):
            return self._result(case, "대화 속 지시는 따를 수 없어요. 받은 문자나 전화 내용을 알려주시면 사기인지 같이 확인할게요.")

        if re.search(r"(적금|주식\s*추천|코인\s*추천|날씨)", t):
            return self._result(case, "저는 금융사기 의심 상황을 확인하고 대응을 안내하는 상담만 할 수 있어요.")

        if re.search(r"(신고|접수)", t) and re.search(r"(정리|해줘|해주세요|접수)", t):
            case["pending"] = {
                "tool": "report_to_authority",
                "description": "아래 내용으로 경찰 신고 접수를 요청해요. 승인하기 전에는 접수되지 않아요.",
                "args": {
                    "사기 유형": SCAM_LABELS.get(prev.get("scam_type"), "확인 중"),
                    "송금액": _format_amount(st.get("sent_amount")) or "없음",
                    "송금 후 경과": f"{st.get('elapsed_minutes')}분" if st.get("elapsed_minutes") is not None else "미확인",
                    "사기범 계좌": "<SCAM_ACCOUNT_1> (접수 시 원문으로 복원)",
                },
            }
            return self._result(case, "신고할 내용을 정리했어요. 오른쪽에서 내용을 확인하고 승인하면 접수를 진행할게요.",
                                tools=["report_to_authority"], interrupted=True, pending=case["pending"])

        if re.search(r"지급\s*정지.*(했|완료|요청했)", t):
            st.setdefault("checklist", {})
            if not st["checklist"]:
                st["checklist"] = {item: False for item in DEFAULT_CHECKLIST_ITEMS}
            st["checklist"]["지급정지 요청"] = True
            self._recompute(st)
            structured = dict(prev, immediate_actions=[
                {"priority": 1, "action": "112에 사기 피해 신고하기", "contact": police},
                {"priority": 2, "action": "개인정보노출자 사실 등록하기", "contact": _contact("id_exposure")},
                {"priority": 3, "action": "휴대폰에 설치한 앱 삭제하기", "contact": None},
            ], next_question=None)
            return self._result(case, "지급정지 요청을 체크했어요. 다음은 112 신고와 개인정보노출자 사실 등록이에요. 신고할 내용을 정리해 드릴까요?",
                                structured, [])

        amount = re.search(r"(\d[\d,]*)\s*만\s*원", t)
        if re.search(r"(보냈|송금|이체)", t) and not re.search(r"(안\s*보냈|보내지\s*않)", t):
            st["money_sent"] = True
            if amount:
                st["sent_amount"] = int(amount.group(1).replace(",", "")) * 10000
            self._recompute(st)
            scam = "loan_scam" if "대출" in t else (prev.get("scam_type") or "voice_phishing")
            structured = {
                "scam_type": scam, "risk_level": "critical", "damage_stage": st["damage_stage"],
                "confidence": 0.9,
                "evidence": ["사기범에게 돈을 보낸 사실이 확인됨"] + (["대출을 갈아타 준다며 먼저 돈을 요구함"] if scam == "loan_scam" else []),
                "unverified": ["송금한 지 얼마나 지났는지"],
                "immediate_actions": [
                    {"priority": 1, "action": "송금한 은행 콜센터에 전화해 지급정지 요청하기", "contact": None},
                    {"priority": 2, "action": "112에 사기 피해 신고하기", "contact": police},
                ],
                "next_question": "송금한 지 얼마나 지났나요?", "injection_detected": False,
            }
            return self._result(case, "지금 가장 먼저 송금한 은행 콜센터나 112에 전화해 지급정지를 요청하세요.\n\n송금한 지 얼마나 지났나요?",
                                structured, ["get_scam_playbook"])

        minutes = re.search(r"(\d+)\s*분", t)
        if minutes and st.get("money_sent"):
            st["elapsed_minutes"] = int(minutes.group(1))
            if not st.get("checklist"):
                st["checklist"] = {item: False for item in DEFAULT_CHECKLIST_ITEMS}
            self._recompute(st)
            structured = dict(prev, unverified=[], next_question=None, immediate_actions=[
                {"priority": 1, "action": "송금한 은행 콜센터에 지급정지 요청하기 (다른 사람 휴대폰 사용)", "contact": None},
                {"priority": 2, "action": "112에 사기 피해 신고하기", "contact": police},
                {"priority": 3, "action": "개인정보노출자 사실 등록하기", "contact": _contact("id_exposure")},
            ])
            text_out = ("아직 지급정지를 요청하기 좋은 시간이에요. 전화를 끊지 말고 바로 은행에 연락하세요."
                        if st["golden_time"] else "시간이 지났어도 지급정지는 지금 요청하세요.")
            return self._result(case, text_out, structured, ["get_scam_playbook"])

        if re.search(r"(앱|어플).*(깔|설치)", t):
            st["link_clicked"] = True if re.search(r"(링크|눌)", t) or st.get("link_clicked") is None else st["link_clicked"]
            st["app_installed"] = True
            self._recompute(st)
            structured = {
                "scam_type": prev.get("scam_type") or "smishing", "risk_level": "critical",
                "damage_stage": st["damage_stage"], "confidence": 0.85,
                "evidence": ["링크를 통해 출처를 알 수 없는 앱을 설치함", "원격제어 앱이면 휴대폰 화면과 통화가 사기범에게 넘어갈 수 있음"],
                "unverified": ["계좌에서 돈이 빠져나갔는지"],
                "immediate_actions": [
                    {"priority": 1, "action": "이 휴대폰으로 금융 앱을 열거나 전화하지 않기", "contact": None},
                    {"priority": 2, "action": "다른 사람 휴대폰으로 경찰에 상담하기", "contact": police},
                    {"priority": 3, "action": "다른 사람 휴대폰으로 금융감독원에 상담하기", "contact": fss},
                ],
                "next_question": "계좌에서 돈이 빠져나갔나요?", "injection_detected": False,
            }
            return self._result(case, "설치한 앱이 원격제어 앱일 수 있어요. 이 휴대폰으로는 은행이나 기관에 전화하지 마세요. 전화가 사기범에게 연결될 수 있어요.\n\n계좌에서 돈이 빠져나갔나요?",
                                structured, ["get_scam_playbook"])

        if re.search(r"(검사|검찰|수사관|지검|금융감독원\s*직원)", t) or (prev.get("scam_type") == "gov_impersonation" and re.search(r"(진짜|틀린|맞다니까)", t)):
            isolated = bool(re.search(r"(비밀|말하면\s*안|아무한테도)", t))
            evidence = ["수사기관은 전화로 이체나 현금 전달을 요구하지 않음"]
            if isolated:
                evidence.insert(0, "가족에게 말하지 말라는 고립 지시가 있음")
            if "사건번호" in t:
                evidence.append("사건번호를 알려준 것은 진짜라는 근거가 되지 않음")
            if prev.get("scam_type") == "gov_impersonation":
                evidence = list(dict.fromkeys((prev.get("evidence") or []) + evidence))[:5]
            structured = {
                "scam_type": "gov_impersonation", "risk_level": "critical", "damage_stage": st["damage_stage"],
                "confidence": 0.9, "evidence": evidence, "unverified": [],
                "immediate_actions": [
                    {"priority": 1, "action": "지금 통화를 끊기", "contact": None},
                    {"priority": 2, "action": "직접 찾은 공식 번호로 사실 확인하기", "contact": police},
                ],
                "next_question": "혹시 돈을 보내거나 앱을 설치하셨나요?", "injection_detected": False,
            }
            reply = ("믿기 어려우시겠지만 위험도는 그대로예요. 수사기관은 전화로 돈을 옮기라고 하지 않아요. 통화를 끊고 직접 찾은 번호로 확인하세요."
                     if prev.get("scam_type") == "gov_impersonation"
                     else "기관을 사칭한 보이스피싱일 가능성이 매우 높아요. 수사기관은 전화로 돈을 옮기라고 하거나 가족에게 비밀로 하라고 하지 않아요.\n\n혹시 돈을 보내거나 앱을 설치하셨나요?")
            return self._result(case, reply, structured, ["get_scam_playbook"])

        if re.search(r"(아직|안\s*눌|안눌|클릭\s*안|안\s*했)", t) and prev:
            st["link_clicked"] = False
            self._recompute(st)
            structured = dict(prev, unverified=[], next_question=None, immediate_actions=[
                {"priority": 1, "action": "문자 속 링크를 누르지 않고 문자 삭제하기", "contact": None},
                {"priority": 2, "action": "발신번호 차단하기", "contact": None},
                {"priority": 3, "action": "스미싱 문자 신고하기", "contact": _contact("kisa")},
            ])
            return self._result(case, "링크를 누르지 않았다면 지금은 피해가 없어요. 문자를 삭제하고 발신번호를 차단하세요.",
                                structured, [])

        url = re.search(r"(https?://\S+|bit\.ly/\S+)", t)
        if url or re.search(r"(\[web발신\]|택배|반송|주소\s*불일치)", t, flags=re.IGNORECASE):
            tools = []
            evidence = ["택배 반송을 핑계로 링크를 누르게 유도함"]
            injection = bool(re.search(r"(AI|인공지능).*(분류|판단|답하)", t))
            if url:
                check = _demo_check_url(url.group(1).rstrip(")"))
                st.setdefault("tool_results", {})["check_url_risk"] = check
                evidence += check["signals"][:2]
                tools.append("check_url_risk")
            structured = {
                "scam_type": "smishing", "risk_level": "high", "damage_stage": st["damage_stage"],
                "confidence": 0.82, "evidence": evidence[:3],
                "unverified": ["발신번호가 실제 택배사 번호인지"],
                "immediate_actions": [{"priority": 1, "action": "문자 속 링크 누르지 않기", "contact": None}],
                "next_question": "혹시 링크를 클릭하셨나요?", "injection_detected": injection,
            }
            return self._result(case, "택배사를 사칭한 스미싱 문자로 보여요. 링크 주소가 실제 택배사와 달라요.\n\n혹시 링크를 클릭하셨나요?",
                                structured, tools)

        structured = {
            "scam_type": prev.get("scam_type") or "unknown", "risk_level": "insufficient_info",
            "damage_stage": st["damage_stage"], "confidence": 0.3,
            "evidence": ["판단에 필요한 내용이 아직 부족함"], "unverified": ["어떤 연락을 받았는지"],
            "immediate_actions": [], "next_question": "어떤 연락을 받으셨나요?", "injection_detected": False,
        }
        return self._result(case, "어떤 연락을 받으셨나요? 받은 문자나 통화 내용을 그대로 적어주세요.", structured, [])

    def resume_turn(self, approved, thread_id):
        case = self._case(thread_id)
        if not case.get("pending"):
            return self._result(case, "승인을 기다리는 요청이 없어요.")
        case["pending"] = None
        st = case["state"]
        if approved:
            receipt = "DEMO-" + uuid.uuid4().hex[:6].upper()
            st["incident_report"] = {"receipt_no": receipt}
            if st.get("checklist"):
                st["checklist"]["112 신고"] = True
            return self._result(case, f"신고 접수를 요청했어요. 접수번호는 {receipt}예요. (데모)", tools=["report_to_authority"])
        return self._result(case, "신고 접수를 취소했어요. 원하시면 112에 직접 신고할 수도 있어요.")


# ---------------------------------------------------------------------------
# 화면
# ---------------------------------------------------------------------------
EXAMPLES = [
    ("택배 문자를 받았어요", "[택배] 주소 불일치로 반송 예정입니다. 주소 확인: http://vv-cj.top/x 이 문자 뭐야?"),
    ("앱을 설치했어요", "링크 눌렀는데 앱을 깔라고 해서 깔았어요"),
    ("돈을 보냈어요", "대출 갈아타기 해준대서 300만원 보냈어"),
    ("검사라고 전화가 왔어요", "중앙지검 수사관이래. 사건번호도 알려줬고 비밀 수사라 가족한테 말하면 안 된대"),
]

HEAD = """
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans+KR:wght@400;500;600;700&display=swap" rel="stylesheet">
<meta name="color-scheme" content="light">
<script>
(() => {
  // 사용자 기기가 다크 모드여도 항상 밝은 화면으로 고정한다
  const strip = () => {
    for (const el of [document.documentElement, document.body]) {
      if (el && el.classList.contains("dark")) el.classList.remove("dark");
    }
  };
  const watch = (el) => new MutationObserver(strip).observe(el, { attributes: true, attributeFilter: ["class"] });
  strip();
  watch(document.documentElement);
  document.addEventListener("DOMContentLoaded", () => { strip(); watch(document.body); });
})();
</script>
"""

CSS = """
:root {
  --uh-paper: #EEF3F2;
  --uh-surface: #FFFFFF;
  --uh-ink: #132B2A;
  --uh-muted: #566B68;
  --uh-line: #D3DDDB;
  --uh-brand: #0E6770;
  --uh-brand-soft: #DDEDEE;
  --uh-r-low: #2F7A55;
  --uh-r-medium: #9A6D08;
  --uh-r-high: #C0501B;
  --uh-r-critical: #A3173A;
  --uh-r-info: #5D6E77;
  --uh-font: "IBM Plex Sans KR", "Apple SD Gothic Neo", "Malgun Gothic", "Noto Sans KR", sans-serif;
}
html, body, gradio-app, .gradio-container, .dark, .dark body {
  background: var(--uh-paper) !important; color-scheme: light !important; }
.gradio-container { font-family: var(--uh-font) !important; color: var(--uh-ink) !important;
  width: 100% !important; max-width: 1320px !important; margin: 0 auto !important; padding: 24px 28px !important; }
.gradio-container .main, .gradio-container .wrap, .gradio-container .contain { margin-left: auto; margin-right: auto; }
.uh-panel, .uh-head, .uh-url, .uh-approve { color: var(--uh-ink); }
.gradio-container .uh-bare, .gradio-container .uh-bare.block, .gradio-container .uh-bare > .html-container {
  background: transparent !important; border: 0 !important; box-shadow: none !important; padding: 0 !important; }
#uh-root { gap: 20px; }

/* 머리말 */
.uh-head { display: flex; flex-wrap: wrap; align-items: flex-end; justify-content: space-between; gap: 16px 32px;
  padding: 8px 4px 20px; border-bottom: 2px solid var(--uh-ink); }
.uh-brand { display: flex; align-items: center; gap: 14px; }
.uh-brand svg { flex: none; }
.uh-brand h1 { margin: 0; font-size: 34px; line-height: 1; font-weight: 700; letter-spacing: -0.02em; color: var(--uh-ink); }
.uh-brand p { margin: 6px 0 0; font-size: 15px; color: var(--uh-muted); max-width: 46ch; }
.uh-sos { display: flex; flex-direction: column; gap: 8px; align-items: flex-start; }
.uh-sos strong { font-size: 14px; font-weight: 600; color: var(--uh-r-critical); }
.uh-sos ul { display: flex; flex-wrap: wrap; gap: 8px; margin: 0; padding: 0; list-style: none; }
.uh-sos li { display: flex; align-items: baseline; gap: 6px; padding: 6px 12px; border: 1px solid var(--uh-line);
  border-radius: 999px; background: var(--uh-surface); font-size: 14px; color: var(--uh-muted); }
.uh-sos li a, .uh-sos li b { color: var(--uh-ink); font-weight: 700; text-decoration: none; font-size: 16px; }
.uh-mode { margin: 12px 0 0; padding: 8px 12px; border-left: 3px solid var(--uh-r-medium); background: #FBF4E3;
  font-size: 13px; color: #6B4E06; }

/* 대화 영역 */
#uh-chat { border: 1px solid var(--uh-line) !important; border-radius: 14px !important; background: var(--uh-surface) !important; }
#uh-chat .message-wrap, #uh-chat .bubble-wrap, #uh-chat [role="log"] { background: var(--uh-surface) !important; }
#uh-chat .message, #uh-chat .message * { color: var(--uh-ink) !important; }
#uh-chat .bot, #uh-chat .message.bot { background: #F3F6F5 !important; border-color: var(--uh-line) !important; }
#uh-chat .user, #uh-chat .message.user { background: var(--uh-brand-soft) !important; border-color: #B7D8DA !important; }
#uh-chat button { color: var(--uh-muted) !important; background: var(--uh-surface) !important; border-color: var(--uh-line) !important; }
.gradio-container textarea, .gradio-container input[type="text"] { background: var(--uh-surface) !important; color: var(--uh-ink) !important;
  border-color: var(--uh-line) !important; }
.gradio-container textarea::placeholder, .gradio-container input::placeholder { color: #8A9A97 !important; }
.gradio-container .block, .gradio-container .form { background: var(--uh-surface) !important; border-color: var(--uh-line) !important; }
.gradio-container .gr-group, .gradio-container .styler { background: transparent !important; }
.gradio-container label, .gradio-container .label-wrap, .gradio-container .label-wrap span { color: var(--uh-ink) !important; }
.gradio-container input[type="checkbox"] { background-color: var(--uh-surface) !important; border-color: var(--uh-muted) !important; }
.gradio-container input[type="checkbox"]:checked { background-color: var(--uh-brand) !important; border-color: var(--uh-brand) !important; }
.gradio-container button.secondary { background: var(--uh-surface) !important; color: var(--uh-ink) !important; border: 1px solid var(--uh-line) !important; }
.gradio-container button.primary { background: var(--uh-brand) !important; color: #fff !important; border: 0 !important; }
.gradio-container button.primary:hover { background: #0B5660 !important; }
.gradio-container footer, .gradio-container footer * { color: var(--uh-muted) !important; }
#uh-chat .message, #uh-chat .message-content, #uh-chat .prose { font-size: 16px !important; line-height: 1.65 !important; }
#uh-input textarea { font-size: 16px !important; line-height: 1.55 !important; }
.uh-chips { gap: 8px !important; flex-wrap: wrap !important; }
.uh-chips button { flex: 0 0 auto !important; min-width: 0 !important; border-radius: 999px !important;
  border: 1px solid var(--uh-line) !important; background: var(--uh-surface) !important; color: var(--uh-ink) !important;
  font-weight: 500 !important; font-size: 14px !important; padding: 6px 14px !important; box-shadow: none !important; }
.uh-chips button:hover { border-color: var(--uh-brand) !important; color: var(--uh-brand) !important; }
.uh-label { margin: 4px 0 0; font-size: 13px; color: var(--uh-muted); }
.uh-controls { align-items: center !important; justify-content: flex-start !important; gap: 16px !important; }
.uh-controls > * { flex: 0 0 auto !important; width: auto !important; }
.uh-controls .block, .uh-controls .form { border: 0 !important; background: transparent !important; box-shadow: none !important; }
.uh-controls button { font-size: 14px !important; padding: 8px 14px !important; }

/* 상태 패널 */
.uh-panel { display: flex; flex-direction: column; gap: 18px; font-family: var(--uh-font); color: var(--uh-ink); }
.uh-risk { position: relative; display: grid; gap: 4px; padding: 22px 24px 20px; border-radius: 16px; color: #fff;
  background: var(--uh-r-info); transition: background-color 450ms ease; }
.uh-risk--low { background: var(--uh-r-low); }
.uh-risk--medium { background: var(--uh-r-medium); }
.uh-risk--high { background: var(--uh-r-high); }
.uh-risk--critical { background: var(--uh-r-critical); }
.uh-risk, .uh-risk span, .uh-risk div { color: #fff !important; }
.uh-risk__eyebrow { font-size: 14px; opacity: .85; }
.uh-risk__level { font-size: 44px; line-height: 1.05; font-weight: 700; letter-spacing: -0.03em; }
.uh-risk__msg { font-size: 18px; font-weight: 500; }
.uh-risk__meta { display: flex; flex-wrap: wrap; align-items: center; gap: 10px 18px; margin-top: 10px; padding-top: 12px;
  border-top: 1px solid rgba(255,255,255,.35); font-size: 14px; }
.uh-risk__type { font-weight: 600; }
.uh-risk__conf { display: inline-flex; align-items: center; gap: 8px; }
.uh-meter { display: inline-block; width: 88px; height: 6px; border-radius: 999px; background: rgba(255,255,255,.3); overflow: hidden; }
.uh-meter > span { display: block; height: 100%; background: #fff; }
.uh-empty { margin: 0; font-size: 15px; line-height: 1.6; color: var(--uh-muted); }

.uh-banner { display: grid; gap: 4px; padding: 14px 16px 14px 18px; border-radius: 10px; border: 1px solid #E7B9C4;
  border-left: 6px solid var(--uh-r-critical); background: #FCF1F3; font-size: 15px; line-height: 1.5; }
.uh-banner strong { font-size: 17px; color: var(--uh-r-critical) !important; }
.uh-banner span { color: #5A2130 !important; }
.uh-banner--now { border-color: var(--uh-r-critical); border-width: 2px 2px 2px 6px; }
.uh-notice { margin: 0; padding: 10px 14px; border-radius: 10px; background: var(--uh-brand-soft); font-size: 14px; color: var(--uh-brand); }

.uh-sec { display: grid; gap: 10px; }
.uh-sec h3 { margin: 0; font-size: 15px; font-weight: 700; color: var(--uh-ink); display: flex; align-items: baseline; gap: 8px; }
.uh-sec h3.uh-sub { margin-top: 6px; font-weight: 600; color: var(--uh-muted); }
.uh-count { font-size: 13px; font-weight: 500; color: var(--uh-muted); }

.uh-actions { margin: 0; padding: 0; list-style: none; counter-reset: uh-step; display: grid; gap: 8px; }
.uh-action { counter-increment: uh-step; display: grid; grid-template-columns: 30px 1fr; column-gap: 12px; row-gap: 4px;
  padding: 12px 14px; border: 1px solid var(--uh-line); border-radius: 10px; background: var(--uh-surface); }
.uh-action::before { content: counter(uh-step); grid-row: span 2; display: grid; place-items: center; width: 30px; height: 30px;
  border-radius: 50%; background: var(--uh-ink); color: #fff; font-weight: 700; font-size: 15px; }
.uh-action:first-child { border-color: var(--uh-ink); border-width: 2px; }
.uh-action__text { font-size: 16px; line-height: 1.45; font-weight: 500; align-self: center; }
.uh-action__contact { font-size: 14px; color: var(--uh-muted); }
.uh-tel { color: var(--uh-brand); font-weight: 700; font-size: 16px; text-decoration: underline; text-underline-offset: 3px; }

.uh-track { margin: 0; padding: 0; list-style: none; display: grid; }
.uh-flag { position: relative; display: grid; grid-template-columns: 1fr auto; column-gap: 12px; padding: 9px 0 9px 22px;
  border-bottom: 1px solid var(--uh-line); font-size: 15px; }
.uh-flag:last-child { border-bottom: 0; }
.uh-flag::before { content: ""; position: absolute; left: 4px; top: 15px; width: 9px; height: 9px; border-radius: 50%;
  border: 2px solid var(--uh-line); background: var(--uh-surface); }
.uh-flag.is-reached::before { border-color: var(--uh-r-critical); background: var(--uh-r-critical); }
.uh-flag__value { font-weight: 600; }
.uh-flag__value--yes { color: var(--uh-r-critical); }
.uh-flag__value--no { color: var(--uh-r-low); }
.uh-flag__value--unknown { color: var(--uh-muted); font-weight: 400; }
.uh-flag__detail { grid-column: 1 / -1; font-size: 13px; color: var(--uh-muted); }

.uh-list { margin: 0; padding-left: 20px; display: grid; gap: 6px; font-size: 15px; line-height: 1.5; }
.uh-list--muted { color: var(--uh-muted); }

.uh-checklist { margin: 0; padding: 0; list-style: none; display: grid; gap: 6px; }
.uh-check { display: flex; align-items: center; gap: 10px; font-size: 15px; }
.uh-check__box { flex: none; width: 18px; height: 18px; border: 2px solid var(--uh-muted); border-radius: 4px; }
.uh-check.is-done { color: var(--uh-muted); text-decoration: line-through; }
.uh-check.is-done .uh-check__box { border-color: var(--uh-r-low); background: var(--uh-r-low)
  url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Cpath d='M3 8.5l3 3 7-7' fill='none' stroke='white' stroke-width='2.2'/%3E%3C/svg%3E") center/12px no-repeat; }

.uh-tools { font-size: 14px; color: var(--uh-muted); border-top: 1px dashed var(--uh-line); padding-top: 10px; }
.uh-tools summary { cursor: pointer; }
.uh-toollist { margin: 8px 0 0; padding-left: 20px; display: grid; gap: 4px; }
.uh-toollist code { margin-left: 8px; font-size: 12px; color: var(--uh-muted); background: var(--uh-paper); padding: 1px 6px; border-radius: 4px; }

.uh-badge { display: inline-block; padding: 2px 10px; border-radius: 999px; color: #fff; font-size: 13px; font-weight: 600; background: var(--uh-r-info); }
.uh-badge--low { background: var(--uh-r-low); } .uh-badge--medium { background: var(--uh-r-medium); }
.uh-badge--high { background: var(--uh-r-high); } .uh-badge--critical { background: var(--uh-r-critical); }

/* 승인 카드 */
#uh-approve { border: 2px solid var(--uh-ink) !important; border-radius: 14px !important; background: var(--uh-surface) !important;
  padding: 18px !important; gap: 12px !important; }
#uh-approve .block { border: 0 !important; background: transparent !important; box-shadow: none !important; padding: 0 !important; }
.uh-approve h3 { margin: 0 0 6px; font-size: 17px; }
.uh-approve p { margin: 0 0 10px; font-size: 15px; color: var(--uh-muted); }
.uh-kv { margin: 0; display: grid; gap: 6px; }
.uh-kv div { display: grid; grid-template-columns: 110px 1fr; gap: 10px; font-size: 15px; }
.uh-kv dt { color: var(--uh-muted); } .uh-kv dd { margin: 0; font-weight: 600; }

/* URL 검사 */
.uh-url { display: grid; gap: 8px; padding: 14px 16px; border-radius: 10px; border-left: 4px solid var(--uh-r-info); background: var(--uh-paper); }
.uh-url--critical { border-color: var(--uh-r-critical); } .uh-url--high { border-color: var(--uh-r-high); }
.uh-url--medium { border-color: var(--uh-r-medium); } .uh-url--low { border-color: var(--uh-r-low); }
.uh-url__verdict { margin: 0; font-size: 17px; font-weight: 700; }
.uh-url__addr { margin: 0; font-size: 14px; color: var(--uh-muted); word-break: break-all; }
.uh-score { display: flex; align-items: center; gap: 10px; font-size: 14px; }
.uh-meter--critical, .uh-meter--high, .uh-meter--medium, .uh-meter--low { background: var(--uh-line); }
.uh-meter--critical > span { background: var(--uh-r-critical); } .uh-meter--high > span { background: var(--uh-r-high); }
.uh-meter--medium > span { background: var(--uh-r-medium); } .uh-meter--low > span { background: var(--uh-r-low); }
.uh-note { margin: 0; font-size: 13px; color: var(--uh-muted); }
.uh-ghost { align-self: flex-start !important; width: auto !important; border: 1px solid var(--uh-brand) !important;
  color: var(--uh-brand) !important; background: var(--uh-surface) !important; font-weight: 600 !important; }

/* 큰 글씨 보기 */
body.uh-large #uh-chat .message, body.uh-large #uh-chat .message-content, body.uh-large #uh-chat .prose,
body.uh-large #uh-input textarea { font-size: 20px !important; }
body.uh-large .uh-panel { zoom: 1.15; }

:focus-visible { outline: 3px solid var(--uh-brand) !important; outline-offset: 2px; }
@media (prefers-reduced-motion: reduce) { .uh-risk { transition: none; } }
@media (max-width: 820px) {
  .uh-brand h1 { font-size: 28px; }
  .uh-risk__level { font-size: 36px; }
}
"""

LOGO_SVG = """
<svg width="44" height="44" viewBox="0 0 44 44" aria-hidden="true">
  <rect x="0" y="0" width="44" height="44" rx="12" fill="#132B2A"/>
  <path d="M26 8v17a7 7 0 0 1-14 0v-2" fill="none" stroke="#fff" stroke-width="3.2" stroke-linecap="round"/>
  <path d="M12 23l-3 3.5" fill="none" stroke="#fff" stroke-width="3.2" stroke-linecap="round"/>
  <path d="M31 13l6-6M31 7l6 6" fill="none" stroke="#7FD1D6" stroke-width="2.6" stroke-linecap="round"/>
</svg>
"""


def _header_html(mode: str, note: str) -> str:
    contacts = []
    for key in ("police", "fss", "kisa"):
        value = _contact(key)
        if value:
            contacts.append(f"<li>{CONTACT_LABELS[key]} {_contact_html(value).replace('uh-tel', 'uh-tel-plain')}</li>")
    mode_html = ""
    if mode == "demo":
        mode_html = (
            '<p class="uh-mode">데모 모드로 실행 중이에요. agent.py를 불러오지 못해 예시 응답을 보여줘요.'
            f" ({_esc(note)})</p>"
        )
    return (
        '<header class="uh-head">'
        f'<div class="uh-brand">{LOGO_SVG}<div><h1>Un Hook</h1>'
        "<p>받은 문자나 전화 내용을 그대로 붙여넣으세요. 사기인지, 지금 무엇을 해야 하는지 알려드려요.</p></div></div>"
        '<div class="uh-sos"><strong>이미 돈을 보냈다면 은행 콜센터에 지급정지부터 요청하세요</strong>'
        f'<ul>{"".join(contacts)}</ul></div>'
        "</header>" + mode_html
    )


def _accepts(fn, name: str) -> bool:
    try:
        return name in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


def _build_theme(gr):
    try:
        theme = gr.themes.Base(
            primary_hue=gr.themes.colors.teal,
            neutral_hue=gr.themes.colors.slate,
            font=[gr.themes.GoogleFont("IBM Plex Sans KR"), "Apple SD Gothic Neo", "Malgun Gothic", "sans-serif"],
            radius_size=gr.themes.sizes.radius_md,
        )
        return theme.set(
            body_background_fill="#EEF3F2",
            body_text_color="#132B2A",
            block_background_fill="#FFFFFF",
            block_border_color="#D3DDDB",
            input_background_fill="#FFFFFF",
            button_primary_background_fill="#0E6770",
            button_primary_background_fill_hover="#0B5660",
            button_primary_text_color="#FFFFFF",
            button_secondary_background_fill="#FFFFFF",
            button_secondary_border_color="#D3DDDB",
            body_background_fill_dark="#EEF3F2",
            body_text_color_dark="#132B2A",
            body_text_color_subdued_dark="#566B68",
            background_fill_primary_dark="#FFFFFF",
            background_fill_secondary_dark="#F3F6F5",
            block_background_fill_dark="#FFFFFF",
            block_border_color_dark="#D3DDDB",
            border_color_primary_dark="#D3DDDB",
            input_background_fill_dark="#FFFFFF",
            button_primary_background_fill_dark="#0E6770",
            button_primary_text_color_dark="#FFFFFF",
            button_secondary_background_fill_dark="#FFFFFF",
            button_secondary_text_color_dark="#132B2A",
            button_secondary_border_color_dark="#D3DDDB",
        )
    except Exception:
        return None


def build_gradio_app():
    import gradio as gr

    _get_agent()
    theme = _build_theme(gr)
    style_kwargs = {"css": CSS, "head": HEAD}
    if theme is not None:
        style_kwargs["theme"] = theme

    blocks_kwargs = {"title": "Un Hook | 금융사기 대응 상담"}
    launch_kwargs = {}
    if _accepts(gr.Blocks.__init__, "css"):  # Gradio 5 이하
        blocks_kwargs.update(style_kwargs)
    else:  # Gradio 6부터 css/theme/head는 launch()로 이동
        launch_kwargs.update(style_kwargs)

    chatbot_kwargs = {
        "elem_id": "uh-chat", "height": 520, "show_label": False,
        "placeholder": "받은 문자, 전화 내용, 지금 상황을 편하게 적어주세요.",
    }
    if _accepts(gr.Chatbot.__init__, "type"):  # Gradio 5는 messages 형식을 명시해야 함
        chatbot_kwargs["type"] = "messages"

    with gr.Blocks(**blocks_kwargs) as demo:
        thread_id = gr.State(lambda: uuid.uuid4().hex)
        url_result = gr.State({})

        gr.HTML(_header_html(_AGENT_MODE, _AGENT_NOTE), elem_classes="uh-bare")

        with gr.Row(elem_id="uh-root", equal_height=False):
            with gr.Column(scale=7, min_width=360):
                chatbot = gr.Chatbot(**chatbot_kwargs)
                with gr.Row(elem_classes="uh-chips"):
                    chip_buttons = [gr.Button(label, size="sm") for label, _ in EXAMPLES]
                with gr.Row(equal_height=True):
                    msg = gr.Textbox(elem_id="uh-input", show_label=False, lines=2, max_lines=8, scale=5,
                                     placeholder="예) 엄마 나 폰 고장나서 임시폰이야. 신분증 사진 좀 보내줘 라는 문자가 왔어요")
                    send = gr.Button("보내기", variant="primary", scale=1, min_width=96)
                with gr.Row(elem_classes="uh-controls"):
                    new_chat = gr.Button("새 상담 시작", size="sm", variant="secondary", scale=0, min_width=120)
                    large = gr.Checkbox(label="글씨 크게 보기", value=False, container=False, scale=0, min_width=160)

                with gr.Accordion("링크만 따로 검사하기", open=False):
                    gr.HTML('<p class="uh-label">링크를 누르지 않고 주소만 검사해요. 검사 결과는 상담에 붙여서 이어갈 수 있어요.</p>',
                            elem_classes="uh-bare")
                    with gr.Row(equal_height=True):
                        url_box = gr.Textbox(show_label=False, placeholder="http://로 시작하는 주소를 붙여넣으세요", scale=5)
                        url_btn = gr.Button("링크 검사", variant="primary", scale=1, min_width=96)
                    url_out = gr.HTML(elem_classes="uh-bare")
                    url_to_chat = gr.Button("검사 결과를 상담에 붙이기", size="sm", variant="secondary", visible=False,
                                            elem_classes="uh-ghost")

            with gr.Column(scale=5, min_width=320):
                with gr.Column(visible=False, elem_id="uh-approve") as approve_box:
                    pending_html = gr.HTML(elem_classes="uh-bare")
                    with gr.Row():
                        approve_btn = gr.Button("승인하고 접수하기", variant="primary")
                        reject_btn = gr.Button("접수하지 않기", variant="secondary")
                panel = gr.HTML(render_state_html({}), elem_classes="uh-bare")

        # ----- 이벤트 -----
        def on_send(text, history, tid):
            history = list(history or [])
            text = (text or "").strip()
            if not text:
                yield gr.update(), history, gr.update(), gr.update(), gr.update()
                return
            history.append({"role": "user", "content": defang_text(text)})
            history.append({"role": "assistant", "content": "확인하고 있어요…"})
            yield "", history, gr.update(), gr.update(), gr.update()

            result = call_run_turn(text, tid)
            reply = result["text"] or ""
            if result["error"]:
                reply = (reply + "\n\n" if reply else "") + f"응답을 만들지 못했어요. 다시 보내주세요. ({result['error']})"
            history[-1] = {"role": "assistant", "content": reply or "응답이 비어 있어요. 다시 보내주세요."}
            yield (
                "", history,
                render_state_html(result["state"], result["structured"], result["tools_called"]),
                gr.update(visible=bool(result["interrupted"])),
                render_pending(result["pending"]),
            )

        send_outputs = [msg, chatbot, panel, approve_box, pending_html]
        send.click(on_send, [msg, chatbot, thread_id], send_outputs)
        msg.submit(on_send, [msg, chatbot, thread_id], send_outputs)

        for button, (_, example_text) in zip(chip_buttons, EXAMPLES):
            button.click(lambda t=example_text: t, None, msg)

        def on_resume(approved, history, tid):
            history = list(history or [])
            result = call_resume_turn(approved, tid)
            reply = result["text"] or ("승인했어요." if approved else "취소했어요.")
            if result["error"]:
                reply += f"\n\n처리하지 못했어요. ({result['error']})"
            history.append({"role": "assistant", "content": reply})
            return (
                history,
                render_state_html(result["state"], result["structured"], result["tools_called"]),
                gr.update(visible=bool(result["interrupted"])),
                render_pending(result["pending"]),
            )

        resume_outputs = [chatbot, panel, approve_box, pending_html]
        approve_btn.click(lambda h, t: on_resume(True, h, t), [chatbot, thread_id], resume_outputs)
        reject_btn.click(lambda h, t: on_resume(False, h, t), [chatbot, thread_id], resume_outputs)

        def on_new_chat():
            return [], uuid.uuid4().hex, render_state_html({}), gr.update(visible=False), "", ""

        new_chat.click(on_new_chat, None, [chatbot, thread_id, panel, approve_box, pending_html, msg])

        def on_check_url(url):
            result = check_url(url)
            if not result:
                return '<p class="uh-note">검사할 주소를 입력해 주세요.</p>', {}, gr.update(visible=False)
            return render_url_result(result, url), result, gr.update(visible=True)

        url_btn.click(on_check_url, url_box, [url_out, url_result, url_to_chat])
        url_box.submit(on_check_url, url_box, [url_out, url_result, url_to_chat])

        def on_url_to_chat(result):
            if not result:
                return gr.update()
            signals = ", ".join(result.get("signals") or []) or "특이 신호 없음"
            return (f"링크 검사창에서 확인한 결과를 같이 봐줘. 주소: {result.get('defanged', '')} / "
                    f"위험 점수: {result.get('risk_score', '?')} / 신호: {signals}")

        url_to_chat.click(on_url_to_chat, url_result, msg)

        large.change(None, large, None,
                     js="(v) => { document.body.classList.toggle('uh-large', v); return []; }")

    demo._uh_launch_kwargs = launch_kwargs
    return demo


def launch(**kwargs):
    """Colab/로컬 공통 실행 함수. Gradio 버전에 맞게 css/theme 위치를 알아서 넣는다."""
    demo = build_gradio_app()
    options = dict(getattr(demo, "_uh_launch_kwargs", {}))
    options.update(kwargs)
    return demo.launch(**options)


def run_cli_fallback() -> None:
    """Gradio를 쓸 수 없을 때 터미널에서 대화한다."""
    _get_agent()
    thread_id = uuid.uuid4().hex
    print(f"Un Hook 상담 ({'데모 모드' if _AGENT_MODE == 'demo' else '실제 에이전트'}) — 종료하려면 q 입력")
    while True:
        try:
            text = input("\n나> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if text.lower() in {"q", "quit", "exit"}:
            break
        if not text:
            continue
        result = call_run_turn(text, thread_id)
        while True:
            print(f"\nUn Hook> {result['text'] or '(응답 없음)'}")
            if result["error"]:
                print(f"  오류: {result['error']}")
            structured = result["structured"] or {}
            risk = _max_risk(structured.get("risk_level"), result["state"].get("risk_level"))
            print(f"  위험도: {RISK_LABELS[risk][0]} / 피해 단계: {result['state'].get('damage_stage', 'none')}")
            for step in structured.get("immediate_actions") or structured.get("next_actions") or []:
                contact = f" ({step.get('contact')})" if step.get("contact") else ""
                print(f"  {step.get('priority')}. {step.get('action')}{contact}")
            if not result["interrupted"]:
                break
            answer = input("  승인할까요? (y/n) > ").strip().lower()
            result = call_resume_turn(answer in {"y", "yes", "ㅇ"}, thread_id)


if __name__ == "__main__":
    import os

    try:  # 로컬(VS Code)에서는 .env의 OPENAI_API_KEY를 읽는다
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    try:
        import gradio  # noqa: F401
    except ImportError as exc:
        import sys

        print("gradio를 불러오지 못해 터미널 모드로 실행합니다.")
        print(f"  이유: {exc}")
        print(f"  사용 중인 파이썬: {sys.executable}")
        print("  브라우저 화면을 보려면: python -m pip install gradio  후 다시 python app.py\n")
        run_cli_fallback()
    else:
        # 로컬은 기본으로 브라우저만 열고, 외부 공유 링크가 필요할 때만 UNHOOK_SHARE=1
        launch(inbrowser=True, share=os.getenv("UNHOOK_SHARE") == "1")