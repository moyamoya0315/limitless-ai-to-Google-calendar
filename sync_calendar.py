import base64
import hashlib
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

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


def extract_events_with_claude(lifelog):
    """Claude APIで会話ログからカレンダー登録の意図を抽出する。

    会話に「登録して」「予定入れて」等の意図が含まれる場合のみ、
    日時・場所・タイトル等を抽出して返す。意図がなければ空リストを返す。
    """
    client = anthropic.Anthropic()

    markdown = lifelog.get("markdown", "")
    lifelog_start = lifelog.get("startTime", "")

    # 相対日時(「明日」「来週」等)を解決するために、会話が行われた基準日時を渡す
    now_local = datetime.now(ZoneInfo(TIMEZONE))
    reference_time = lifelog_start or now_local.isoformat()

    prompt = f"""あなたは会話ログからカレンダー登録の**明示的な指示**を抽出するアシスタントです。

## 会話ログ
{markdown}

## 基準日時 (相対日時の解釈に使用)
{reference_time}
タイムゾーン: {TIMEZONE}

## タスク
上記の会話ログを分析し、話者が**カレンダーや予定の登録を明示的に指示している箇所だけ**を抽出してください。

## 最重要ルール
**「登録して」「予定入れて」「カレンダーに入れて」「リマインドして」等の明確な登録指示の言葉が含まれている場合のみ抽出してください。**
予定や日時に言及しているだけでは抽出しません。話者が「カレンダーに登録したい」という意思を明確に表明している場合のみです。

### 抽出すべき例（明示的な登録指示がある）
- 「明日15時から渋谷でAさんと打ち合わせ、**カレンダーに登録して**」
- 「来週月曜の10時に歯医者の**予定入れて**」
- 「金曜日のランチを**カレンダーに入れておいて**」
- 「14時からの会議、**予定に追加して**」

### 抽出してはいけない例（登録指示がない）
- 「降りて12に面接するやつね」→ 単なる会話中の言及。登録指示なし。
- 「明日3時から会議あるんだよね」→ 予定の話題だが、登録指示なし。
- 「来週の木曜、歯医者行かなきゃ」→ 独り言・つぶやき。登録指示なし。
- 「先週の金曜にミーティングした」→ 過去の出来事。
- 「12時に面接がある」→ 事実の共有のみ。登録指示なし。

## 音声認識の誤りへの対応
会話ログは音声認識で生成されており、聞き取りミスが含まれる場合があります。
文脈から明らかに誤認識と判断できる場合は、正しい内容に修正した上でイベント情報を生成してください。
例: 「しぶやえきで3じから**こうぎ**」→ 文脈的に「会議」の可能性が高ければ修正

## 出力形式
以下のJSON形式で返してください。JSONのみを返し、他のテキストは含めないでください。
**登録指示が見つからない場合は必ず空配列 `{{"events": []}}` を返してください。迷ったら空配列を返してください。**

```json
{{
  "events": [
    {{
      "summary": "イベントタイトル(日本語、簡潔に)",
      "description": "詳細(参加者、目的等)",
      "location": "場所(あれば、なければ空文字)",
      "start_time": "ISO-8601形式 (例: 2026-04-08T15:00:00+09:00)",
      "end_time": "ISO-8601形式 (終了時刻が不明な場合は開始から1時間後)"
    }}
  ]
}}
```

## 重要な注意事項
- 「明日」「来週月曜」等の相対日時は基準日時から計算し、絶対日時に変換してください
- 日時は必ずタイムゾーン付きISO-8601形式で返してください
- 終了時刻が明示されていない場合は開始から1時間後をデフォルトとしてください
- 1つの会話に複数の登録意図があれば配列に複数入れてください
- 登録指示が見つからない場合は必ず空配列を返してください
- **判断に迷う場合は登録しない（空配列を返す）方を選んでください**
"""

    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )

    response_text = message.content[0].text.strip()
    if "```json" in response_text:
        response_text = response_text.split("```json")[1].split("```")[0].strip()
    elif "```" in response_text:
        response_text = response_text.split("```")[1].split("```")[0].strip()

    parsed = json.loads(response_text)
    return parsed.get("events", [])


def get_calendar_service():
    """Google Calendar APIサービスを構築する"""
    creds_b64 = os.environ["GOOGLE_CREDENTIALS"]
    creds_json = json.loads(base64.b64decode(creds_b64))
    credentials = service_account.Credentials.from_service_account_info(
        creds_json, scopes=SCOPES
    )
    return build("calendar", "v3", credentials=credentials)


def make_event_key(lifelog_id, event_data):
    """重複防止用のユニークキーを生成する。

    1つのlifelogから複数イベントが抽出される可能性があるため、
    lifelog_id + イベント内容のハッシュで一意化する。
    """
    fingerprint = f"{event_data.get('summary','')}|{event_data.get('start_time','')}|{event_data.get('end_time','')}"
    digest = hashlib.sha1(fingerprint.encode("utf-8")).hexdigest()[:12]
    return f"{lifelog_id}_{digest}"


def is_already_synced(service, calendar_id, event_key):
    """同じevent_keyのイベントが既に存在するか確認する"""
    events_result = (
        service.events()
        .list(
            calendarId=calendar_id,
            privateExtendedProperty=f"limitless_event_key={event_key}",
            maxResults=1,
        )
        .execute()
    )
    return len(events_result.get("items", [])) > 0


def create_calendar_event(service, calendar_id, event_data, event_key, lifelog_id):
    """Googleカレンダーにイベントを作成する"""
    event = {
        "summary": event_data["summary"],
        "description": event_data.get("description", ""),
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
                "limitless_event_key": event_key,
                "limitless_lifelog_id": lifelog_id,
            }
        },
    }

    location = event_data.get("location", "").strip()
    if location:
        event["location"] = location

    created = service.events().insert(calendarId=calendar_id, body=event).execute()
    logger.info("イベント作成完了 (lifelog_id=%s)", lifelog_id)
    return created


def main():
    calendar_id = os.environ["GOOGLE_CALENDAR_ID"]
    service = get_calendar_service()

    lifelogs = fetch_lifelogs()
    if not lifelogs:
        logger.info("新しいlifelogはありません")
        return

    created_count = 0
    skipped_count = 0
    no_intent_count = 0

    for lifelog in lifelogs:
        lifelog_id = lifelog["id"]
        lifelog_title = lifelog.get("title", lifelog_id)

        events = extract_events_with_claude(lifelog)
        if not events:
            logger.info("登録意図なし (lifelog_id=%s)", lifelog_id)
            no_intent_count += 1
            continue

        for event_data in events:
            event_key = make_event_key(lifelog_id, event_data)
            if is_already_synced(service, calendar_id, event_key):
                logger.info("スキップ (同期済み, lifelog_id=%s)", lifelog_id)
                skipped_count += 1
                continue

            create_calendar_event(service, calendar_id, event_data, event_key, lifelog_id)
            created_count += 1

    logger.info(
        "完了: %d件作成, %d件スキップ, %d件は登録意図なし",
        created_count,
        skipped_count,
        no_intent_count,
    )


if __name__ == "__main__":
    main()
