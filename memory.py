"""Store-backed report history for 작업 묶음 5 (agent-design.md 2.5, 3.1, 3.2).

설계서 3.1에 따라 `report_history`는 Store에만 저장하고 State에 두지 않는다.
이 모듈은 Store 접근과 이력 대조만 제공하며, 결과를 `history_matches` State에
반영하고 프롬프트에 주입하는 `MemoryInjectMiddleware`는 `middleware.py`에서
이 함수들을 호출한다 (설계서 5절 파일 경계).

저장 레코드에는 PII 원문을 넣지 않는다. 도메인·전화번호는 정규화한 값만,
사건 요약은 마스킹된 문장만 보관한다 (설계서 1.5 보안, 3.1 핵심 원칙).
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, TypedDict

from langgraph.store.base import BaseStore

# 설계서 3.1: report_history는 최근 5건만 유지한다.
MAX_HISTORY_RECORDS = 5

# 설계서 3.2 실행 순서 근거: 도메인·전화번호는 정규화 후 완전 일치,
# 문구는 정규화 후 부분 일치. 아래 임계치는 담당자(작업 묶음 2) 확정 전 기본값이다.
#
# 부분 일치는 최장 공통 부분수열(LCS)로 본다. 같은 사기 문구는 글자를 끼워 넣거나
# 조사를 바꿔 재사용되므로("주소 불일치로 반송" → "주소지 불일치 반송 예정"),
# 연속된 부분문자열만 보면 놓친다. 길이와 비율을 함께 요구해 짧은 우연 일치를 막는다.
# 한국어는 어미(습니다·세요)가 자주 겹쳐 짧은 문장끼리 우연히 높은 LCS가 나온다.
# "안녕하세요 반갑습니다" vs "안녕히 가세요 고맙습니다"가 0.6/6자 기준에서 70%로
# 잡혔다. 실제 사기 문구는 길고 재사용 시 유사도가 훨씬 높아 아래 기준을 넘는다.
MIN_PHRASE_MATCH_CHARS = 8
MIN_PHRASE_MATCH_RATIO = 0.75
# 저장·비교할 문구 길이 상한. LCS는 길이의 곱에 비례하므로 상한이 필요하다.
MAX_PHRASE_CHARS = 300
# 원문 추출 시 훑을 최근 메시지 수.
_MESSAGE_SCAN_LIMIT = 20

# 설계서 1.5 보안: 식별 정보는 원문 대신 해시 등 최소 정보만 저장한다.
_HASH_LENGTH = 16
# 문구 비교 전에 제거할 숫자열 길이. 전화번호·계좌번호가 문구 채널로
# 새어 나가지 않게 한다.
_DIGIT_RUN_MIN = 6

_NAMESPACE_PREFIX = "report_history"

_URL_RE = re.compile(r"\b(?:https?://)?((?:[\w-]+\.)+[a-z]{2,})(?:[/:?#]\S*)?", re.IGNORECASE)
_PHONE_RE = re.compile(r"\b0\d{1,2}[-.\s]?\d{3,4}[-.\s]?\d{4}\b")
_NON_PHRASE_RE = re.compile(r"[^0-9a-z가-힣]+")
_DIGIT_RUN_RE = re.compile(rf"\d{{{_DIGIT_RUN_MIN},}}")
# PIIMiddleware가 남긴 <SCAM_PHONE_1> 같은 토큰. 문구 비교에 섞이면 안 된다.
_TOKEN_RE = re.compile(r"<[A-Z_]+_\d+>")


class ReportRecord(TypedDict, total=False):
    """Store에 보관하는 과거 신고 1건.

    세부 필드는 설계서 5절에서 미확정으로 남긴 항목이라 total=False로 둔다.
    """

    summary: str  # 마스킹된 사건 요약
    domains: list[str]  # 정규화된 도메인
    phones: list[str]  # 전화번호 해시 (원문·숫자 미저장, 설계서 1.5 보안)
    phrase: str  # 정규화된 문구
    scam_type: str
    reported: bool  # 실제 신고 접수 여부 (모의 처리와 구분, 설계서 2.1 Store)
    receipt_no: str  # 모의 접수번호 (report_to_authority가 기록)


class HistoryMatch(TypedDict):
    """이번 입력과 과거 이력의 일치 항목. 프롬프트 주입·evidence 근거로 쓴다."""

    key: str
    field: str  # "domain" | "phone" | "phrase"
    value: str
    summary: str
    scam_type: str
    reported: bool


def _namespace(user_id: str) -> tuple[str, str]:
    return (_NAMESPACE_PREFIX, user_id)


def normalize_domain(value: str) -> str:
    """도메인을 소문자로 낮추고 선행 www.를 제거한다."""
    domain = value.strip().lower().rstrip(".")
    return domain[4:] if domain.startswith("www.") else domain


def normalize_phone(value: str) -> str:
    """전화번호에서 숫자만 남긴다."""
    return re.sub(r"\D", "", value)


def hash_identifier(value: str) -> str:
    """전화번호 등 식별 정보를 대조 가능한 해시로 바꾼다 (설계서 1.5 보안)."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:_HASH_LENGTH]


