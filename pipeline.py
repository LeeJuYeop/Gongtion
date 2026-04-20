import json
import logging
import os
import pathlib
import re
import time
import requests
from urllib.parse import urlparse, parse_qs
from google import genai
from google.genai import types

log = logging.getLogger(__name__)

_PROFILE_PATH = pathlib.Path(__file__).parent / "profile.json"
_user_profile: dict | None = None
_profile_loaded = False


def load_user_profile() -> dict | None:
    global _user_profile, _profile_loaded
    if _profile_loaded:
        return _user_profile
    _profile_loaded = True
    try:
        with _PROFILE_PATH.open(encoding="utf-8") as f:
            _user_profile = json.load(f)
        log.info("[profile] 로드 완료: 기술스택 %d개, 프로젝트 %d개",
                 len(_user_profile.get("tech_stack", [])),
                 len(_user_profile.get("projects", [])))
    except FileNotFoundError:
        log.info("[profile] profile.json 없음 — 개인화 비활성")
    except Exception as e:
        log.warning("[profile] 로드 실패 (%s) — 개인화 비활성", e)
    return _user_profile

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


def _build_gemini_prompt(text: str, url: str, extract: set[str] | None, profile: dict | None = None) -> str:
    """extract 집합에 따라 Gemini 프롬프트를 동적으로 구성한다.
    extract=None이면 전체 필드(+ detailed_content)를 추출하는 풀 프롬프트를 반환한다.
    extract가 주어지면 해당 필드만 추출하는 경량 프롬프트를 반환한다.
    어떤 모드든 ai_comment는 항상 포함된다.
    """
    full = extract is None

    # ── properties 스키마 ──────────────────────────────────────────────────────
    schema_parts: list[str] = []
    if full:
        schema_parts += [
            '    "회사명": {{"title": [{{"text": {{"content": "회사명을 입력"}}}}]}}',
            '    "공고명": {{"rich_text": [{{"text": {{"content": "공고 제목 그대로 입력"}}}}]}}',
        ]
    if full or "직무" in extract:
        schema_parts.append(
            '    "직무": {{"multi_select": [{{"name": "서버_백엔드 | DevOps_SRE | 시스템_네트워크 | 시스템소프트웨어 | 웹풀스택 중 해당하는 것 모두. 없으면 기타"}}]}}'
        )
    if full or "기술스택" in extract:
        schema_parts.append(
            '    "기술스택": {{"multi_select": [{{"name": "기술1"}}, {{"name": "기술2"}}]}}'
        )
    if full or "경력" in extract:
        schema_parts.append('    "경력": {{"select": {{"name": "신입 | 경력 | 무관 중 해당하는 것"}}}}')
    if full or "채용유형" in extract:
        schema_parts.append('    "채용유형": {{"select": {{"name": "인턴 또는 정규직"}}}}')
    if full:
        schema_parts += [
            '    "지역": {{"multi_select": [{{"name": "시/도 단위 근무지1"}}, {{"name": "시/도 단위 근무지2"}}]}}',
            f'    "링크": {{"url": "{url}"}}',
        ]
    properties_schema = ",\n".join(schema_parts)

    # ── detailed_content (풀 모드 전용) ───────────────────────────────────────
    detailed_schema = (
        ',\n  "detailed_content": "주요업무·자격요건·우대사항 등을 마크다운 형식으로 상세히 요약한 긴 문자열"'
        if full else ""
    )

    # ── 규칙 ──────────────────────────────────────────────────────────────────
    rules: list[str] = [
        "1. 응답은 반드시 위 스키마와 동일한 구조의 유효한 JSON 객체 하나로만 출력할 것.",
    ]
    if full or "기술스택" in extract:
        rules.append('2. 기술스택이 명시되지 않았다면 "multi_select": [] 로 비워둘 것. 없는 기술을 지어내지 말 것.')
    if full or "경력" in extract:
        rules.append('3. 경력이 명시되지 않았다면 "select": {{"name": ""}} 처럼 빈 문자열로 둘 것. "경력무관"·"누구나" 등은 반드시 "무관"으로 입력할 것.')
    if full or "채용유형" in extract:
        rules.append('4. 채용유형이 명시되지 않았다면 "select": {{"name": ""}} 처럼 빈 문자열로 둘 것.')
    if full or "직무" in extract:
        rules.append('5. 직무는 반드시 "서버_백엔드", "DevOps_SRE", "시스템_네트워크", "시스템소프트웨어", "웹풀스택", "기타" 중에서만 선택. 복수 해당 시 여러 개 포함 가능.')
    if full:
        rules.append(f'6. 지역이 명시되지 않았다면 [] 로 비워둘 것. 시/도 단위(서울·경기 등)로만 입력.')
        rules.append(f'7. 링크 값은 반드시 "{url}" 그대로 사용할 것.')
        rules.append('8. detailed_content는 마크다운 헤더(## 주요업무, ## 자격요건 등)를 사용해 가독성 있게 작성할 것.')
    rules.append('9. select 타입 값에는 쉼표(,)를 절대 사용하지 않을 것.')
    rules.append('10. ai_comment는 이 포지션의 핵심 특징을 1~2문장으로 간결하게 요약할 것. (예: "신입 가능한 토스증권 원장 플랫폼 포지션. Kafka/Kubernetes 실전 경험 가능하나 핀테크 도메인 지식 요구됨.")')

    if profile:
        tech = ", ".join(profile.get("tech_stack", []))
        interests = ", ".join(profile.get("learning_interests", []))
        projects_lines = "\n".join(
            f'- {p["name"]}: {p["description"]} [사용기술: {", ".join(p.get("tech_used", [])) or "미기재"}]'
            for p in profile.get("projects", [])
        )
        profile_block = f"""
[지원자 프로필]
보유 기술: {tech}
학습 관심: {interests}
주요 프로젝트:
{projects_lines}
"""
        rules.append(
            '11. personal_comment는 [지원자 프로필]을 참고해 다음 두 가지를 2~3문장으로 분석할 것: '
            '(a) 지원자 보유 기술 중 이 포지션과 매칭되는 것 vs 새로 배워야 하는 것, '
            '(b) 지원자 프로젝트 경험 중 이 포지션 지원 시 어필할 수 있는 것. '
            '프로필이 공고와 전혀 무관하면 "프로필과 연관성 낮음"으로 간결히 작성.'
        )
    else:
        profile_block = ""

    personal_comment_schema = (
        ',\n  "personal_comment": "지원자 관점의 개인화 분석 (2~3문장)"'
        if profile else ""
    )

    rules_str = "\n".join(rules)

    return f"""너는 채용 공고를 분석해서 아래 JSON 스키마에 맞춰 데이터를 추출하는 전문 파서야.
반드시 아래 스키마 구조를 그대로 유지하면서 값(value)만 채워서 응답해.

[JSON 스키마]
{{
  "properties": {{
{properties_schema}
  }},
  "ai_comment": "이 포지션의 핵심을 1~2문장으로 요약"{personal_comment_schema}{detailed_schema}
}}

[절대 지켜야 할 규칙]
{rules_str}
{profile_block}
[채용공고 텍스트]
{text}
"""


