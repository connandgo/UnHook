"""pii.py — 개인정보 마스킹 및 토큰화 (설계서 3.1 pii_vault, 3.2 PIIMiddleware, 3.3 G4, 5.1)

규칙
- 주민등록번호·카드번호: 누구의 것이든 라벨로 가린다. 원문은 어디에도 저장하지 않는다.
- 계좌번호·전화번호
  - 외부 원문(external_texts)에 있으면 사기범 측으로 보고 <SCAM_ACCOUNT_1> 같은 토큰으로 바꾼다.
    원문은 State.pii_vault에만 둔다.
  - 사용자 진술이나 구분되지 않은 입력(mixed)에서는 번호 바로 앞뒤 단서로 판단한다.
    "내 계좌", "제 번호"면 라벨로 가리고 저장하지 않는다. 그 외에는 사기범 측으로 본다.
  - 본문의 머리말·따옴표·구분자로 출처를 추정하지 않는다 (5.1).
- URL에 퍼센트 인코딩된 번호도 한 번 디코딩해 가린다 (5.1).
- 모델에는 토큰이나 라벨만 전달한다. 원문 복원은 정리서·신고 단계에서 restore_tokens()로 한다.
- 로그에는 원문을 남기지 않는다.

연결 API
- prepare_masked_input(user_statement, external_texts=None, *, vault=None) -> PreparedInput
    앱·Agent가 invoke 전에 호출한다. guards.prepare_guarded_message로 만든 메시지와 갱신된 vault를 돌려준다.
- make_guard_masker(vault) -> Callable[[str], str]
    guards.prepare_guarded_message(mask_text=...)에 직접 넘기는 문자열 어댑터 (mixed 기준, vault를 제자리 갱신).
- PIIMiddleware
    before_agent·before_model 안전망, wrap_tool_call에서 지정 Tool 인자만 원문 복원.
- neutralize_tokens(text)
    대화 밖(Store 등)에 저장할 문장에서 토큰 번호를 없앤다. 토큰 번호는 대화마다 다시 시작하기 때문이다.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import unquote

from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from state import RuntimeContext, UnHookState

logger = logging.getLogger("unhook.pii")

PIIKind = Literal["rrn", "card", "account", "phone"]
Source = Literal["user", "external", "mixed"]

MASK_LABELS: dict[str, str] = {
    "rrn": "[주민등록번호 가림]",
    "card": "[카드번호 가림]",
    "account": "[본인 계좌번호 가림]",
    "phone": "[본인 전화번호 가림]",
}
TOKEN_PREFIX: dict[str, str] = {"account": "SCAM_ACCOUNT", "phone": "SCAM_PHONE"}
TOKEN_PATTERN = re.compile(r"<SCAM_(?:ACCOUNT|PHONE)_\d+>")
NEUTRAL_TOKEN_LABELS: dict[str, str] = {"ACCOUNT": "[사기범 계좌]", "PHONE": "[사기범 번호]"}

# 실행 직전에 토큰을 원문으로 바꿀 Tool 인자
# - verify_caller_number.phone: 실제 번호로 조회해야 함
# - report_to_authority.target: memory.build_record가 번호를 해시로 저장하려면 원문이 필요함
TOOL_ARGS_TO_RESTORE: dict[str, tuple[str, ...]] = {
    "verify_caller_number": ("phone",),
    "report_to_authority": ("target",),
}

BLOCKED_NOTICE = "주민등록번호나 카드번호가 들어간 내용은 안전하게 처리하지 못했어요. 번호를 빼고 다시 입력해 주세요."
BLOCKED_PLACEHOLDER = "[개인정보 보호를 위해 입력이 차단되었습니다]"

_GUARD_INPUT_KEY = "unhook_input_version"  # guards.prepare_guarded_message가 붙이는 표시


class PIIMaskingError(ValueError):
    """주민번호·카드번호를 안전하게 가리지 못함. 메시지에 원문을 넣지 않는다."""


# ---------------------------------------------------------------------------
# 탐지
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

_VICTIM_CUE = re.compile(r"(?:내|제|저의|나의|본인|우리)\s*(?:명의\s*)?(?:계좌|통장|번호|폰|휴대폰|핸드폰|전화번호)")
_SCAMMER_CUE = re.compile(
    r"보냈|보낸|보내라|입금|이체|송금|받는\s*사람|받는\s*계좌|사기|그쪽|상대|저쪽|수사관|검사|직원|사칭|"
    r"에서\s*(?:전화|문자|연락)|로\s*(?:보내|입금|이체)|전화\s*(?:왔|옴|온|받)|문자\s*(?:왔|옴|온|받)|연락\s*(?:왔|옴|온)"
)
_SCAMMER_AFTER = re.compile(
    r"^\s*(?:으로|로|에|한테|에게)?\s*(?:보냈|보내|입금|이체|송금|전화\s*(?:왔|옴|온)|문자\s*(?:왔|옴|온)|연락\s*(?:왔|옴|온))"
)
_ENCODED_CHUNK = re.compile(r"[^\s\"'<>]*%[0-9A-Fa-f]{2}[^\s\"'<>]*")


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
    tokenized: list[str] = field(default_factory=list)   # 사용한 토큰
    blocked: bool = False                                  # 주민번호·카드번호가 남음


@dataclass
class PreparedInput:
    message: HumanMessage
    vault: dict[str, str]


def _digits(value: str) -> str:
    return re.sub(r"\D", "", value)


def _luhn_ok(number: str) -> bool:
    total = 0
    for i, d in enumerate(reversed([int(c) for c in number])):
        if i % 2 == 1:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def _near(text: str, start: int, end: int, pattern: re.Pattern, before: int = 18, after: int = 10) -> bool:
    return bool(pattern.search(text[max(0, start - before):min(len(text), end + after)]))


def find_pii(text: str) -> list[PIIMatch]:
    """주민번호 → 카드 → 전화번호 → 계좌 순으로 겹치지 않게 찾는다."""
    if not text:
        return []
    found: list[PIIMatch] = []
    taken: list[tuple[int, int]] = []

    def add(kind: PIIKind, m: re.Match) -> None:
        if all(m.end() <= s or m.start() >= e for s, e in taken):
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
    """아직 가려지지 않은 PII 종류 목록 (URL 인코딩 포함, 중복 제거)."""
    return list(dict.fromkeys(m.kind for m in find_pii(_decode_encoded_pii(text or ""))))


# ---------------------------------------------------------------------------
# 마스킹 · 토큰화 · 복원
# ---------------------------------------------------------------------------
def _decode_encoded_pii(text: str) -> str:
    """퍼센트 인코딩된 조각을 한 번 디코딩해 PII가 드러나면 디코딩본으로 바꾼다. 없으면 그대로."""
    def replace(m: re.Match) -> str:
        chunk = m.group(0)
        decoded = unquote(chunk)
        return decoded if decoded != chunk and find_pii(decoded) else chunk
    return _ENCODED_CHUNK.sub(replace, text)


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


def _owner(text: str, m: PIIMatch, source: Source) -> Literal["scammer", "victim"]:
    """계좌·전화번호의 주인. 외부 원문이면 사기범, 아니면 번호에 가장 가까운 단서를 따른다."""
    if source == "external":
        return "scammer"
    if _SCAMMER_AFTER.match(text[m.end:m.end + 14]):
        return "scammer"
    window = text[max(0, m.start - 18):m.start]
    last_victim = max((x.end() for x in _VICTIM_CUE.finditer(window)), default=-1)
    last_scammer = max((x.end() for x in _SCAMMER_CUE.finditer(window)), default=-1)
    return "victim" if last_victim > last_scammer else "scammer"


def mask_text(text: str, vault: dict[str, str] | None = None, *, source: Source = "mixed") -> MaskResult:
    """PII를 가리고 사기범 측 번호를 토큰화한다. vault는 복사본을 만들어 반환하므로 원본을 바꾸지 않는다.

    source: "external"(외부 원문), "user"(분리된 사용자 진술), "mixed"(구분되지 않은 입력)
    """
    new_vault = dict(vault or {})
    if not text:
        return MaskResult(text=text or "", vault=new_vault)

    working = _decode_encoded_pii(text)
    matches = find_pii(working)
    if not matches:
        return MaskResult(text=text, vault=new_vault)

    parts: list[str] = []
    cursor = 0
    masked: list[str] = []
    tokenized: list[str] = []
    for m in matches:
        parts.append(working[cursor:m.start])
        if m.kind in ("rrn", "card"):
            replacement = MASK_LABELS[m.kind]
            masked.append(m.kind)
        elif _owner(working, m, source) == "scammer":
            replacement = _token_for(m.kind, m.value, new_vault)
            tokenized.append(replacement)
        else:
            replacement = MASK_LABELS[m.kind]
            masked.append(m.kind)
        parts.append(replacement)
        cursor = m.end
    parts.append(working[cursor:])
    result_text = "".join(parts)

    blocked = any(kind in ("rrn", "card") for kind in find_unmasked_pii(result_text))
    return MaskResult(text=result_text, vault=new_vault, masked=masked,
                      tokenized=list(dict.fromkeys(tokenized)), blocked=blocked)


def restore_tokens(text: str, vault: dict[str, str] | None) -> str:
    """토큰을 원문으로 되돌린다. 정리서 표시·신고 접수·이력 대조에서만 쓰고 모델 입력에는 쓰지 않는다."""
    if not text or not vault:
        return text or ""
    return TOKEN_PATTERN.sub(lambda m: vault.get(m.group(0), m.group(0)), text)


def resolve_token(token: str, vault: dict[str, str] | None) -> str | None:
    """토큰 하나의 원문. 없으면 None."""
    return (vault or {}).get(token.strip())


def neutralize_tokens(text: str) -> str:
    """Store 등 대화 밖에 저장할 문장용. <SCAM_ACCOUNT_1> → [사기범 계좌]처럼 번호 없는 라벨로 바꾼다."""
    if not text:
        return text or ""
    return re.sub(r"<SCAM_(ACCOUNT|PHONE)_\d+>", lambda m: NEUTRAL_TOKEN_LABELS[m.group(1)], text)


# ---------------------------------------------------------------------------
# guards.py 연결
# ---------------------------------------------------------------------------
def make_guard_masker(vault: dict[str, str], *, source: Source = "mixed"):
    """guards.prepare_guarded_message(mask_text=...)에 넘기는 str -> str 어댑터.

    넘긴 vault dict를 제자리에서 갱신한다. 주민번호·카드번호가 남으면 PIIMaskingError를 던지고,
    guards는 이를 GuardInputError("PII masking failed")로 바꿔 앱에 전달한다.
    user_statement와 external_texts에 같은 기준을 쓰므로, 입력을 분리했다면 prepare_masked_input을 쓴다.
    """
    def masker(text: str) -> str:
        result = mask_text(text, vault, source=source)
        if result.blocked:
            raise PIIMaskingError("주민등록번호·카드번호 마스킹 실패")
        vault.update(result.vault)
        return result.text
    return masker


def _assert_masked(text: str) -> str:
    """이미 가린 텍스트를 guards에 넘길 때 쓰는 확인 함수. 원문 PII가 남아 있으면 실패시킨다."""
    if find_unmasked_pii(text):
        raise PIIMaskingError("마스킹되지 않은 개인정보가 남아 있음")
    return text


def prepare_masked_input(
    user_statement: str,
    external_texts: list[str] | None = None,
    *,
    vault: dict[str, str] | None = None,
) -> PreparedInput:
    """Agent invoke 전에 입력을 가리고 guards 형식의 메시지를 만든다.

    external_texts는 사기범 측 원문으로 보고 번호를 토큰화한다. 생략하면 guards 규칙대로 mixed 입력이다.
    반환된 vault를 invoke 입력의 pii_vault로 함께 넘겨야 토큰 복원이 가능하다.
    입력 오류·마스킹 실패는 guards.GuardInputError로 전달된다.
    """
    import guards  # guards가 pii를 import하게 되더라도 순환이 생기지 않도록 지연 import

    valid = isinstance(user_statement, str) and (
        external_texts is None
        or (not isinstance(external_texts, (str, bytes))
            and all(isinstance(part, str) for part in external_texts))
    )
    if not valid:  # 형식 오류는 원문을 건드리지 않고 guards의 오류 처리를 그대로 따른다
        guards.prepare_guarded_message(user_statement, mask_text=_assert_masked, external_texts=external_texts)

    new_vault = dict(vault or {})
    statement = mask_text(user_statement, new_vault, source="mixed" if external_texts is None else "user")
    new_vault = statement.vault
    externals: list[str] | None = None
    blocked = statement.blocked
    if external_texts is not None:
        externals = []
        for part in external_texts:
            result = mask_text(part, new_vault, source="external")
            new_vault = result.vault
            blocked = blocked or result.blocked
            externals.append(result.text)
    if blocked:
        raise guards.GuardInputError("PII masking failed")

    message = guards.prepare_guarded_message(statement.text, mask_text=_assert_masked, external_texts=externals)
    return PreparedInput(message=message, vault=new_vault)


# ---------------------------------------------------------------------------
# 미들웨어
# ---------------------------------------------------------------------------
def _mask_only_sensitive(text: str) -> tuple[str, bool]:
    """Tool 결과용: 주민번호·카드번호만 가린다. Tool이 돌려준 공식 대표번호를 사기범 토큰으로 바꾸지 않기 위함."""
    working = _decode_encoded_pii(text)
    parts, cursor = [], 0
    for m in find_pii(working):
        if m.kind in ("rrn", "card"):
            parts.append(working[cursor:m.start])
            parts.append(MASK_LABELS[m.kind])
            cursor = m.end
    if not parts:
        return text, False
    parts.append(working[cursor:])
    return "".join(parts), True


class PIIMiddleware(AgentMiddleware[UnHookState, RuntimeContext]):
    """G4 안전망. 입력은 prepare_masked_input에서 이미 가려지므로 여기서는 남은 원문을 막는다.

    - before_agent: 사람 메시지 검사. guards 형식(JSON)이면 필드별로 가리고 형식을 유지한다.
    - before_model: 사람 메시지 + Tool 결과(주민번호·카드번호만) 재검사.
    - wrap_tool_call / awrap_tool_call: TOOL_ARGS_TO_RESTORE에 지정한 인자의 토큰만 원문으로 복원.
    - 주민번호·카드번호를 가리지 못하면 해당 입력을 차단하고 턴을 끝낸다.
    목록 맨 앞에 등록한다.
    """

    state_schema = UnHookState

    def __init__(self, tool_args_to_restore: dict[str, tuple[str, ...]] | None = None) -> None:
        super().__init__()
        self.tool_args_to_restore = TOOL_ARGS_TO_RESTORE if tool_args_to_restore is None else tool_args_to_restore

    @staticmethod
    def _mask_human(message: HumanMessage, vault: dict[str, str]) -> tuple[BaseMessage | None, dict[str, str], bool]:
        content = message.content
        if message.additional_kwargs.get(_GUARD_INPUT_KEY) == 1 and isinstance(content, str):
            try:
                payload = json.loads(content)
            except (TypeError, ValueError):
                return None, vault, False  # 형식 오류는 guards가 GuardInputError로 처리한다
            separation = payload.get("separation")
            fields = [("user_statement", None, "mixed" if separation != "explicit" else "user")]
            fields += [("external_texts", i, "external") for i in range(len(payload.get("external_texts") or []))]
            changed = blocked = False
            for name, index, source in fields:
                original = payload[name] if index is None else payload[name][index]
                if not isinstance(original, str):
                    continue
                result = mask_text(original, vault, source=source)
                vault = result.vault
                new_text = BLOCKED_PLACEHOLDER if result.blocked else result.text
                blocked |= result.blocked
                if new_text != original:
                    changed = True
                    if index is None:
                        payload[name] = new_text
                    else:
                        payload[name][index] = new_text
            if not changed:
                return None, vault, blocked
            return message.model_copy(update={"content": json.dumps(payload, ensure_ascii=False)}), vault, blocked

        if isinstance(content, str):
            result = mask_text(content, vault)
            if result.blocked:
                return message.model_copy(update={"content": BLOCKED_PLACEHOLDER}), result.vault, True
            if result.text != content:
                return message.model_copy(update={"content": result.text}), result.vault, False
            return None, result.vault, False
        return None, vault, False

    def _scan(self, state: UnHookState, include_tools: bool) -> dict[str, Any] | None:
        vault = dict(state.get("pii_vault") or {})
        replaced: list[BaseMessage] = []
        block = False
        for message in state.get("messages", []):
            try:
                if isinstance(message, HumanMessage):
                    new_message, vault, blocked = self._mask_human(message, vault)
                elif include_tools and isinstance(message, ToolMessage) and isinstance(message.content, str):
                    text, changed = _mask_only_sensitive(message.content)
                    new_message, blocked = (message.model_copy(update={"content": text}) if changed else None), False
                else:
                    continue
            except Exception:
                logger.exception("PII 마스킹 중 오류 — 해당 메시지 차단")
                new_message = message.model_copy(update={"content": BLOCKED_PLACEHOLDER})
                blocked = True
            block |= blocked
            if new_message is not None:
                replaced.append(new_message)

        if not replaced and vault == (state.get("pii_vault") or {}):
            return None
        update: dict[str, Any] = {"pii_vault": vault}
        if replaced:
            update["messages"] = replaced
            logger.info("PII 안전망 처리: 메시지 %d개", len(replaced))
        if block:
            logger.warning("주민번호·카드번호 처리 실패로 입력 차단")
            update["messages"] = replaced + [AIMessage(content=BLOCKED_NOTICE)]
            update["jump_to"] = "end"
        return update

    @hook_config(can_jump_to=["end"])
    def before_agent(self, state: UnHookState, runtime) -> dict[str, Any] | None:
        return self._scan(state, include_tools=False)

    @hook_config(can_jump_to=["end"])
    def before_model(self, state: UnHookState, runtime) -> dict[str, Any] | None:
        return self._scan(state, include_tools=True)

    def _restore_args(self, request):
        names = self.tool_args_to_restore.get(request.tool_call.get("name"), ())
        if not names:
            return request
        vault = (request.state or {}).get("pii_vault") or {}
        args = dict(request.tool_call.get("args") or {})
        restored = {k: restore_tokens(v, vault) for k, v in args.items() if k in names and isinstance(v, str)}
        if any(restored[k] != args[k] for k in restored):
            logger.info("Tool 인자 토큰 복원: %s", request.tool_call.get("name"))
            request = request.override(tool_call={**request.tool_call, "args": {**args, **restored}})
        return request

    def wrap_tool_call(self, request, handler):
        return handler(self._restore_args(request))

    async def awrap_tool_call(self, request, handler):
        return await handler(self._restore_args(request))


__all__ = [
    "PIIKind", "Source", "PIIMatch", "MaskResult", "PreparedInput", "PIIMaskingError",
    "MASK_LABELS", "TOKEN_PATTERN", "TOOL_ARGS_TO_RESTORE",
    "find_pii", "find_unmasked_pii", "mask_text", "restore_tokens", "resolve_token", "neutralize_tokens",
    "make_guard_masker", "prepare_masked_input", "PIIMiddleware",
]
