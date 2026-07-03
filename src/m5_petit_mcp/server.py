from mcp.server.fastmcp import FastMCP
from mcp.types import ImageContent
import asyncio
import websockets
from websockets.protocol import State as WsState
import json
import requests
import base64
import io
import wave
import struct
from typing import Optional

mcp = FastMCP("m5-petit")

import os
import tempfile
from pathlib import Path

# Support comma-separated hosts for fallback (e.g. "m5.local,192.168.1.100")
_m5_hosts: list[str] = []
_active_host: Optional[str] = None

# Host for ASR (port 8765) and TTS (port 8766) services.
# Set VOICE_API_HOST to the machine running those services.
VOICE_API_HOST = os.environ.get("VOICE_API_HOST", "")
_ASR_URL = f"http://{VOICE_API_HOST}:8765" if VOICE_API_HOST else ""
_TTS_URL = f"http://{VOICE_API_HOST}:8766" if VOICE_API_HOST else ""
# Fallback TTS when the primary VOICE_API_HOST is unreachable
_TTS_FALLBACK_URL = os.environ.get("TTS_FALLBACK_URL", "http://localhost:8766")
# Dashboard server (album, voice memo, relay endpoints)
_DASHBOARD_URL = f"http://{os.environ.get('DASHBOARD_HOST', '127.0.0.1')}:8765"


def _init_hosts():
    global _m5_hosts
    raw = os.environ.get("M5_HOSTS", "") or os.environ.get("M5_HOST", "")
    _m5_hosts = [h.strip() for h in raw.split(",") if h.strip()]
    if not _m5_hosts:
        raise ValueError("M5_HOST or M5_HOSTS environment variable must be set")

_init_hosts()


def _http_get(path: str, timeout: int = 5, **kwargs) -> requests.Response:
    """HTTP GET with host fallback."""
    global _active_host
    hosts = []
    if _active_host:
        hosts.append(_active_host)
    hosts.extend(h for h in _m5_hosts if h != _active_host)

    last_exc: Optional[Exception] = None
    for host in hosts:
        try:
            r = requests.get(f"http://{host}{path}", timeout=timeout, **kwargs)
            _active_host = host
            return r
        except (requests.ConnectionError, requests.Timeout) as e:
            last_exc = e
            continue
    raise last_exc or ConnectionError(f"All hosts unreachable: {hosts}")


def _http_post(path: str, timeout: int = 5, **kwargs) -> requests.Response:
    """HTTP POST with host fallback."""
    global _active_host
    hosts = []
    if _active_host:
        hosts.append(_active_host)
    hosts.extend(h for h in _m5_hosts if h != _active_host)

    last_exc: Optional[Exception] = None
    for host in hosts:
        try:
            r = requests.post(f"http://{host}{path}", timeout=timeout, **kwargs)
            _active_host = host
            return r
        except (requests.ConnectionError, requests.Timeout) as e:
            last_exc = e
            continue
    raise last_exc or ConnectionError(f"All hosts unreachable: {hosts}")


# ===================== WebSocket state =====================
_ws: Optional[websockets.WebSocketClientProtocol] = None
_ws_lock = asyncio.Lock()
_reader_task: Optional[asyncio.Task] = None
_sensor_data: dict = {}
_touch_queue: asyncio.Queue = asyncio.Queue(maxsize=20)
_menu_queue: asyncio.Queue = asyncio.Queue(maxsize=20)


async def _ensure_ws():
    global _ws, _reader_task, _active_host
    if _ws is not None and _ws.state == WsState.OPEN:
        return
    async with _ws_lock:
        if _ws is not None and _ws.state == WsState.OPEN:
            return
        hosts = []
        if _active_host:
            hosts.append(_active_host)
        hosts.extend(h for h in _m5_hosts if h != _active_host)

        last_exc: Optional[Exception] = None
        for host in hosts:
            try:
                _ws = await websockets.connect(f"ws://{host}:8080")
                _active_host = host
                break
            except Exception as e:
                last_exc = e
                continue
        else:
            raise last_exc or ConnectionError(f"WS: all hosts unreachable: {hosts}")

        if _reader_task is not None:
            _reader_task.cancel()
        _reader_task = asyncio.create_task(_ws_reader())


