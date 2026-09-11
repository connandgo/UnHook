# 설계서 수정 제안 — `URLRiskResult`에 `status` 추가

| 항목 | 내용 |
|---|---|
| 대상 | 설계서 2.5 `check_url_risk` 반환 계약, 5절 공유 계약, `schemas.py` |
| 제안자 | 작업 묶음 5 |
| 상태 | **제안** — 합의 전까지 구현하지 않는다 |
| 영향 | 묶음 ①(모델이 결과를 읽음), ②(State 반영), ⑤(구현) |

AGENTS.md "설계서에 없는 것을 임의로 추가하지 않는다. 필요하면 설계서 수정을 먼저 제안한다"에 따른 제안서다.

---

## 1. 문제

### 1.1 `risk_score`를 읽는 코드가 없다

현재 계약은 `URLRiskResult(blacklisted: bool, risk_score: int, signals: list[str])`다.
`tools.py`가 값을 만들고 `tests/test_tools_memory.py`가 그 값을 검증하지만, **그 밖의 어떤 모듈도
이 값을 읽어 동작을 바꾸지 않는다.** 즉 생산자와 자기 테스트만 있고 소비자가 없다.

- `ScamAssessment`(2.4)에 해당 필드가 없다 → 사용자에게 도달하지 않는다
- `audit.py`(묶음 4)도 참조하지 않는다
- 설계서가 요구하는 산출물은 점수가 아니라 문장이다. 2.4는 `evidence`를 "signals, URL 검사 결과를
  **사람이 이해할 수 있는 문장으로** 저장"이라 정의하고, 4.2 TS-01 Pass 기준도
  "evidence 3개에 세 신호 모두 포함"이지 점수 기준이 아니다

### 1.2 `int` 하나로 세 가지를 말할 수 없다

`check_url_risk`가 전달해야 하는 정보는 세 가지다.

| # | 정보 | 현재 표현 |
|---|---|---|
| 1 | 위험의 세기 | `risk_score` |
| 2 | 근거의 종류 | `signals` |
| 3 | **판단 가능 여부** | **표현 수단 없음** |

3번이 실제로 문제를 만든다. 현재 구현에서:

```
https://www.naver.com    risk_score=0    검사했고 깨끗함
https://bit.ly/3xK9p     risk_score=15   목적지를 볼 수 없음
```

두 값 모두 "낮은 숫자"다. 그러나 의미는 정반대다. 앞은 확인된 안전이고, 뒤는 **미확인**이다.
단축 URL은 정상 기업도 쓰므로 위험하다고 단정할 수 없고, 동시에 안전하다고도 말할 수 없다.

설계서 1.5 안정성은 "외부 API 또는 Tool 조회에 실패한 경우 해당 정보를 추측하여 생성하지 않고
'확인 불가' 또는 미확인 정보로 처리"를 요구한다. 낮은 점수는 이 요구를 만족하지 못한다.
모델이 숫자를 보고 "15/100이니 안전"으로 읽으면 그대로 오판이 된다.

---

## 2. 근거 — 묶음 3이 같은 문제를 이미 풀었다

묶음 3(`guards.py`)도 "이 입력이 얼마나 의심스러운가"를 표현해야 했다.
**숫자를 쓰지 않고 상태 enum과 사유 코드를 썼다.**

```python
class InputGuardResult(TypedDict):
    message_id: str
    status: Literal["not_checked", "not_detected", "detected", "unavailable"]
    reason_codes: list[InjectionReason]
```

점수 필드가 없다. 그리고 AGENTS.md에 이렇게 명시했다.

> `not_detected`는 안전 인증이 아니며 판별 오류는 `unavailable`로 구분한다.

1.2에서 지적한 것과 정확히 같은 문제이며, 해법은 **별도 상태값**이었다.

| | "위험 없음" | "판단 불가" |
|---|---|---|
| 묶음 3 | `not_detected` | `unavailable` |
| 묶음 5 (현재) | `risk_score=0` | `risk_score=15` |

