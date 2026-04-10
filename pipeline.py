import json
import logging
import os
import re
import requests
from urllib.parse import urlparse, parse_qs
from google import genai
from google.genai import types

log = logging.getLogger(__name__)

JINA_BASE_URL = 'https://r.jina.ai/'
NOTION_API_VERSION = '2022-06-28'
NOTION_PAGES_URL = 'https://api.notion.com/v1/pages'


def transform_saramin_url(url: str) -> str:
    """사람인 링크를 감지하여 본문 전용(view-detail) 주소로 변환한다."""
    if "saramin.co.kr" not in url:
        return url
    parsed = urlparse(url)
    query_params = parse_qs(parsed.query)
    rec_idx = query_params.get('rec_idx', [None])[0]
    if not rec_idx:
        match = re.search(r'rec_idx=(\d+)', url)
        if match:
            rec_idx = match.group(1)
    if rec_idx:
        return f"https://www.saramin.co.kr/zf_user/jobs/relay/view-detail?rec_idx={rec_idx}"
    return url


def fetch_with_jina(url: str) -> str:
    """Jina Reader로 URL 본문을 가져온다. 실패 시 예외를 발생시킨다."""
    transformed_url = transform_saramin_url(url)
    if transformed_url != url:
        log.info('[1/3] 사람인 URL 변환: %s → %s', url, transformed_url)
    log.info('[1/3] Jina로 본문 가져오는 중... URL: %s', transformed_url)
    jina_url = JINA_BASE_URL + transformed_url
    response = requests.get(jina_url, timeout=30)
    response.raise_for_status()
    log.info('[1/3] 본문 가져오기 완료 (글자 수: %d)', len(response.text))
    return response.text


def summarize_job_posting(text: str, url: str) -> dict:
    """Gemini API로 채용공고 본문을 분석해 Notion API용 딕셔너리를 반환한다."""
    client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

    prompt = f"""
너는 채용 공고를 분석해서 아래 JSON 스키마에 맞춰 데이터를 추출하는 전문 파서야.
반드시 아래 스키마 구조를 그대로 유지하면서 값(value)만 채워서 응답해.

[JSON 스키마]
{{
  "properties": {{
    "회사명": {{
      "title": [{{"text": {{"content": "회사명을 입력"}}}}]
    }},
    "직무": {{
      "select": {{"name": "직무명 입력 (예: 클라우드 엔지니어, 백엔드 개발자 등)"}}
    }},
    "기술스택": {{
      "multi_select": [
        {{"name": "기술1"}},
        {{"name": "기술2"}}
      ]
    }},
    "경력": {{
      "select": {{"name": "경력 조건 입력 (예: 경력무관, 신입, 1년이상, 3년이하 등)"}}
    }},
    "채용유형": {{
      "select": {{"name": "인턴 또는 정규직"}}
    }},
    "지역": {{
      "select": {{"name": "근무지 입력 (예: 서울, 대전, 판교 등)"}}
    }},
    "링크": {{
      "url": "{url}"
    }}
  }},
  "detailed_content": "주요업무, 자격요건, 우대사항 등 공고 핵심 내용을 마크다운 형식으로 상세히 요약한 긴 문자열"
}}

[절대 지켜야 할 규칙]
1. 응답은 반드시 위 스키마와 동일한 구조의 유효한 JSON 객체 하나로만 출력할 것.
2. 기술스택이 명시되지 않았다면 "multi_select": [] 로 비워둘 것. 없는 기술을 지어내지 말 것.
3. 경력, 채용유형, 지역이 명시되지 않았다면 해당 "select": {{"name": ""}} 처럼 빈 문자열로 둘 것. 지어내지 말 것.
4. detailed_content는 마크다운 헤더(## 주요업무, ## 자격요건 등)를 사용해 가독성 있게 작성할 것.
5. 링크 값은 반드시 "{url}" 그대로 사용할 것.
6. "직무명, 지역, 경력 등 select 타입에 들어갈 값에는 **쉼표(,)**를 절대 사용하지 않을 것. 쉼표가 있다면 공백이나 하이픈(-)으로 대체할 것."

[채용공고 텍스트]
{text}
"""

    log.info('[2/3] Gemini API 호출 중...')
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
        ),
    )
    result = json.loads(response.text or "")
    log.info('[2/3] Gemini 분석 완료')
    return result