async def _ws_reader():
    global _sensor_data
    try:
        async for message in _ws:
            if isinstance(message, str):
                try:
                    data = json.loads(message)
                    event = data.get("event")
                    if event == "sensors":
                        _sensor_data = data
                    elif event == "touch":
                        if not _touch_queue.full():
                            _touch_queue.put_nowait(data)
                    elif event == "menu_select":
                        if not _menu_queue.full():
                            _menu_queue.put_nowait(data)
                except Exception:
                    pass
    except Exception:
        pass


async def _send(cmd: str):
    global _ws
    await _ensure_ws()
    try:
        await _ws.send(cmd)
    except Exception:
        _ws = None
        await _ensure_ws()
        await _ws.send(cmd)


# ===================== Tools: HTTP (response required) =====================

@mcp.tool()
async def take_snapshot():
    """Take a photo with the M5 camera"""
    r = await asyncio.to_thread(lambda: _http_get("/snapshot", timeout=10))
    if r.status_code != 200:
        return "camera failed"
    img_b64 = base64.b64encode(r.content).decode()
    return [ImageContent(type="image", data=img_b64, mimeType="image/jpeg")]


@mcp.tool()
async def list_faces():
    """Get the list of face image files on the SD card"""
    r = await asyncio.to_thread(lambda: _http_get("/face_list"))
    return r.json()


@mcp.tool()
async def show_face(name: str):
    """Display a face image for 5 seconds. name: filename from list_faces"""
    await asyncio.to_thread(
        lambda: _http_get(f"/face_play?name={name}")
    )
    return f"showing face: {name}"


@mcp.tool()
async def list_sounds():
    """Get the list of sound effect files on the SD card"""
    r = await asyncio.to_thread(lambda: _http_get("/se_list"))
    return r.json()


@mcp.tool()
async def get_volume():
    """Get current volume (0-100)"""
    r = await asyncio.to_thread(lambda: _http_get("/getvolume"))
    return int(r.text)


# ===================== Tools: WebSocket (fire-and-forget) =====================

@mcp.tool()
async def look(x: int, y: int, mouth: Optional[int] = None):
    """Move gaze direction. x/y: -100 to 100, mouth: 0 to 100 (optional). Returns to center after 5 seconds."""
    cmd = f"LOOK {x} {y}"
    if mouth is not None:
        cmd += f" {mouth}"
    await _send(cmd)
    return f"looked to x={x}, y={y}"


@mcp.tool()
async def blink(left: bool = False, right: bool = False):
    """Wink. Set left/right to True to close the corresponding eye (returns after 0.8 seconds)."""
    await _send(f"BLINK {1 if left else 0} {1 if right else 0}")
    return f"blinked left={left} right={right}"


@mcp.tool()
async def set_face_draw_mode():
    """Switch face to draw mode (real-time rendering of eyes and mouth)"""
    await _send("MODE draw")
    return "switched to draw mode"


@mcp.tool()
async def set_face_slideshow_mode():
    """Switch face to slideshow mode (displays SD JPEGs every 3 seconds)"""
    await _send("MODE jpeg")
    return "switched to slideshow mode"


@mcp.tool()
async def play_sound(name: str):
    """Play a sound effect. name: filename from list_sounds"""
    await asyncio.to_thread(lambda: _http_get(f"/se_play?name={name}"))
    return f"played: {name}"


@mcp.tool()
async def set_volume(value: int):
    """Set volume. value: 0-100"""
    await _send(f"VOL {value}")
    return f"volume set to {value}"


@mcp.tool()
async def list_icons():
    """Get list of available icons"""
    return {"icons": ["love", "cry"]}


@mcp.tool()
async def play_icon(name: str):
    """Display an icon for 3 seconds. name: love (heart) or cry (tears)"""
    await _send(f"ICON {name}")
    return f"icon: {name}"