같은 저장소 안에서 같은 성격의 판정을 서로 다른 방식으로 표현하고 있다.
묶음 3의 패턴이 더 정확하므로 그쪽에 맞추는 것을 제안한다.

---

## 3. 제안

### 3.1 스키마

```python
URLStatus = Literal["confirmed", "suspicious", "unverifiable", "clean", "malformed"]


class URLRiskResult(TypedDict):
    status: URLStatus          # 추가
    blacklisted: bool          # 유지 (2.5 계약)
    risk_score: int            # 유지 (2.5 계약), 의미를 3.3처럼 좁힌다
    signals: list[str]         # 유지
```

기존 세 필드를 그대로 두므로 **이미 이 타입을 쓰는 코드는 깨지지 않는다.**

### 3.2 상태 정의

| status | 의미 | 예시 |
|---|---|---|
| `confirmed` | KISA 목록 등록. 확인된 사실 | `vv-cj.top` |
| `suspicious` | 위험 신호를 탐지 | `kakao.com.evil.ru`, `203.0.113.9/kb/login` |
| `unverifiable` | **목적지를 판단할 수 없음.** 안전 판정이 아니다 | `bit.ly/3xK9p` |
| `clean` | 검사했고 알려진 위험 신호 없음 | `naver.com` |
| `malformed` | URL 형식이 아님 (2.5 기존 규약) | `"이게 뭐야"` |

판정 우선순위 (위에서부터 먼저 적용):

1. URL 파싱 실패 → `malformed`
2. `blacklisted=True` → `confirmed`
3. 단축 URL 외의 신호가 하나라도 있음 → `suspicious`
4. 단축 URL 신호만 있음 → `unverifiable`
5. 그 외 → `clean`

### 3.3 `risk_score`의 의미를 좁힌다

> `risk_score`는 **탐지된 위험의 세기**만 뜻한다. 판단 불가는 점수로 표현하지 않는다.

| status | `risk_score` |
|---|---|
| `confirmed` | 100 |
| `suspicious` | 결정적 하한 / 보강 합계 (현행 규칙) |
| `unverifiable` · `clean` · `malformed` | **0** |

`shortener` 가중치(현재 15)는 제거하고 `status="unverifiable"`과 signals 문장이 대신한다.
이렇게 하면 `risk_score`에 처음으로 명확한 역할이 생긴다 — `suspicious` 안에서 25인지 55인지를
구분하는 강도 지표다. 지금처럼 "0인데 안전한 것인지 못 본 것인지 모르는" 상태가 사라진다.

### 3.4 각 묶음의 사용법

| 묶음 | 사용 |
|---|---|
| ① Agent | 모델 프롬프트에서 `unverifiable`은 안전 판정 근거로 쓰지 않는다. 되묻기 또는 `unverified` 기록 |
| ② Middleware | `status`로 분기할 수 있다. `confirmed`면 추가 조회 없이 진행 등 |
| ④ 응답 검증 | Tool 결과와 응답의 정합성을 검사할 수 있다 (아래) |
| ⑤ Tool | 판정 구현 |

특히 ④가 실질적이다. 다만 G5가 못 하던 일을 하게 되는 것이 아니라, **G5가 원리상 못 잡는
종류**를 잡게 되는 것이다.

현재 `audit.py`의 G5(`FALSE_SAFETY_RULES`)는 **출력 문구**를 정규식으로 검사한다.
"안전합니다"·"사기가 아닙니다"·"걱정 안 하셔도 됩니다"는 이미 잡히며, 이는 `risk_score` 값과
무관하게 동작한다. 이 부분은 제안 여부와 관계없이 지금도 정상이다.

문제는 금지어를 쓰지 않은 응답이다. 실측하면 아래 두 문장은 G5를 통과한다.

