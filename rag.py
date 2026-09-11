"""Playbook retrieval (RAG) for 작업 묶음 6 (agent-design.md 2.5, 5절).

`tools.py`의 `get_scam_playbook` Tool이 `search_playbook()`에 검색을 위임한다.
Tool 쪽은 예외를 잡지 않으므로 이 모듈의 `search_playbook()`은 어떤 실패에서도
예외를 밖으로 내지 않고 로컬 JSON 폴백으로 `PlaybookResult`를 채워 돌려준다
(설계서 2.5 에러 처리, 1.5 안정성).

데이터 (설계서 2.5 RAG 구성):
- `data/playbook_docs/*.md`  출처별 원문. YAML 헤더(`source`, `url`, ...) 뒤에
  `## <step_key>` 섹션이 이어지고, 각 섹션 첫머리에 `scam_types` / `damage_stages`
  / `contacts` 세 줄, 그 아래가 본문이다. 섹션 하나가 청크 하나가 된다.
- `data/playbook_fallback.json`  `step_keys`(checklist 키와 동일한 정규 이름),
  `step_order[damage_stage]`(단계별 안내 순서표), `by_damage_stage`(폴백 절차).
- `data/contacts.json`  연락처 테이블. 청크와 폴백은 `id`로 참조하고, 결과에는
  `label`만 내보내므로 테이블에 없는 연락처가 나올 수 없다.

검색은 유형+단계 일치 → 단계 일치·공통(any) 청크 → 폴백의 3단계로 완화하고, 최종 순서는
유사도가 아니라 `step_order`로 정렬한다 (4.2 TS-02-C003의 절차 순서를 임베딩
노이즈로부터 보호). 벡터 스토어는 모듈 전역에 1회 적재·캐싱한다 (1.5 성능).
"""

from __future__ import annotations

import json
import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

import yaml
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_core.vectorstores import InMemoryVectorStore

from schemas import PlaybookResult

logger = logging.getLogger(__name__)

# 설계서 2.5 RAG 구성에서 확정한 값. config.py는 공통 파일이므로 여기에 둔다 (5절).
EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_TIMEOUT = 10  # 초. 설계서 1.5 성능: 외부 API에는 Timeout을 둔다.
EMBEDDING_MAX_RETRIES = 2
# 코사인 유사도 하한. text-embedding-3-small 실측(2026-09-11): 필터 통과 청크 최저 0.257,
# 중앙값 0.41. 관련 여부는 메타데이터 필터가 가르고, 이 값은 깨진 결과를 거르는 안전망이다.
PLAYBOOK_SCORE_THRESHOLD = 0.25
PLAYBOOK_TOP_K = 5  # ScamAssessment.immediate_actions 상한과 동일 (2.4).
# step_key 중복 제거 전에 넉넉히 가져온다. 필터는 선필터라 비용 차이는 없다.
_FETCH_K = 20
_STEP_SEPARATOR = " — "

DATA_DIR = Path(__file__).resolve().parent / "data"
PLAYBOOK_DOCS_DIR = DATA_DIR / "playbook_docs"
PLAYBOOK_FALLBACK_PATH = DATA_DIR / "playbook_fallback.json"
CONTACTS_PATH = DATA_DIR / "contacts.json"

# 검색 쿼리를 한국어 문장으로 만들기 위한 표기. schemas.ScamType / DamageStage 값 기준.
_SCAM_TYPE_KO = {
    "smishing": "스미싱 문자",
    "voice_phishing": "보이스피싱 전화",
    "messenger_phishing": "메신저 피싱",
    "loan_scam": "대출 사기",
    "gov_impersonation": "수사기관·정부기관 사칭",
    "investment_scam": "투자 사기",
    "unknown": "금융사기",
}
_DAMAGE_STAGE_KO = {
    "none": "아직 피해가 없는",
    "link_clicked": "링크를 클릭한",
    "info_exposed": "개인정보를 알려준",
    "app_installed": "앱을 설치한",
    "money_sent": "이미 송금한",
}

_ANY = "any"
_FRONT_MATTER = re.compile(r"\A---\n(.*?)\n---\n(.*)\Z", re.DOTALL)
_SECTION_SPLIT = re.compile(r"^## ", re.MULTILINE)
_META_LINE = re.compile(r"^[a-z_]+:\s")

# 모듈 전역 캐시 (설계서 2.5 사전 로딩). 적재는 성공·실패와 무관하게 1회만 시도한다.
_INDEX: InMemoryVectorStore | None = None
_LOAD_ATTEMPTED = False
_CHUNK_COUNT = 0


# ---------------------------------------------------------------------------
# 데이터 로딩
# ---------------------------------------------------------------------------

def _read_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fp:
        return json.load(fp)


# 정적 로컬 데이터는 1회만 읽는다 (설계서 1.5 성능).
@lru_cache(maxsize=1)
def _load_fallback() -> dict[str, Any]:
    return _read_json(PLAYBOOK_FALLBACK_PATH)