@mcp.tool()
async def set_face_color(color: str):
    """Set face color with hex color code. color: '#f5956e' or 'f5956e' (hex RGB)"""
    hex_color = color.lstrip("#")
    await _send(f"COLOR {hex_color}")
    return f"face color set to #{hex_color}"


@mcp.tool()
async def sleep():
    """Put M5 into sleep mode (dim screen, touch 3 times to wake)"""
    await _send("SLEEP")
    return "sleeping"


@mcp.tool()
async def wake():
    """Wake M5 from sleep"""
    await _send("WAKE")
    return "waking up"


# ===================== Tools: Brightness / Power save =====================

@mcp.tool()
async def set_brightness(value: int):
    """Set screen brightness. value: 0-100"""
    await _send(f"BRIGHTNESS {value}")
    return f"brightness set to {value}"


@mcp.tool()
async def get_brightness():
    """Get current screen brightness (0-100)"""
    r = await asyncio.to_thread(lambda: _http_get("/getbrightness"))
    return int(r.text)


@mcp.tool()
async def set_power_save(enabled: bool):
    """Toggle power save mode. ON limits brightness and reduces to 10fps rendering."""
    await _send(f"POWERSAVE {'ON' if enabled else 'OFF'}")
    return f"power save {'on' if enabled else 'off'}"


@mcp.tool()
async def get_power_save():
    """Get power save mode status"""
    r = await asyncio.to_thread(lambda: _http_get("/getpowersave"))
    return r.text == "true"


@mcp.tool()
async def batch_commands(commands: list[str]):
    """Send multiple commands at once. e.g. ["BRIGHTNESS 5", "VOL 0", "POWERSAVE ON"]"""
    for cmd in commands:
        await _send(cmd)
    return f"sent {len(commands)} commands"


# ===================== Tools: File upload =====================

def _convert_wav_mono_16k(file_path: str) -> io.BytesIO:
    """Convert any audio file to mono 16bit 16000Hz WAV using ffmpeg."""
    import subprocess
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        out_path = f.name
    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-i", file_path,
                "-ac", "1", "-ar", "16000", "-sample_fmt", "s16",
                out_path,
            ],
            capture_output=True,
            check=True,
        )
        buf = io.BytesIO(Path(out_path).read_bytes())
        buf.seek(0)
        return buf
    finally:
        Path(out_path).unlink(missing_ok=True)


@mcp.tool()
async def upload_wav(file_path: str):
    """Upload a WAV file to the M5 SD card. Automatically converted to mono 16bit 16kHz."""
    def _upload():
        mono_buf = _convert_wav_mono_16k(file_path)
        filename = file_path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        return _http_post(
            "/upload_wav",
            timeout=30,
            files={"file": (filename, mono_buf, "audio/wav")},
        )
    r = await asyncio.to_thread(_upload)
    if r.status_code == 200:
        return f"uploaded (converted to mono 16kHz): {file_path}"
    return f"upload failed: {r.status_code}"


@mcp.tool()
async def upload_face(file_path: str):
    """Upload a face image (JPG) to the M5 SD card. file_path: local JPG file path."""
    def _upload():
        with open(file_path, "rb") as f:
            return _http_post("/upload_face", timeout=30, files={"file": f})
    r = await asyncio.to_thread(_upload)
    if r.status_code == 200:
        return f"uploaded: {file_path}"
    return f"upload failed: {r.status_code}"


# ===================== Tools: Microphone control =====================

@mcp.tool()
async def mic_start():
    """Turn on microphone and start audio stream via WebSocket"""
    await _send("MIC_START")
    return "mic started"


@mcp.tool()
async def mic_stop():
    """Turn off microphone"""
    await _send("MIC_STOP")
    return "mic stopped"


# ===================== Tools: Event reception =====================

@mcp.tool()
async def get_sensor_data():
    """Get latest sensor data (ambient/proximity/battery/voltage/rssi/accelerometer/gyro)"""
    try:
        r = await asyncio.to_thread(lambda: _http_get("/sensors"))
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    await _ensure_ws()
    if not _sensor_data:
        await asyncio.sleep(0.3)
    return _sensor_data


