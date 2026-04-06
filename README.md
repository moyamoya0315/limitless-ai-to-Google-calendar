# Limitless AI → Google Calendar Sync

Limitless AI Pendantの会話ログ(lifelogs)を15分ごとに自動取得し、Claude AIで要約・整形してGoogleカレンダーに予定として登録するGitHub Actionです。

## セットアップ

### 1. Limitless AI APIキーの取得

1. [Limitless](https://www.limitless.ai/) のデスクトップまたはWebアプリを開く
2. Developer設定からAPIキーをコピー

### 2. Google サービスアカウントの作成

1. [Google Cloud Console](https://console.cloud.google.com/) でプロジェクトを作成
2. Google Calendar APIを有効化
3. サービスアカウントを作成し、JSONキーをダウンロード
4. 対象のGoogleカレンダーの共有設定で、サービスアカウントのメールアドレスに「予定の変更」権限を付与
5. JSONキーをbase64エンコード:
   ```bash
   base64 -w 0 service-account-key.json
   ```

### 3. Anthropic APIキーの取得

1. [Anthropic Console](https://console.anthropic.com/) でAPIキーを作成

### 4. GitHub Secretsの設定

リポジトリの Settings → Secrets and variables → Actions で以下を設定:

| Secret名 | 説明 |
|---|---|
| `LIMITLESS_API_KEY` | Limitless AI APIキー |
| `ANTHROPIC_API_KEY` | Anthropic APIキー |
| `GOOGLE_CREDENTIALS` | サービスアカウントJSONのbase64エンコード値 |
| `GOOGLE_CALENDAR_ID` | GoogleカレンダーID（例: `xxxx@group.calendar.google.com`） |

### 5. 動作確認

リポジトリの Actions タブから「Sync Limitless AI to Google Calendar」ワークフローを手動実行（Run workflow）して動作を確認できます。

## 仕組み

1. **15分ごと**にGitHub Actionsが起動
2. Limitless AI APIから過去20分間の会話ログを取得
3. Claude AI (Haiku) が会話内容を分析し、タイトルと要約を生成
4. Googleカレンダーにイベントとして登録
5. lifelog IDによる重複防止で、同じログが2回登録されることはありません