def mask_phone(value: str) -> str:
    """표시용 마스킹. 뒤 4자리만 남긴다."""
    digits = normalize_phone(value)
    return f"···{digits[-4:]}" if len(digits) > 4 else "···"


def normalize_phrase(text: str) -> str:
    """문구 비교용 정규화.

    문구 채널은 "같은 문장을 쓰는가"만 본다. URL과 번호는 각자 전용 채널이 있으므로
    문구에서 제거한다. 남기면 두 가지가 잘못된다 — 같은 사실을 두 번 세고,
    일치 문자열이 `httpvvcjtop`처럼 사용자에게 보일 근거 문장으로 나간다.

    순서가 중요하다. 토큰과 URL을 먼저 지우고, 구분자를 지운 뒤, 숫자열을 지운다.
    구분자를 먼저 지우면 "010-1111-2222"가 11자리 숫자열로 보이기 때문이다.
    """
    without_url = _URL_RE.sub(" ", _TOKEN_RE.sub(" ", text))
    compact = _NON_PHRASE_RE.sub("", without_url.lower())
    return _DIGIT_RUN_RE.sub("", compact)[:MAX_PHRASE_CHARS]


def extract_domains(text: str) -> list[str]:
    """입력 텍스트에서 도메인을 추출한다. 중복은 제거하고 순서를 유지한다."""
    found = [normalize_domain(m.group(1)) for m in _URL_RE.finditer(text)]
    return list(dict.fromkeys(d for d in found if d))


def extract_phones(text: str) -> list[str]:
    """입력 텍스트에서 전화번호를 추출한다. 중복은 제거하고 순서를 유지한다."""
    found = [normalize_phone(m.group(0)) for m in _PHONE_RE.finditer(text)]
    return list(dict.fromkeys(p for p in found if p))


def build_record(
    summary: str,
    *,
    source_text: str = "",
    scam_type: str = "unknown",
    reported: bool = False,
) -> ReportRecord:
    """신고 이력 레코드를 만든다.

    `summary`는 이미 마스킹된 문장이어야 한다. `source_text`에서는 도메인·전화번호를
    정규화해 추출하고 원문은 보관하지 않는다.
    """
    return ReportRecord(
        summary=summary,
        domains=extract_domains(source_text),
        phones=[hash_identifier(p) for p in extract_phones(source_text)],
        phrase=normalize_phrase(source_text),
        scam_type=scam_type,
        reported=reported,
    )


def save_report(store: BaseStore, user_id: str, record: ReportRecord) -> str:
    """신고 이력 1건을 Store에 저장하고 최근 MAX_HISTORY_RECORDS건만 남긴다.

    Store 접근이 실패하면 예외를 그대로 올린다. 호출부(미들웨어)가 실패를 흡수한다.
    """
    namespace = _namespace(user_id)
    existing = store.search(namespace, limit=MAX_HISTORY_RECORDS + 1)
    key = f"r{len(existing) + 1:04d}"
    while store.get(namespace, key) is not None:
        key = f"{key}x"
    store.put(namespace, key, dict(record))

    items = sorted(
        store.search(namespace, limit=MAX_HISTORY_RECORDS + 1),
        key=lambda item: item.created_at,
        reverse=True,
    )
    for stale in items[MAX_HISTORY_RECORDS:]:
        store.delete(namespace, stale.key)
    return key


def load_history(store: BaseStore, user_id: str) -> list[tuple[str, ReportRecord]]:
    """해당 사용자의 과거 신고 이력을 최신순으로 읽는다. 없으면 빈 리스트."""
    items = store.search(_namespace(user_id), limit=MAX_HISTORY_RECORDS)
    items = sorted(items, key=lambda item: item.created_at, reverse=True)
    return [(item.key, ReportRecord(**item.value)) for item in items]


def match_history(
    history: list[tuple[str, ReportRecord]], text: str
) -> list[HistoryMatch]:
    """이번 입력을 과거 이력과 대조해 일치 항목을 돌려준다.

    도메인·전화번호는 정규화 후 완전 일치, 문구는 정규화 후 부분 일치로 본다
    (설계서 3.2). 일치가 없으면 빈 리스트를 돌려준다.
    """
    domains = set(extract_domains(text))
    # 저장된 값은 해시라 현재 입력도 해시로 바꿔 대조한다. 표시용 마스킹은
    # 사용자가 방금 입력한 번호에서 만들고, Store에서 꺼내 쓰지 않는다.
    phone_by_hash = {hash_identifier(p): mask_phone(p) for p in extract_phones(text)}
    phrase = normalize_phrase(text)

    matches: list[HistoryMatch] = []
    for key, record in history:
        summary = record.get("summary", "")
        scam_type = record.get("scam_type", "unknown")
        reported = bool(record.get("reported", False))

        for domain in record.get("domains") or []:
            if domain in domains:
                matches.append(HistoryMatch(
                    key=key, field="domain", value=domain,
                    summary=summary, scam_type=scam_type, reported=reported,
                ))
        for phone_hash in record.get("phones") or []:
            if phone_hash in phone_by_hash:
                matches.append(HistoryMatch(
                    key=key, field="phone", value=phone_by_hash[phone_hash],
                    summary=summary, scam_type=scam_type, reported=reported,
                ))

        past_phrase = record.get("phrase") or ""
        similarity = phrase_similarity(phrase, past_phrase)
        if similarity is not None:
            matches.append(HistoryMatch(
                key=key, field="phrase", value=f"{round(similarity * 100)}%",
                summary=summary, scam_type=scam_type, reported=reported,
            ))
    return matches