@mcp.tool()
async def wait_for_touch(timeout: float = 10.0):
    """Wait for a touch event. Returns None if no touch within timeout seconds."""
    await _ensure_ws()
    try:
        data = await asyncio.wait_for(_touch_queue.get(), timeout=timeout)
        return data
    except asyncio.TimeoutError:
        return None


@mcp.tool()
async def wait_for_menu_select(timeout: float = 30.0):
    """Wait for a menu item to be selected on M5 touch menu.
    When camera is selected, a snapshot (base64) is included in the data field.
    When sensor is selected, latest sensor data is included in the sensors field.
    When mic is selected, microphone is already started on the M5 side.
    Returns None if no selection within timeout seconds.
    item: 'camera' | 'sensor' | 'mic'
    """
    await _ensure_ws()
    try:
        data = await asyncio.wait_for(_menu_queue.get(), timeout=timeout)
    except asyncio.TimeoutError:
        return None

    if data.get("item") == "sensor" and _sensor_data:
        data["sensors"] = _sensor_data

    return data


# ===================== Tools: Album (requires dashboard server) =====================

@mcp.tool()
async def save_to_album(person_id: str, title: str):
    """Take a snapshot with the camera and save to album.
    person_id: character ID (e.g. alice, bob) — set CHARACTER_ID env var for your own ID
    title: photo title (e.g. walk, today's sky)
    Requires DASHBOARD_HOST env var pointing to the dashboard server.
    """
    r = await asyncio.to_thread(lambda: _http_get("/snapshot", timeout=10))
    if r.status_code != 200:
        return "camera failed"
    import requests as _req
    payload = {
        "person_id": person_id,
        "title": title,
        "image_b64": base64.b64encode(r.content).decode(),
    }
    resp = await asyncio.to_thread(
        lambda: _req.post(f"{_DASHBOARD_URL}/api/album/snapshot", json=payload, timeout=15)
    )
    if resp.status_code == 200:
        j = resp.json()
        return {"ok": True, "filename": j.get("filename")}
    return {"ok": False, "status": resp.status_code}


@mcp.tool()
async def lock_album_photo(album_owner_id: str, filename: str):
    """Toggle lock on an album photo. Locked photos are not auto-deleted.
    album_owner_id: photo owner's character ID
    filename: filename from list_album
    Returns locked=true if locked, false if unlocked.
    """
    import requests as _req
    resp = await asyncio.to_thread(
        lambda: _req.post(f"{_DASHBOARD_URL}/api/album/{album_owner_id}/{filename}/lock", timeout=10)
    )
    if resp.status_code == 200:
        return resp.json()
    return {"ok": False, "status": resp.status_code, "body": resp.text}


@mcp.tool()
async def delete_album_photo(filename: str):
    """Delete a photo from your own album.
    filename: filename from list_album
    Only your own album can be deleted (determined by CHARACTER_ID env var).
    """
    person_id = os.environ.get("CHARACTER_ID", "")
    if not person_id:
        return {"ok": False, "error": "CHARACTER_ID env var is not set"}
    import requests as _req
    resp = await asyncio.to_thread(
        lambda: _req.delete(f"{_DASHBOARD_URL}/api/album/{person_id}/{filename}", timeout=10)
    )
    if resp.status_code == 200:
        return {"ok": True}
    return {"ok": False, "status": resp.status_code, "body": resp.text}


@mcp.tool()
async def list_album(person_id: str, unread_by: str = ""):
    """Get list of photos in an album. read_by field shows who has viewed each photo.
    person_id: character ID of the album owner
    unread_by: if set, returns only photos not yet viewed by that character
    """
    import requests as _req
    resp = await asyncio.to_thread(
        lambda: _req.get(f"{_DASHBOARD_URL}/api/album/{person_id}", timeout=10)
    )
    if resp.status_code != 200:
        return {"ok": False, "status": resp.status_code}
    photos = resp.json()
    if unread_by:
        photos = [p for p in photos if unread_by not in p.get("read_by", [])]
    return photos


