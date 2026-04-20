import json
import logging
import os
import re
import requests
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.aws_lambda import SlackRequestHandler
from pipeline import process_url, fetch_with_jina, summarize_job_posting, create_notion_page, load_user_profile

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S'
)
log = logging.getLogger(__name__)

load_dotenv()

app = App(
    token=os.environ.get("SLACK_BOT_TOKEN"),
    signing_secret=os.environ.get("SLACK_SIGNING_SECRET"),
    process_before_response=True,
)

URL_PATTERN = re.compile(r'https?://[^\s>]+')


@app.event({"type": "message", "subtype": "message_changed"})
def handle_message_changed(body):
    pass  # URL 미리보기 생성 등으로 발생하는 이벤트 — 무시


def ack_message(ack):
    ack()

def process_message(message, say):
    text = message.get('text', '')
    urls = URL_PATTERN.findall(text)

    if not urls:
        return

    extracted_url = urls[0].strip('<>')
    thread_ts = message.get('ts')

    step = 'Jina'
    try:
        content = fetch_with_jina(extracted_url)
        step = 'Gemini'
        profile = load_user_profile()
        result = summarize_job_posting(content, extracted_url, profile=profile)
        step = 'Notion'
        page = create_notion_page(result)
        notion_url = page.get('url', '')
        say(text=f'✅ Notion 페이지가 생성되었습니다! {notion_url}', thread_ts=thread_ts)
    except requests.exceptions.Timeout:
        log.error('[%s] 요청 시간 초과', step)
        say(text=f'⚠️ 요청 처리에 실패했습니다. (사유: {step} 요청 시간 초과)', thread_ts=thread_ts)
    except requests.exceptions.HTTPError as e:
        log.error('[%s] HTTP %d 오류\nResponse body: %s', step, e.response.status_code, e.response.text)
        say(text=f'⚠️ 요청 처리에 실패했습니다. (사유: {step} HTTP 오류 {e.response.status_code})\n```{e.response.text}```', thread_ts=thread_ts)
    except requests.exceptions.RequestException as e:
        log.error('[%s] 네트워크 오류: %s', step, e)
        say(text=f'⚠️ 요청 처리에 실패했습니다. (사유: {step} 네트워크 오류 {e})', thread_ts=thread_ts)
    except Exception as e:
        log.error('[%s] 예외 발생: %s', step, e)
        say(text=f'⚠️ 처리 중 오류가 발생했습니다. (사유: {step} {e})', thread_ts=thread_ts)


app.message(re.compile(r'.*'))(ack=ack_message, lazy=[process_message])


slack_handler = SlackRequestHandler(app=app)


def lambda_handler(event, context):
    ## slack API의 url 인증(challenge)을 위한 단계
    # 1. API Gateway로부터 들어온 body 문자열을 객체로 변환
    try:
        raw_body = event.get("body", "{}")
        body = json.loads(raw_body)
    except Exception:
        body = {}

    # 2. [공식 문서 기준] url_verification 타입인지 확인
    if body.get("type") == "url_verification":
        challenge_value = body.get("challenge")
        return {
            "statusCode": 200,
            "headers": {
                "Content-Type": "application/json"
            },
            "body": json.dumps({
                "challenge": challenge_value
            })
        }

    # 3. Slack retry 요청 무시 (3초 내 응답 불가로 인한 중복 처리 방지)
    headers = event.get("headers") or {}
    if headers.get("X-Slack-Retry-Num") or headers.get("x-slack-retry-num"):
        log.info('Slack retry 요청 무시 (X-Slack-Retry-Num: %s)',
                 headers.get("X-Slack-Retry-Num") or headers.get("x-slack-retry-num"))
        return {"statusCode": 200, "body": ""}

    # 4. 크롤러에서 직접 URL을 전달한 경우
    if event.get("source") == "crawler":
        url = event.get("url", "")
        if url:
            try:
                process_url(url)
            except Exception as e:
                log.error('크롤러 URL 처리 실패 (%s): %s', url, e)
                return {"statusCode": 500, "body": str(e)}
        return {"statusCode": 200, "body": ""}

    # 5. 인증이 아닌 일반 이벤트는 Slack Bolt 핸들러로 전달
    return slack_handler.handle(event, context)