def summarize_job_posting(text: str, url: str, extract: set[str] | None = None, profile: dict | None = None) -> dict:
    """Gemini API로 채용공고 본문을 분석해 딕셔너리를 반환한다.
    extract=None이면 모든 속성과 detailed_content를 추출한다.
    extract가 주어지면 해당 속성만 추출한다 (ai_comment는 항상 포함).
    profile이 주어지면 personal_comment(개인화 분석)도 함께 추출한다.
    """
    client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
    prompt = _build_gemini_prompt(text, url, extract, profile)

    log.info('[2/3] Gemini API 호출 중... (추출 필드: %s)', "전체" if extract is None else extract)
    max_retries = 3
    response = None
    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                ),
            )
            break
        except Exception as e:
            is_transient = any(k in str(e) for k in ("503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED"))
            if is_transient and attempt < max_retries - 1:
                wait = 10 * (2 ** attempt)  # 10s → 20s → 40s
                log.warning('[2/3] Gemini 일시적 오류 — %d초 후 재시도 (%d/%d): %s', wait, attempt + 1, max_retries, e)
                time.sleep(wait)
            else:
                raise
    result = json.loads(response.text or "")  # type: ignore[union-attr]
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


# 시/도 단위 정규화 테이블: 입력 접두사 → 표준 이름
_REGION_PREFIXES = [
    ("서울", "서울"), ("경기", "경기"), ("인천", "인천"),
    ("부산", "부산"), ("대구", "대구"), ("광주", "광주"),
    ("대전", "대전"), ("울산", "울산"), ("세종", "세종"),
    ("강원", "강원"),
    ("충북", "충북"), ("충청북", "충북"),
    ("충남", "충남"), ("충청남", "충남"),
    ("전북", "전북"), ("전라북", "전북"),
    ("전남", "전남"), ("전라남", "전남"),
    ("경북", "경북"), ("경상북", "경북"),
    ("경남", "경남"), ("경상남", "경남"),
    ("제주", "제주"),
]


