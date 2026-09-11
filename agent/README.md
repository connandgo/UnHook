# Un Hook Agent Core

장민서 담당 범위인 **Agent Core 및 LLM 설계·개발** 모듈이다. 기준 문서는 저장소의 `docs/agent-design.md`이며, Tool·Middleware·Guardrail 구현은 다른 담당자의 모듈을 주입받는다.

## 포함 범위

- `gpt-5-nano` 기본 모델과 `gpt-5` 검토 모델 설정
- LangChain `create_agent` 조립
- `ScamAssessment`, `DamageFlags`, `ActionStep` Structured Output
- 신뢰할 수 없는 `quoted_content`를 JSON 데이터로 격리하는 프롬프트
- 긴 입력·긴 대화·복합 유형·Tool 충돌·낮은 신뢰도에 대한 gpt-5 승격
- 송금 피해 시 gpt-5 승격 억제 및 조회형 Tool 제거
- 신고 Tool을 사용자 승인 전 모델에서 제거
- 모델·Tool 호출 횟수와 LangGraph 반복 제한
- 동기 `invoke`와 비동기 `ainvoke`

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
from middleware import ALL_MIDDLEWARE       # 다른 담당자 구현
from tools import ALL_TOOLS                 # 다른 담당자 구현

unhook = build_unhook_agent(
    tools=ALL_TOOLS,
    middleware=ALL_MIDDLEWARE,
)

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
        # EmergencyRouteMiddleware가 현재 입력에서 송금을 새로 감지했다면 True
        emergency_detected=False,
    )
)

print(result.assessment.model_dump())
print(result.model_used)
```

FastAPI 같은 비동기 서버에서는 `await unhook.ainvoke(turn)`을 사용한다.

## 팀 모듈 연결 계약

Agent Core에 전달할 Tool 이름은 설계서와 정확히 같아야 한다.

| 이름 | 담당 | Agent Core 동작 |
|---|---|---|
| `check_url_risk` | Tool/API | 일반 턴에 제공, 송금 상태에서는 제거 |
| `verify_caller_number` | Tool/API | 일반 턴에 제공, 송금 상태에서는 제거 |
| `get_scam_playbook` | Tool/API·RAG | 피해 단계별 대응 절차 검색 |
| `report_to_authority` | Tool/API·HITL | `report_approved=True`인 턴에만 모델에 제공 |

커스텀 Middleware는 설계서 3.2 순서대로 전달한다.

```python
ALL_MIDDLEWARE = [
    emergency_route,
    topic_filter,
    injection_guard,
    content_isolation,
    pii_middleware,
    memory_inject,
    retry_middleware,
    human_in_the_loop,
    damage_state,
    output_audit,
    summarization,
]
```

Agent Core는 전달받은 순서를 바꾸지 않고 마지막에 모델/Tool 호출 제한 Middleware를 추가한다.

`DamageStateMiddleware`는 모델의 `damage_flags`를 검증한 뒤 State를 갱신하고, `damage_stage`와 `risk_level`을 코드 규칙으로 다시 산출해야 한다. 모델이 반환한 두 값을 그대로 신뢰하면 안 된다. 과거 신고 이력은 Tool로 조회하지 않고 `MemoryInjectMiddleware`가 Store를 조회해 `history_matches`에 반영한다.

## 모델 선택

다음 조건 중 하나면 처음부터 `gpt-5`를 사용한다.

- 입력 4,000자 이상
- 여러 메시지를 한 번에 입력
- 누적 대화 6턴 이상
- 두 가지 이상의 사기 유형 후보
- Tool 결과 충돌
- 이전 출력 감사 실패

`gpt-5-nano` 결과의 `confidence < 0.7`이면 nano 평가 결과를 함께 전달해 한 번 `gpt-5`로 재검토한다. 기존 State의 `money_sent=True`이거나 현재 입력에서 `EmergencyRouteMiddleware`가 송금을 감지해 `emergency_detected=True`를 전달하면, 조회형 Tool과 gpt-5 승격을 막고 nano를 유지한다.

## 입력 경계

이 모듈은 `user_statement`와 `quoted_content`를 분리해 받는다. Guardrail 담당 모듈은 호출 전에 PII를 마스킹해야 한다. `pii_vault`는 `UnHookState` 통합 계약에는 있지만 `build_turn_payload`에 포함되지 않으므로 모델 입력으로 전달되지 않는다.

`tool_results`에도 마스킹된 데이터만 넣어야 한다. Agent Core는 전달된 Tool 결과가 이미 Guardrail을 통과했다고 가정한다.

## 아직 다른 담당자와 통합이 필요한 부분

- 실제 `check_url_risk`, `verify_caller_number`, `get_scam_playbook`, `report_to_authority`
- 설계서의 11개 Custom/Built-in Middleware
- `DamageStateMiddleware`의 상태 전이·단조 증가 규칙
- `PIIMiddleware`의 마스킹과 `pii_vault`
- `HumanInTheLoopMiddleware`의 승인 중단·재개
- 실제 앱의 `thread_id`, `user_id`, 체크포인터·Store 영속화

기본값은 `InMemorySaver`와 `InMemoryStore`이므로 프로세스 종료 시 사라진다. 팀 통합 시 동일 인스턴스를 재사용하거나 영속 구현을 주입해야 한다.

## 테스트

```bash
pytest -q agent/tests
```

테스트는 스키마 제약, 모델 승격 조건, 송금 긴급 분기, 승인 전 신고 Tool 제거, PII Vault의 모델 프롬프트 제외를 검증한다. 실제 OpenAI 호출은 API 키와 비용이 필요하므로 단위 테스트에서 수행하지 않는다.
