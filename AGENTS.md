# AGENTS.md — Un Hook

이 저장소에서 작업하는 AI 코딩 에이전트(Claude Code, Codex, Cursor 등)를 위한 안내 문서.

## 단일 기준 문서

**모든 설계·구현 판단은 [`docs/agent-design.md`](docs/agent-design.md)를 기준으로 한다.**

이 문서(AGENTS.md)는 설계 내용을 다시 적지 않는다. 설계서와 여기 내용이 다르면 설계서가 맞다. 코드와 설계서가 다르면 설계서를 먼저 확인하고, 설계 변경이 필요하면 코드보다 설계서를 먼저 고친다.

## 작업 전 반드시 읽을 절

작업 성격에 따라 아래 절을 먼저 읽는다.

| 작업 | 읽을 절 |
|---|---|
| 무엇이든 시작 전 | 1.1 Agent 정의, 1.5 제약 및 고려 사항 |
| 전체 흐름 파악 | 2.1 전체 구조도, 2.2 동작 흐름 |
| LLM 호출·모델 선택 | 2.3 LLM 모델 설계 (gpt-5-nano 기본, gpt-5 승격 조건) |
| 출력 스키마 | 2.4 Structured Output 설계 (`ScamAssessment`) |
| Tool 추가·수정 | 2.5 Tool 설계 (docstring은 설계서 문장을 그대로 사용) |
| State·Context 필드 | 3.1 Context |
| Middleware hook | 3.2 Middleware |
| 보안·입력 처리 | 3.3 Guardrails, 5.1 입력 보안 연결 계약 |
| 테스트 작성 | 4.1 테스트 시나리오, 4.2 테스트 케이스 |

## 작업 규칙

- 설계서에 없는 Tool, State 필드, Middleware를 임의로 추가하지 않는다. 필요하면 설계서 수정을 먼저 제안한다.
- 피해 단계·위험도 갱신은 Tool이 아니라 `DamageStateMiddleware`가 담당한다 (2.5, 3.1).
- 개인정보·금융정보는 원문을 저장하지 않는다. 마스킹·토큰화 규칙은 3.1, 3.3을 따른다.
- 외부 조회 실패 시 추측으로 채우지 않고 미확인으로 처리한다 (1.5 안정성).
- 설계서를 수정했으면 문서 말미의 **변경 이력**에 항목을 추가한다.

## 저장소 구조

```
README.md              프로젝트 개요, 팀 구성
AGENTS.md              이 문서
CLAUDE.md              Claude Code용 진입점 (AGENTS.md를 가져옴)
docs/agent-design.md   AI Agent 설계서 (단일 기준 문서)
docs/images/           설계서 첨부 이미지
schemas.py             출력 스키마, 공통 Literal, Tool 결과 타입
state.py               AgentState, Runtime Context, 새 대화 초기값
config.py              공통 모델명 및 승격 기준
guards.py              입력 준비, 주제 필터, 인젝션 탐지, 원문 격리 (③)
.env.example           환경변수 이름 예시
requirements.txt       공통 코드 의존성
tests/                 공통 계약 및 입력 보안 검증
```

예정된 구현 파일과 공유 계약은 설계서 5절을 참조한다.

## 입력 보안 연결

