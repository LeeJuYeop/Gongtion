# Gongtion

![Python](https://img.shields.io/badge/python-3.12-blue)
![GitHub Actions](https://img.shields.io/github/actions/workflow/status/LeeJuYeop/Gongtion/crawler.yml?label=crawler)

한국 채용공고를 자동 수집·AI 분석하여 Notion DB에 저장하는 GitHub Actions 기반 파이프라인

---

## 주요 기능

- **자동 크롤링** — 직행(공식 API), 원티드(비공식 JSON API)에서 공고를 수집하고 Notion 링크 필드를 기준으로 중복을 자동 방지
- **AI 구조화** — Gemini 2.5 Flash가 공고 본문에서 직무·기술스택·경력·채용유형을 추출하고 한줄 요약 코멘트를 생성
- **개인화 분석** — `profile.json`에 보유 기술과 프로젝트를 작성하면, 공고와의 매칭도를 Notion 페이지에 추가 (선택)
- **Notion 저장** — 구조화된 필드를 DB Properties로, AI 요약과 공고 본문을 페이지 블록으로 저장
- **설정 파일 기반** — `keywords.json`만 수정하면 검색 키워드·직무·지역·경력 필터를 바꿀 수 있어 코드 수정 불필요

---

## 동작 원리

```
GitHub Actions (현재동작 : 화·수·토 07:00 KST // 커스터마이징 가능)
  └── crawler.py
        ├── 직행 API ──────── 공고 목록 수집 → ProseMirror 본문 추출
        │                     → Gemini (기술스택만 추출) → Notion 저장
        └── 원티드 API ────── 공고 목록 수집 → 상세 API 본문 추출
                              → Gemini (직무·경력·채용유형·기술스택 추출) → Notion 저장
```

- 회사명·공고명·지역은 API 응답에서 직접 주입하여 Gemini 할루시네이션을 방지
- API 본문 추출 실패 시 Jina Reader로 자동 폴백
- 실행당 처리 한도: 직행 8건, 원티드 2건 (API 과금 대응)
- `keywords.json`의 `zighang` / `zighang_weekend` 필터를 상황에 따라 다르게 적용할 수 있으며, 실행 스케줄과 필터 전환 조건은 GitHub Actions 설정으로 커스터마이징 가능

---

## Notion DB 스키마

Notion DB에 아래 Properties를 생성해야 한다.

| Property 이름 | 타입 | 비고 |
|---|---|---|
| 회사명 | Title | |
| 공고명 | Rich text | |
| 직무 | Multi-select | 서버\_백엔드, DevOps\_SRE 등 |
| 기술스택 | Multi-select | Gemini 추출 |
| 경력 | Select | 신입 / 경력 / 무관 |
| 채용유형 | Select | 정규직 / 인턴 |
| 지역 | Multi-select | 시/도 단위 |
| 링크 | URL | **중복 방지 기준 필드 — 이름 정확히 일치 필수** |

페이지 본문 구조: AI 요약 Callout(🤖) → 개인화 분석 Callout(👤, 선택) → 공고 본문 마크다운

---

## 파일 구성

```
Gongtion/
├── crawler.py              # 사이트별 URL 수집, 중복 확인, 오케스트레이션
├── pipeline.py             # 본문 추출 → Gemini 분석 → Notion 저장 공통 파이프라인
├── keywords.json           # 검색 키워드·직무·지역·경력 필터 설정
├── profile.json            # 개인화 분석용 프로필 (선택)
├── requirements.txt        # Python 의존성
└── .github/workflows/
    └── crawler.yml         # 크론 스케줄 및 Secrets 주입
```

---

## Fork하여 사용하기

### Step 1 — 저장소 Fork

GitHub 우상단 **Fork** 버튼을 눌러 본인 계정에 복사한다.

---

### Step 2 — Notion 설정

**2-1. Integration 생성**

1. [notion.so/my-integrations](https://www.notion.so/my-integrations) 에서 **New integration** 클릭
2. 이름 입력 후 생성 → **Internal Integration Secret** 복사 → `NOTION_API_KEY`로 사용

**2-2. Notion DB 생성**

1. Notion에서 새 페이지 생성 → **Database** (전체 페이지) 선택
2. 위 [Notion DB 스키마](#notion-db-스키마) 표를 참고하여 Property를 타입에 맞게 추가
3. `링크` Property는 이름과 타입(URL)을 **정확히** 일치시킬 것 — 중복 방지 로직이 이 필드에 의존

**2-3. Integration 연결 및 DB ID 확인**

1. DB 페이지 우상단 `···` → **Connections** → 생성한 Integration 추가
2. DB URL에서 ID 추출: `notion.so/workspace/`**`xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`**`?v=...`  
   32자리 부분이 `NOTION_DATABASE_ID`

---

### Step 3 — Gemini API Key 발급

[aistudio.google.com/app/apikey](https://aistudio.google.com/app/apikey) 에서 무료로 발급한다.  
무료 티어(분당 15회)로 기본 크롤링 스케줄을 충분히 소화할 수 있다.

---

### Step 4 — GitHub Secrets 등록

포크한 저장소의 **Settings → Secrets and variables → Actions → New repository secret** 에서 아래 세 가지를 등록한다.

| Secret 이름 | 값 |
|---|---|
| `GEMINI_API_KEY` | Step 3에서 발급한 API Key |
| `NOTION_API_KEY` | Step 2-1의 Integration Secret |
| `NOTION_DATABASE_ID` | Step 2-3에서 확인한 DB ID |

---

### Step 5 — keywords.json 설정

`keywords.json`을 열어 본인의 조건에 맞게 수정한다. 코드 수정은 불필요하다.

```jsonc
{
  // 원티드 검색 키워드
  "keywords": ["클라우드", "DevOps", "백엔드", "인프라", "SRE"],

  // 평일(월·화) 크롤링 필터
  "zighang": {
    "depthTwos": ["서버_백엔드", "DevOps_SRE"],   // 직무 카테고리
    "regions": [],                                 // 빈 배열 = 전국
    "employeeTypes": [],
    "careerMin": 0,
    "careerMax": 0,
    "educations": []
  },

  // 토요일 크롤링 필터 (지역 등 조건을 다르게 운영하고 싶을 때)
  "zighang_weekend": {
    "depthTwos": ["서버_백엔드", "DevOps_SRE"],
    "regions": ["서울", "경기"],
    "employeeTypes": [],
    "careerMin": 0,
    "careerMax": 0,
    "educations": []
  }
}
```

**`depthTwos` 허용값:** `서버_백엔드`, `DevOps_SRE`, `시스템_네트워크`, `시스템소프트웨어`, `웹풀스택`

**`careerMin / careerMax` 예시:**

| careerMin | careerMax | 의미 |
|---|---|---|
| `0` | `0` | 신입 포함 전체 |
| `0` | `3` | 3년 이하 |
| `1` | `5` | 1~5년 |

---

### Step 6 — (선택) profile.json 설정

AI가 공고와 내 프로필을 비교하여 "보유 기술 매칭 / 새로 배울 기술 / 어필 가능한 프로젝트"를 Notion 페이지에 추가하려면 `profile.json`을 작성한다.

```jsonc
{
  "tech_stack": ["Python", "AWS", "Docker", "Linux"],
  "learning_interests": ["Kubernetes", "Kafka", "Terraform"],
  "projects": [
    {
      "name": "프로젝트명",
      "description": "한 줄 설명",
      "tech_used": ["Python", "AWS Lambda", "Notion API"]
    }
  ]
}
```

파일이 없으면 개인화 기능은 자동으로 비활성화되고 나머지는 정상 동작한다.

---

### Step 7 — GitHub Actions 활성화 및 테스트

1. 포크한 저장소의 **Actions** 탭 진입
2. **"I understand my workflows, enable them"** 클릭  
   *(GitHub은 Fork 저장소의 Actions를 기본적으로 비활성화함 — 이 단계를 빠뜨리면 크론이 동작하지 않음)*
3. **Actions → Auto Job Crawler → Run workflow** 로 수동 실행
4. 실행 로그에서 `크롤러 완료 | 직행 신규: N건` 확인
5. Notion DB에 페이지가 생성되었는지 확인

기본 크론 스케줄(화·수·토 07:00 KST)을 변경하려면 `.github/workflows/crawler.yml`의 `cron` 표현식을 수정한다.

```yaml
# crawler.yml
on:
  schedule:
    - cron: '0 22 * * 1,2,5'  # UTC 기준 → KST 화·수·토 07:00
```
