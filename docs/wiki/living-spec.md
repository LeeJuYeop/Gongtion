# Living Spec

Gongtion 프로젝트의 현재 시스템 상태를 반영하는 단일 진실원(single source of truth) 문서.
과거 이력은 기록하지 않으며, 각 카테고리 섹션은 가장 최신 결정 사항만 담는다.
변경 이력은 이 문서가 아닌 해당 변경을 발생시킨 UL의 "Living Spec Update Log" 섹션에서 추적한다.

## 개념

(아직 등록된 내용 없음)

## 기능

- **자동 크롤링**: 직행(공식 API), 원티드(비공식 JSON API)에서 공고 수집. Git 저장소 내 기존 저장 항목을 기준으로 중복 자동 방지.
- **AI 구조화(옵션)**: 공고 본문에서 직무·기술스택·경력·채용유형을 추출하고 한줄 요약 코멘트를 생성. 파이프라인 필수 코어에서 분리된 옵션 기능이며, AWS Lambda 공용 프록시를 통해 Gemini 2.5 Flash를 호출한다(사용자별 Gemini API 키 발급·등록 불필요).
- **개인화 분석(선택)**: `profile.json`에 보유 기술·프로젝트를 작성하면 공고와의 매칭도를 산출. 결과는 Git 파일에 함께 기록(과거 Notion 페이지 첨부 방식에서 저장 매체만 전환, 기능 자체는 유지).
- **Git 저장**: 수집·분석된 공고를 각 fork 저장소 내부에 파일로 저장(Notion DB 저장 폐기).
- **설정 파일 기반**: `keywords.json` 수정만으로 검색 키워드·직무·지역·경력 필터 변경 (코드 수정 불필요). 원본 태그 문자열(`depthTwos`/`regions`/`employeeTypes`)은 온보딩 스크립트가 자동 기입하므로 직접 손으로 입력할 필요 없음.
- **온보딩 스크립트(`scripts/setup.py`)**: Fork → Clone 후 1회 실행하는 로컬 CLI. (a) 직행 API를 실시간 조회해 현재 유효한 태그 전체를 수집하고 체크박스 picklist로 보여줘 `keywords.json`을 생성, (b) opt-in 질문 이후 대화형 입력(쉼표 리스트, projects 반복 루프)으로 `profile.json`을 생성(`json.dump` 후 자기 검증), (c) 생성된 파일을 `git add/commit/push`로 자동 반영(사용자의 기존 git 자격증명 재사용, push 전 origin이 본인 fork인지 확인). Actions 활성화·시크릿 등록·셀프테스트는 스크립트가 하지 않고 사용자가 웹 UI에서 수동으로 처리(아래 아키텍쳐 절 참고).

## 아키텍쳐

**실행 모델**: 분산형 fork 구조 유지 — 각 사용자가 저장소를 Fork하여 자신의 GitHub Actions 크론으로 실행(중앙 실행 모델로 전환하지 않음).

GitHub Actions 크론(화·수·토 07:00 KST 기준, 온보딩 스크립트가 ±0~25분 랜덤 오프셋 자동 배정, 커스터마이징 가능) → `crawler.py` 실행.
- 직행: 공고 목록 수집 → ProseMirror 본문 추출 → (옵션) AI 프록시로 기술스택 추출 → Git 저장.
- 원티드: 공고 목록 수집 → 상세 API 본문 추출 → (옵션) AI 프록시로 직무·경력·채용유형·기술스택 추출 → Git 저장.
- 회사명·공고명·지역은 API 응답에서 직접 주입하여 AI 할루시네이션 방지.
- API 본문 추출 실패 시 Jina Reader로 자동 폴백.
- 실행당 처리 한도: 직행 8건, 원티드 2건 (API 과금 대응).
- `keywords.json`의 `zighang`/`zighang_weekend` 필터를 상황별로 다르게 적용 가능하며, 스케줄·필터 전환 조건은 GitHub Actions 설정으로 커스터마이징 가능.

