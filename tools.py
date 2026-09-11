"""Tool implementations for 작업 묶음 5 (agent-design.md 2.5).

각 Tool의 docstring은 설계서 2.5의 문장을 그대로 사용한다. 모델이 "언제 이 Tool을
호출할지" 판단하는 근거이기 때문이다 (설계서 2.5 주석, AGENTS.md 작업 규칙).

Tool은 State를 직접 쓰지 않는다 (설계서 3.1 핵심 원칙). 조회 결과는 반환값으로만
돌려주고, State 반영은 미들웨어가 담당한다.
"""

from __future__ import annotations

import csv
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from langchain.tools import ToolRuntime, tool

import memory
from schemas import CallerVerificationResult, PlaybookResult, URLRiskResult

DATA_DIR = Path(__file__).resolve().parent / "data"
KISA_URL_CSV = DATA_DIR / "kisa_urls.csv"

# 설계서 1.5 성능: 외부 API에는 Timeout을 설정한다.
# 2.5: 타임아웃 3회 재시도 후 is_official=None.
FINLIFE_TIMEOUT_SECONDS = 5.0
FINLIFE_MAX_ATTEMPTS = 3
FINLIFE_ENDPOINT = "https://finlife.fss.or.kr/finlifeapi/companySearch.json"
# 환경변수 이름은 설계서 5절에 따라 이 파일의 담당자가 확정한다.
FINLIFE_API_KEY_ENV = "FSS_FINLIFE_API_KEY"

# check_url_risk 위험 신호 가중치. 합계는 0~100으로 자른다.
_SIGNAL_SCORES = {
    "blacklist": 100,
    "ip_host": 40,
    "suspicious_tld": 25,
    "shortener": 20,
    "lookalike": 25,
    "short_path": 10,
    "punycode": 30,
    "at_sign": 20,
}

# 설계서 1.3 S1: 비정상 TLD. 실습 범위에서 자주 쓰이는 목록만 둔다.
_SUSPICIOUS_TLDS = {
    "top", "xyz", "cc", "tk", "ml", "ga", "cf", "gq", "buzz", "rest",
    "click", "link", "work", "loan", "men", "kim", "icu", "shop",
}
_SHORTENER_HOSTS = {
    "bit.ly", "me2.do", "buly.kr", "url.kr", "vo.la", "han.gl", "tinyurl.com",
    "t.co", "goo.gl", "is.gd", "c11.kr", "abit.ly", "durl.me",
}
# 유사 도메인 판정 기준 브랜드. 사칭이 잦은 택배·금융·기관 키워드.
_LOOKALIKE_BRANDS = {
    "cj": "CJ대한통운", "cjlogistics": "CJ대한통운", "lotte": "롯데택배",
    "hanjin": "한진택배", "epost": "우체국", "koreapost": "우체국",
    "kakao": "카카오", "naver": "네이버", "kbstar": "국민은행",
    "shinhan": "신한은행", "wooribank": "우리은행", "hanabank": "하나은행",
    "nonghyup": "농협", "nhbank": "농협", "toss": "토스",
    "police": "경찰청", "gov": "정부기관", "fss": "금융감독원",
}
_LEGIT_SUFFIXES = (
    ".co.kr", ".go.kr", ".or.kr", ".ac.kr", ".re.kr", ".com", ".net", ".kr",
)

_IPV4_HOST_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")
_HOST_RE = re.compile(r"^[\w.-]+$")

_blacklist_cache: set[str] | None = None


# ---------------------------------------------------------------------------
# check_url_risk
# ---------------------------------------------------------------------------

def load_blacklist(path: Path | None = None) -> set[str]:
    """KISA 피싱사이트 URL CSV를 1회만 읽어 캐싱한다 (설계서 1.5 성능).

    파일이 없거나 읽기에 실패하면 빈 집합으로 진행한다. 조회 실패를 추측으로
    채우지 않기 위해 blacklisted는 False가 되고, 나머지 규칙 신호만 남는다.
    """
    global _blacklist_cache
    if path is None and _blacklist_cache is not None:
        return _blacklist_cache

    target = path or KISA_URL_CSV
    entries: set[str] = set()
    try:
        with target.open(encoding="utf-8-sig", newline="") as handle:
            # 헤더 앞의 주석(#) 줄을 걸러야 DictReader가 올바른 열 이름을 잡는다.
            rows = (line for line in handle if not line.lstrip().startswith("#"))
            for row in csv.DictReader(rows):
                raw = (row.get("url") or "").strip()
                if not raw or raw.startswith("#"):
                    continue
                host, _ = _split_url(raw)
                if host:
                    entries.add(host)
    except (OSError, csv.Error):
        entries = set()

    if path is None:
        _blacklist_cache = entries
    return entries