@mcp.tool()
async def view_album_photo(album_owner_id: str, filename: str, viewer_id: str):
    """Retrieve an album photo and mark it as read. Returns photo as base64.
    album_owner_id: character ID of the photo owner
    filename: filename from list_album
    viewer_id: character ID of the viewer
    """
    import requests as _req
    img_resp = await asyncio.to_thread(
        lambda: _req.get(f"{_DASHBOARD_URL}/api/album/{album_owner_id}/{filename}", timeout=10)
    )
    if img_resp.status_code != 200:
        return {"ok": False, "status": img_resp.status_code}
    img_b64 = base64.b64encode(img_resp.content).decode()
    await asyncio.to_thread(
        lambda: _req.post(
            f"{_DASHBOARD_URL}/api/album/{album_owner_id}/{filename}/read",
            params={"viewer": viewer_id}, timeout=5
        )
    )
    return [ImageContent(type="image", data=img_b64, mimeType="image/jpeg")]


# ===================== Tools: TTS / ASR =====================

_VOICE_SETTINGS_DEFAULT = 6  # VOICEVOX speaker ID default

def _voice_settings_path() -> str:
    char_id = os.environ.get("CHARACTER_ID", "")
    data_dir = os.environ.get("PETIT_DATA_DIR", os.path.join(os.path.expanduser("~"), "petit_claude"))
    return os.path.join(data_dir, "characters", char_id, "voice_settings.json")

def _load_voice_settings() -> dict:
    try:
        with open(_voice_settings_path()) as f:
            return json.load(f)
    except Exception:
        return {}

def _save_voice_settings(settings: dict):
    path = _voice_settings_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)


@mcp.tool()
async def set_voice(
    voicevox_speaker: Optional[int] = None,
    speed_scale: Optional[float] = None,
    pitch_scale: Optional[float] = None,
    intonation_scale: Optional[float] = None,
    volume_scale: Optional[float] = None,
    pre_phoneme_length: Optional[float] = None,
    post_phoneme_length: Optional[float] = None,
):
    """Save your voice settings. These defaults are used by speak() going forward.
    Only specified parameters are updated; omitted ones stay unchanged.
    Requires CHARACTER_ID env var.

    voicevox_speaker: VOICEVOX speaker ID
      6=Shikoku Metan (tsun), 23=WhiteCUL (normal), 47=NurseRobot_Type-T, 29=No.7
    speed_scale: speech rate (0.5-2.0)
    pitch_scale: pitch (-0.15 to 0.15)
    intonation_scale: intonation (0-2.0)
    volume_scale: volume (0-2.0)
    pre_phoneme_length: silence before speech (seconds)
    post_phoneme_length: silence after speech (seconds)
    """
    settings = _load_voice_settings()
    updates = {
        "voicevox_speaker": voicevox_speaker,
        "speed_scale": speed_scale,
        "pitch_scale": pitch_scale,
        "intonation_scale": intonation_scale,
        "volume_scale": volume_scale,
        "pre_phoneme_length": pre_phoneme_length,
        "post_phoneme_length": post_phoneme_length,
    }
    for k, v in updates.items():
        if v is not None:
            settings[k] = v
    await asyncio.to_thread(_save_voice_settings, settings)
    saved = {k: v for k, v in updates.items() if v is not None}
    return f"voice settings saved: {saved}"


