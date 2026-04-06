import base64
import json
import logging
import os
from datetime import datetime, timedelta, timezone

import anthropic
import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

LIMITLESS_API_URL = "https://api.limitless.ai/v1/lifelogs"
TIMEZONE = os.environ.get("TIMEZONE", "Asia/Tokyo")
SCOPES = ["https://www.googleapis.com/auth/calendar"]


def fetch_lifelogs():
    """過去20分間のlifelogを取得する"""
    api_key = os.environ["LIMITLESS_API_KEY"]
    now = datetime.now(timezone.utc)
    start = now - timedelta(minutes=20)

    params = {
        "timezone": TIMEZONE,
        "start": start.isoformat(),
        "end": now.isoformat(),
        "direction": "desc",
        "includeMarkdown": "true",
        "includeHeadings": "true",
        "limit": 50,
    }
    headers = {"X-API-Key": api_key}

    resp = requests.get(LIMITLESS_API_URL, headers=headers, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    lifelogs = data.get("data", {}).get("lifelogs", [])
    logger.info("取得したlifelog数: %d", len(lifelogs))
    return lifelogs


def format_with_claude(lifelog):
    """Claude APIでlifelogを構造化データに変換する"""
    client = anthropic.Anthropic()

    title = lifelog.get("title", "")
    markdown = lifelog.get("markdown", "")
    start_time = lifelog.get("startTime", "")
    end_time = lifelog.get("endTime", "")

    prompt = f"""以下の会話ログから、Googleカレンダーに登録するためのイベント情報をJSON形式で返してください。

## 会話ログ
タイトル: {title}
開始時刻: {start_time}
終了時刻: {end_time}

内容:
{markdown}

## 出力形式
以下のJSON形式で返してください。JSONのみを返し、他のテキストは含めないでください。
```json
{{
  "summary": "イベントのタイトル（会話の主要トピックを簡潔に）",
  "description": "会話の要点を箇条書きで簡潔にまとめたもの",
  "start_time": "{start_time}",
  "end_time": "{end_time}"
}}
```

注意:
- summaryは日本語で、会話の主要トピックを簡潔に表現してください
- descriptionは会話の要点を箇条書きで簡潔にまとめてください
- start_timeとend_timeはISO-8601形式のまま返してください
"""

    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )

    response_text = message.content[0].text.strip()
    # JSON部分を抽出（```json ... ``` で囲まれている場合に対応）
    if "```json" in response_text:
        response_text = response_text.split("```json")[1].split("```")[0].strip()
    elif "```" in response_text:
        response_text = response_text.split("```")[1].split("```")[0].strip()

    return json.loads(response_text)


def get_calendar_service():
    """Google Calendar APIサービスを構築する"""
    creds_b64 = os.environ["GOOGLE_CREDENTIALS"]
    creds_json = json.loads(base64.b64decode(creds_b64))
    credentials = service_account.Credentials.from_service_account_info(
        creds_json, scopes=SCOPES
    )
    return build("calendar", "v3", credentials=credentials)


def is_already_synced(service, calendar_id, limitless_id):
    """同じlimitless_idのイベントが既に存在するか確認する"""
    events_result = (
        service.events()
        .list(
            calendarId=calendar_id,
            privateExtendedProperty=f"limitless_id={limitless_id}",
            maxResults=1,
        )
        .execute()
    )
    return len(events_result.get("items", [])) > 0


def create_calendar_event(service, calendar_id, event_data, limitless_id):
    """Googleカレンダーにイベントを作成する"""
    event = {
        "summary": event_data["summary"],
        "description": event_data["description"],
        "start": {
            "dateTime": event_data["start_time"],
            "timeZone": TIMEZONE,
        },
        "end": {
            "dateTime": event_data["end_time"],
            "timeZone": TIMEZONE,
        },
        "extendedProperties": {
            "private": {
                "limitless_id": limitless_id,
            }
        },
    }

    created = service.events().insert(calendarId=calendar_id, body=event).execute()
    logger.info("イベント作成: %s (%s)", event_data["summary"], created.get("htmlLink"))
    return created


def main():
    calendar_id = os.environ["GOOGLE_CALENDAR_ID"]
    service = get_calendar_service()

    lifelogs = fetch_lifelogs()
    if not lifelogs:
        logger.info("新しいlifelogはありません")
        return

    synced = 0
    skipped = 0

    for lifelog in lifelogs:
        lifelog_id = lifelog["id"]

        if is_already_synced(service, calendar_id, lifelog_id):
            logger.info("スキップ (同期済み): %s", lifelog.get("title", lifelog_id))
            skipped += 1
            continue

        event_data = format_with_claude(lifelog)
        create_calendar_event(service, calendar_id, event_data, lifelog_id)
        synced += 1

    logger.info("完了: %d件同期, %d件スキップ", synced, skipped)


if __name__ == "__main__":
    main()
