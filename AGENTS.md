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
| 보안·입력 처리 | 3.3 Guardrails |
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
```

## 팀·담당

담당자와 역할은 [`README.md`](README.md)와 설계서 상단 표를 참조한다. 특정 영역(Tool, Middleware, Guardrail 등)을 수정할 때는 설계서에 적힌 담당자 확인이 필요한 항목("미정", "담당자 확정 필요")을 임의로 결정하지 않는다.