@mcp.tool()
async def speak(
    text: str,
    engine: str = "voicevox",
    # piper
    speaker: int = 0,
    length_scale: float = 1.0,
    noise_scale: float = 0.5,
    noise_w: float = 0.8,
    sentence_silence: float = 0.2,
    # kokoro
    voice: str = "jf_alpha",
    speed: float = 1.0,
    # voicevox
    voicevox_speaker: Optional[int] = None,
    speed_scale: Optional[float] = None,
    pitch_scale: Optional[float] = None,
    intonation_scale: Optional[float] = None,
    volume_scale: Optional[float] = None,
    pre_phoneme_length: Optional[float] = None,
    post_phoneme_length: Optional[float] = None,
    save_as_memo: bool = False,
    memo_title: str = "",
):
    """Synthesize text with TTS and play it on M5.
    Requires VOICE_API_HOST env var pointing to the TTS/ASR server.

    engine: "voicevox" (default) / "kokoro" / "piper"

    [voicevox]
      voicevox_speaker: speaker ID (uses set_voice default if omitted, fallback to 6)
        6=Shikoku Metan (tsun), 23=WhiteCUL (normal), 47=NurseRobot_Type-T, 29=No.7
      speed_scale: speech rate (0.5-2.0)
      pitch_scale: pitch (-0.15 to 0.15)
      intonation_scale: intonation (0-2.0)
      volume_scale: volume (0-2.0)
      pre_phoneme_length / post_phoneme_length: silence in seconds
      * Omitted parameters use values saved by set_voice
    save_as_memo: if True, also saves to dashboard voice memo
    memo_title: memo title (defaults to first 20 chars of text)

    [kokoro]
      voice: jf_alpha / jf_gongitsune / jf_nezumi / jf_tebukuro / jm_kumo
      speed: speed multiplier

    [piper]
      speaker: speaker ID (default 0)
      length_scale: speed (lower = faster)
    """
    if not _TTS_URL:
        return "TTS unavailable: VOICE_API_HOST env var is not set"

    saved = _load_voice_settings()
    resolved_speaker = voicevox_speaker if voicevox_speaker is not None else saved.get("voicevox_speaker", _VOICE_SETTINGS_DEFAULT)
    resolved_speed = speed_scale if speed_scale is not None else saved.get("speed_scale", 1.0)

    wav_bytes_holder: list[bytes] = []

    def _tts_and_upload():
        if engine == "kokoro":
            payload = {"text": text, "engine": "kokoro", "voice": voice, "speed": speed, "lang": "ja"}
        elif engine == "voicevox":
            payload = {"text": text, "engine": "voicevox", "voicevox_speaker": resolved_speaker, "speed_scale": resolved_speed}
            for key, val, saved_key in [
                ("pitch_scale", pitch_scale, "pitch_scale"),
                ("intonation_scale", intonation_scale, "intonation_scale"),
                ("volume_scale", volume_scale, "volume_scale"),
                ("pre_phoneme_length", pre_phoneme_length, "pre_phoneme_length"),
                ("post_phoneme_length", post_phoneme_length, "post_phoneme_length"),
            ]:
                v = val if val is not None else saved.get(saved_key)
                if v is not None:
                    payload[key] = v
        else:  # piper
            payload = {
                "text": text, "engine": "piper", "speaker": speaker,
                "length_scale": length_scale, "noise_scale": noise_scale,
                "noise_w": noise_w, "sentence_silence": sentence_silence,
            }
        for _tts_url in [_TTS_URL, _TTS_FALLBACK_URL]:
            try:
                r = requests.post(f"{_tts_url}/speak", json=payload, timeout=30)
                r.raise_for_status()
                break
            except (requests.ConnectionError, requests.Timeout):
                if _tts_url == _TTS_FALLBACK_URL:
                    raise
                continue
        wav_bytes = r.content
        if save_as_memo:
            wav_bytes_holder.append(wav_bytes)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(wav_bytes)
            tmp_path = f.name
        try:
            mono_buf = _convert_wav_mono_16k(tmp_path)
            resp = _http_post(
                "/upload_wav", timeout=30,
                files={"file": ("tts_speak.wav", mono_buf, "audio/wav")},
            )
            resp.raise_for_status()
        finally:
            os.unlink(tmp_path)

    await asyncio.to_thread(_tts_and_upload)
    await asyncio.to_thread(lambda: _http_get("/se_play?name=tts_speak.wav", timeout=10))

    if save_as_memo and wav_bytes_holder:
        char_id = os.environ.get("CHARACTER_ID", "unknown")
        title = memo_title or text[:20]
        def _upload_memo():
            requests.post(
                f"{_DASHBOARD_URL}/api/voice_memo/{char_id}/upload",
                data={"title": title},
                files={"file": ("memo.wav", wav_bytes_holder[0], "audio/wav")},
                timeout=15,
            )
        await asyncio.to_thread(_upload_memo)

    return f"spoke: {text}"