def phrase_similarity(left: str, right: str) -> float | None:
    """두 정규화 문구의 유사도. 기준 미달이면 None.

    최장 공통 부분수열 길이를 짧은 쪽 길이로 나눈다. 길이(`MIN_PHRASE_MATCH_CHARS`)와
    비율(`MIN_PHRASE_MATCH_RATIO`)을 모두 넘어야 일치로 본다. 길이만 보면 긴 글에서
    우연히 겹치고, 비율만 보면 짧은 글이 쉽게 통과한다.
    """
    shorter = min(len(left), len(right))
    if shorter < MIN_PHRASE_MATCH_CHARS:
        return None
    overlap = _lcs_length(left, right)
    if overlap < MIN_PHRASE_MATCH_CHARS:
        return None
    ratio = overlap / shorter
    return ratio if ratio >= MIN_PHRASE_MATCH_RATIO else None


def _lcs_length(left: str, right: str) -> int:
    """최장 공통 부분수열 길이. 연속일 필요는 없다."""
    previous = [0] * (len(right) + 1)
    for i in range(1, len(left) + 1):
        current = [0] * (len(right) + 1)
        left_char = left[i - 1]
        for j in range(1, len(right) + 1):
            if left_char == right[j - 1]:
                current[j] = previous[j - 1] + 1
            else:
                current[j] = max(previous[j], current[j - 1])
        previous = current
    return previous[len(right)]


def _with_subject_particle(word: str) -> str:
    """받침 유무에 따라 이/가를 붙인다. "도메인가"처럼 나오지 않게 한다."""
    last = word[-1]
    if "가" <= last <= "힣":
        has_final = (ord(last) - ord("가")) % 28 != 0
        return f"{word}이" if has_final else f"{word}가"
    return f"{word}가"


def describe_matches(matches: list[HistoryMatch]) -> list[str]:
    """일치 항목을 프롬프트·evidence에 넣을 한국어 문장으로 바꾼다.

    전화번호는 `match_history`에서 이미 뒤 4자리로 마스킹되어 들어온다.
    """
    labels = {"domain": "도메인", "phone": "발신번호", "phrase": "문구"}
    lines: list[str] = []
    for match in matches:
        # phone은 match_history가 이미 마스킹해 담는다.
        value = match["value"]
        status = "신고 접수된" if match["reported"] else "기록된"
        subject = _with_subject_particle(labels[match["field"]])
        verb = "유사합니다" if match["field"] == "phrase" else "일치합니다"
        lines.append(f"과거 {status} 사건과 {subject} {verb} ({value}).")
    return lines


def summarize_for_prompt(matches: list[HistoryMatch]) -> str:
    """MemoryInjectMiddleware가 시스템 프롬프트에 주입할 문장 블록."""
    if not matches:
        return ""
    lines = describe_matches(matches)
    return "이번 입력은 이 사용자의 과거 신고 이력과 다음이 일치합니다:\n" + "\n".join(
        f"- {line}" for line in lines
    )


def source_text_from_messages(messages: Any) -> str:
    """대화 메시지에서 문구 대조용 원문을 모은다.

    `guards.prepare_guarded_message`가 만든 HumanMessage는 content가
    `{"user_statement": ..., "external_texts": [...]}` JSON이다. 붙여넣은 문자
    원문(`external_texts`)만 쓴다 — 사용자 진술은 사람마다 달라 문구 대조 근거가
    되지 못한다. 형식이 다르면 빈 문자열을 돌려주고 예외를 올리지 않는다.
    """
    collected: list[str] = []
    for message in list(messages or [])[-_MESSAGE_SCAN_LIMIT:]:
        content = getattr(message, "content", None)
        if not isinstance(content, str) or not content.startswith("{"):
            continue
        try:
            payload = json.loads(content)
        except (ValueError, TypeError):
            continue
        if not isinstance(payload, dict):
            continue
        for text in payload.get("external_texts") or []:
            if isinstance(text, str):
                collected.append(text)
    return " ".join(collected)


def record_from_state(state: dict[str, Any], source_text: str) -> ReportRecord:
    """신고 접수 직후 State에서 이력 레코드를 만든다.

    `incident_report`의 세부 필드는 설계서 5절에서 미확정이므로 요약 문자열만 읽는다.
    """
    report = state.get("incident_report") or {}
    summary = str(report.get("summary") or "").strip()
    if not summary:
        summary = f"피해 단계 {state.get('damage_stage', 'none')} 사건"
    structured = state.get("structured_response")
    scam_type = getattr(structured, "scam_type", None) or "unknown"
    return build_record(
        summary, source_text=source_text, scam_type=scam_type, reported=True
    )
