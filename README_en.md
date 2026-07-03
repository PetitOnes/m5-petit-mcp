# M5 Petit MCP

## [日本語ページ](./README.md)

An MCP server for controlling an M5Stack Petit (a Petit with a face display, speaker, camera, and sensors) from Claude.

Gives Claude tools like `speak`, `show_face`, `take_snapshot`, and `get_sensor_data`. Meant to be used together with [m5-petit-app](https://github.com/PetitOnes/m5-petit-app).

## Setup

Install uv first if you don't already have it:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

```bash
git clone https://github.com/PetitOnes/m5-petit-mcp.git
cd m5-petit-mcp
uv sync
```

Add it to Claude Code's `.mcp.json`:

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

## Environment variables

| Variable | Description | Default |
| --- | --- | --- |
| `M5_HOST` / `M5_HOSTS` | Hostname/IP of the M5 device (comma-separated for fallback) | — |
| `CHARACTER_ID` | M5 character ID (used for album / voice memo storage, etc.) | — |
| `VOICE_API_HOST` | Host running ASR (speech recognition) and TTS (speech synthesis). ASR is expected on `:8765`, TTS on `:8766` | — |
| `TTS_FALLBACK_URL` | Fallback TTS endpoint if the primary one is unreachable | `http://localhost:8766` |
| `DASHBOARD_HOST` / `DASHBOARD_URL` | URL of the [m5-petit-app](https://github.com/PetitOnes/m5-petit-app) dashboard (used for album, voice memo, and relay features) | `http://127.0.0.1:8765` |
| `PETIT_DATA_DIR` | Where voice settings and similar data are stored (auto-created on `set_voice`) | `~/petit_claude` |
| `M5_ALLOWED_TOOLS` | Comma-separated list of tool names to expose, if you want to restrict them (all tools are exposed if unset) | — |

## Tools

### Face / display

`take_snapshot` `list_faces` `show_face` `set_face_color` `set_face_draw_mode` `set_face_slideshow_mode` `look` `blink` `list_icons` `play_icon` `set_brightness` `get_brightness` `upload_face`

### Sound

`list_sounds` `play_sound` `get_volume` `set_volume` `speak` (requires `VOICE_API_HOST`) `set_voice` `upload_wav`

### Sensors / input

`get_sensor_data` `wait_for_touch` `wait_for_menu_select` `mic_start` `mic_stop`

### Power

`sleep` `wake` `set_power_save` `get_power_save`

### Album (requires m5-petit-app)

`save_to_album` `list_album` `view_album_photo` `lock_album_photo` `delete_album_photo`

### Voice memo (requires m5-petit-app; transcription requires `VOICE_API_HOST`)

`list_voice_memos` `listen_voice_memo` `save_tts_memo` `lock_voice_memo` `transcribe_audio`

### Other

`batch_commands` — send multiple commands in one call

`conversation_relay` — hand off a conversation to another character. Requires a dashboard with a multi-character relay API (`/api/relay/start`), which the current [m5-petit-app](https://github.com/PetitOnes/m5-petit-app) (single-user / single-M5 setup) **does not implement**. Only works with a multi-character dashboard.