def markdown_to_notion_blocks(markdown: str) -> list:
    """마크다운 문자열을 Notion 블록 리스트로 변환한다.
    ## → heading_2, ### → heading_3, 나머지 → paragraph.
    Notion rich_text 최대 2000자 제한을 준수해 긴 줄은 분할한다.
    """
    blocks = []
    MAX_LEN = 2000

    for line in markdown.splitlines():
        stripped = line.strip()
        if not stripped:
            continue

        if stripped.startswith('### '):
            block_type, content = 'heading_3', stripped[4:]
        elif stripped.startswith('## '):
            block_type, content = 'heading_2', stripped[3:]
        elif stripped.startswith('# '):
            block_type, content = 'heading_1', stripped[2:]
        else:
            block_type, content = 'paragraph', stripped

        while content:
            chunk, content = content[:MAX_LEN], content[MAX_LEN:]
            blocks.append({
                "object": "block",
                "type": block_type,
                block_type: {
                    "rich_text": [{"text": {"content": chunk}}]
                }
            })
            block_type = 'paragraph'

    return blocks


def sanitize_properties(properties: dict) -> dict:
    """Notion API 전달 전 속성값을 정제한다.
    - select 필드: null·빈 값 제거, 쉼표를 공백으로 대체
    - multi_select 필드: 빈 항목 제거, 쉼표 포함 시 개별 항목으로 분리
    """
    for key in ("직무", "경력", "채용유형", "지역"):
        prop = properties.get(key)
        if prop is None:
            continue
        select_obj = prop.get("select")
        if not select_obj or not isinstance(select_obj, dict):
            properties.pop(key, None)
            continue
        name = (select_obj.get("name") or "").strip()
        if not name:
            properties.pop(key, None)
        elif "," in name:
            log.warning('[sanitize] select 필드 "%s" 쉼표 제거: %s', key, name)
            properties[key] = {"select": {"name": name.replace(",", " ")}}

    for key in ("기술스택",):
        prop = properties.get(key)
        if prop is None:
            continue
        items = prop.get("multi_select") or []
        clean_items = []
        for item in items:
            name = (item.get("name") or "").strip()
            if not name:
                continue
            if "," in name:
                log.warning('[sanitize] multi_select 필드 "%s" 쉼표 분리: %s', key, name)
                for part in name.split(","):
                    part = part.strip()
                    if part:
                        clean_items.append({"name": part})
            else:
                clean_items.append({"name": name})
        properties[key] = {"multi_select": clean_items}

    return properties


def create_notion_page(gemini_result: dict) -> dict:
    """Gemini 결과를 Notion 데이터베이스에 페이지로 저장한다. 생성된 페이지 정보를 반환한다."""
    properties = sanitize_properties(gemini_result["properties"])

    payload = {
        "parent": {"database_id": os.environ.get("NOTION_DATABASE_ID")},
        "properties": properties,
        "children": markdown_to_notion_blocks(gemini_result.get("detailed_content", ""))
    }

    log.info('[3/3] Notion API 호출 중...')

    headers = {
        "Authorization": f"Bearer {os.environ.get('NOTION_API_KEY')}",
        "Content-Type": "application/json",
        "Notion-Version": NOTION_API_VERSION,
    }

    response = requests.post(NOTION_PAGES_URL, headers=headers, json=payload, timeout=30)
    response.raise_for_status()
    log.info('[3/3] Notion 페이지 생성 완료')
    return response.json()


def process_url(url: str) -> dict:
    """URL을 받아 Jina → Gemini → Notion 파이프라인을 실행한다. 생성된 Notion 페이지 정보를 반환한다."""
    log.info('===== 파이프라인 시작: %s =====', url)
    content = fetch_with_jina(url)
    result = summarize_job_posting(content, url)
    page = create_notion_page(result)
    log.info('===== 파이프라인 완료 =====')
    return page
