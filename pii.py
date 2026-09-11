"""pii.py — 개인정보 마스킹 및 토큰화 (설계서 3.1 pii_vault, 3.2 PIIMiddleware, 3.3 G4)

규칙
- 주민등록번호·카드번호: 누구의 것이든 피해자 정보로 보고 라벨로 가린다. 원문은 어디에도 저장하지 않는다.
- 계좌번호·전화번호
  - 붙여넣은 원문(문자·메신저) 안에 있거나, 사용자 진술에서 "보낸 계좌", "전화 온 번호"처럼
    사기범 측을 가리키면 <SCAM_ACCOUNT_1> 같은 토큰으로 바꾸고 원문은 State.pii_vault에만 둔다.
  - "내 계좌", "제 번호"처럼 본인 것이면 라벨로 가리고 저장하지 않는다.
- 모델에는 항상 토큰이나 라벨만 전달한다. 원문 복원은 정리서·신고 단계에서 restore_tokens()로 한다.
- 로그에는 원문을 남기지 않고 종류와 개수만 남긴다.

공개 함수 (다른 파일에서 사용)
- find_pii(text)                 탐지만
- find_unmasked_pii(text)        남아 있는 원문 PII 종류 (audit.py가 출력 검사에 사용)
- mask_text(text, vault)         마스킹 + 토큰화 → MaskResult
- restore_tokens(text, vault)    토큰을 원문으로 (정리서·report_to_authority 전용)
- resolve_token(token, vault)    토큰 하나를 원문으로

Tool 인자 복원
- 모델은 토큰만 알기 때문에 verify_caller_number(phone="<SCAM_PHONE_1>")처럼 부른다.
  PIIMiddleware.wrap_tool_call이 실행 직전에 지정한 인자만 원문으로 바꿔 Tool에 넘긴다.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from state import RuntimeContext, UnHookState

logger = logging.getLogger("unhook.pii")

PIIKind = Literal["rrn", "card", "account", "phone"]

MASK_LABELS: dict[str, str] = {
    "rrn": "[주민등록번호 가림]",
    "card": "[카드번호 가림]",
    "account": "[본인 계좌번호 가림]",
    "phone": "[본인 전화번호 가림]",
}
TOKEN_PREFIX: dict[str, str] = {"account": "SCAM_ACCOUNT", "phone": "SCAM_PHONE"}
TOKEN_PATTERN = re.compile(r"<SCAM_(?:ACCOUNT|PHONE)_\d+>")

# 실행 직전에 토큰을 원문으로 바꿀 Tool 인자. 모델에게 돌아가는 결과는 before_model에서 다시 가려진다.
# - verify_caller_number.phone: 실제 번호로 조회해야 함
# - report_to_authority.target: memory.build_record가 번호를 해시로 저장하려면 원문이 필요함
#   (summary는 마스킹된 요약으로 저장되므로 토큰 그대로 둔다)
TOOL_ARGS_TO_RESTORE: dict[str, tuple[str, ...]] = {
    "verify_caller_number": ("phone",),
    "report_to_authority": ("target",),
}

BLOCKED_NOTICE = "주민등록번호나 카드번호가 들어간 내용은 안전하게 처리하지 못했어요. 번호를 빼고 다시 입력해 주세요."
BLOCKED_PLACEHOLDER = "[개인정보 보호를 위해 입력이 차단되었습니다]"

# ---------------------------------------------------------------------------
# 탐지 패턴
# ---------------------------------------------------------------------------
_SEP = r"[\s.\-]?"
_RRN_HYPHEN = re.compile(r"(?<!\d)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\s?-\s?[1-8]\d{6}(?!\d)")
_RRN_BARE = re.compile(r"(?<!\d)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])[1-8]\d{6}(?!\d)")
_CARD_GROUPED = re.compile(r"(?<!\d)\d{4}[\s\-]\d{4}[\s\-]\d{4}[\s\-]\d{4}(?!\d)|(?<!\d)\d{4}[\s\-]\d{6}[\s\-]\d{5}(?!\d)")
_CARD_BARE = re.compile(r"(?<!\d)\d{15,16}(?!\d)")
_PHONE = re.compile(
    r"(?<![\d\-])(?:\+?82[\s\-]?1[016789]|01[016789])" + _SEP + r"\d{3,4}" + _SEP + r"\d{4}(?![\d\-])"
    r"|(?<![\d\-])0(?:2|3[1-3]|4[1-4]|5[1-5]|6[1-4]|70)" + _SEP + r"\d{3,4}" + _SEP + r"\d{4}(?![\d\-])"
)
_ACCOUNT_GROUPED = re.compile(r"(?<![\d\-])\d{2,6}-\d{2,6}-\d{1,7}(?:-\d{1,3})?(?![\d\-])")
_ACCOUNT_BARE = re.compile(r"(?<!\d)\d{10,14}(?!\d)")

_RRN_CONTEXT = re.compile(r"주민|생년월일")
_CARD_CONTEXT = re.compile(r"카드")
_ACCOUNT_CONTEXT = re.compile(r"계좌|입금|이체|송금|은행|뱅크|국민|신한|우리|하나|농협|기업|새마을|우체국|토스|케이뱅크")

# 누구의 번호인지 판단하는 단서 (번호 앞 18자, 뒤 10자 안에서 찾음)
_VICTIM_CUE = re.compile(r"(?:내|제|저의|나의|본인|우리)\s*(?:명의\s*)?(?:계좌|통장|번호|폰|휴대폰|핸드폰|전화번호)")
_SCAMMER_CUE = re.compile(
    r"보냈|보낸|보내라|입금|이체|송금|받는\s*사람|받는\s*계좌|사기|그쪽|상대|저쪽|수사관|검사|직원|사칭|"
    r"에서\s*(?:전화|문자|연락)|로\s*(?:보내|입금|이체)|전화\s*(?:왔|옴|온|받)|문자\s*(?:왔|옴|온|받)|연락\s*(?:왔|옴|온)"
)

# 붙여넣은 원문을 알아보는 단서
_EXTERNAL_HEADER = re.compile(r"^\s*[\[(]\s*(?:web\s*발신|국제\s*발신|국외\s*발신|해외\s*발신|광고)\s*[\])]", re.IGNORECASE)
_EXTERNAL_COLON = re.compile(r"(?:문자|메시지|메세지|카톡|톡|내용|원문)\s*(?:내용\s*)?[:：]")
_QUOTED = re.compile(r"“[^”]{6,}”|\"[^\"]{6,}\"|「[^」]{6,}」|『[^』]{6,}』|‘[^’]{6,}’")


@dataclass(frozen=True)
class PIIMatch:
    kind: PIIKind
    start: int
    end: int
    value: str


@dataclass
class MaskResult:
    text: str
    vault: dict[str, str]
    masked: list[str] = field(default_factory=list)      # 라벨로 가린 종류
    tokenized: list[str] = field(default_factory=list)   # 새로 만들거나 재사용한 토큰
    blocked: bool = False                                  # 주민번호·카드번호를 안전하게 처리하지 못함


def _digits(value: str) -> str:
    return re.sub(r"\D", "", value)


def _luhn_ok(number: str) -> bool:
    digits = [int(d) for d in number]
    checksum = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        checksum += d
    return checksum % 10 == 0


def _near(text: str, start: int, end: int, pattern: re.Pattern, before: int = 18, after: int = 10) -> bool:
    return bool(pattern.search(text[max(0, start - before):min(len(text), end + after)]))


def find_pii(text: str) -> list[PIIMatch]:
    """주민번호 → 카드 → 전화번호 → 계좌 순으로 겹치지 않게 찾는다."""
    if not text:
        return []
    found: list[PIIMatch] = []
    taken: list[tuple[int, int]] = []

    def free(s: int, e: int) -> bool:
        return all(e <= ts or s >= te for ts, te in taken)

    def add(kind: PIIKind, m: re.Match) -> None:
        if free(m.start(), m.end()):
            found.append(PIIMatch(kind, m.start(), m.end(), m.group(0)))
            taken.append((m.start(), m.end()))

    for m in _RRN_HYPHEN.finditer(text):
        add("rrn", m)
    for m in _RRN_BARE.finditer(text):
        if _near(text, m.start(), m.end(), _RRN_CONTEXT):
            add("rrn", m)
    for m in _CARD_GROUPED.finditer(text):
        add("card", m)
    for m in _CARD_BARE.finditer(text):
        if _luhn_ok(m.group(0)) or _near(text, m.start(), m.end(), _CARD_CONTEXT):
            add("card", m)
    for m in _PHONE.finditer(text):
        add("phone", m)
    for m in _ACCOUNT_GROUPED.finditer(text):
        if 10 <= len(_digits(m.group(0))) <= 16:
            add("account", m)
    for m in _ACCOUNT_BARE.finditer(text):
        if _near(text, m.start(), m.end(), _ACCOUNT_CONTEXT):
            add("account", m)
    return sorted(found, key=lambda x: x.start)


def find_unmasked_pii(text: str) -> list[str]:
    """아직 가려지지 않은 PII 종류 목록 (중복 제거, 발견 순서)."""
    return list(dict.fromkeys(m.kind for m in find_pii(text)))


# ---------------------------------------------------------------------------
# 붙여넣은 원문 / 사용자 진술 구분
# ---------------------------------------------------------------------------
def external_spans(text: str) -> list[tuple[int, int]]:
    """붙여넣은 원문으로 보이는 구간. guards.py의 원문 격리 기준이 확정되면 이 함수를 교체한다."""
    spans: list[tuple[int, int]] = []
    pos = 0
    lines = text.splitlines(keepends=True)
    in_block = False
    for line in lines:
        start, end = pos, pos + len(line)
        if _EXTERNAL_HEADER.match(line):
            in_block = True
        elif not line.strip():
            in_block = False
        if in_block:
            spans.append((start, end))
        else:
            colon = _EXTERNAL_COLON.search(line)
            if colon:
                spans.append((start + colon.end(), end))
        pos = end
    for m in _QUOTED.finditer(text):
        spans.append((m.start(), m.end()))
    return spans


def _in_spans(start: int, end: int, spans: list[tuple[int, int]]) -> bool:
    return any(s <= start and end <= e for s, e in spans)


# ---------------------------------------------------------------------------
# 마스킹 · 토큰화 · 복원
# ---------------------------------------------------------------------------
def _normalize(kind: str, value: str) -> str:
    digits = _digits(value)
    if kind == "phone" and digits.startswith("82"):
        digits = "0" + digits[2:]
    return digits


def _token_for(kind: str, value: str, vault: dict[str, str]) -> str:
    key = _normalize(kind, value)
    prefix = TOKEN_PREFIX[kind]
    for token, raw in vault.items():
        if token.startswith(f"<{prefix}_") and _normalize(kind, raw) == key:
            return token
    count = sum(1 for token in vault if token.startswith(f"<{prefix}_"))
    token = f"<{prefix}_{count + 1}>"
    vault[token] = value
    return token


_SCAMMER_AFTER = re.compile(r"^\s*(?:으로|로|에|한테|에게)?\s*(?:보냈|보내|입금|이체|송금|전화\s*(?:왔|옴|온)|문자\s*(?:왔|옴|온)|연락\s*(?:왔|옴|온))")


def _owner(text: str, m: PIIMatch, spans: list[tuple[int, int]]) -> Literal["scammer", "victim"]:
    """계좌·전화번호가 누구 것인지. 붙여넣은 원문이면 사기범, 아니면 번호에 가장 가까운 단서를 따른다.

    단서가 없으면 사기범 측으로 본다. 이 서비스에서 사용자가 적는 번호는 대부분 연락해 온 쪽이고,
    본인 번호를 토큰화해도 모델에는 토큰만 가므로 노출되지 않는다.
    """
    if _in_spans(m.start, m.end, spans):
        return "scammer"
    if _SCAMMER_AFTER.match(text[m.end:m.end + 14]):
        return "scammer"
    window_start = max(0, m.start - 18)
    window = text[window_start:m.start]
    last_victim = max((x.end() for x in _VICTIM_CUE.finditer(window)), default=-1)
    last_scammer = max((x.end() for x in _SCAMMER_CUE.finditer(window)), default=-1)
    if last_victim > last_scammer:
        return "victim"
    return "scammer"


def mask_text(
    text: str,
    vault: dict[str, str] | None = None,
    *,
    external_splitter: Callable[[str], list[tuple[int, int]]] | None = None,
) -> MaskResult:
    """text 안의 PII를 가리고 토큰화한다. vault는 복사본을 만들어 반환하므로 원본을 바꾸지 않는다."""
    new_vault = dict(vault or {})
    if not text:
        return MaskResult(text=text or "", vault=new_vault)

    matches = find_pii(text)
    if not matches:
        return MaskResult(text=text, vault=new_vault)

    spans = (external_splitter or external_spans)(text)
    parts: list[str] = []
    cursor = 0
    masked: list[str] = []
    tokenized: list[str] = []
    for m in matches:
        parts.append(text[cursor:m.start])
        if m.kind in ("rrn", "card"):
            replacement = MASK_LABELS[m.kind]
            masked.append(m.kind)
        elif _owner(text, m, spans) == "scammer":
            replacement = _token_for(m.kind, m.value, new_vault)
            tokenized.append(replacement)
        else:
            replacement = MASK_LABELS[m.kind]
            masked.append(m.kind)
        parts.append(replacement)
        cursor = m.end
    parts.append(text[cursor:])
    result_text = "".join(parts)

    leftover = find_unmasked_pii(result_text)
    blocked = any(kind in ("rrn", "card") for kind in leftover)
    return MaskResult(text=result_text, vault=new_vault, masked=masked,
                      tokenized=list(dict.fromkeys(tokenized)), blocked=blocked)


def restore_tokens(text: str, vault: dict[str, str] | None) -> str:
    """토큰을 원문으로 되돌린다. 정리서 표시·신고 접수에서만 쓴다. 모델 입력에는 절대 쓰지 않는다."""
    if not text or not vault:
        return text or ""
    return TOKEN_PATTERN.sub(lambda m: vault.get(m.group(0), m.group(0)), text)


def resolve_token(token: str, vault: dict[str, str] | None) -> str | None:
    """토큰 하나의 원문. 없으면 None (Tool은 None이면 '확인 불가'로 처리)."""
    if not vault:
        return None
    return vault.get(token.strip())


# ---------------------------------------------------------------------------
# 메시지 처리
# ---------------------------------------------------------------------------
def _mask_content(content: Any, vault: dict[str, str], splitter) -> tuple[Any, dict[str, str], bool, bool]:
    """메시지 content(str 또는 블록 리스트)를 마스킹. (새 content, vault, 바뀌었나, 차단해야 하나)"""
    if isinstance(content, str):
        result = mask_text(content, vault, external_splitter=splitter)
        return result.text, result.vault, result.text != content, result.blocked
    if isinstance(content, list):
        changed = blocked = False
        new_blocks = []
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                result = mask_text(block["text"], vault, external_splitter=splitter)
                vault = result.vault
                changed |= result.text != block["text"]
                blocked |= result.blocked
                new_blocks.append({**block, "text": result.text})
            else:
                new_blocks.append(block)
        return new_blocks, vault, changed, blocked
    return content, vault, False, False


def _has_rrn_or_card(content: Any) -> bool:
    texts = [content] if isinstance(content, str) else [
        b.get("text", "") for b in content if isinstance(b, dict)
    ] if isinstance(content, list) else []
    return any(_RRN_HYPHEN.search(t) or _CARD_GROUPED.search(t) for t in texts if t)


class PIIMiddleware(AgentMiddleware[UnHookState, RuntimeContext]):
    """G4. 모든 모델 호출 전에 PII를 가리고, 사기범 측 번호는 pii_vault에 토큰으로 보관한다.

    - before_agent: 이번 입력을 가장 먼저 가린다. 이 미들웨어를 목록 맨 앞에 두면
      인젝션 판별(nano)·로그·Checkpointer 모두 가려진 텍스트만 본다.
    - before_model: 매 모델 호출 전 사람·Tool 메시지를 한 번 더 검사하는 안전망.
    - wrap_tool_call: 지정한 Tool 인자의 토큰만 실행 직전에 원문으로 바꾼다 (TOOL_ARGS_TO_RESTORE).
    - 주민번호·카드번호를 안전하게 처리하지 못하면 입력을 차단하고 턴을 끝낸다. 그 외는 통과 + 로그.

    external_splitter: guards.py의 원문 구간 판별 함수를 넘기면 그 기준을 따른다.
    """

    state_schema = UnHookState

    def __init__(
        self,
        external_splitter: Callable[[str], list[tuple[int, int]]] | None = None,
        tool_args_to_restore: dict[str, tuple[str, ...]] | None = None,
    ) -> None:
        super().__init__()
        self.external_splitter = external_splitter
        self.tool_args_to_restore = TOOL_ARGS_TO_RESTORE if tool_args_to_restore is None else tool_args_to_restore

    def _scan(self, state: UnHookState, kinds: tuple[type, ...]) -> dict[str, Any] | None:
        vault = dict(state.get("pii_vault") or {})
        replaced: list[BaseMessage] = []
        block = False
        for message in state.get("messages", []):
            if not isinstance(message, kinds):
                continue
            try:
                content, vault, changed, blocked = _mask_content(message.content, vault, self.external_splitter)
            except Exception:  # 마스킹 자체가 실패한 경우
                logger.exception("PII 마스킹 중 오류")
                if _has_rrn_or_card(message.content):
                    block = True
                    replaced.append(message.model_copy(update={"content": BLOCKED_PLACEHOLDER}))
                continue
            if blocked:
                block = True
                replaced.append(message.model_copy(update={"content": BLOCKED_PLACEHOLDER}))
            elif changed:
                replaced.append(message.model_copy(update={"content": content}))

        if not replaced:
            return None
        logger.info("PII 처리: 메시지 %d개, 토큰 %d개", len(replaced), len(vault))
        update: dict[str, Any] = {"messages": replaced, "pii_vault": vault}
        if block:
            logger.warning("주민번호·카드번호 처리 실패로 입력 차단")
            update["messages"] = replaced + [AIMessage(content=BLOCKED_NOTICE)]
            update["jump_to"] = "end"
        return update

    @hook_config(can_jump_to=["end"])
    def before_agent(self, state: UnHookState, runtime) -> dict[str, Any] | None:
        return self._scan(state, (HumanMessage,))

    @hook_config(can_jump_to=["end"])
    def before_model(self, state: UnHookState, runtime) -> dict[str, Any] | None:
        return self._scan(state, (HumanMessage, ToolMessage))


    def wrap_tool_call(self, request, handler):
        names = self.tool_args_to_restore.get(request.tool_call.get("name"), ())
        vault = (request.state or {}).get("pii_vault") or {}
        args = dict(request.tool_call.get("args") or {})
        restored = {k: restore_tokens(v, vault) for k, v in args.items() if k in names and isinstance(v, str)}
        if restored and any(restored[k] != args[k] for k in restored):
            logger.info("Tool 인자 토큰 복원: %s(%s)", request.tool_call.get("name"), ", ".join(restored))
            request = request.override(tool_call={**request.tool_call, "args": {**args, **restored}})
        return handler(request)


__all__ = [
    "PIIKind", "PIIMatch", "MaskResult", "MASK_LABELS", "TOKEN_PATTERN",
    "find_pii", "find_unmasked_pii", "external_spans", "mask_text",
    "restore_tokens", "resolve_token", "TOOL_ARGS_TO_RESTORE", "PIIMiddleware",
]