③ 구현의 기준은 [설계서 5.1](docs/agent-design.md#51-입력-보안-연결-계약)이다.
`guards.py`와 공유 타입·설정을 함께 반영해야 한다. 아래는 연결 작업 안내이며 전체 Agent 구현은 아니다.

공격자는 **피해자에게 보낸 원문과 URL 문자열만** 제어한다. 본문 속 판정 지시와 URL 경로·쿼리 속
판정 지시가 두 공격 예시다. 시스템 직접 접근·공식 RAG 오염·링크 대상 페이지 내부 공격은 범위 밖이다.
URL은 접속 없이 파싱하며 검사 사본만 한 번 디코딩한다. 원래 URL과 사용자 상담은 보존한다.
명시적 입력은 `external_texts`를 검사하고, 분리되지 않은 `mixed` 입력만 전체를 검사한다.

### 제공 API

| API | 용도 |
|---|---|
| `prepare_guarded_message(user_statement, *, mask_text, external_texts=None)` | 마스킹을 완료한 `HumanMessage` 생성. Agent 호출 및 Checkpointer 저장 전에 사용 |
| `build_input_middlewares(*, classifier=None)` | TopicFilter → InjectionGuard → ContentIsolation 세 인스턴스 반환. 기본 nano 또는 테스트용 구조화 Runnable 사용 |
| `GuardInputError` | 빈 입력, 형식·길이 오류, 마스킹 실패. 앱에서 재입력 안내 |
| `InjectionDecision`, `InputGuardResult` (`schemas.py`) | 판별 출력 및 요청별 결과 계약 |
| `UnHookState.input_guard` | 최신 메시지 ID·판별 상태·고정 사유 코드. 새 턴에 갱신 |

```python
from guards import build_input_middlewares, prepare_guarded_message

# Agent 생성 시 middleware 목록에 연결한다. 전체 순서는 ②가 조립한다.
input_middlewares = build_input_middlewares()

def run_guarded_turn(agent, config, user_statement, mask_text, external_texts=None):
    message = prepare_guarded_message(
        user_statement,
        mask_text=mask_text,  # ④가 제공하는 실제 마스킹 함수
        external_texts=external_texts,
    )
    result = agent.invoke({"messages": [message]}, config=config)
    return result["structured_response"]
```

- `mask_text`는 필수다. 원문을 그대로 반환하는 임시 함수로 실제 사용자 입력을 처리하지 않는다.
  준비 함수를 사용하지 않고 Agent에 원문을 넘기면 가드가 거부하더라도 이미 체크포인트에 저장될 수 있다.
  앱의 로그·추적에도 마스킹 전 입력을 남기지 않는다.
- 앱에서 분리한 사용자 진술과 문자 원문만 각각 `user_statement`, `external_texts`로 전달한다.
  단일 채팅창 입력은 `external_texts`를 생략해 `mixed`로 유지한다. 본문 구분자를 파싱해 출처를 추정하지 않는다.
- 모델 호출 시 JSON 경계와 보안 정책을 추가하지만 완전한 인젝션 방어를 보장하는 것은 아니다.
  `not_detected`는 안전 인증이 아니며 판별 오류는 `unavailable`로 구분한다.

### 팀별 연결 작업

| 담당 | 반영할 사항 |
|---|---|
| ① Agent | 공통 State·응답 스키마 연결, 입력 준비 후 invoke/ainvoke. 최종 `structured_response` 사용 |
| ② Middleware | 긴급 분기·최종 감사·보안 보강의 실제 hook 순서 조립. 후속 턴에 State 초기화 금지. 승인 재개는 기존 체크포인트 사용 |
| ④ 개인정보 | 구현: `pii.prepare_masked_input()`(출처별 마스킹 + 준비 함수 호출 + vault 반환), 문자열 어댑터 `pii.make_guard_masker(vault)` |
| ④ 응답 검증 | 구현: `OutputAuditMiddleware.after_agent`가 보강 이후 최종 구조화 출력 검사 (보안 미들웨어보다 앞에 등록). 원시 AIMessage/스트림을 최종 출력으로 노출하지 않음 |
| ⑤ Tool·⑥ RAG | 반환 데이터의 개인정보 제거. Tool 결과는 호출 시 불신 데이터로 감싸며 `tool_call_id` 유지 |
| 통합 | 준비 함수 오류 처리, 사용자 진술/외부 원문 입력 구분, 체크포인트·승인·감사 통합 테스트 |

`before_*`는 등록 순서, `after_*`는 역순으로 실행된다. `wrap_model_call`은 중첩되므로
ContentIsolation을 메시지를 추가하는 wrapper보다 안쪽에 둔다. 단순히 전체 리스트 끝에
출력 감사를 붙이면 의도한 실행 순서가 되지 않는다. 요약 미들웨어도 입력 출처와 마스킹을 유지해야 한다.
현재 구현은 ①·②·④의 완성 코드를 대신하지 않는다.
`pii.mask_text`는 `MaskResult`를 반환하므로 준비 함수에는 `pii.prepare_masked_input()` 또는
`pii.make_guard_masker(vault)`를 사용한다. 반환된 vault는 invoke 입력의 `pii_vault`로 함께 넘기고 버리지 않는다.
④의 마스킹은 URL 퍼센트 인코딩 번호도 처리한다. 입력 보안의 URL 디코딩은 공격 문구 검사 용도로 별개다.

### 검증 및 공유

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

시연 흐름은 **정상 상담 / 인젝션 포함 원문 상담 / 무관한 요청** 3개다.
공격 예시는 **본문 삽입형 / URL 삽입형** 2개이며, 피해자가 해당 원문을 전달하는 형태로 테스트한다.
추가 자동 테스트는 마스킹 연결, 판별 실패, 새 턴·스레드 분리, 동기·비동기 실행 등을 검증한다.
모의 모델 테스트이므로 실제 nano의 공격 탐지율이나 개인정보 마스킹 정확도를 입증하지 않는다.

보안 연구보고서는 `.gitignore`로 제외한 로컬 참고 자료다. 코드·문서가 이에 의존하지 않도록 유지하고
`git add -f`로 포함하지 않는다. 공통 변경이 main에 머지된 **이후** 각자 작업 브랜치에서
`git fetch origin` 및 `git rebase origin/main`으로 반영한다. 충돌 시 공통 타입·설계서 계약을 유지한다.
이 안내 자체는 커밋·push·merge 완료를 의미하지 않는다.

## 팀·담당

담당자와 역할은 [`README.md`](README.md)와 설계서 상단 표를 참조한다. 특정 영역(Tool, Middleware, Guardrail 등)을 수정할 때는 설계서에 적힌 담당자 확인이 필요한 항목("미정", "담당자 확정 필요")을 임의로 결정하지 않는다.
