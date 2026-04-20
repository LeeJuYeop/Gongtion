# Gongtion

## 프로젝트 목적
한국 채용공고를 자동 수집·AI 요약하여 Notion DB에 저장하는 시스템.

## 실행 방식
- **완전자동**: GitHub Actions 크론(화/수/토 07:00 KST) → `crawler.py` 실행
- **수동**: Slack에 공고 URL 붙여넣기 → AWS Lambda(`app.py`) 처리

## 파이프라인
`pipeline.py` 공유 로직: Jina(텍스트 추출) → Gemini 2.5 Flash(AI 분석·구조화) → Notion API(페이지 생성)

## 크롤링 대상
- 활성: 직행(공식 API), 원티드(비공식 JSON API)
- 비활성: 사람인, 잡코리아 (JS 렌더링 문제로 disabled)

## 중복 방지
공고 처리 전 Notion DB의 링크 필드를 조회하여 이미 존재하면 스킵.

## 키워드 설정
`keywords.json`에서 검색 키워드 및 사이트별 필터(평일/주말 모드) 관리. 코드 수정 불필요.

## 주요 파일
- `app.py` — Slack 이벤트 수신 + Lambda 핸들러
- `crawler.py` — 채용사이트 크롤러 + 배치 처리
- `pipeline.py` — Jina/Gemini/Notion 공통 파이프라인
- `keywords.json` — 검색 키워드·필터 설정

## 환경변수
`NOTION_API_KEY`, `NOTION_DATABASE_ID`, `GEMINI_API_KEY`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`

## 테스트 방법
로컬 테스트 없음. GitHub Actions `workflow_dispatch`(수동 트리거) 또는 Slack에 URL 입력으로만 검증.

## 참조 금지 파일
`.claudeignore`에 명시된 파일(`localtest.py`, `*test.py`, `*notes.txt`, `*notes.md`, `.env`, `.git/`)은 절대 참조하지 말 것.
