"""
자동 채용공고 크롤러 — GitHub Actions cron으로 4시간마다 실행된다.
각 채용사이트에서 키워드 매칭 공고 URL을 수집하고,
Notion DB에 없는 신규 공고만 pipeline을py의 Jina → Gemini → Notion 파이프라인으로 처리한다.
"""

import json
import logging
import os
import time
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup
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

def load_keywords() -> list[str]:
    """keywords.json 에서 필터링 키워드 목록을 읽는다."""
    with open("keywords.json", encoding="utf-8") as f:
        return json.load(f)["keywords"]


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

def fetch_saramin_urls(keywords: list[str]) -> set[str]:
    """사람인 Open API(oapi.saramin.co.kr)에서 채용공고 URL을 수집한다.

    NOTE: 사람인 메인 사이트가 완전 JS 렌더링으로 전환되어 requests+BeautifulSoup으로는
    공고 목록을 가져올 수 없다. 공식 Open API를 사용한다.
    API 키는 https://oapi.saramin.co.kr 에서 무료 발급 가능하다.
    GitHub Actions Secret에 SARAMIN_API_KEY를 추가해야 한다.
    """
    api_key = os.environ.get("SARAMIN_API_KEY")
    if not api_key:
        log.warning("[사람인] SARAMIN_API_KEY 환경변수가 없어 수집을 건너뜁니다.")
        return set()

    urls: set[str] = set()
    for kw in keywords:
        try:
            resp = requests.get(
                "https://oapi.saramin.co.kr/job-search",
                params={
                    "access-key": api_key,
                    "keywords": kw,
                    "count": 40,
                    "start": 1,
                    "_type": "json",
                    "sort": "pd",
                },
                headers=BROWSER_HEADERS,
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            before = len(urls)
            for job in data.get("jobs", {}).get("job", []):
                job_url = job.get("url")
                if job_url:
                    urls.add(job_url)
            log.info("[사람인] '%s' → %d건", kw, len(urls) - before)
        except Exception as e:
            log.warning("[사람인] '%s' 수집 실패: %s", kw, e)
        time.sleep(1)
    return urls


def fetch_wanted_urls(keywords: list[str]) -> set[str]:
    """원티드 비공식 검색 API에서 채용공고 URL을 수집한다.

    NOTE: 원티드 API가 422를 반환하면 로그의 'Response body' 줄을 확인해
    실제 validation 오류 메시지를 파악할 것.
    지속 실패 시 공식 OpenAPI(openapi.wanted.jobs) 전환을 고려:
    WANTED_API_KEY 환경변수를 추가하고 Authorization 헤더를 포함해야 한다.
    """
    urls: set[str] = set()
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
            before = len(urls)
            for job in data.get("data", []):
                job_id = job.get("id")
                if job_id:
                    urls.add(f"https://www.wanted.co.kr/wd/{job_id}")
            log.info("[원티드] '%s' → %d건", kw, len(urls) - before)
        except requests.exceptions.HTTPError as e:
            body = e.response.text[:300] if e.response is not None else ""
            log.warning("[원티드] '%s' 수집 실패: %s | Response body: %s", kw, e, body)
        except Exception as e:
            log.warning("[원티드] '%s' 수집 실패: %s", kw, e)
        time.sleep(1)
    return urls


def fetch_jobkorea_urls(keywords: list[str]) -> set[str]:
    """잡코리아 검색결과 HTML에서 채용공고 URL을 수집한다.

    NOTE: 잡코리아는 JS 렌더링에 의존할 수 있어 HTML 파싱이 실패할 수 있다.
    동작하지 않으면 잡코리아 RSS(https://www.jobkorea.co.kr/rss) 엔드포인트를
    feedparser로 파싱하는 방식으로 교체를 고려할 것.
    """
    urls: set[str] = set()
    for kw in keywords:
        try:
            search_url = (
                f"https://www.jobkorea.co.kr/Search/"
                f"?stext={quote(kw)}&tabType=recruit&OrderBy=2"
            )
            resp = requests.get(search_url, headers=BROWSER_HEADERS, timeout=15)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")
            before = len(urls)
            for a in soup.select(".list-post .information-title a"):
                href = a.get("href", "")
                if href:
                    full = "https://www.jobkorea.co.kr" + href if href.startswith("/") else href
                    urls.add(full)
            log.info("[잡코리아] '%s' → %d건", kw, len(urls) - before)
        except Exception as e:
            log.warning("[잡코리아] '%s' 수집 실패: %s", kw, e)
        time.sleep(1)
    return urls


def fetch_zighang_urls(keywords: list[str]) -> set[str]:
    """직행(zighang.com) 검색결과 HTML에서 채용공고 URL을 수집한다.

    NOTE: 직행은 공식 RSS/API가 없어 HTML 스크래핑에 의존한다.
    사이트 구조 변경 시 a[href*='/jobs/'] 셀렉터를 수정해야 한다.
    """
    urls: set[str] = set()
    for kw in keywords:
        try:
            search_url = f"https://zighang.com/jobs?q={quote(kw)}"
            resp = requests.get(search_url, headers=BROWSER_HEADERS, timeout=15)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")
            before = len(urls)
            for a in soup.select("a[href*='/jobs/']"):
                href = a.get("href", "")
                if href and href != "/jobs/":
                    full = "https://zighang.com" + href if href.startswith("/") else href
                    urls.add(full)
            log.info("[직행] '%s' → %d건", kw, len(urls) - before)
        except Exception as e:
            log.warning("[직행] '%s' 수집 실패: %s", kw, e)
        time.sleep(1)
    return urls


# ── 메인 오케스트레이션 ────────────────────────────────────────────────────────

def collect_all_urls(keywords: list[str]) -> set[str]:
    """모든 사이트에서 URL을 수집해 하나의 집합으로 반환한다. 개별 사이트 실패는 무시한다."""
    all_urls: set[str] = set()
    all_urls.update(fetch_saramin_urls(keywords))
    all_urls.update(fetch_wanted_urls(keywords))
    all_urls.update(fetch_jobkorea_urls(keywords))
    all_urls.update(fetch_zighang_urls(keywords))
    return all_urls


MAX_NEW_PER_RUN = 15  # 런당 최대 신규 처리 건수 (Gemini API 사용량 제한)


def main():
    keywords = load_keywords()
    log.info("=== 크롤러 시작 | 키워드: %s ===", keywords)

    all_urls = collect_all_urls(keywords)
    log.info("총 %d개 URL 수집 완료. 중복 확인 중...", len(all_urls))

    new_count = 0
    fail_count = 0

    for url in all_urls:
        if new_count >= MAX_NEW_PER_RUN:
            log.info("런당 최대 처리 건수(%d) 도달 — 나머지는 다음 실행에서 처리됨", MAX_NEW_PER_RUN)
            break

        try:
            if is_duplicate(url):
                log.debug("중복 — 건너뜀: %s", url)
                continue
        except Exception as e:
            log.warning("중복 확인 실패 (%s): %s — 처리 진행", url, e)

        try:
            process_url(url)
            new_count += 1
        except Exception as e:
            log.error("파이프라인 실패 (%s): %s", url, e)
            fail_count += 1

        time.sleep(2)  # Gemini / Notion API rate limit 대응

    log.info("=== 크롤러 완료 | 신규 저장: %d건 | 실패: %d건 ===", new_count, fail_count)


if __name__ == "__main__":
    main()