@mcp.tool()
async def list_voice_memos(person_id: str, unlistened_by: str = ""):
    """Get list of voice memos.
    Requires DASHBOARD_HOST env var.

    person_id: character ID of the memo owner
    unlistened_by: if set, returns only memos not yet listened to by that character
    """
    def _fetch():
        params = {}
        if unlistened_by:
            params["unlistened_by"] = unlistened_by
        r = requests.get(f"{_DASHBOARD_URL}/api/voice_memo/{person_id}", params=params, timeout=10)
        r.raise_for_status()
        return r.json()
    return await asyncio.to_thread(_fetch)


@mcp.tool()
async def listen_voice_memo(
    owner_id: str,
    filename: str,
    play_on_m5: bool = False,
    transcribe: bool = False,
):
    """Retrieve a voice memo, optionally play it on M5, and mark it as listened.
    Requires DASHBOARD_HOST env var. Requires VOICE_API_HOST if transcribe=True.

    owner_id: character ID of the memo owner
    filename: filename from list_voice_memos
    play_on_m5: if True, play through M5 speaker
    transcribe: if True, return speech-to-text transcription
    """
    char_id = os.environ.get("CHARACTER_ID", "unknown")

    def _fetch_and_play():
        r = requests.get(f"{_DASHBOARD_URL}/api/voice_memo/{owner_id}/{filename}", timeout=15)
        r.raise_for_status()
        audio_bytes = r.content
        ext = Path(filename).suffix.lower()

        transcript = None
        if transcribe:
            if not _ASR_URL:
                transcript = "(transcription unavailable: VOICE_API_HOST not set)"
            else:
                with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as f:
                    f.write(audio_bytes)
                    tmp_path = f.name
                try:
                    wav_buf = _convert_wav_mono_16k(tmp_path)
                    asr_r = requests.post(
                        f"{_ASR_URL}/transcribe",
                        files={"file": (Path(filename).stem + ".wav", wav_buf, "audio/wav")},
                        timeout=60,
                    )
                    asr_r.raise_for_status()
                    transcript = asr_r.json().get("text", "")
                except Exception as e:
                    transcript = f"(transcription failed: {e})"
                finally:
                    Path(tmp_path).unlink(missing_ok=True)

        if play_on_m5:
            with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as f:
                f.write(audio_bytes)
                tmp_path = f.name
            try:
                mono_buf = _convert_wav_mono_16k(tmp_path)
                resp = _http_post(
                    "/upload_wav", timeout=30,
                    files={"file": ("voice_memo.wav", mono_buf, "audio/wav")},
                )
                resp.raise_for_status()
                _http_get("/se_play?name=voice_memo.wav", timeout=10)
            finally:
                os.unlink(tmp_path)

        requests.post(
            f"{_DASHBOARD_URL}/api/voice_memo/{owner_id}/{filename}/listen",
            params={"listener": char_id},
            timeout=10,
        )
        return transcript

    transcript = await asyncio.to_thread(_fetch_and_play)
    result = f"listened: {owner_id}/{filename}"
    if transcript:
        result += f"\ntranscription: {transcript}"
    return result


