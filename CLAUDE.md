# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 프로젝트 개요

**Gongtion** — Slack 채널에서 채용공고 URL을 감지하면 Jina Reader로 본문을 크롤링하고, Gemini API로 정형화한 뒤 Notion 데이터베이스에 자동 저장하는 Slack 봇.

### Lambda 특수 처리 (`app.py`)

- **url_verification**: API Gateway를 통한 Slack의 challenge 요청을 직접 처리 (Bolt 핸들러 우회)
- **Retry 무시**: `X-Slack-Retry-Num` 헤더 감지 시 즉시 200 반환 (중복 처리 방지)
- **Lazy listener**: `ack_message`로 즉시 ACK 후 `process_message`를 lazy 실행 (Slack 3초 응답 제한 대응)
- **사람인 URL 변환**: `transform_saramin_url()` — 사람인 링크를 `view-detail` 형식으로 변환해 크롤링 품질 향상

### Notion 블록 변환 (`markdown_to_notion_blocks`)

Gemini가 반환한 마크다운 `detailed_content`를 Notion 블록 배열로 변환. Notion rich_text 2000자 제한으로 인해 긴 줄은 자동 분할된다.

## 배포

`main` 브랜치에 push하면 GitHub Actions (`.github/workflows/main.yml`)가 자동으로:
1. `requirements.txt`를 `./package/`에 설치
2. `package/` + `app.py`를 `deployment_package.zip`으로 패키징
3. AWS Lambda 함수 `Gongtion-Lambda` (리전: `ap-northeast-2`)에 배포

## 주의사항

- `build/` 디렉토리는 Lambda 패키징용으로 미리 설치된 의존성이며, 직접 편집하지 않는다.
- `localtest.py`는 운영 배포 대상이 아니다. 운영 코드는 `app.py`만 수정한다.
- .gitignore에 등록된 파일들은 테스트 혹은 기록을 위한 용도로, 따로 명령이 없다면 내용을 참고하거나 수정하지 않는다.
- Notion API는 `null` date 값을 허용하지 않으므로, 마감기한 미기재 시 해당 속성을 payload에서 제외해야 한다.
- select 타입 값에 쉼표(`,`)가 포함되면 Notion API 오류가 발생하므로 Gemini 프롬프트에 명시적으로 금지 규칙을 포함한다.