def _normalize_region(raw: str) -> str:
    """'서울 강남구', '경기도 성남시' 등을 시/도 단위 표준명으로 변환한다."""
    raw = raw.strip()
    for prefix, canonical in _REGION_PREFIXES:
        if raw.startswith(prefix):
            return canonical
    return raw  # 알 수 없는 지역은 원본 유지


def sanitize_properties(properties: dict) -> dict:
    """Notion API 전달 전 속성값을 정제한다.
    - select 필드: null·빈 값 제거, 쉼표를 공백으로 대체
    - multi_select 필드(기술스택·지역): 빈 항목 제거, 지역은 시/도 단위로 정규화
    """
    for key in ("경력", "채용유형"):
        prop = properties.get(key)
        if prop is None:
            continue
        select_obj = prop.get("select")
        if not select_obj or not isinstance(select_obj, dict):
            properties.pop(key, None)
            continue
        raw_name = select_obj.get("name")
        if isinstance(raw_name, list):
            log.warning('[sanitize] select 필드 "%s" 리스트 반환 — 첫 번째 값 사용: %s', key, raw_name)
            raw_name = raw_name[0] if raw_name else ""
        name = (raw_name or "").strip()
        if not name:
            properties.pop(key, None)
        elif "," in name:
            log.warning('[sanitize] select 필드 "%s" 쉼표 제거: %s', key, name)
            properties[key] = {"select": {"name": name.replace(",", " ")}}

    for key in ("직무", "기술스택", "지역"):
        prop = properties.get(key)
        if prop is None:
            continue
        items = prop.get("multi_select") or []
        # Gemini가 select로 잘못 반환한 경우 변환
        if not items and prop.get("select"):
            select_name = (prop["select"].get("name") or "").strip()
            items = [{"name": select_name}] if select_name else []
        clean_items = []
        seen: set[str] = set()
        for item in items:
            raw_name = item.get("name") if isinstance(item, dict) else item
            # Gemini가 name을 리스트로 묶어 반환한 경우 개별 항목으로 분리
            if isinstance(raw_name, list):
                raw_names = [str(x).strip() for x in raw_name if x]
            else:
                raw_names = [(raw_name or "").strip()]
            for raw in raw_names:
                if not raw:
                    continue
                name = _normalize_region(raw) if key == "지역" else raw
                if "," in name and key != "지역":
                    log.warning('[sanitize] multi_select 필드 "%s" 쉼표 분리: %s', key, name)
                    for part in name.split(","):
                        part = part.strip()
                        if part and part not in seen:
                            clean_items.append({"name": part})
                            seen.add(part)
                elif name and name not in seen:
                    clean_items.append({"name": name})
                    seen.add(name)
        properties[key] = {"multi_select": clean_items}

    return properties