@mcp.tool()
async def save_tts_memo(text: str, title: str = ""):
    """Synthesize text with TTS and save as a voice memo (does not play on M5).
    Requires CHARACTER_ID and DASHBOARD_HOST env vars.
    Voice settings from set_voice are used.

    text: text to save as memo
    title: memo title (defaults to first 20 chars of text)
    """
    if not _TTS_URL:
        return "TTS unavailable: VOICE_API_HOST env var is not set"
    char_id = os.environ.get("CHARACTER_ID", "unknown")
    saved = _load_voice_settings()
    resolved_speaker = saved.get("voicevox_speaker", _VOICE_SETTINGS_DEFAULT)
    resolved_speed = saved.get("speed_scale", 1.0)

    def _generate_and_upload():
        payload = {"text": text, "engine": "voicevox", "voicevox_speaker": resolved_speaker, "speed_scale": resolved_speed}
        for key, saved_key in [
            ("pitch_scale", "pitch_scale"), ("intonation_scale", "intonation_scale"),
            ("volume_scale", "volume_scale"), ("pre_phoneme_length", "pre_phoneme_length"),
            ("post_phoneme_length", "post_phoneme_length"),
        ]:
            v = saved.get(saved_key)
            if v is not None:
                payload[key] = v
        for _tts_url in [_TTS_URL, _TTS_FALLBACK_URL]:
            try:
                r = requests.post(f"{_tts_url}/speak", json=payload, timeout=30)
                r.raise_for_status()
                break
            except (requests.ConnectionError, requests.Timeout):
                if _tts_url == _TTS_FALLBACK_URL:
                    raise
                continue
        memo_title = title or text[:20]
        requests.post(
            f"{_DASHBOARD_URL}/api/voice_memo/{char_id}/upload",
            data={"title": memo_title},
            files={"file": ("memo.wav", r.content, "audio/wav")},
            timeout=15,
        ).raise_for_status()
        return memo_title

    memo_title = await asyncio.to_thread(_generate_and_upload)
    return f"saved voice memo: '{memo_title}'"


@mcp.tool()
async def lock_voice_memo(owner_id: str, filename: str):
    """Toggle lock/unlock on a voice memo. Locked memos are not auto-deleted.
    Requires DASHBOARD_HOST env var.

    owner_id: character ID of the memo owner
    filename: filename from list_voice_memos
    """
    def _toggle():
        r = requests.post(
            f"{_DASHBOARD_URL}/api/voice_memo/{owner_id}/{filename}/lock",
            timeout=10,
        )
        r.raise_for_status()
        return r.json()
    result = await asyncio.to_thread(_toggle)
    status = "locked" if result.get("locked") else "unlocked"
    return f"{status}: {owner_id}/{filename}"


@mcp.tool()
async def transcribe_audio(file_path: str):
    """Transcribe a local WAV file to text using ASR.
    Requires VOICE_API_HOST env var.
    """
    if not _ASR_URL:
        return "ASR unavailable: VOICE_API_HOST env var is not set"
    def _transcribe():
        with open(file_path, "rb") as f:
            r = requests.post(
                f"{_ASR_URL}/transcribe",
                files={"file": (os.path.basename(file_path), f, "audio/wav")},
                timeout=60,
            )
            r.raise_for_status()
            return r.json()
    return await asyncio.to_thread(_transcribe)


@mcp.tool()
async def conversation_relay(to_character: str, message: str, turns_remaining: int = 2):
    """Pass a message to another character to start a conversation relay.
    Call after your own speak().
    Requires DASHBOARD_HOST env var (uses DASHBOARD_URL env var if set).

    to_character: character ID to pass the message to
    message: the message you just said
    turns_remaining: remaining turns (default 2)
    """
    char_id = os.environ.get("CHARACTER_ID", "unknown")
    dashboard_url = os.environ.get("DASHBOARD_URL", _DASHBOARD_URL)

    def _relay():
        r = requests.post(
            f"{dashboard_url}/api/relay/start",
            json={"from_char": char_id, "to_char": to_character, "message": message, "turns_remaining": turns_remaining},
            timeout=10,
        )
        r.raise_for_status()
        return r.json()

    result = await asyncio.to_thread(_relay)
    return f"relay started: passed to {to_character} ({result})"


# M5_ALLOWED_TOOLS: comma-separated list of tool names to expose (all others are removed)
_allowed_tools_env = os.environ.get("M5_ALLOWED_TOOLS")
if _allowed_tools_env:
    _allowed = set(_allowed_tools_env.split(","))
    for _name in list(mcp._tool_manager._tools.keys()):
        if _name not in _allowed:
            mcp.remove_tool(_name)


def main():
    mcp.run()