@lru_cache(maxsize=1)
def _load_contact_labels() -> dict[str, str]:
    """연락처 id → label. 결과에는 label만 나간다 (4.2 연락처 테이블 일치)."""
    data = _read_json(CONTACTS_PATH)
    labels: dict[str, str] = {}
    for entry in data.get("agencies", []) + data.get("sites", []):
        labels[entry["id"]] = entry["label"]
    return labels


def _parse_playbook_file(path: Path) -> list[Document]:
    """원문 파일 하나를 `## step_key` 단위 청크로 나눈다."""
    text = path.read_text(encoding="utf-8")
    match = _FRONT_MATTER.match(text)
    if not match:
        raise ValueError(f"{path.name}: YAML 헤더가 없다")
    header = yaml.safe_load(match.group(1)) or {}
    docs: list[Document] = []
    for section in _SECTION_SPLIT.split(match.group(2))[1:]:
        title, _, rest = section.partition("\n")
        lines = rest.split("\n")
        meta_lines: list[str] = []
        while lines and _META_LINE.match(lines[0]):
            meta_lines.append(lines.pop(0))
        meta = yaml.safe_load("\n".join(meta_lines)) or {}
        body = " ".join(" ".join(lines).split())
        step_key = title.strip()
        if not body or not step_key:
            raise ValueError(f"{path.name}: '{step_key}' 섹션에 본문이 없다")
        docs.append(Document(
            page_content=f"{step_key}. {body}",
            metadata={
                "step_key": step_key,
                "scam_types": list(meta.get("scam_types") or [_ANY]),
                "damage_stages": list(meta.get("damage_stages") or []),
                "contacts": list(meta.get("contacts") or []),
                "source": header.get("source", path.stem),
                "url": header.get("url", ""),
                "description": body,
            },
        ))
    return docs


def load_playbook_documents() -> list[Document]:
    """`data/playbook_docs/*.md` 전체를 청크 목록으로 읽는다. 검증 스크립트·테스트용."""
    docs: list[Document] = []
    for path in sorted(PLAYBOOK_DOCS_DIR.glob("*.md")):
        docs.extend(_parse_playbook_file(path))
    return docs


def _default_embeddings() -> Embeddings:
    # 지연 import: 테스트는 가짜 임베딩을 주입하므로 langchain_openai가 없어도 된다.
    from langchain_openai import OpenAIEmbeddings

    return OpenAIEmbeddings(
        model=EMBEDDING_MODEL,
        timeout=EMBEDDING_TIMEOUT,
        max_retries=EMBEDDING_MAX_RETRIES,
    )


def load_playbook_index(embeddings: Embeddings | None = None, force: bool = False) -> None:
    """벡터 스토어를 1회 적재한다. 앱 시작 시 호출하고, 실패해도 예외를 내지 않는다.

    적재에 실패하면 `search_playbook()`은 폴백 JSON으로만 동작한다.
    `embeddings`를 주면 그 임베딩을 쓴다 (테스트는 가짜 임베딩 주입).
    """
    global _INDEX, _LOAD_ATTEMPTED, _CHUNK_COUNT
    if _LOAD_ATTEMPTED and not force:
        return
    _LOAD_ATTEMPTED = True
    _INDEX = None
    _CHUNK_COUNT = 0
    try:
        docs = load_playbook_documents()
        if not docs:
            raise ValueError("playbook_docs에 청크가 없다")
        _INDEX = InMemoryVectorStore.from_documents(docs, embeddings or _default_embeddings())
        _CHUNK_COUNT = len(docs)
        logger.info("playbook index loaded: %d chunks", _CHUNK_COUNT)
    except Exception:  # noqa: BLE001 - 적재 실패는 폴백으로 흡수한다 (1.5 안정성).
        logger.warning("playbook index load failed; using fallback only", exc_info=True)


def is_index_loaded() -> bool:
    return _INDEX is not None


def reset_index() -> None:
    """캐시를 비운다. 테스트에서 적재 상태를 초기화할 때만 쓴다."""
    global _INDEX, _LOAD_ATTEMPTED, _CHUNK_COUNT
    _INDEX, _LOAD_ATTEMPTED, _CHUNK_COUNT = None, False, 0


# ---------------------------------------------------------------------------
# 검색
# ---------------------------------------------------------------------------

def _format_step(step_key: str, description: str) -> str:
    return f"{step_key}{_STEP_SEPARATOR}{description}"


def _resolve_contacts(contact_ids: list[str], labels: dict[str, str]) -> list[str]:
    seen: list[str] = []
    for cid in contact_ids:
        label = labels.get(cid)
        if label and label not in seen:
            seen.append(label)
    return seen


