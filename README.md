# Un Hook

보이스피싱·스미싱·메신저 피싱 등 금융사기 의심 상황을 대화로 파악하고, 피해 단계에 맞는 대응 행동을 안내하는 LangChain 기반 AI Agent.

## 문서

- [AI Agent 설계서](docs/agent-design.md) — 팀 공통 기술 명세. 모든 구현은 이 문서를 기준으로 한다.
- [구현 파일 및 공유 계약](docs/agent-design.md#5-구현-파일-및-공유-계약) — 파일별 작업 범위와 공통 코드 사용 규칙.

## 공통 코드

`schemas.py`는 출력 스키마와 Tool 결과 타입, `state.py`는 State와 Runtime Context를 정의한다.
`config.py`는 설계서에 확정된 모델명과 승격 판단 기준을 제공한다.
현재는 공통 계약 구현 단계이며, Agent·Tool·보안 기능과 시연 화면은 구현 예정이다.

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

```python
from config import DEFAULT_MODEL, ESCALATION_MODEL, LOW_CONFIDENCE_THRESHOLD
from schemas import ScamAssessment
from state import RuntimeContext, UnHookState, create_initial_state
```

API 키 이름은 `.env.example`을 참고한다. 키는 실행 환경에 설정하며,
현재 공통 코드는 `.env`를 자동으로 읽거나 API를 호출하지 않는다.

## 디렉터리 구조

아래는 구현 예정 파일까지 포함한 팀 작업 구조다. `[예정]`은 아직 생성하지 않은 파일/폴더이며,
숫자는 작업 묶음 번호다. 번호와 팀원 이름의 매핑은 별도 합의한다.

```text
UnHook/
|-- README.md                  프로젝트 개요 및 팀 작업 구조
|-- AGENTS.md                  공통 개발 안내
|-- CLAUDE.md                  AGENTS.md 참조
|-- .gitignore
|-- docs/
|   |-- agent-design.md        단일 기준 설계서 및 공유 계약
|   `-- images/                설계 이미지
|-- requirements.txt           공통 코드 의존성
|-- .env.example               환경변수 이름, 실제 키는 포함하지 않음
|-- config.py                  공통 모델명 및 승격 기준, 전원 공유
|-- app.py                     [예정] 시연 화면, 나중에 연결
|
|-- schemas.py                 출력 스키마 및 Tool 결과 타입 (1, 전원 공유)
|-- state.py                   State 및 Context (2, 전원 공유)
|
|-- agent.py                   [예정] Agent 조립, 모델, 프롬프트 (1)
|-- middleware.py              [예정] 피해 상태, 긴급 분기, 승인, 조립 (2)
|-- guards.py                  [예정] 주제 필터, 인젝션, 원문 격리 (3)
|-- pii.py                     [예정] 개인정보 마스킹, 토큰화 (4)
|-- audit.py                   [예정] 응답 검증 (4)
|-- tools.py                   [예정] URL 검사, 번호 확인, 신고 (5)
|-- memory.py                  [예정] 과거 신고 이력 저장 및 대조 (5)
|-- rag.py                     [예정] 대응 절차 검색 (6)
|
|-- data/                      [예정]
|   |-- kisa_urls.csv           URL 검사 데이터 (5)
|   |-- contacts.json          공식 연락처 (6)
|   |-- playbook_fallback.json 검색 실패 시 기본 절차 (6)
|   `-- playbook_docs/          금감원/KISA 원문 (6)
`-- tests/
    `-- test_contracts.py      공통 스키마 및 State 연결 검증
```

`memory.py`는 Store 저장·조회·대조 로직을 제공한다. 이를 호출해 State와 프롬프트에
반영하는 `MemoryInjectMiddleware`는 `middleware.py`에서 구현한다.
이력 조회를 별도 Tool로 등록하지 않는 기존 설계를 따른다.

미정인 신고 정리서·이력 레코드의 세부 필드, 모듈 생성 함수의 인자와 반환 계약은
[설계서 5절](docs/agent-design.md#5-구현-파일-및-공유-계약)을 기준으로 담당자끼리 확정한 후 구현한다.

## 팀 (5층 6반 1조)

| 이름 | 담당 |
|---|---|
| 이지수 | 보안 및 Guardrail 설계·개발 |
| 강준모 | Context/State 및 Middleware 설계·개발 |
| 김가연 | 테스트·통합 및 시연 |
| 노윤성 | Tool/API 설계·개발 |
| 백승현 | 서비스 기획 및 요구사항 설계 |
| 장민서 | Agent Core 및 LLM 설계·개발 |
