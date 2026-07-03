# M5 Petit MCP

## [EnglishPage](./README_en.md)

M5Stack Petit(表情・音・カメラ・センサー付きのぷち)をClaudeから操作するためのMCPサーバーです。

Claudeに`speak`(発話)・`show_face`(表情変更)・`take_snapshot`(カメラ)・`get_sensor_data`(センサー取得)などのツールを提供します。[m5-petit-app](https://github.com/PetitOnes/m5-petit-app)と組み合わせて使うことを想定しています。

## セットアップ

uvが未インストールの場合は先にインストールします。

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

```bash
git clone https://github.com/PetitOnes/m5-petit-mcp.git
cd m5-petit-mcp
uv sync
```

Claude Codeの`.mcp.json`に追加します。

```json
{
  "mcpServers": {
    "m5-petit": {
      "command": "uv",
      "args": ["--directory", "/path/to/m5-petit-mcp", "run", "m5-petit-mcp"],
      "env": {
        "M5_HOST": "petit.local",
        "CHARACTER_ID": "petit"
      }
    }
  }
}
```

## 環境変数

| 変数名 | 説明 | デフォルト |
| --- | --- | --- |
| `M5_HOST` / `M5_HOSTS` | M5デバイスのホスト名/IP(カンマ区切りでフォールバック指定可) | — |
| `CHARACTER_ID` | M5キャラクターID(アルバム・ボイスメモの保存先などに使用) | — |
| `VOICE_API_HOST` | 音声認識(ASR)・音声合成(TTS)サーバーのホスト。ASRは`:8765`、TTSは`:8766`で待ち受け想定 | — |
| `TTS_FALLBACK_URL` | プライマリのTTSに接続できないときのフォールバック先 | `http://localhost:8766` |
| `DASHBOARD_HOST` / `DASHBOARD_URL` | [m5-petit-app](https://github.com/PetitOnes/m5-petit-app)ダッシュボードのURL(アルバム・ボイスメモ・リレー機能で使用) | `http://127.0.0.1:8765` |
| `PETIT_DATA_DIR` | 音声設定などのデータ保存先(`set_voice`実行時に自動作成される) | `~/petit_claude` |
| `M5_ALLOWED_TOOLS` | 公開するツール名をカンマ区切りで制限したいとき(未設定なら全ツール公開) | — |

## ツール一覧

### 表情・ディスプレイ

`take_snapshot` `list_faces` `show_face` `set_face_color` `set_face_draw_mode` `set_face_slideshow_mode` `look` `blink` `list_icons` `play_icon` `set_brightness` `get_brightness` `upload_face`

### 音

`list_sounds` `play_sound` `get_volume` `set_volume` `speak`(要`VOICE_API_HOST`) `set_voice` `upload_wav`

### センサー・入力

`get_sensor_data` `wait_for_touch` `wait_for_menu_select` `mic_start` `mic_stop`

### 電源

`sleep` `wake` `set_power_save` `get_power_save`

### アルバム(要 m5-petit-app)

`save_to_album` `list_album` `view_album_photo` `lock_album_photo` `delete_album_photo`

### ボイスメモ(要 m5-petit-app、文字起こしは要 `VOICE_API_HOST`)

`list_voice_memos` `listen_voice_memo` `save_tts_memo` `lock_voice_memo` `transcribe_audio`

### その他

`batch_commands` — 複数コマンドをまとめて送信

`conversation_relay` — 別のキャラクターに会話を引き継ぐ。ダッシュボード側に複数キャラクター用のリレーAPI(`/api/relay/start`)が必要で、**現状の[m5-petit-app](https://github.com/PetitOnes/m5-petit-app)(1ユーザー/1M5構成)は未対応**です。複数キャラクター構成のダッシュボードを使う場合のみ機能します。
