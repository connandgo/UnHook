# 설계서 수정 제안 — `report_to_authority` 반환에 접수번호 포함

| 항목 | 내용 |
|---|---|
| 대상 | 설계서 2.5 `report_to_authority` 반환 타입, 5절 공유 계약, 4.2 TS-03-C004 |
| 제안자 | 작업 묶음 5 |
| 상태 | **제안** — 합의 전까지 구현하지 않는다 |
| 영향 | 묶음 ①(모델이 결과를 읽음), ②(HITL 재개), ⑤(구현), 통합(화면 표시) |

AGENTS.md "설계서에 없는 것을 임의로 추가하지 않는다. 필요하면 설계서 수정을 먼저 제안한다"에 따른 제안서다.
PR #5부터 세 차례 제기했으나 결정되지 않아 문서로 정리한다.

---

## 1. 문제 — 설계서 두 조항이 서로 모순된다

**2.5 Tool 표**는 반환을 `bool`로 못박는다.

| Tool 이름 | 입력 파라미터 | 반환 타입 |
|---|---|---|
| `report_to_authority` | `scam_type`, `target`, `summary` | **`bool`** |

**5절 공유 계약**도 같다.

> `report_to_authority` 반환은 2.5의 `bool`을 따른다.

그런데 **4.2 TS-03-C004**는 접수번호 안내를 요구한다.

| 케이스 | 사용자 입력 | 예상 최종 응답 |
|---|---|---|
| TS-03-C004 | "접수해줘" → (승인 버튼 클릭) | 승인 전 대기 화면 → **승인 후 `receipt_no` 안내** |

`bool`로는 접수번호를 전달할 수 없다. 두 조항을 동시에 만족하는 구현이 존재하지 않는다.

## 2. 현재 구현과 실측

설계서 계약을 우선해 `bool`을 반환하고, 접수번호는 Store 레코드에만 기록해 두었다.

```
Tool이 모델에게 돌려주는 값 : True          ← receipt_no 없음
Store 레코드                : UH-SMI-A535787A
```

**모델도 앱도 이 번호에 닿을 수 없다.**

- 모델은 ToolMessage로 `True`만 받는다
- `report_history`는 Store에 있고 State가 아니다(설계서 3.1). 앱은 State만 본다
- `UnHookState`에 접수 결과를 담을 필드가 없다 (`incident_report`는 접수 *전* 정리서다)

따라서 TS-03-C004의 Pass 조건 중 "승인 후 `receipt_no` 안내"는 **현재 구조에서 달성 불가능하다.**

## 3. 제안

```python
class ReportReceipt(TypedDict):
    accepted: bool        # 기존 bool의 의미를 그대로 옮긴다
    receipt_no: str       # 모의 접수번호. 실패 시 ""
    agency: str           # 접수 기관 표시명
```

`report_to_authority`의 반환 타입을 `bool` → `ReportReceipt`로 바꾼다.

### 왜 이 형태인가

- `accepted`가 기존 `bool`의 자리를 그대로 이어받으므로 판정 로직이 바뀌지 않는다
- 다른 Tool 3종이 모두 `dict`(TypedDict)를 반환한다. `report_to_authority`만 스칼라인 것이 오히려 예외였다
- `agency`를 함께 두면 응답에서 "어디에 접수되었는지"를 말할 수 있다. 2.5의 `get_scam_playbook`이 돌려주는 `contacts`와 연결된다

### 실패 처리는 그대로

2.5의 "실패 시 예외 발생, 재시도 안 함"을 유지한다. `accepted=False`는 쓰지 않고 예외로 올린다.
`accepted` 필드는 형식상의 성공 표시이며, 미승인 시 Tool 자체가 실행되지 않는다(G7, `HumanInTheLoopMiddleware`).

## 4. 설계서 수정 범위

**2.5 Tool 표** — 반환 타입 칸

```
변경 전: bool
변경 후: dict (accepted: bool, receipt_no: str, agency: str)
```

**5절 공유 계약**

```
변경 전: `report_to_authority` 반환은 2.5의 `bool`을 따른다.
변경 후: `report_to_authority` 반환은 `ReportReceipt`를 따른다.
         `receipt_no`는 모의 접수번호이며 실제 기관 접수를 의미하지 않는다.
```

**4.2 TS-03-C004** — Pass 판정 기준에 반환값 확인을 추가한다.

**변경 이력** — 항목 추가.

## 5. 검토한 대안

| 대안 | 판단 |
|---|---|
| 그대로 둔다 | TS-03-C004를 통과시킬 방법이 없다. ①②가 머지되고 실제로 돌릴 때 걸린다 |
| State에 `report_receipt` 필드를 추가한다 | Tool은 State를 직접 쓰지 않는다(3.1). 미들웨어가 Tool 반환값에서 옮겨야 하는데, 그 반환값이 `bool`이라 옮길 내용이 없다 |
| `summary`에 접수번호를 끼워 넣는다 | `summary`는 입력 파라미터다. 출력 통로가 아니다 |
| 접수번호를 없애고 TS-03-C004를 고친다 | 실제 신고 접수는 접수번호를 돌려준다. 시연에서 사용자가 확인할 유일한 증거를 없애는 셈이다 |
| Store를 앱이 직접 읽는다 | 앱이 Store 내부 구조에 결합된다. `report_history`는 재방문 경고용이지 이번 턴 결과 통로가 아니다 |

## 6. 영향과 이관 작업

| 묶음 | 작업 |
|---|---|
| ⑤ | `schemas.py`에 `ReportReceipt` 추가, `tools.py` 반환 변경, 테스트 수정. **약 20줄** |
| ① | 프롬프트에서 접수 결과를 사용자에게 전달하도록 안내 |
| ② | 승인 재개(`resume_turn`) 후 반환값 처리 |
| 통합 | 승인 후 화면에 접수번호 표시 |

`schemas.py`는 전원 공유 파일이므로 리뷰가 필요하다. `bool`을 읽는 코드는 현재 없으므로(Tool을 호출하는 `agent.py`가 아직 없다) 지금 바꾸면 깨지는 곳이 없다. **①②가 머지된 뒤에는 세 묶음이 동시에 손대야 한다.**

## 7. 합의가 필요한 지점

1. 반환 타입을 `bool` → `ReportReceipt`로 바꾸는 데 동의하는가
2. 필드 구성(`accepted`·`receipt_no`·`agency`)이 적절한가
3. 접수번호 형식 — 현재 구현은 `UH-<유형 3자>-<8자리>` (예: `UH-SMI-A535787A`)
4. 실패를 예외로 올리는 현행 규약을 유지하는가

동의되면 묶음 5가 설계서 수정과 구현·테스트를 함께 올린다.
동의되지 않으면 현행 `bool`을 유지하고, **4.2 TS-03-C004의 `receipt_no` 안내 조건을 함께 삭제해야 한다.**
지금처럼 두 조항이 모순된 상태로 남겨두면 통합 시점에 반드시 걸린다.
