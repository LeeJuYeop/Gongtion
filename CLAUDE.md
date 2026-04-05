# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 프로젝트 개요

**Gongtion** — 두 가지 모드로 채용공고를 Notion DB에 자동 저장한다.

- **반자동 (Slack bot + Lambda)**: Slack 채널에 URL을 붙여넣으면 처리
- **완전자동 (GitHub Actions cron)**: 4시간마다 사람인·원티드·잡코리아·직행을 폴링해 키워드 매칭 공고를 자동 수집

두 모드 모두 공통 파이프라인 `pipeline.py` → Jina Reader → Gemini API → Notion DB를 사용한다.

## 아키텍처

```
[Slack URL 입력]          [GitHub Actions cron: 0 */4 * * *]
      │                              │
  app.py                        crawler.py
      └────────────┬───────────────┘
                   ▼
             pipeline.py
          (process_url 진입점)
                   │
         Jina → Gemini → Notion
```

### 핵심 모듈

- **`pipeline.py`**: 공용 파이프라인. `transform_saramin_url`, `fetch_with_jina`, `summarize_job_posting`, `markdown_to_notion_blocks`, `sanitize_properties`, `create_notion_page`, `process_url` 포함. Gemini 프롬프트·Notion 스키마 변경은 이 파일만 수정하면 된다.
- **`app.py`**: Slack Bolt 핸들러 + Lambda 진입점. pipeline.py를 import해 사용. Slack/Lambda 특수 처리만 담당.
- **`crawler.py`**: GitHub Actions에서 실행되는 자동 크롤러. `keywords.json`을 읽어 각 사이트에서 URL을 수집하고, Notion DB 쿼리로 중복 확인 후 `process_url` 호출.
- **`keywords.json`**: 크롤러 키워드 설정. 코드 수정 없이 이 파일만 변경하면 된다.

### Lambda 특수 처리 (`app.py`)

- **url_verification**: API Gateway를 통한 Slack의 challenge 요청을 직접 처리 (Bolt 핸들러 우회)
- **Retry 무시**: `X-Slack-Retry-Num` 헤더 감지 시 즉시 200 반환 (중복 처리 방지)
- **Lazy listener**: `ack_message`로 즉시 ACK 후 `process_message`를 lazy 실행 (Slack 3초 응답 제한 대응)
- **사람인 URL 변환**: `transform_saramin_url()` — 사람인 링크를 `view-detail` 형식으로 변환해 크롤링 품질 향상

### Notion 블록 변환 (`markdown_to_notion_blocks`)

Gemini가 반환한 마크다운 `detailed_content`를 Notion 블록 배열로 변환. Notion rich_text 2000자 제한으로 인해 긴 줄은 자동 분할된다.

## 배포

### Lambda (반자동 모드)
`main` 브랜치에서 `app.py`, `pipeline.py`, `requirements.txt` 중 하나라도 변경되면 GitHub Actions (`.github/workflows/main.yml`)가 자동으로:
1. `requirements.txt`를 `./package/`에 설치
2. `package/` + `app.py` + `pipeline.py`를 `deployment_package.zip`으로 패키징
3. AWS Lambda 함수 `Gongtion-Lambda` (리전: `ap-northeast-2`)에 배포

### 자동 크롤러
`.github/workflows/crawler.yml`이 UTC 0/4/8/12/16/20시에 `crawler.py`를 실행한다. GitHub Actions 탭의 `workflow_dispatch`로 수동 실행도 가능하다.
필요 Secrets: `GEMINI_API_KEY`, `NOTION_API_KEY`, `NOTION_DATABASE_ID`

## 주의사항

- `build/` 디렉토리는 Lambda 패키징용으로 미리 설치된 의존성이며, 직접 편집하지 않는다.
- `localtest.py`는 운영 배포 대상이 아니다. 운영 코드는 `app.py`, `pipeline.py`만 수정한다.
- .gitignore에 등록된 파일들은 테스트 혹은 기록을 위한 용도로, 따로 명령이 없다면 내용을 참고하거나 수정하지 않는다.
- `beautifulsoup4`는 `requirements.txt`에 포함하지 않는다. Lambda 패키지 크기 절감을 위해 crawler.yml에서만 별도 설치한다.
- Notion API는 `null` date 값을 허용하지 않으므로, 마감기한 미기재 시 해당 속성을 payload에서 제외해야 한다. (`sanitize_properties`가 처리)
- select 타입 값에 쉼표(`,`)가 포함되면 Notion API 오류가 발생하므로 Gemini 프롬프트에 명시적으로 금지 규칙을 포함한다. (`sanitize_properties`가 2차 방어로 처리)
- 크롤러 HTML 셀렉터는 사이트 구조 변경 시 `crawler.py` 각 함수의 `NOTE:` 주석을 참조해 수정한다.
