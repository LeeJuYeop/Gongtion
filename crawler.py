"""
자동 채용공고 크롤러 — GitHub Actions cron으로 4시간마다 실행된다.
각 채용사이트에서 키워드 매칭 공고 URL을 수집하고,
Notion DB에 없는 신규 공고만 Gemini → Notion 파이프라인으로 처리한다.
직행은 자체 API에서 본문을 추출해 Jina를 생략하고, 원티드는 Jina를 사용한다.
"""

import datetime
import json
import logging
import os
import re
import time

import requests
# from bs4 import BeautifulSoup  # 잡코리아 비활성화로 미사용
from dotenv import load_dotenv

from pipeline import process_url

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S'
)
log = logging.getLogger(__name__)

NOTION_API_KEY = os.environ.get("NOTION_API_KEY")
NOTION_DATABASE_ID = os.environ.get("NOTION_DATABASE_ID")
NOTION_API_VERSION = "2022-06-28"

# 사이트 요청 시 봇 차단을 줄이기 위한 브라우저 헤더
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9",
}


# ── 설정 로드 ──────────────────────────────────────────────────────────────────

def load_config() -> dict:
    """keywords.json 전체를 읽어 반환한다."""
    with open("keywords.json", encoding="utf-8") as f:
        return json.load(f)


# ── Notion 중복 확인 ───────────────────────────────────────────────────────────

def _notion_headers() -> dict:
    return {
        "Authorization": f"Bearer {NOTION_API_KEY}",
        "Notion-Version": NOTION_API_VERSION,
        "Content-Type": "application/json",
    }


def is_duplicate(url: str) -> bool:
    """Notion DB의 링크 필드를 쿼리해 이미 저장된 공고인지 확인한다."""
    res = requests.post(
        f"https://api.notion.com/v1/databases/{NOTION_DATABASE_ID}/query",
        headers=_notion_headers(),
        json={"filter": {"property": "링크", "url": {"equals": url}}},
        timeout=10,
    )
    res.raise_for_status()
    return len(res.json()["results"]) > 0


# ── 사이트별 URL 수집 ──────────────────────────────────────────────────────────

# def fetch_saramin_urls(keywords: list[str]) -> set[str]:
#     """사람인 Open API(oapi.saramin.co.kr)에서 채용공고 URL을 수집한다.
#
#     NOTE: 사람인 메인 사이트가 완전 JS 렌더링으로 전환되어 requests+BeautifulSoup으로는
#     공고 목록을 가져올 수 없다. 공식 Open API를 사용한다.
#     API 키는 https://oapi.saramin.co.kr 에서 무료 발급 가능하다.
#     GitHub Actions Secret에 SARAMIN_API_KEY를 추가해야 한다.
#     """
#     api_key = os.environ.get("SARAMIN_API_KEY")
#     if not api_key:
#         log.warning("[사람인] SARAMIN_API_KEY 환경변수가 없어 수집을 건너뜁니다.")
#         return set()
#
#     urls: set[str] = set()
#     for kw in keywords:
#         try:
#             resp = requests.get(
#                 "https://oapi.saramin.co.kr/job-search",
#                 params={
#                     "access-key": api_key,
#                     "keywords": kw,
#                     "count": 40,
#                     "start": 1,
#                     "_type": "json",
#                     "sort": "pd",
#                 },
#                 headers=BROWSER_HEADERS,
#                 timeout=15,
#             )
#             resp.raise_for_status()
#             data = resp.json()
#             before = len(urls)
#             for job in data.get("jobs", {}).get("job", []):
#                 job_url = job.get("url")
#                 if job_url:
#                     urls.add(job_url)
#             log.info("[사람인] '%s' → %d건", kw, len(urls) - before)
#         except Exception as e:
#             log.warning("[사람인] '%s' 수집 실패: %s", kw, e)
#         time.sleep(1)
#     return urls


