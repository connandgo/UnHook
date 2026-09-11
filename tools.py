"""Tool implementations for 작업 묶음 5 (agent-design.md 2.5).

각 Tool의 docstring은 설계서 2.5의 문장을 그대로 사용한다. 모델이 "언제 이 Tool을
호출할지" 판단하는 근거이기 때문이다 (설계서 2.5 주석, AGENTS.md 작업 규칙).

Tool은 State를 직접 쓰지 않는다 (설계서 3.1 핵심 원칙). 조회 결과는 반환값으로만
돌려주고, State 반영은 미들웨어가 담당한다.

반환 데이터에는 개인정보를 남기지 않는다 (AGENTS.md 팀별 연결 작업 ⑤).
외부 API 응답처럼 이 모듈이 내용을 통제할 수 없는 문자열은 `_scrub()`으로 한 번 거른다.
`ContentIsolationMiddleware`(묶음 3)가 ToolMessage를 `untrusted_tool_result`로 감싸지만
그것은 구조적 격리이며 내용 검사는 아니다.
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

# 외부 문자열 1건의 상한. 초과분은 자른다. 조회 결과 식별에는 충분하고,
# 오염된 응답이 프롬프트를 밀어내는 것을 막는다.
MAX_EXTERNAL_FIELD_CHARS = 120

DATA_DIR = Path(__file__).resolve().parent / "data"
KISA_URL_CSV = DATA_DIR / "kisa_urls.csv"

# 설계서 1.5 성능: 외부 API에는 Timeout을 설정한다.
# 2.5: 타임아웃 3회 재시도 후 is_official=None.
FINLIFE_TIMEOUT_SECONDS = 5.0
FINLIFE_MAX_ATTEMPTS = 3
FINLIFE_ENDPOINT = "https://finlife.fss.or.kr/finlifeapi/companySearch.json"
# 환경변수 이름은 설계서 5절에 따라 이 파일의 담당자가 확정한다.
FINLIFE_API_KEY_ENV = "FSS_FINLIFE_API_KEY"

# check_url_risk 위험 신호 가중치.
#
# 신호를 두 부류로 나눈다. 전부 더하기만 하면 서로 독립이 아닌 신호(같은 스미싱
# 킷에서 한 세트로 나오는 .top + 유사 도메인 + 단축 경로)를 여러 번 세게 되고,
# 단독으로 확정인 신호(@ 위장)가 보강 신호보다 낮게 나온다.
#
# 결정적 신호는 하한선을 세우고, 보강 신호는 누적한다.
#   risk_score = min(max(결정적 하한, 보강 합계), 100)

# 단독으로 확정에 가까운 신호. 정상 사용 사례가 거의 없다.
_DECISIVE_SCORES = {
    "blacklist": 100,  # KISA 등록 = 확인된 사실
    "at_sign": 90,     # 브라우저가 @ 앞을 무시해 실제 접속지를 숨긴다
    "punycode": 60,    # 유니코드 위장. 문자 링크에 정상 사용 사례가 없다
    "ip_host": 55,     # 정상 기관은 문자에 IP 주소를 보내지 않는다
}

# 혼자서는 "의심"에 그치고, 누적될 때 의미가 생기는 신호.
_CUMULATIVE_SCORES = {
    "lookalike": 25,
    "suspicious_tld": 20,
    # 단축 URL은 위험한 것이 아니라 목적지를 판단할 수 없는 것이다.
    # 정상 기업도 쓰므로 낮게 두고, signals에 확인 불가를 명시한다 (설계서 1.5 안정성).
    "shortener": 15,
    "short_path": 10,
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
    "police": "경찰청", "fss": "금융감독원",
}
# 유사 도메인 판정 전에 거르는 알려진 정상 도메인.
# 브랜드 부분 문자열 매칭은 정식 도메인도 사칭으로 잡는다 (cjlogistics.com,
# kakaostory.com). 하위 도메인까지 함께 허용한다 (obank.kbstar.com).
_LEGIT_DOMAINS = {
    "cj.co.kr", "cjlogistics.com", "doortodoor.co.kr",
    "lotteglogis.com", "hanjin.co.kr", "hanjin.com",
    "epost.go.kr", "koreapost.go.kr",
    "kakao.com", "kakaostory.com", "kakaocorp.com", "daum.net",
    "naver.com", "navercorp.com",
    "kbstar.com", "kbfg.com", "shinhan.com", "shinhansec.com",
    "wooribank.com", "hanabank.com", "kebhana.com",
    "nonghyup.com", "nhbank.com", "banking.nonghyup.com",
    "toss.im", "tossbank.com",
    "gov.kr", "police.go.kr", "fss.or.kr", "kisa.or.kr", "spo.go.kr",
}

_LEGIT_SUFFIXES = (
    ".co.kr", ".go.kr", ".or.kr", ".ac.kr", ".re.kr", ".com", ".net", ".kr",
)

_IPV4_HOST_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")
_HOST_RE = re.compile(r"^[\w.-]+$")

_blacklist_cache: set[str] | None = None

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _scrub(value: str) -> str:
    """이 모듈이 통제할 수 없는 외부 문자열을 반환 전에 거른다.

    개인정보는 `audit.mask_output_pii`로 라벨 처리한다. 묶음 4의 출력 쪽 규칙을
    그대로 쓰는 이유는 Tool 반환값도 모델을 거쳐 사용자에게 도달하기 때문이다.
    `pii.mask_text`는 토큰화를 하므로 쓰지 않는다 — vault를 함께 넘기지 않는
    자리에서는 해석 불가능한 토큰만 남는다.
    """
    if not value:
        return ""
    text = _CONTROL_CHARS_RE.sub(" ", str(value)).strip()
    if len(text) > MAX_EXTERNAL_FIELD_CHARS:
        text = text[:MAX_EXTERNAL_FIELD_CHARS].rstrip() + "…"
    try:
        import audit
        masked, _ = audit.mask_output_pii(text, None)
        return masked
    except ImportError:
        # audit.py가 없어도 Tool 자체는 동작해야 한다. 길이·제어문자 정제는 유지된다.
        return text


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


def is_known_legit(host: str) -> bool:
    """알려진 정상 도메인인지 본다. 하위 도메인도 허용한다.

    접미사 비교라 `kakao.com.evil.ru` 같은 위장은 통과하지 못한다.
    """
    return any(host == d or host.endswith(f".{d}") for d in _LEGIT_DOMAINS)


def _brand_search_text(host: str) -> str:
    """브랜드 비교용 문자열. 최상위 TLD만 떼고 나머지 전체를 이어 붙인다.

    호스트 전체를 보는 이유는 `kakao.com.evil.ru`처럼 브랜드를 하위 도메인에
    넣어 사용자를 속이는 형태를 잡기 위해서다. 마지막 라벨만 보면 `evil`만 남아
    놓친다. 정식 도메인은 `is_known_legit()`이 앞에서 걸러낸다.

        vv-cj.top         -> vvcj
        kakao.com.evil.ru -> kakaocomevil
    """
    trimmed = host.rsplit(".", 1)[0] if "." in host else host
    return re.sub(r"[^a-z0-9]", "", trimmed)


def analyze_url(url: str) -> URLRiskResult:
    """check_url_risk의 순수 판정부. Tool 없이 단독 테스트할 수 있다.

    결정적 신호는 하한선을, 보강 신호는 누적 합계를 만들고 둘 중 큰 값을 쓴다.
    점수가 포화해도 signals에는 탐지된 근거를 모두 남긴다 (설계서 2.4 evidence).
    """
    host, path = _split_url(url)
    if not host:
        # 설계서 2.5: 형식 불명 URL이면 risk_score=0, signals=["형식 불명"].
        return URLRiskResult(blacklisted=False, risk_score=0, signals=["형식 불명"])

    signals: list[str] = []
    decisive = 0
    cumulative = 0

    def hit_decisive(key: str, message: str) -> None:
        nonlocal decisive
        signals.append(message)
        decisive = max(decisive, _DECISIVE_SCORES[key])

    def hit_cumulative(key: str, message: str) -> None:
        nonlocal cumulative
        signals.append(message)
        cumulative += _CUMULATIVE_SCORES[key]

    blacklist = load_blacklist()
    blacklisted = host in blacklist
    if blacklisted:
        hit_decisive("blacklist", "KISA 피싱사이트 목록에 등록된 주소")

    if "@" in url:
        hit_decisive("at_sign", "주소에 @ 포함 (실제 접속지가 @ 뒤 주소로 바뀜)")

    if host.startswith("xn--") or ".xn--" in host:
        hit_decisive("punycode", "퓨니코드 도메인 (한글·유니코드 위장 가능)")

    if _IPV4_HOST_RE.match(host):
        hit_decisive("ip_host", "도메인 대신 IP 주소를 직접 사용")

    tld = host.rsplit(".", 1)[-1]
    if tld in _SUSPICIOUS_TLDS:
        hit_cumulative("suspicious_tld", f"비정상 TLD .{tld}")

    if host in _SHORTENER_HOSTS:
        hit_cumulative(
            "shortener", "단축 URL 서비스 도메인 (실제 목적지 확인 불가)"
        )

    # 알려진 정상 도메인은 브랜드 부분 문자열 매칭에서 제외한다.
    # cjlogistics.com은 CJ대한통운의 정식 도메인이지 사칭이 아니다.
    if not is_known_legit(host):
        label = _brand_search_text(host)
        for brand, korean in _LOOKALIKE_BRANDS.items():
            if brand and brand in label and label != brand:
                hit_cumulative(
                    "lookalike", f"{korean} 유사 도메인 (정식 도메인 아님)"
                )
                break

    trimmed = path.strip("/")
    if trimmed and len(trimmed) <= 3 and "/" not in trimmed:
        hit_cumulative("short_path", "단축형 경로")

    if not signals:
        signals.append("알려진 위험 신호 없음")

    return URLRiskResult(
        blacklisted=blacklisted,
        risk_score=min(max(decisive, cumulative), 100),
        signals=signals,
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

    wanted = _scrub(company_name) if company_name else ""
    matched_company = ""
    official_numbers: list[str] = []
    for entry in companies:
        name = _scrub(str(entry.get("kor_co_nm") or ""))
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
        # target만으로는 도메인·번호만 남고 문구 채널이 비어 TS-05 재방문 경고가
        # 절반만 동작한다. 대화의 붙여넣은 원문을 함께 넣는다.
        pasted = memory.source_text_from_messages(
            (getattr(runtime, "state", None) or {}).get("messages")
        )
        record = memory.build_record(
            summary, source_text=f"{target} {pasted}".strip(),
            scam_type=scam_type, reported=True
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