def _split_url(raw: str) -> tuple[str, str]:
    """URL에서 (호스트, 경로)를 뽑는다. 파싱 불가면 ("", "")."""
    candidate = raw.strip()
    if not candidate:
        return "", ""
    if "://" not in candidate:
        candidate = f"http://{candidate}"
    try:
        parsed = urllib.parse.urlsplit(candidate)
    except ValueError:
        return "", ""
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host or not _HOST_RE.match(host):
        return "", ""
    if "." not in host and not _IPV4_HOST_RE.match(host):
        return "", ""
    return host, parsed.path or ""


def _registrable_label(host: str) -> str:
    """도메인에서 브랜드 비교에 쓸 라벨을 뽑는다 (vv-cj.top -> vvcj)."""
    for suffix in _LEGIT_SUFFIXES:
        if host.endswith(suffix):
            host = host[: -len(suffix)]
            break
    else:
        host = host.rsplit(".", 1)[0]
    label = host.rsplit(".", 1)[-1]
    return re.sub(r"[^a-z0-9]", "", label)


def analyze_url(url: str) -> URLRiskResult:
    """check_url_risk의 순수 판정부. Tool 없이 단독 테스트할 수 있다."""
    host, path = _split_url(url)
    if not host:
        # 설계서 2.5: 형식 불명 URL이면 risk_score=0, signals=["형식 불명"].
        return URLRiskResult(blacklisted=False, risk_score=0, signals=["형식 불명"])

    signals: list[str] = []
    score = 0

    blacklist = load_blacklist()
    blacklisted = host in blacklist
    if blacklisted:
        signals.append("KISA 피싱사이트 목록에 등록된 주소")
        score += _SIGNAL_SCORES["blacklist"]

    if _IPV4_HOST_RE.match(host):
        signals.append("도메인 대신 IP 주소를 직접 사용")
        score += _SIGNAL_SCORES["ip_host"]

    tld = host.rsplit(".", 1)[-1]
    if tld in _SUSPICIOUS_TLDS:
        signals.append(f"비정상 TLD .{tld}")
        score += _SIGNAL_SCORES["suspicious_tld"]

    if host in _SHORTENER_HOSTS:
        signals.append("단축 URL 서비스 도메인")
        score += _SIGNAL_SCORES["shortener"]

    label = _registrable_label(host)
    for brand, korean in _LOOKALIKE_BRANDS.items():
        if brand and brand in label and label != brand:
            signals.append(f"{korean} 유사 도메인 (정식 도메인 아님)")
            score += _SIGNAL_SCORES["lookalike"]
            break

    if host.startswith("xn--") or ".xn--" in host:
        signals.append("퓨니코드 도메인 (한글·유니코드 위장 가능)")
        score += _SIGNAL_SCORES["punycode"]

    if "@" in url:
        signals.append("주소에 @ 포함 (실제 접속지 위장 가능)")
        score += _SIGNAL_SCORES["at_sign"]

    trimmed = path.strip("/")
    if trimmed and len(trimmed) <= 3 and "/" not in trimmed:
        signals.append("단축형 경로")
        score += _SIGNAL_SCORES["short_path"]

    if not signals:
        signals.append("알려진 위험 신호 없음")

    return URLRiskResult(
        blacklisted=blacklisted, risk_score=min(score, 100), signals=signals
    )


@tool
def check_url_risk(url: str) -> URLRiskResult:
    """문자에 포함된 URL이 알려진 피싱 사이트인지 확인하고, 단축 URL·유사 도메인·IP 직접 주소·비정상 TLD 등 위험 신호를 검사합니다."""
    return analyze_url(url)


# ---------------------------------------------------------------------------
# verify_caller_number
# ---------------------------------------------------------------------------