def fetch_wanted_urls(keywords: list[str]) -> dict[str, dict]:
    """원티드 비공식 검색 API에서 채용공고 URL과 메타데이터를 수집한다.
    반환값: {url: {"company_name": "...", "title": "..."}}

    NOTE: 원티드 API가 422를 반환하면 로그의 'Response body' 줄을 확인해
    실제 validation 오류 메시지를 파악할 것.
    지속 실패 시 공식 OpenAPI(openapi.wanted.jobs) 전환을 고려:
    WANTED_API_KEY 환경변수를 추가하고 Authorization 헤더를 포함해야 한다.
    """
    url_meta: dict[str, dict] = {}
    for kw in keywords:
        try:
            resp = requests.get(
                "https://www.wanted.co.kr/api/v4/jobs",
                params={
                    "job_sort": "job.latest_order",
                    "limit": 20,
                    "offset": 0,
                    "query": kw,
                    "country": "kr",
                    "years": -1,
                    "locations": "all",
                },
                headers={
                    **BROWSER_HEADERS,
                    "Referer": "https://www.wanted.co.kr/",
                    "Accept": "application/json, text/plain, */*",
                },
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            before = len(url_meta)
            for job in data.get("data", []):
                job_id = job.get("id")
                if job_id:
                    url = f"https://www.wanted.co.kr/wd/{job_id}"
                    url_meta[url] = {
                        "company_name": job.get("company", {}).get("name", ""),
                        "title": job.get("position", ""),
                        "regions": [job["address"]["location"]] if job.get("address", {}).get("location") else [],
                    }
            log.info("[원티드] '%s' → %d건", kw, len(url_meta) - before)
        except requests.exceptions.HTTPError as e:
            body = e.response.text[:300] if e.response is not None else ""
            log.warning("[원티드] '%s' 수집 실패: %s | Response body: %s", kw, e, body)
        except Exception as e:
            log.warning("[원티드] '%s' 수집 실패: %s", kw, e)
        time.sleep(1)
    return url_meta


def fetch_wanted_content(url: str) -> str | None:
    """원티드 공고 URL에서 ID를 추출해 상세 API를 호출하고 본문을 마크다운으로 반환한다.
    detail 필드(intro/main_tasks/requirements/preferred_points/benefits)를 섹션별로 조합한다.
    실패 시 None을 반환해 Jina로 폴백되게 한다.
    """
    match = re.search(r'/wd/(\d+)', url)
    if not match:
        return None
    job_id = match.group(1)
    try:
        resp = requests.get(
            f"https://www.wanted.co.kr/api/v4/jobs/{job_id}",
            headers={**BROWSER_HEADERS, "Referer": "https://www.wanted.co.kr/", "Accept": "application/json"},
            timeout=10,
        )
        resp.raise_for_status()
        detail = resp.json().get("job", {}).get("detail") or {}
        if not detail:
            log.warning("[원티드] 상세 API 본문 없음 (%s) — Jina로 폴백", url)
            return None

        _SECTION = [
            ("intro",            "## 회사 소개"),
            ("main_tasks",       "## 주요 업무"),
            ("requirements",     "## 자격 요건"),
            ("preferred_points", "## 우대 사항"),
            ("benefits",         "## 복리후생"),
        ]
        parts: list[str] = []
        for key, header in _SECTION:
            text = (detail.get(key) or "").strip()
            if text:
                parts.append(f"{header}\n{text}")
        return "\n\n".join(parts) if parts else None
    except Exception as e:
        log.warning("[원티드] 상세 API 실패 (%s): %s — Jina로 폴백", url, e)
        return None


# def fetch_jobkorea_urls(keywords: list[str]) -> set[str]:
#     """잡코리아 검색결과 HTML에서 채용공고 URL을 수집한다.
#
#     NOTE: 잡코리아는 JS 렌더링에 의존할 수 있어 HTML 파싱이 실패할 수 있다.
#     동작하지 않으면 잡코리아 RSS(https://www.jobkorea.co.kr/rss) 엔드포인트를
#     feedparser로 파싱하는 방식으로 교체를 고려할 것.
#     """
#     urls: set[str] = set()
#     for kw in keywords:
#         try:
#             search_url = (
#                 f"https://www.jobkorea.co.kr/Search/"
#                 f"?stext={quote(kw)}&tabType=recruit&OrderBy=2"
#             )
#             resp = requests.get(search_url, headers=BROWSER_HEADERS, timeout=15)
#             resp.raise_for_status()
#             soup = BeautifulSoup(resp.text, "html.parser")
#             before = len(urls)
#             for a in soup.select(".list-post .information-title a"):
#                 href = a.get("href", "")
#                 if href:
#                     full = "https://www.jobkorea.co.kr" + href if href.startswith("/") else href
#                     urls.add(full)
#             log.info("[잡코리아] '%s' → %d건", kw, len(urls) - before)
#         except Exception as e:
#             log.warning("[잡코리아] '%s' 수집 실패: %s", kw, e)
#         time.sleep(1)
#     return urls


def _pm_node_to_lines(node: dict) -> list[str]:
    """ProseMirror 노드를 마크다운 줄 목록으로 재귀 변환한다."""
    ntype = node.get("type", "")
    children = node.get("content") or []

    if ntype == "text":
        return [node.get("text", "")]
    if ntype == "hardBreak":
        return ["\n"]
    if ntype in ("doc",):
        return [line for c in children for line in _pm_node_to_lines(c)]
    if ntype == "paragraph":
        text = "".join(line for c in children for line in _pm_node_to_lines(c)).strip()
        return [text] if text else [""]
    if ntype == "heading":
        level = node.get("attrs", {}).get("level", 2)
        text = "".join(line for c in children for line in _pm_node_to_lines(c)).strip()
        return [f"{'#' * level} {text}"]
    if ntype == "bulletList":
        lines = []
        for item in children:
            text = " ".join(t for c in (item.get("content") or []) for t in _pm_node_to_lines(c)).strip()
            if text:
                lines.append(f"- {text}")
        return lines
    if ntype == "orderedList":
        lines = []
        for i, item in enumerate(children, 1):
            text = " ".join(t for c in (item.get("content") or []) for t in _pm_node_to_lines(c)).strip()
            if text:
                lines.append(f"{i}. {text}")
        return lines
    if ntype == "image":
        return []
    # 알 수 없는 노드: 자식 재귀
    return [line for c in children for line in _pm_node_to_lines(c)]


def prosemirror_to_markdown(doc: dict) -> str:
    """직행 summary 필드의 ProseMirror JSON을 마크다운 문자열로 변환한다."""
    lines = [line for node in (doc.get("content") or []) for line in _pm_node_to_lines(node)]
    text = "\n".join(lines)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def _zighang_career(career_min: int, career_max: int) -> str:
    """직행 careerMin/careerMax 값을 경력 레이블로 변환한다.
    min=0, max=0 → 신입 / min=0, max>0 → 무관 / min>0 → 경력
    """
    if career_min == 0 and career_max == 0:
        return "신입"
    if career_min == 0:
        return "무관"
    return "경력"


def fetch_zighang_content(url: str) -> str | None:
    """직행 공고 URL에서 ID를 추출해 상세 API를 호출하고 본문을 마크다운으로 반환한다.
    상세 API 호출 실패 또는 본문 없을 경우 None을 반환해 Jina로 폴백되게 한다.
    """
    match = re.search(r'/recruitment/([a-f0-9-]+)', url)
    if not match:
        return None
    recruitment_id = match.group(1)
    try:
        resp = requests.get(
            f"https://api.zighang.com/api/recruitments/{recruitment_id}",
            headers=BROWSER_HEADERS,
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json().get("data") or {}
        summary = data.get("summary")
        if not summary or not summary.get("content"):
            log.warning("[직행] 상세 API 본문 없음 (%s) — Jina로 폴백", url)
            return None
        return prosemirror_to_markdown(summary)
    except Exception as e:
        log.warning("[직행] 상세 API 실패 (%s): %s — Jina로 폴백", url, e)
        return None


def fetch_zighang_urls(cfg: dict) -> dict[str, dict]:
    """직행(zighang.com) 공개 API로 채용공고 URL과 메타데이터를 수집한다.
    반환값: {url: {"category": depthTwos, "regions": [...]}}

    API: https://api.zighang.com/api/recruitments/v3
    지원 필터: depthTwos(직무), regions(지역), employeeTypes(채용유형),
              careerMin/careerMax(경력), educations(학력)
    NOTE: deadlineType 파라미터는 API에서 지원되지 않는다.
    keywords.json 의 "zighang" 섹션으로 필터를 제어한다.
    """
    url_meta: dict[str, dict] = {}

    params: list[tuple] = [
        ("page", 0),
        ("size", 50),
        ("sortCondition", "ZIGHANG_SCORE"),
        ("orderCondition", "DESC"),
    ]

    for key in ("depthTwos", "regions", "employeeTypes", "educations"):
        for val in cfg.get(key, []):
            params.append((key, val))

    career_min = cfg.get("careerMin")
    career_max = cfg.get("careerMax")
    if career_min is not None:
        params.append(("careerMin", career_min))
    if career_max is not None:
        params.append(("careerMax", career_max))

    try:
        resp = requests.get(
            "https://api.zighang.com/api/recruitments/v3",
            params=params,
            headers=BROWSER_HEADERS,
            timeout=15,
        )
        resp.raise_for_status()

        data = resp.json()
        for item in data.get("data", {}).get("content", []):
            item_id = item.get("id")
            if item_id:
                url = f"https://zighang.com/recruitment/{item_id}"
                raw_regions = item.get("regions", [])
                if isinstance(raw_regions, str):
                    raw_regions = [raw_regions]
                else:
                    # API가 중첩 리스트나 비문자열 요소를 반환할 경우 평탄화
                    flat = []
                    for r in raw_regions:
                        if isinstance(r, str):
                            flat.append(r)
                        elif isinstance(r, list):
                            flat.extend(x for x in r if isinstance(x, str))
                    raw_regions = flat
                raw_category = item.get("depthTwos", "기타")
                if isinstance(raw_category, str):
                    raw_category = [raw_category] if raw_category else ["기타"]
                employ_types = item.get("employeeTypes") or []
                url_meta[url] = {
                    "category": raw_category,
                    "regions": raw_regions,
                    "title": item.get("title", ""),
                    "company_name": item.get("company", {}).get("name", ""),
                    "employ_type": employ_types[0] if employ_types else "",
                    "career": _zighang_career(
                        item.get("careerMin", 0),
                        item.get("careerMax", 0),
                    ),
                }

        log.info("[직행] API 수집 → %d건", len(url_meta))
    except Exception as e:
        log.warning("[직행] API 수집 실패: %s", e)

    return url_meta


# ── 메인 오케스트레이션 ────────────────────────────────────────────────────────

MAX_NEW_ZIGHANG = 8   # 직행 런당 최대 신규 처리 건수
MAX_NEW_WANTED  = 2   # 원티드 런당 최대 신규 처리 건수


def process_urls(
    urls,
    limit: int,
    label: str,
    meta: dict[str, dict] | None = None,
    content_fetcher=None,
    extract: set[str] | None = None,
) -> tuple[int, int]:
    """URL 집합을 순회하며 중복 확인 후 파이프라인을 실행한다. (new_count, fail_count) 반환.
    meta가 주어지면 {url: {필드: 값}} 매핑을 파이프라인에 전달한다.
    content_fetcher가 주어지면 url을 인자로 호출해 본문 텍스트를 얻고 Jina를 생략한다.
    extract가 주어지면 Gemini가 해당 속성만 추출하는 경량 모드로 동작한다.
    """
    new_count = 0
    fail_count = 0
    for url in urls:
        if new_count >= limit:
            log.info("[%s] 최대 처리 건수(%d) 도달 — 나머지는 다음 실행에서 처리됨", label, limit)
            break
        try:
            if is_duplicate(url):
                log.debug("중복 — 건너뜀: %s", url)
                continue
        except Exception as e:
            log.warning("중복 확인 실패 (%s): %s — 처리 진행", url, e)
        try:
            m = meta.get(url) if meta else None
            content = content_fetcher(url) if content_fetcher else None
            process_url(
                url,
                job_category=m.get("category") if m else None,
                job_regions=m.get("regions") if m else None,
                content=content,
                job_title=m.get("title") if m else None,
                job_company=m.get("company_name") if m else None,
                job_career=m.get("career") if m else None,
                job_employ_type=m.get("employ_type") if m else None,
                extract=extract,
            )
            new_count += 1
        except Exception as e:
            log.exception("파이프라인 실패 (%s): %s", url, e)
            fail_count += 1
            new_count += 1  # 실패도 처리 건수로 카운트해 limit을 초과하지 않도록 한다
        time.sleep(2)  # Gemini / Notion API rate limit 대응
    return new_count, fail_count


def resolve_zighang_cfg(config: dict) -> tuple[dict, str]:
    """CRAWL_REGION_MODE 환경변수 또는 요일에 따라 zighang 설정을 선택한다.
    반환값: (zighang_cfg, 모드명)
    """
    mode_env = os.environ.get("CRAWL_REGION_MODE", "").lower()
    if mode_env == "weekend":
        is_weekend = True
    elif mode_env == "weekday":
        is_weekend = False
    else:
        is_weekend = datetime.datetime.today().weekday() >= 5  # 토=5, 일=6

    if is_weekend:
        return config.get("zighang_weekend", config.get("zighang", {})), "주말"
    return config.get("zighang", {}), "평일"


def main():
    config = load_config()
    keywords = config["keywords"]
    zighang_cfg, region_mode = resolve_zighang_cfg(config)
    log.info("=== 크롤러 시작 | 지역모드: %s | 키워드: %s ===", region_mode, keywords)

    zighang_meta = fetch_zighang_urls(zighang_cfg)
    wanted_meta  = fetch_wanted_urls(keywords)
    log.info("수집 완료 — 직행: %d건, 원티드: %d건. 중복 확인 중...", len(zighang_meta), len(wanted_meta))

    # 직행: 회사명·공고명·직무·지역·경력·채용유형 직접 주입 → Gemini는 기술스택 + AI 한줄평만 담당
    z_new, z_fail = process_urls(
        zighang_meta.keys(), MAX_NEW_ZIGHANG, "직행",
        zighang_meta, fetch_zighang_content,
        extract={"기술스택"},
    )
    # 원티드: 회사명·공고명·지역 직접 주입 → Gemini는 직무·경력·채용유형·기술스택 + AI 한줄평 담당
    w_new, w_fail = process_urls(
        wanted_meta.keys(), MAX_NEW_WANTED, "원티드",
        wanted_meta, fetch_wanted_content,
        extract={"직무", "경력", "채용유형", "기술스택"},
    )

    log.info(
        "=== 크롤러 완료 | 직행 신규: %d건(실패 %d건) | 원티드 신규: %d건(실패 %d건) ===",
        z_new, z_fail, w_new, w_fail,
    )


if __name__ == "__main__":
    main()