def create_notion_page(gemini_result: dict, direct_content: str | None = None) -> dict:
    """Gemini 결과를 Notion 데이터베이스에 페이지로 저장한다. 생성된 페이지 정보를 반환한다.
    direct_content가 주어지면 Gemini의 detailed_content 대신 해당 텍스트를 본문으로 사용한다.
    ai_comment가 있으면 본문 최상단에 callout 블록으로 추가한다.
    """
    properties = sanitize_properties(gemini_result["properties"])

    body_markdown = direct_content if direct_content is not None else gemini_result.get("detailed_content", "")
    body_blocks = markdown_to_notion_blocks(body_markdown)

    ai_comment = (gemini_result.get("ai_comment") or "").strip()
    personal_comment = (gemini_result.get("personal_comment") or "").strip()

    prefix_blocks = []
    if ai_comment:
        prefix_blocks.append({
            "object": "block",
            "type": "callout",
            "callout": {
                "rich_text": [{"text": {"content": ai_comment}}],
                "icon": {"type": "emoji", "emoji": "🤖"},
                "color": "gray_background",
            },
        })
    if personal_comment:
        prefix_blocks.append({
            "object": "block",
            "type": "callout",
            "callout": {
                "rich_text": [{"text": {"content": personal_comment}}],
                "icon": {"type": "emoji", "emoji": "👤"},
                "color": "blue_background",
            },
        })
    children = prefix_blocks + body_blocks

    payload = {
        "parent": {"database_id": os.environ.get("NOTION_DATABASE_ID")},
        "properties": properties,
        "children": children,
    }

    log.info('[3/3] Notion API 호출 중...')

    headers = {
        "Authorization": f"Bearer {os.environ.get('NOTION_API_KEY')}",
        "Content-Type": "application/json",
        "Notion-Version": NOTION_API_VERSION,
    }

    response = requests.post(NOTION_PAGES_URL, headers=headers, json=payload, timeout=30)
    if not response.ok:
        log.error('[3/3] Notion API 오류 응답: %s', response.text)
    response.raise_for_status()
    log.info('[3/3] Notion 페이지 생성 완료')
    return response.json()


def process_url(
    url: str,
    job_category: str | list[str] | None = None,
    job_regions: list[str] | None = None,
    content: str | None = None,
    job_title: str | None = None,
    job_company: str | None = None,
    job_career: str | None = None,
    job_employ_type: str | None = None,
    extract: set[str] | None = None,
) -> dict:
    """URL을 받아 Gemini → Notion 파이프라인을 실행한다. 생성된 Notion 페이지 정보를 반환한다.

    content      : 주어지면 Jina 생략. API에서 가져온 본문을 Notion에도 그대로 사용.
    extract      : Gemini가 추출할 속성 집합. None이면 전체 추출(풀 모드).
    job_*        : API에서 직접 얻은 값으로 Gemini 결과를 덮어쓴다.
    """
    log.info('===== 파이프라인 시작: %s =====', url)
    profile = load_user_profile()
    if content is None:
        content = fetch_with_jina(url)
        direct_content = None          # Gemini의 detailed_content 사용
    else:
        log.info('[1/3] 외부 제공 본문 사용 — Jina 생략 (글자 수: %d)', len(content))
        direct_content = content       # API 원문을 Notion 본문으로 직접 사용

    result = summarize_job_posting(content, url, extract, profile)

    # API 직접 주입값으로 Gemini 결과 덮어쓰기
    props = result.setdefault("properties", {})
    if job_category:
        cats = job_category if isinstance(job_category, list) else [job_category]
        props["직무"] = {"multi_select": [{"name": c} for c in cats if c]}
    if job_regions is not None:
        props["지역"] = {"multi_select": [{"name": r} for r in job_regions if r]}
    if job_title:
        props["공고명"] = {"rich_text": [{"text": {"content": job_title}}]}
    if job_company:
        props["회사명"] = {"title": [{"text": {"content": job_company}}]}
    if job_career:
        props["경력"] = {"select": {"name": job_career}}
    if job_employ_type:
        props["채용유형"] = {"select": {"name": job_employ_type}}
    # 링크는 항상 원본 URL로 고정
    props["링크"] = {"url": url}

    page = create_notion_page(result, direct_content)
    log.info('===== 파이프라인 완료 =====')
    return page