```
통과  "특별한 위험 신호는 발견되지 않았어요."
통과  "알려진 피싱 사이트 목록에는 없는 주소예요."
```

단축 URL에 대해 모델이 이렇게 답하면 G5도 G6(evidence가 비어 있지 않음)도 통과하지만
내용은 틀렸다. **위험 신호가 없는 것이 아니라 목적지를 보지 못한 것**이기 때문이다.
사용자는 검사 결과 깨끗한 것으로 읽는다.

`status`가 있으면 문구가 아니라 사실관계를 검사할 수 있다.

```
Tool 결과에 unverifiable이 있는데 응답이 "위험 없음" 취지 → 위반
```

현재는 이 검사를 만들 수 없다. `risk_score=15`만으로는 "낮은 위험"인지 "판단 불가"인지
코드가 구분할 수 없기 때문이다.

| | G5 (현재) | `status` 추가 시 |
|---|---|---|
| 검사 대상 | 출력 **문구** | Tool 결과와 응답의 **정합성** |
| "안전합니다" | 잡음 | 잡음 (변화 없음) |
| "위험 신호 없어요" (단축 URL에) | **통과** | 잡을 수 있음 |

---

## 4. 설계서 수정 범위

**2.5 Tool 표** — `check_url_risk` 반환 타입

```
변경 전: dict (blacklisted: bool, risk_score: int, signals: list[str])
변경 후: dict (status: str, blacklisted: bool, risk_score: int, signals: list[str])
```

에러 처리 칸도 함께 고친다.

```
변경 전: 형식 불명 URL이면 risk_score=0, signals=["형식 불명"] 반환 (예외 미발생)
변경 후: 형식 불명 URL이면 status="malformed", risk_score=0, signals=["형식 불명"] 반환 (예외 미발생)
```

**5절 공유 계약** — 3.2 상태 정의표와 3.3 점수 의미를 추가한다.

**변경 이력** — 항목 추가.

---

## 5. 검토한 대안

| 대안 | 판단 |
|---|---|
| 그대로 둔다 | 계약은 지켜지지만 "판단 불가"를 표현할 수단이 없다. 1.5 안정성 요구를 만족하지 못한다 |
| 단축 URL을 높은 점수로 올린다 | 정상 기업도 쓰므로 오탐이 된다. 위험해서가 아니라 못 보기 때문이다 |
| `risk_score`를 제거한다 | 2.5 계약 변경 폭이 크고, `suspicious` 내 강도 비교 수단을 잃는다 |
| `signals` 문장만으로 충분하다 | 모델은 읽을 수 있으나 코드가 분기할 수 없다. ②·④가 쓸 수 없다 |
| `unverified`(2.4)에 넣는다 | `ScamAssessment` 필드라 Tool이 직접 쓸 수 없다. Tool은 State를 쓰지 않는다(3.1) |

---

## 6. 합의가 필요한 지점

1. `URLRiskResult`에 필드를 추가하는 데 동의하는가 (`schemas.py`는 전원 공유 파일)
2. status 5종의 이름과 구분이 적절한가
3. `shortener` 가중치 제거에 동의하는가
4. 묶음 ①이 프롬프트에서 `unverifiable`을 어떻게 다룰지

동의되면 묶음 5가 설계서 수정과 구현·테스트를 함께 올린다.
동의되지 않으면 현행 계약을 유지하고, `risk_score`가 소비되지 않는 상태도 그대로 둔다.

---

## 참고 — 별건

묶음 3이 AGENTS.md에서 ⑤에게 남긴 항목이 따로 있다.

> ⑤ Tool·⑥ RAG | 반환 데이터의 개인정보 제거. Tool 결과는 호출 시 불신 데이터로 감싸며 `tool_call_id` 유지

현재 Tool 4종은 입력 URL·번호를 반환값에 반향하지 않아 PII 유출 경로는 확인되지 않았으나,
"불신 데이터로 감싸기"는 미구현이다. 본 제안과 별개로 처리한다.
