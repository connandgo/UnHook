# Un Hook Agent Core

장민서 담당 범위인 **Agent Core 및 LLM 설계·개발** 모듈이다. 기준 문서는 저장소의 `docs/agent-design.md`이며, 루트의 Tool·Middleware·Guardrail 구현을 기본값으로 조립한다.

## 포함 범위

- `gpt-5-nano` 단일 모델의 기본·재검토 프로필 설정
- LangChain `create_agent` 조립
- `ScamAssessment`, `DamageFlags`, `ActionStep` Structured Output
- 신뢰할 수 없는 `quoted_content`를 JSON 데이터로 격리하는 프롬프트
- 긴 입력·긴 대화·복합 유형·Tool 충돌·낮은 신뢰도에 대한 nano 재검토
- 송금 피해 시 추가 재검토 억제 및 조회형 Tool 제거
- 신고 Tool을 사용자 승인 전 모델에서 제거
- 모델·Tool 호출 횟수와 LangGraph 반복 제한
- 동기 `invoke`와 비동기 `ainvoke`
- `middleware.build_middleware()` 자동 연결
- `pii.prepare_masked_input()`을 통한 모델·체크포인터 입력 전 마스킹
- 터미널 대화 테스트용 `python -m agent.cli`

## 설치와 API 키

프로젝트 루트에서 실행한다.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r agent/requirements.txt
export OPENAI_API_KEY="발급받은 키"
```

API 키는 코드나 `.env.example`에 입력하지 않는다. 운영 환경의 Secret 또는 로컬 환경변수로 주입한다.

## 기본 사용법

```python
from agent import AgentTurnInput, StateSnapshot, build_unhook_agent
unhook = build_unhook_agent()

result = unhook.invoke(
    AgentTurnInput(
        thread_id="case-001",
        user_id="user-001",
        age_group="general",
        user_statement="링크는 눌렀지만 송금은 하지 않았어요.",
        quoted_content="[택배] 주소 불일치. 아래 링크에서 확인하세요.",
        state=StateSnapshot(),
        multiple_messages=False,
        conversation_turns=1,
    )
)

print(result.assessment.model_dump())
print(result.model_used)
```

FastAPI 같은 비동기 서버에서는 `await unhook.ainvoke(turn)`을 사용한다.

`tools`를 생략하면 루트 `tools.ALL_TOOLS`, `middleware`를 생략하면 루트
`middleware.build_middleware()`가 자동 연결된다. 테스트에서 완전히 끄려면 각각
`tools=()`, `middleware=()`를 명시한다.

## 터미널에서 직접 대화하기

프로젝트 루트에서 위 가상환경과 API 키를 사용한다.

```bash
source .venv/bin/activate
export OPENAI_API_KEY="본인의_API_키"
python -m agent.cli
```

첫 번째 입력에는 본인이 한 행동이나 질문을, 두 번째 입력에는 상대가 보낸 문자나
통화 원문을 넣는다. 원문이 없으면 Enter를 누른다. `/quit` 또는 `/exit`로 종료한다.
CLI는 모델 선택, Tool 호출·결과, 사기 유형, 위험도, 근거, 즉시 조치와 다음 질문을
표시한다. 동일 실행 안에서는 같은 `thread_id`, Checkpointer, Store와 상태를 이어 쓴다.

## 팀 모듈 연결 계약

Agent Core에 전달할 Tool 이름은 설계서와 정확히 같아야 한다.

| 이름 | 담당 | Agent Core 동작 |
|---|---|---|
| `check_url_risk` | Tool/API | 일반 턴에 제공, 송금 상태에서는 제거 |
| `verify_caller_number` | Tool/API | 일반 턴에 제공, 송금 상태에서는 제거 |
| `get_scam_playbook` | Tool/API·RAG | 피해 단계별 대응 절차 검색 |
| `report_to_authority` | Tool/API·HITL | `report_approved=True`인 턴에만 모델에 제공 |

기본 Middleware는 루트 `build_middleware()`가 정한 실행 순서로 자동 연결된다.

```python
from middleware import build_middleware

middlewares = build_middleware()
```

현재 연결되는 항목은 `PIIMiddleware`, `OutputAuditMiddleware`, 입력 가드 3개,
긴급 경로 3개, Memory 주입 2개, `DamageStateMiddleware`, HITL, Tool 재시도까지
총 13개다. Agent Core는 마지막에 모델/Tool 호출 제한 Middleware를 추가한다.

`DamageStateMiddleware`는 모델의 `damage_flags`를 검증한 뒤 State를 갱신하고, `damage_stage`와 `risk_level`을 코드 규칙으로 다시 산출해야 한다. 모델이 반환한 두 값을 그대로 신뢰하면 안 된다. 과거 신고 이력은 Tool로 조회하지 않고 `MemoryInjectMiddleware`가 Store를 조회해 `history_matches`에 반영한다.

## 모델 선택

다음 조건 중 하나면 `gpt-5-nano` 재검토 프로필을 사용한다.

- 입력 4,000자 이상
- 여러 메시지를 한 번에 입력
- 누적 대화 6턴 이상
- 두 가지 이상의 사기 유형 후보
- Tool 결과 충돌
- 이전 출력 감사 실패

`gpt-5-nano` 결과의 `confidence < 0.7`이거나 같은 턴의 출력 감사에서 복구하지 못한
위반이 발견되면 nano 평가 결과를 함께 전달해 한 번 더 `gpt-5-nano`로 재검토한다. 기존 State의
`money_sent=True`이거나 현재 사용자 진술에서 송금 완료 표현이 감지되면 조회형 Tool과
추가 재검토를 막고 첫 nano 결과를 유지한다.

## 입력 경계

이 모듈은 `user_statement`와 `quoted_content`를 분리해 받는다. Agent Core가 invoke 전에
`pii.prepare_masked_input()`을 호출하므로, 모델과 Checkpointer에는 마스킹된 메시지만
들어간다. 사기범 측 전화번호·계좌번호의 원문은 `pii_vault`에 저장되고 허용된 Tool의
지정 인자에서만 복원된다.

`tool_results`에도 마스킹된 데이터만 넣어야 한다. Agent Core는 전달된 Tool 결과가 이미 Guardrail을 통과했다고 가정한다.

기본값은 `InMemorySaver`와 `InMemoryStore`이므로 프로세스 종료 시 사라진다. 팀 통합 시 동일 인스턴스를 재사용하거나 영속 구현을 주입해야 한다.

## 테스트

```bash
PYTHONPATH=. pytest -q tests agent/tests
```

테스트는 스키마 제약, 전체 Middleware 연결, 입력 전 PII 마스킹, 모델 승격 조건,
송금 긴급 분기, 승인 전 신고 Tool 제거를 검증한다. 실제 OpenAI 호출은 단위 테스트에서
수행하지 않는다.