def _fallback_result(damage_stage: str) -> PlaybookResult:
    """로컬 JSON 공통 절차. 여기서도 실패하면 빈 결과를 돌려준다 (tools.py 미구현 시와 동일)."""
    try:
        fallback = _load_fallback()
        labels = _load_contact_labels()
        entries = fallback["by_damage_stage"].get(damage_stage)
        if entries is None:
            entries = fallback["by_damage_stage"]["none"]
        steps = [_format_step(e["step_key"], e["description"]) for e in entries][:PLAYBOOK_TOP_K]
        contact_ids = [cid for e in entries for cid in e.get("contacts", [])]
        return PlaybookResult(steps=steps, contacts=_resolve_contacts(contact_ids, labels))
    except Exception:  # noqa: BLE001
        logger.error("playbook fallback unavailable", exc_info=True)
        return PlaybookResult(steps=[], contacts=[])


def _common_stage_filter(damage_stage: str) -> Callable[[Document], bool]:
    """단계 일치 + 전 유형 공통(`any`) 청크만. 유형을 모를 때 다른 유형 전용 설명을 섞지 않는다."""
    return lambda doc: (
        damage_stage in doc.metadata.get("damage_stages", [])
        and _ANY in doc.metadata.get("scam_types", [])
    )


def _type_and_stage_filter(scam_type: str, damage_stage: str) -> Callable[[Document], bool]:
    def _match(doc: Document) -> bool:
        types = doc.metadata.get("scam_types", [])
        return (
            damage_stage in doc.metadata.get("damage_stages", [])
            and (_ANY in types or scam_type in types)
        )

    return _match


def _build_query(scam_type: str, damage_stage: str) -> str:
    type_ko = _SCAM_TYPE_KO.get(scam_type, _SCAM_TYPE_KO["unknown"])
    stage_ko = _DAMAGE_STAGE_KO.get(damage_stage, "")
    # 신고·기관 쪽으로만 치우치지 않게 행동 동사를 열거한다. 실측에서 "대응 절차와 신고 기관"
    # 문구는 KISA의 기기 위생 단계(링크 클릭 금지·악성앱 삭제·소액결제 차단) 점수를 떨어뜨렸다.
    return f"{type_ko}. {stage_ko} 상황. 지금 할 일: 차단, 삭제, 확인, 신고, 지급정지, 변경"


def _search_chunks(query: str, doc_filter: Callable[[Document], bool]) -> list[tuple[Document, float]]:
    assert _INDEX is not None
    hits = _INDEX.similarity_search_with_score(query, k=_FETCH_K, filter=doc_filter)
    return [(doc, score) for doc, score in hits if score >= PLAYBOOK_SCORE_THRESHOLD]


def _compose_result(
    hits: list[tuple[Document, float]], step_order: list[str], labels: dict[str, str],
) -> PlaybookResult:
    # 같은 step_key는 최고 점수 1건만 남긴다. hits는 점수 내림차순이다.
    best: dict[str, Document] = {}
    for doc, _score in hits:
        best.setdefault(doc.metadata["step_key"], doc)

    def _order(step_key: str) -> int:
        return step_order.index(step_key) if step_key in step_order else len(step_order)

    ordered = sorted(best.values(), key=lambda d: _order(d.metadata["step_key"]))[:PLAYBOOK_TOP_K]
    steps = [_format_step(d.metadata["step_key"], d.metadata["description"]) for d in ordered]
    contact_ids = [cid for d in ordered for cid in d.metadata.get("contacts", [])]
    return PlaybookResult(steps=steps, contacts=_resolve_contacts(contact_ids, labels))


def search_playbook(scam_type: str, damage_stage: str) -> PlaybookResult:
    """사기 유형·피해 단계에 맞는 절차와 연락처를 찾는다. 어떤 경우에도 예외를 내지 않는다."""
    try:
        if damage_stage not in _DAMAGE_STAGE_KO:
            logger.warning("unknown damage_stage %r; using 'none' fallback", damage_stage)
            return _fallback_result("none")
        if not _LOAD_ATTEMPTED:
            load_playbook_index()
        if _INDEX is None:
            return _fallback_result(damage_stage)

        fallback = _load_fallback()
        labels = _load_contact_labels()
        step_order = fallback["step_order"].get(damage_stage, [])
        query = _build_query(scam_type, damage_stage)

        # ① 유형 + 단계 일치. unknown 유형은 유형 필터를 걸 수 없으므로 ②부터.
        hits: list[tuple[Document, float]] = []
        if scam_type in _SCAM_TYPE_KO and scam_type != "unknown":
            hits = _search_chunks(query, _type_and_stage_filter(scam_type, damage_stage))
        # ② 단계 일치 + 공통 절차(any)만.
        if not hits:
            hits = _search_chunks(query, _common_stage_filter(damage_stage))
        # ③ 폴백.
        if not hits:
            return _fallback_result(damage_stage)

        result = _compose_result(hits, step_order, labels)
        if not result["contacts"]:
            result["contacts"] = _fallback_result(damage_stage)["contacts"]
        return result
    except Exception:  # noqa: BLE001 - tools.get_scam_playbook은 예외를 잡지 않는다.
        logger.warning("search_playbook failed; using fallback", exc_info=True)
        return _fallback_result(damage_stage if damage_stage in _DAMAGE_STAGE_KO else "none")