### AI 프록시 및 레이트리밋 대응
- AI 처리(구조화·한줄요약·개인화 매칭)는 사용자 fork가 직접 Gemini를 호출하지 않고, 운영자가 소유한 AWS Lambda 공용 프록시를 경유한다(운영자의 Gemini 키는 Lambda 환경변수에만 존재).
- 다수 사용자의 크론이 몰려 Gemini 무료 티어 레이트리밋(분당 15회)에 걸리는 문제를 다음 조합으로 대응: 크론 지터(자동 랜덤 오프셋) + Lambda 호출 직전 0~5분 랜덤 sleep + Lambda Reserved Concurrency 1~2로 전역 직렬화 + Lambda 내부 토큰버킷(분당 12회 자체 상한)과 429 시 지수 백오프 재시도(1s→2s→4s, 2~3회) + 여러 공고를 배치로 묶어 호출 수 절감.
- 재시도까지 실패하면 요약 없이 진행(최후 폴백).
- 이 규모(5명 내외, 약 1개월 운영)에서 SQS 등 큐 인프라는 도입하지 않음.
- 인증: GitHub OIDC(Actions `id-token: write` + API Gateway JWT Authorizer로 issuer/repository claim 검증)로 확정. 사용자 fork에 AI 관련 시크릿을 요구하지 않는다.

### 온보딩 자동화 원칙
- 온보딩 스크립트(`scripts/setup.py`)는 `gh` CLI에 의존하지 않는다 — 스크립트가 사용자 GitHub 계정 쓰기 권한(Secrets 등록, Actions 상태 변경)을 갖는 자동화는 결함 하나가 여러 사용자 계정에 반복 영향을 미치는 위험이 있기 때문. plain git(커밋/푸시)만 사용한다.
- Actions 활성화는 GitHub 웹 UI에서 사용자가 수동 2클릭으로 처리(Fork 저장소는 기본 비활성).
- 셀프테스트도 자동화하지 않고, 필요 시 사용자가 Actions 탭에서 수동으로 "Run workflow" 클릭. "Actions 비활성화" 상태는 에러 로그 없는 침묵 실패이므로 README 트러블슈팅 섹션과 setup.py 종료 시 "다음 할 일 체크리스트" 출력으로 보완.

### 파일 구성
- `crawler.py` — 사이트별 URL 수집, 중복 확인, 오케스트레이션
- `pipeline.py` — 본문 추출 → (옵션) AI 프록시 호출 → Git 저장 공통 파이프라인
- `scripts/setup.py` — 온보딩 CLI(keywords.json·profile.json 자동 생성 및 git 반영)
- `keywords.json` — 검색 키워드·직무·지역·경력 필터 설정(setup.py가 생성)
- `profile.json` — 개인화 분석용 프로필 (선택, setup.py가 생성)
- `profile.json.example` — 주석 달린 샘플(setup.py 없이 직접 편집하려는 사용자를 위한 스키마 안내)
- `requirements.txt` — Python 의존성
- `.github/workflows/crawler.yml` — 크론 스케줄(자동 지터 포함), Secrets 주입 없음(OIDC)

## 기술스택

- Python 3.12
- GitHub Actions (크론 스케줄링)
- Git (파일 기반 저장소 — 각 fork 내부)
- AWS Lambda (AI 공용 프록시, Gemini 호출 대행)
- Gemini 2.5 Flash (AI 구조화·요약, Lambda 경유)
- Jina Reader (본문 추출 폴백)
- 직행 공식 API, 원티드 비공식 JSON API (크롤링 소스)

사용자 fork 측 필수 환경변수(Secrets): 없음 — GitHub OIDC 인증으로 시크릿 등록 자체가 불필요.
운영자(Lambda) 측 환경변수: `GEMINI_API_KEY`.