def _finlife_request(phone: str, company_name: str | None) -> list[dict[str, Any]]:
    """금감원 finlife companySearch를 호출해 회사 목록을 돌려준다."""
    api_key = os.environ.get(FINLIFE_API_KEY_ENV, "")
    if not api_key:
        raise RuntimeError(f"{FINLIFE_API_KEY_ENV} 환경변수가 설정되지 않음")

    query = urllib.parse.urlencode({
        "auth": api_key,
        "topFinGrpNo": "020000",
        "pageNo": "1",
    })
    request = urllib.request.Request(
        f"{FINLIFE_ENDPOINT}?{query}", headers={"Accept": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=FINLIFE_TIMEOUT_SECONDS) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return payload.get("result", {}).get("baseList", []) or []


def verify_number(
    phone: str,
    company_name: str | None = None,
    *,
    fetch: Any = None,
) -> CallerVerificationResult:
    """verify_caller_number의 순수 판정부. fetch를 주입하면 네트워크 없이 테스트한다."""
    normalized = memory.normalize_phone(phone)
    caller = fetch or _finlife_request

    last_error: Exception | None = None
    for _ in range(FINLIFE_MAX_ATTEMPTS):
        try:
            companies = caller(phone, company_name)
            break
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, RuntimeError) as exc:
            last_error = exc
    else:
        # 설계서 2.5: 타임아웃 3회 재시도 후 is_official=None → unverified에 기록.
        # 설계서 1.5 안정성: 조회 실패를 추측으로 채우지 않는다.
        return CallerVerificationResult(
            is_official=None, official_numbers=[], company=company_name or ""
        )

    if last_error is not None and not companies:
        return CallerVerificationResult(
            is_official=None, official_numbers=[], company=company_name or ""
        )

    wanted = company_name.strip() if company_name else ""
    matched_company = ""
    official_numbers: list[str] = []
    for entry in companies:
        name = str(entry.get("kor_co_nm") or "")
        number = memory.normalize_phone(str(entry.get("cal_tel") or ""))
        if not number:
            continue
        if wanted and wanted not in name:
            continue
        official_numbers.append(number)
        if number == normalized:
            matched_company = name

    if not official_numbers:
        return CallerVerificationResult(
            is_official=None, official_numbers=[], company=wanted
        )

    is_official = normalized in official_numbers
    return CallerVerificationResult(
        is_official=is_official,
        official_numbers=sorted(set(official_numbers)),
        company=matched_company or wanted,
    )


@tool
def verify_caller_number(phone: str, company_name: str | None = None) -> CallerVerificationResult:
    """걸려온 전화번호가 해당 금융회사의 공식 대표번호인지 대조합니다. 기관 사칭 판별에 사용합니다."""
    return verify_number(phone, company_name)


# ---------------------------------------------------------------------------
# report_to_authority
# ---------------------------------------------------------------------------

def make_receipt_no(scam_type: str) -> str:
    """모의 접수번호를 만든다 (설계서 4.2 TS-03-C004의 receipt_no)."""
    return f"UH-{scam_type[:3].upper()}-{uuid.uuid4().hex[:8].upper()}"


@tool
def report_to_authority(
    scam_type: str, target: str, summary: str, runtime: ToolRuntime
) -> bool:
    """확인된 사기 건을 신고 기관에 접수합니다. 실행 전 반드시 사용자 승인이 필요합니다."""
    # 승인 확인은 HumanInTheLoopMiddleware가 담당한다 (설계서 3.2, G7).
    # 여기까지 실행됐다는 것은 승인이 끝났다는 뜻이다.
    context = getattr(runtime, "context", None)
    user_id = getattr(context, "user_id", None)
    if not user_id:
        # 설계서 2.1: 모델이 임의로 다른 사용자의 ID를 선택하지 못하게 한다.
        raise ValueError("report_to_authority: Runtime Context에 user_id가 없음")

    receipt_no = make_receipt_no(scam_type)
    store = getattr(runtime, "store", None)
    if store is not None:
        record = memory.build_record(
            summary, source_text=target, scam_type=scam_type, reported=True
        )
        record["receipt_no"] = receipt_no
        # 설계서 2.5: 실패 시 예외 발생, 재시도 안 함.
        memory.save_report(store, user_id, record)
    return True


# ---------------------------------------------------------------------------
# get_scam_playbook 연결 (구현은 작업 묶음 6의 rag.py)
# ---------------------------------------------------------------------------

@tool
def get_scam_playbook(scam_type: str, damage_stage: str) -> PlaybookResult:
    """사기 유형과 현재 피해 단계에 맞는 공식 대응 절차와 신고 기관 연락처를 조회합니다."""
    try:
        import rag
    except ImportError:
        # rag.py는 작업 묶음 6에서 구현한다. 없으면 조회 불가로 처리하고
        # 추측으로 채우지 않는다 (설계서 1.5 안정성).
        return PlaybookResult(steps=[], contacts=[])
    return rag.search_playbook(scam_type, damage_stage)


# 설계서 2.5 Tool 목록. Agent 조립(작업 묶음 1)에서 이 리스트를 사용한다.
ALL_TOOLS = [
    check_url_risk,
    verify_caller_number,
    get_scam_playbook,
    report_to_authority,
]

# 설계서 3.2: EmergencyRouteMiddleware가 송금 피해 턴에 비활성화할 조회형 Tool.
LOOKUP_TOOL_NAMES = ["check_url_risk", "verify_caller_number"]
