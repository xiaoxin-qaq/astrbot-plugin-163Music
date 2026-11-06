import os
import re
import time
import base64
import aiohttp
import asyncio
import shutil
import subprocess
import urllib.parse
import tempfile
import json
from typing import Dict, Any, Optional, List, Tuple

try:
    import imageio_ffmpeg
except ImportError:
    imageio_ffmpeg = None

from astrbot.api import star, logger
from astrbot.api.star import StarTools
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core.message.message_event_result import MessageChain
from astrbot.api.message_components import Plain, Image, Record, File

# 音质从高到低排列：jymaster 超清母带 / dolby 杜比全景声 / vivid 臻音全景声 /
# sky 沉浸环绕声 / jyeffect 高清环绕声 / hires Hi-Res / lossless 无损 /
# exhigh 极高 / higher 较高 / standard 标准
QUALITY_LADDER = [
    "jymaster", "dolby", "vivid", "sky", "jyeffect",
    "hires", "lossless", "exhigh", "higher", "standard",
]
# 无 VIP 时可回退的音质
FREE_QUALITIES = ("lossless", "exhigh", "higher", "standard")
# 点歌会话 1 分 50 秒无新操作时收尾，赶在 QQ 2 分钟撤回窗口内
POINT_SONG_IDLE_SECONDS = 110
POINT_SONG_STAGES = ("type", "artist", "song")

# 下载会话 100 秒没有新的点歌/下载操作时自动收尾
DOWNLOAD_IDLE_SECONDS = 100
DOWNLOAD_STAGES = ("track", "dl_type", "dl_artist", "playlist")

# 音质等级的中文名（展示用，附赠猫娘理解）
QUALITY_NAMES = {
    "jymaster": "超清母带",
    "dolby": "杜比全景声",
    "vivid": "臻音全景声",
    "sky": "沉浸环绕声",
    "jyeffect": "高清环绕声",
    "hires": "Hi-Res",
    "lossless": "无损",
    "exhigh": "极高",
    "higher": "较高",
    "standard": "标准",
}

# LRC 时间标签：[mm:ss]、[mm:ss.xx]、[mm:ss.xxx]、[mm:ss:xx]
_LRC_TIMESTAMP_RE = re.compile(r"\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]")


def _lrc_timestamp_ms(match) -> int:
    minutes = int(match.group(1))
    seconds = int(match.group(2))
    fraction = match.group(3) or "0"
    if len(fraction) == 1:
        fraction += "00"
    elif len(fraction) == 2:
        fraction += "0"
    else:
        fraction = fraction[:3]
    return (minutes * 60 + seconds) * 1000 + int(fraction)


def _merge_lrc_lyrics(original: str, translation: str = "") -> str:
    """把翻译按时间轴并进原文，得到播放器可直接显示的双语 LRC。"""
    original = (original or "").strip()
    translation = (translation or "").strip()
    if not original:
        return translation
    if not translation:
        return original

    translated_lines: Dict[int, List[str]] = {}
    for line in translation.splitlines():
        matches = list(_LRC_TIMESTAMP_RE.finditer(line))
        if not matches:
            continue
        text = _LRC_TIMESTAMP_RE.sub("", line).strip()
        if not text:
            continue
        for m in matches:
            bucket = translated_lines.setdefault(_lrc_timestamp_ms(m), [])
            if text not in bucket:
                bucket.append(text)

    if not translated_lines:
        return f"{original}\n\n{translation}"

    merged: List[str] = []
    matched = False
    for line in original.splitlines():
        matches = list(_LRC_TIMESTAMP_RE.finditer(line))
        if not matches:
            merged.append(line)
            continue
        text = _LRC_TIMESTAMP_RE.sub("", line).strip()
        if not text:
            merged.append(line)
            continue
        extra: List[str] = []
        for m in matches:
            for translated in translated_lines.get(_lrc_timestamp_ms(m), []):
                if translated and translated not in text and translated not in extra:
                    extra.append(translated)
        if extra:
            line = f"{line.rstrip()}  {' / '.join(extra)}"
            matched = True
        merged.append(line)
    if not matched:
        return f"{original}\n\n{translation}"
    return "\n".join(merged).strip()


def _id3_synchsafe_decode(data: bytes) -> int:
    value = 0
    for byte in data:
        value = (value << 7) | (byte & 0x7F)
    return value


def _id3_synchsafe_encode(value: int) -> bytes:
    return bytes((
        (value >> 21) & 0x7F,
        (value >> 14) & 0x7F,
        (value >> 7) & 0x7F,
        value & 0x7F,
    ))


def _id3_text_end(data: bytes, encoding: int) -> int:
    if encoding in (0, 3):
        pos = data.find(b"\x00")
        return len(data) if pos < 0 else pos
    start = 2 if encoding == 1 and data[:2] in (b"\xff\xfe", b"\xfe\xff") else 0
    pos = start
    while pos + 1 < len(data):
        if data[pos:pos + 2] == b"\x00\x00":
            return pos
        pos += 2
    return len(data)


def _id3_decode_text(data: bytes, encoding: int) -> str:
    raw = data[:_id3_text_end(data, encoding)]
    try:
        if encoding == 0:
            return raw.decode("latin-1")
        if encoding == 1:
            if raw[:2] == b"\xff\xfe":
                return raw[2:].decode("utf-16-le")
            if raw[:2] == b"\xfe\xff":
                return raw[2:].decode("utf-16-be")
            return raw.decode("utf-16-le")
        if encoding == 2:
            return raw.decode("utf-16-be")
        if encoding == 3:
            return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("utf-8", "replace")
    return ""


def _id3_txxx_description(payload: bytes) -> str:
    if not payload:
        return ""
    return _id3_decode_text(payload[1:], payload[0]).strip()


def _id3_parse_frames(body: bytes, version: int) -> List[Tuple[bytes, bytes, bytes]]:
    frames: List[Tuple[bytes, bytes, bytes]] = []
    offset = 0
    while offset + 10 <= len(body):
        frame_id = body[offset:offset + 4]
        if not re.fullmatch(rb"[A-Z0-9]{4}", frame_id):
            break
        if version == 4:
            size = _id3_synchsafe_decode(body[offset + 4:offset + 8])
        else:
            size = int.from_bytes(body[offset + 4:offset + 8], "big")
        if size < 0 or offset + 10 + size > len(body):
            break
        flags = body[offset + 8:offset + 10]
        payload = body[offset + 10:offset + 10 + size]
        frames.append((frame_id, flags, payload))
        offset += 10 + size
    return frames


def _id3_build_frame(frame_id: bytes, flags: bytes, payload: bytes, version: int) -> bytes:
    if version == 4:
        size = _id3_synchsafe_encode(len(payload))
    else:
        size = len(payload).to_bytes(4, "big")
    return frame_id + size + flags + payload


def _id3_build_uslt_payload(lyrics: str) -> bytes:
    text = (lyrics or "").replace("\x00", "")

    def encode(value: str) -> bytes:
        return b"\xff\xfe" + value.encode("utf-16-le") + b"\x00\x00"

    # 编码 1 = UTF-16 + BOM；语言 und = 未指定
    return b"\x01" + b"und" + encode("") + encode(text)


def _write_mp3_uslt(audio_path: str, lyrics: str) -> bool:
    """给 MP3 写入标准 USLT 歌词帧。

    ffmpeg 7.x 会把 `-metadata lyrics` 写成 description 为 USLT 的 TXXX 帧，
    部分播放器不识别。这里直接重写 ID3v2 标签，确保生成真正的 USLT 帧。
    """
    if not lyrics:
        return False

    try:
        with open(audio_path, "rb") as f:
            data = f.read()
    except OSError as e:
        logger.warning(f"Netease Music plugin: read mp3 for lyrics failed: {e!s}")
        return False

    version = 3
    frames: List[Tuple[bytes, bytes, bytes]] = []
    audio = data
    if len(data) >= 10 and data[:3] == b"ID3":
        version = data[3]
        if version not in (3, 4):
            logger.warning(f"Netease Music plugin: unsupported ID3v2.{version}, skip USLT")
            return False
        if data[5]:
            logger.warning(f"Netease Music plugin: unsupported ID3 flags 0x{data[5]:02x}, skip USLT")
            return False
        tag_size = _id3_synchsafe_decode(data[6:10])
        body = data[10:10 + tag_size]
        audio = data[10 + tag_size:]
        frames = _id3_parse_frames(body, version)

    kept: List[Tuple[bytes, bytes, bytes]] = []
    for frame_id, flags, payload in frames:
        if frame_id == b"USLT":
            continue
        if frame_id == b"TXXX":
            desc = _id3_txxx_description(payload).upper()
            if desc in ("USLT", "LYRICS", "UNSYNCEDLYRICS"):
                continue
        kept.append((frame_id, flags, payload))
    kept.append((b"USLT", b"\x00\x00", _id3_build_uslt_payload(lyrics)))

    new_body = b"".join(
        _id3_build_frame(frame_id, flags, payload, version)
        for frame_id, flags, payload in kept
    )
    padding = b"\x00" * 1024
    tag_size = len(new_body) + len(padding)
    header = b"ID3" + bytes((version, 0, 0)) + _id3_synchsafe_encode(tag_size)
    temp_path = audio_path + ".id3tmp"
    try:
        with open(temp_path, "wb") as f:
            f.write(header + new_body + padding + audio)
        os.replace(temp_path, audio_path)
        return True
    except OSError as e:
        logger.warning(f"Netease Music plugin: write USLT failed: {e!s}")
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except OSError:
            pass
        return False


def _find_ffmpeg() -> Optional[str]:
    if imageio_ffmpeg:
        try:
            path = imageio_ffmpeg.get_ffmpeg_exe()
            if path and os.path.isfile(path):
                return path
        except Exception:
            pass
    path = shutil.which("ffmpeg")
    if path:
        return path
    candidates = [
        r"C:\ffmpeg\bin\ffmpeg.exe",
        r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
        r"C:\Program Files (x86)\ffmpeg\bin\ffmpeg.exe",
        r"C:\Users\Administrator\ffmpeg\bin\ffmpeg.exe",
        r"C:\Windows\ffmpeg\bin\ffmpeg.exe",
        os.path.join(os.getcwd(), "ffmpeg", "bin", "ffmpeg.exe"),
    ]
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    try:
        proc = subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=5)
        if proc.returncode == 0:
            return "ffmpeg"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


class NeteaseMusicAPI:
    def __init__(self, api_url: str, session: aiohttp.ClientSession):
        self.base_url = api_url.rstrip("/")
        self.session = session

    async def search_songs(self, keyword: str, limit: int) -> List[Dict[str, Any]]:
        url = f"{self.base_url}/search?keywords={urllib.parse.quote(keyword)}&limit={limit}&type=1"
        async with self.session.get(url) as r:
            r.raise_for_status()
            data = await r.json()
            return data.get("result", {}).get("songs", [])

    async def search_artists(self, keyword: str, limit: int) -> List[Dict[str, Any]]:
        url = f"{self.base_url}/search?keywords={urllib.parse.quote(keyword)}&limit={limit}&type=100"
        async with self.session.get(url) as r:
            r.raise_for_status()
            data = await r.json()
            return data.get("result", {}).get("artists", [])

    async def get_artist_top_songs(self, artist_id: int, limit: int) -> List[Dict[str, Any]]:
        url = f"{self.base_url}/artist/top/song?id={str(artist_id)}"
        async with self.session.get(url) as r:
            r.raise_for_status()
            data = await r.json()
            return (data.get("songs") or [])[:limit]

    async def get_song_details(self, song_id: int) -> Optional[Dict[str, Any]]:
        url = f"{self.base_url}/song/detail?ids={str(song_id)}"
        async with self.session.get(url) as r:
            r.raise_for_status()
            data = await r.json()
            return data["songs"][0] if data.get("songs") else None

    async def get_lyrics(self, song_id: int, cookie: str = "") -> Dict[str, Any]:
        clean_cookie = "; ".join(part.strip() for part in cookie.split(";")) if cookie else ""
        url = f"{self.base_url}/lyric"
        params = {"id": str(song_id)}
        if clean_cookie:
            params["cookie"] = clean_cookie
        async with self.session.get(url, params=params) as r:
            r.raise_for_status()
            data = await r.json()
            return data if isinstance(data, dict) else {}

    async def get_audio_url(self, song_id: int, quality: str, cookie: str = "") -> Optional[Dict[str, Any]]:
        clean_cookie = "; ".join(part.strip() for part in cookie.split(";")) if cookie else ""
        url = f"{self.base_url}/song/url/v1?id={str(song_id)}&level={quality}&cookie={clean_cookie}"
        async with self.session.get(url) as r:
            r.raise_for_status()
            data = await r.json()
            audio_list = data.get("data") or [{}]
            return audio_list[0] if audio_list else {}

    async def qr_key(self) -> Optional[str]:
        url = f"{self.base_url}/login/qr/key?timestamp={int(time.time() * 1000)}"
        async with self.session.get(url) as r:
            r.raise_for_status()
            data = await r.json()
            return data.get("data", {}).get("unikey")

    async def qr_create(self, key: str) -> Optional[str]:
        url = f"{self.base_url}/login/qr/create?key={key}&qrimg=true&timestamp={int(time.time() * 1000)}"
        async with self.session.get(url) as r:
            r.raise_for_status()
            data = await r.json()
            return data.get("data", {}).get("qrimg")

    async def qr_check(self, key: str) -> Dict[str, Any]:
        url = f"{self.base_url}/login/qr/check?key={key}&timestamp={int(time.time() * 1000)}"
        async with self.session.get(url) as r:
            r.raise_for_status()
            return await r.json()

    async def login_status(self, cookie: str) -> Dict[str, Any]:
        url = f"{self.base_url}/login/status?timestamp={int(time.time() * 1000)}"
        async with self.session.post(url, json={"cookie": cookie}) as r:
            r.raise_for_status()
            return await r.json()

    async def get_user_playlists(self, uid: int, cookie: str = "", limit: int = 50) -> List[Dict[str, Any]]:
        clean_cookie = "; ".join(part.strip() for part in cookie.split(";")) if cookie else ""
        url = (
            f"{self.base_url}/user/playlist?uid={uid}&limit={limit}"
            f"&cookie={clean_cookie}&timestamp={int(time.time() * 1000)}"
        )
        async with self.session.get(url) as r:
            r.raise_for_status()
            data = await r.json()
            return data.get("playlist") or []

    async def get_playlist_detail(self, playlist_id: int, cookie: str = "") -> Optional[Dict[str, Any]]:
        clean_cookie = "; ".join(part.strip() for part in cookie.split(";")) if cookie else ""
        url = f"{self.base_url}/playlist/detail?id={playlist_id}&cookie={clean_cookie}&timestamp={int(time.time() * 1000)}"
        async with self.session.get(url) as r:
            r.raise_for_status()
            data = await r.json()
            return data.get("playlist")

    async def get_playlist_tracks(self, playlist_id: int, limit: int, cookie: str = "") -> List[Dict[str, Any]]:
        clean_cookie = "; ".join(part.strip() for part in cookie.split(";")) if cookie else ""
        url = f"{self.base_url}/playlist/track/all?id={playlist_id}&limit={limit}&cookie={clean_cookie}"
        async with self.session.get(url) as r:
            r.raise_for_status()
            data = await r.json()
            return data.get("songs") or []

    async def download_image(self, url: str) -> Optional[bytes]:
        if not url:
            return None
        async with self.session.get(url) as r:
            if r.status == 200:
                return await r.read()
        return None

    async def download_audio(self, url: str, save_path: str) -> bool:
        try:
            async with self.session.get(url) as r:
                if r.status == 200:
                    with open(save_path, "wb") as f:
                        f.write(await r.read())
                    return True
        except Exception as e:
            logger.error(f"Netease Music plugin: Failed to download audio. Error: {e!s}")
        return False


class Main(star.Star):
    def __init__(self, context, config: Optional[Dict[str, Any]] = None):
        super().__init__(context)
        self.config = config or {}
        self.config.setdefault("api_url", "http://127.0.0.1:3000")
        self.config.setdefault("quality", "jymaster")
        self.config.setdefault("search_limit", 30)
        self.config.setdefault("page_size", 10)
        self.config.setdefault("max_fetch_songs", 1000)
        self.config.setdefault("cookie", "")

        self.waiting_users: Dict[str, Dict[str, Any]] = {}
        self.download_pending_recall: Dict[str, List[int]] = {}
        self._session_timers: Dict[str, asyncio.Task] = {}
        self.download_timers: Dict[str, asyncio.Task] = {}
        self._vip_cache: Optional[Tuple[float, int]] = None

        self.http_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60))
        self.api = NeteaseMusicAPI(self.config["api_url"], self.http_session)

        self._ffmpeg_path = _find_ffmpeg()
        if self._ffmpeg_path:
            ffmpeg_dir = os.path.dirname(self._ffmpeg_path)
            current_path = os.environ.get("PATH", "")
            if ffmpeg_dir not in current_path:
                os.environ["PATH"] = ffmpeg_dir + os.pathsep + current_path
            logger.info(f"Netease Music plugin: ffmpeg found at {self._ffmpeg_path}")
        else:
            logger.warning("Netease Music plugin: ffmpeg not found.")

        self.cleanup_task: Optional[asyncio.Task] = None
        self.temp_dir = os.path.join(tempfile.gettempdir(), "netease_music_plugin")
        os.makedirs(self.temp_dir, exist_ok=True)

        self._qr_task: Optional[asyncio.Task] = None
        self._qr_phase: str = "idle"

        # 持久化数据统一放在 data/plugin_data/<plugin_name>，避免插件更新时丢失。
        plugin_name = getattr(self, "name", None) or "astrbot_plugin_163music"
        self._data_dir = StarTools.get_data_dir(plugin_name)
        self._cookie_path = str(self._data_dir / "netease_login.json")
        self._load_saved_cookie()

    # ---------- 配置读取（统一来自 _conf_schema.json 对应的配置项） ----------

    def _cfg_int(self, key: str, default: int, upper: Optional[int] = None) -> int:
        try:
            val = int(self.config.get(key, default) or default)
        except (TypeError, ValueError):
            val = default
        val = max(1, val)
        if upper is not None:
            val = min(upper, val)
        return val

    def _search_limit(self) -> int:
        """搜索时抓取的歌曲 / 歌手数量。
        保底为每页数量的 3 倍，确保列表一定能翻页；
        配置值更大时按配置抓取。"""
        return max(self._cfg_int("search_limit", 30), self._page_size() * 3)

    def _page_size(self) -> int:
        """歌曲列表每页显示数量（点歌与下载共用）。"""
        return self._cfg_int("page_size", 10)

    def _max_fetch(self) -> int:
        """下载歌单时最大抓取的歌曲数量（上限 1000）。"""
        return self._cfg_int("max_fetch_songs", 1000, upper=1000)

    async def _get_vip_type(self) -> int:
        """读取当前登录账号的 VIP 等级（缓存 10 分钟，避免频繁请求）。"""
        now = time.time()
        if self._vip_cache and now - self._vip_cache[0] < 600:
            return self._vip_cache[1]
        vip = 0
        cookie = self.config.get("cookie", "")
        if cookie:
            try:
                data = await self.api.login_status(cookie)
                profile = (data.get("data") or {}).get("profile") or {}
                vip = int(profile.get("vipType", 0) or 0)
            except Exception as e:
                logger.warning(f"Netease Music plugin: get vip type failed: {e!s}")
                vip = 0
        self._vip_cache = (now, vip)
        logger.info(f"Netease Music plugin: vipType={vip}")
        return vip

    async def _quality_chain(self) -> List[str]:
        """构建点歌的音质尝试链：配置的优先音质在最前，
        其余按从高到低排列；按 VIP 等级过滤：
        - 黑胶SVIP（vipType>=110）：可用全部音质（含母带/杜比）
        - 普通黑胶VIP（vipType>0）：剔除 SVIP 专属的母带和杜比
        - 无 VIP：退回免费可获取的最高音质"""
        preferred = str(self.config.get("quality", "jymaster") or "jymaster").strip()
        chain: List[str] = []
        if preferred in QUALITY_LADDER:
            chain.append(preferred)
        for q in QUALITY_LADDER:
            if q not in chain:
                chain.append(q)
        vip = await self._get_vip_type()
        if vip >= 110:
            pass
        elif vip > 0:
            chain = [q for q in chain if q not in ("jymaster", "dolby")]
        else:
            chain = [q for q in chain if q in FREE_QUALITIES]
        return chain

    # ---------- 消息工具 ----------

    def _get_plain_text(self, event: AstrMessageEvent) -> str:
        try:
            obj = getattr(event, "message_obj", None)
            if obj is not None:
                parts = []
                for comp in getattr(obj, "message", []) or []:
                    t = getattr(comp, "text", None)
                    if t:
                        parts.append(t)
                if parts:
                    return "".join(parts).strip()
        except Exception:
            pass
        return (getattr(event, "message_str", "") or "").strip()

    @staticmethod
    def _disable_llm(event: AstrMessageEvent) -> None:
        """彻底阻断本插件消息的默认 LLM 链路。

        AstrBot v4 中 event.call_llm=True 表示禁止默认 LLM 请求；
        这里同时 stop_event()，兼容旧版本并确保后续 handler / LLM 不再执行。
        """
        try:
            event.should_call_llm(True)
        except Exception:
            try:
                event.call_llm = True
            except Exception:
                pass
        try:
            event.stop_event()
        except Exception:
            pass

    def _chain_to_cqhttp(self, chain: MessageChain) -> List[Dict[str, Any]]:
        segs: List[Dict[str, Any]] = []
        for comp in chain.chain:
            if isinstance(comp, Plain):
                segs.append({"type": "text", "data": {"text": comp.text}})
            elif isinstance(comp, Image):
                f = getattr(comp, "file", None)
                url = getattr(comp, "url", None)
                b64 = getattr(comp, "base64", None)
                if f:
                    segs.append({"type": "image", "data": {"file": f}})
                elif b64:
                    segs.append({"type": "image", "data": {"file": "base64://" + b64}})
                elif url:
                    segs.append({"type": "image", "data": {"file": url}})
                else:
                    raise ValueError("Image component has no file/url/base64")
            elif isinstance(comp, Record):
                f = getattr(comp, "file", None) or getattr(comp, "url", None)
                if not f:
                    raise ValueError("Record component has no file/url")
                segs.append({"type": "record", "data": {"file": f}})
            elif isinstance(comp, File):
                f = getattr(comp, "file", None)
                if not f:
                    raise ValueError("File component has no file")
                segs.append({"type": "file", "data": {"file": f, "name": getattr(comp, "name", None)}})
            else:
                raise ValueError(f"unsupported component: {type(comp).__name__}")
        return segs

    @staticmethod
    def _get_cqhttp_client(event: AstrMessageEvent):
        try:
            if str(event.get_platform_name()).lower() != "aiocqhttp":
                return None
        except Exception:
            return None
        client = getattr(event, "bot", None) or getattr(event, "client", None)
        if client is None:
            return None
        return getattr(client, "api", None) or client

    @staticmethod
    def _get_event_message_id(event: AstrMessageEvent) -> Optional[int]:
        try:
            obj = getattr(event, "message_obj", None)
            if obj is not None:
                mid = getattr(obj, "message_id", None)
                if mid:
                    return int(mid)
        except Exception:
            pass
        try:
            mid = getattr(event, "message_id", None)
            if mid:
                return int(mid)
        except Exception:
            pass
        return None

    async def _send_track(self, event: AstrMessageEvent, chain: MessageChain) -> Optional[int]:
        try:
            api = self._get_cqhttp_client(event)
            if api is not None:
                segs = self._chain_to_cqhttp(chain)
                if "GROUP" in str(event.get_message_type()).upper():
                    res = await api.call_action("send_group_msg", group_id=event.get_group_id(), message=segs)
                else:
                    res = await api.call_action("send_private_msg", user_id=event.get_sender_id(), message=segs)
                if isinstance(res, dict):
                    data = res.get("data") or {}
                    mid = res.get("message_id") or data.get("message_id")
                    if mid:
                        return int(mid)
        except Exception as e:
            logger.warning(f"Netease Music plugin: tracked send failed, fallback. {e!s}")
        await event.send(chain)
        return None

    async def _recall(self, event: AstrMessageEvent, message_id: Optional[int]) -> bool:
        if not message_id:
            return False
        try:
            api = self._get_cqhttp_client(event)
            if api is not None:
                await api.call_action("delete_msg", message_id=message_id)
                return True
        except Exception as e:
            logger.warning(f"Netease Music plugin: failed to recall {message_id}: {e!s}")
        return False

    async def _recall_user_message(self, event: AstrMessageEvent) -> bool:
        mid = self._get_event_message_id(event)
        if not mid:
            return False
        return await self._recall(event, mid)

    def _track_pending_recall(self, event: AstrMessageEvent, msg_id: Optional[int]) -> None:
        if not msg_id:
            return
        try:
            sid = event.get_session_id()
            self.download_pending_recall.setdefault(sid, []).append(int(msg_id))
        except Exception:
            pass

    def _track_user_message(self, event: AstrMessageEvent) -> None:
        self._track_pending_recall(event, self._get_event_message_id(event))

    async def _send_download_msg(self, event: AstrMessageEvent, chain: MessageChain) -> Optional[int]:
        mid = await self._send_track(event, chain)
        self._track_pending_recall(event, mid)
        return mid

    async def _recall_all_pending(self, event: AstrMessageEvent) -> int:
        sid = event.get_session_id()
        pending = self.download_pending_recall.pop(sid, [])
        if not pending:
            return 0
        seen = set()
        unique_ids: List[int] = []
        for mid in pending:
            if mid in seen:
                continue
            seen.add(mid)
            unique_ids.append(mid)
        count = 0
        for mid in unique_ids:
            ok = await self._recall(event, mid)
            if ok:
                count += 1
        return count

    async def _recall_pending_except(self, event: AstrMessageEvent, keep_ids: List[Optional[int]]) -> int:
        """撤回下载会话的中间消息，但保留歌曲列表等指定消息。"""
        sid = event.get_session_id()
        keep = {int(mid) for mid in keep_ids if mid}
        pending = self.download_pending_recall.get(sid, [])
        recall_ids: List[int] = []
        remaining: List[int] = []
        for mid in pending:
            if int(mid) in keep:
                remaining.append(int(mid))
            else:
                recall_ids.append(int(mid))
        self.download_pending_recall[sid] = remaining
        return await self._recall_ids(event, recall_ids)

    async def _recall_ids(self, event: AstrMessageEvent, msg_ids: List[Optional[int]]) -> int:
        """按顺序撤回一组消息（自动去重、跳过空值）。"""
        seen = set()
        count = 0
        for mid in msg_ids:
            if not mid or mid in seen:
                continue
            seen.add(mid)
            if await self._recall(event, mid):
                count += 1
        return count

    def _load_saved_cookie(self):
        if self.config.get("cookie"):
            return
        try:
            if os.path.isfile(self._cookie_path):
                with open(self._cookie_path, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                if saved.get("cookie"):
                    self.config["cookie"] = saved["cookie"]
                    logger.info("Netease Music plugin: Restored login cookie.")
        except Exception as e:
            logger.warning(f"Netease Music plugin: Failed to load cookie: {e!s}")

    def _save_cookie(self, cookie: str):
        try:
            with open(self._cookie_path, "w", encoding="utf-8") as f:
                json.dump({"cookie": cookie}, f)
        except Exception as e:
            logger.warning(f"Netease Music plugin: Failed to save cookie: {e!s}")

    async def initialize(self):
        self.cleanup_task = asyncio.create_task(self._periodic_cleanup())
        logger.info("Netease Music plugin: cleanup task started.")

    async def terminate(self):
        if self.cleanup_task and not self.cleanup_task.done():
            self.cleanup_task.cancel()
            try:
                await self.cleanup_task
            except asyncio.CancelledError:
                pass
        for task in self._session_timers.values():
            task.cancel()
        self._session_timers.clear()
        for task in self.download_timers.values():
            task.cancel()
        self.download_timers.clear()
        if self.http_session and not self.http_session.closed:
            await self.http_session.close()

    async def _periodic_cleanup(self):
        while True:
            await asyncio.sleep(60)
            now = time.time()
            # 点歌 / 下载会话由各自的看门狗负责超时提示与收尾，这里跳过
            expired = [
                sid for sid, us in self.waiting_users.items()
                if us.get("expire", 0) < now
                and not us.get("busy")
                and sid not in self._session_timers
                and sid not in self.download_timers
            ]
            for sid in expired:
                self._stop_song_timer(sid)
                self._stop_download_timer(sid)
                self.waiting_users.pop(sid, None)
                self.download_pending_recall.pop(sid, None)
            try:
                for filename in os.listdir(self.temp_dir):
                    filepath = os.path.join(self.temp_dir, filename)
                    if os.path.isfile(filepath):
                        if now - os.path.getmtime(filepath) > 3600:
                            os.remove(filepath)
            except Exception as e:
                logger.warning(f"Netease Music plugin: cleanup error: {e!s}")

    def _start_song_timer(self, session_id: str):
        old = self._session_timers.get(session_id)
        if old and not old.done():
            old.cancel()
        self._session_timers[session_id] = asyncio.create_task(self._song_timeout_watchdog(session_id))

    def _stop_song_timer(self, session_id: str):
        task = self._session_timers.pop(session_id, None)
        if task and not task.done():
            task.cancel()

    async def _song_timeout_watchdog(self, session_id: str):
        """点歌会话看门狗：1分50秒无操作时单独发提示并结束会话。

        结束动作（含撤回列表）赶在 QQ 2 分钟撤回窗口内完成，
        保证列表等中间消息一定能撤掉。"""
        try:
            while True:
                us = self.waiting_users.get(session_id)
                if not us or us.get("stage") not in POINT_SONG_STAGES:
                    return
                remain = us.get("expire", 0) - time.time()
                if remain <= 0:
                    break
                await asyncio.sleep(min(remain, 5))

            us = self.waiting_users.get(session_id)
            if not us or us.get("stage") not in POINT_SONG_STAGES:
                return
            if time.time() < us.get("expire", 0):
                return  # 刚刚续期了，交给新的看门狗
            self.waiting_users.pop(session_id, None)
            self._session_timers.pop(session_id, None)
            event = us.get("_event")
            if event:
                # 此刻距离列表发出约 1 分 50 秒，仍在 QQ 撤回窗口内
                list_msg_ids = list(us.get("list_msg_ids") or [])
                if not list_msg_ids and us.get("msg_id"):
                    list_msg_ids = [us.get("msg_id")]
                await self._recall_ids(event, list_msg_ids)
                # 撤回期间如果主人已经开了新会话，就不要动新会话的待撤回消息
                if self.waiting_users.get(session_id) is not None:
                    return
                await self._recall_all_pending(event)
                if self.waiting_users.get(session_id) is not None:
                    return
                try:
                    await event.send(MessageChain([Plain(
                        "1 分 50 秒到啦…本喵没有等到数字喵…\n"
                        "消息收好爪，先收工下班啦~\n"
                        "想听歌随时再 /点歌 哦！ (=;ω;=)"
                    )]))
                except Exception as e:
                    logger.warning(f"Netease Music plugin: timeout end msg failed: {e!s}")
        except asyncio.CancelledError:
            pass
        finally:
            if self._session_timers.get(session_id) is asyncio.current_task():
                self._session_timers.pop(session_id, None)

    def _start_download_timer(self, session_id: str):
        old = self.download_timers.get(session_id)
        if old and not old.done():
            old.cancel()
        self.download_timers[session_id] = asyncio.create_task(self._download_timeout_watchdog(session_id))

    def _stop_download_timer(self, session_id: str):
        task = self.download_timers.pop(session_id, None)
        if task and not task.done():
            task.cancel()

    async def _download_timeout_watchdog(self, session_id: str):
        """下载会话看门狗：100 秒没有新的点歌/下载操作时收尾。

        收尾时撤回中间提示，但保留歌曲文件与当前歌曲列表。
        """
        try:
            while True:
                us = self.waiting_users.get(session_id)
                if not us or us.get("stage") not in DOWNLOAD_STAGES:
                    self.download_timers.pop(session_id, None)
                    return
                if us.get("busy"):
                    await asyncio.sleep(5)
                    continue
                remain = us.get("expire", 0) - time.time()
                if remain > 0:
                    await asyncio.sleep(min(remain, 5))
                    continue

                self.waiting_users.pop(session_id, None)
                self.download_timers.pop(session_id, None)
                event = us.get("_event")
                if not event:
                    self.download_pending_recall.pop(session_id, None)
                    return

                keep_ids: List[Optional[int]] = [us.get("msg_id")]
                keep_ids.extend(us.get("list_msg_ids") or [])
                # 先清理中间消息，保留歌曲文件和当前列表
                await self._recall_pending_except(event, keep_ids)
                # 撤回期间如果主人已经开了新会话，就不要动新会话的待撤回消息
                if self.waiting_users.get(session_id) is not None:
                    return
                # 列表保留给主人查看，不再参与后续撤回
                self.download_pending_recall.pop(session_id, None)
                try:
                    await event.send(MessageChain([Plain(
                        "喵~ 已经 100 秒没有新的下载请求啦，本喵先收工喵。\n"
                        "中间消息已经收好，歌曲文件和列表给主人留着~\n"
                        "还想下载的话，重新 /163下载 就好啦！ (=^･ω･^=)"
                    )]))
                except Exception as e:
                    logger.warning(f"Netease Music plugin: download timeout end msg failed: {e!s}")
                return
        except asyncio.CancelledError:
            pass
        finally:
            if self.download_timers.get(session_id) is asyncio.current_task():
                self.download_timers.pop(session_id, None)

    @staticmethod
    def _extract_playlist_id(text: str) -> Optional[int]:
        if not text:
            return None
        s = text.strip()
        if s.isdigit():
            return int(s)
        m = re.search(r"[?&]id=(\d+)", s)
        if m:
            return int(m.group(1))
        m = re.search(r"playlist[/=](\d+)", s)
        if m:
            return int(m.group(1))
        m = re.search(r"(?<!\d)(\d{6,})(?!\d)", s)
        if m:
            return int(m.group(1))
        return None

    @staticmethod
    def _song_artists(song: Dict[str, Any]) -> str:
        if song.get("artists"):
            return " / ".join(a.get("name", "") for a in song["artists"])
        return " / ".join(ar.get("name", "") for ar in (song.get("ar") or []))

    @staticmethod
    def _song_album(song: Dict[str, Any]) -> str:
        return (song.get("album") or song.get("al") or {}).get("name") or "未知专辑"

    @staticmethod
    def _song_duration(song: Dict[str, Any]) -> int:
        return song.get("duration") or song.get("dt") or 0

    @filter.command("163help")
    async def help_handler(self, event: AstrMessageEvent):
        self._disable_llm(event)
        help_text = (
            "喵~ 主人欢迎回来！这里是您的专属点歌猫娘 (=^･ω･^=)\n"
            "================================\n"
            "\n"
            "【点歌】\n"
            "  /点歌 <关键词>\n"
            "  /163music <关键词>\n"
            "  例：/点歌 Lemon\n"
            "  说个歌名或歌手名，本喵竖起耳朵就去叼喵~\n"
            "\n"
            "【歌单下载】\n"
            "  /163下载              翻出主人的歌单小仓库\n"
            "  /163下载 <歌单ID>     叼回指定歌单\n"
            "  /163下载 <分享链接>   直接贴链接也可以喵\n"
            "  /163下载 <关键词>     搜歌手或歌曲下载\n"
            "  例：/163下载 2935523602\n"
            "  例：/163下载 周杰伦\n"
            "\n"
            "【翻页与选择】\n"
            "  点歌：回复数字选一首（全局编号）\n"
            "  下载：回复数字可多个，如 1 2 3\n"
            "  p2 或 /p2      翻到第 2 页\n"
            "  回复 *         本喵就收爪休息喵\n"
            "  点歌 1 分 50 秒、下载 100 秒没新操作，\n"
            "  本喵会自动清理中间消息并收爪下班喵\n"
            "  下载的歌曲文件和当前列表会保留喵\n"
            "\n"
            "【登录】\n"
            "  /login     扫「喵之契约」二维码（主人专属）\n"
            "  /logins    看看项圈上的登录状态\n"
            "\n"
            "【小贴士】\n"
            "  · 点歌会同时发文件和语音；\n"
            "    文件发不出去时，再补发网易云播放链接喵~\n"
            "  · 本喵默认叼「超清母带」级别的鱼，\n"
            "    项圈等级不够时自动换成当前能叼动\n"
            "    的最肥美的一条喵（VIP自动降级）\n"
            "  · 下载的歌会把封面、歌名、歌手、歌词\n"
            "    像藏小鱼干一样仔细藏进文件里喵\n"
            "  · 搜索数量、每页数量、最大抓取数\n"
            "    都能在插件配置里调整喵\n"
            "\n"
            "================================\n"
            "随时叫本喵哦，主人~ (ฅ'ω'ฅ)"
        )
        await event.send(MessageChain([Plain(help_text)]))

    @filter.command("163music")
    async def cmd_handler(self, event: AstrMessageEvent, keyword: str = ""):
        await self._handle_music_request(event, keyword)

    @filter.command("点歌")
    async def diange_handler(self, event: AstrMessageEvent, keyword: str = ""):
        await self._handle_music_request(event, keyword)

    async def _handle_music_request(self, event: AstrMessageEvent, keyword: str = ""):
        self._disable_llm(event)
        logger.info(f"Netease Music plugin: music request, keyword={keyword!r}")
        if not keyword.strip():
            await event.send(MessageChain([Plain(
                "喵呜~ 主人想让本喵放什么歌呀？\n"
                "本喵歪着头等主人开口呢，比如：\n"
                "  /点歌 Lemon\n"
                "  /163music Lemon\n"
                "只要说出口，立刻叼回来给主人喵！ (=ΦωΦ=)"
            )]))
            return
        await self.ask_search_type(event, keyword.strip())

    @filter.command("163下载")
    async def download_handler(self, event: AstrMessageEvent, playlist_id: str = ""):
        self._disable_llm(event)
        session_id = event.get_session_id()
        self._stop_song_timer(session_id)
        self._stop_download_timer(session_id)
        self.waiting_users.pop(session_id, None)
        self.download_pending_recall.pop(session_id, None)

        raw = self._get_plain_text(event)
        raw = re.sub(r"^[/!！]?163下载\s*", "", raw).strip()
        if not raw:
            raw = (playlist_id or "").strip()

        logger.info(f"Netease Music plugin: /163下载, raw={raw!r}")

        if not self.config.get("cookie"):
            await event.send(MessageChain([Plain(
                "呜喵…歌单仓库的门没登录打不开喵…\n"
                "主人用 /login 扫一下「喵之契约」，\n"
                "给本喵系上项圈好不好嘛？ (=;ω;=)"
            )]))
            return

        if not raw:
            await self.show_user_playlists(event)
            return

        pid = self._extract_playlist_id(raw)
        if pid is not None:
            await self.show_playlist_tracks(event, pid, "")
            return

        await self._start_download_search(event, raw)

    @filter.command("login")
    async def login_handler(self, event: AstrMessageEvent):
        self._disable_llm(event)
        if not event.is_admin():
            await event.send(MessageChain([Plain(
                "喵嗷！这是主人的专属铃铛喵！\n"
                "别人乱碰的话，本喵会炸毛的哦 (｀ω´)"
            )]))
            return
        if self._qr_task and not self._qr_task.done():
            self._qr_task.cancel()
        try:
            key = await self.api.qr_key()
            qrimg = await self.api.qr_create(key) if key else None
        except Exception as e:
            logger.error(f"Netease Music plugin: QR error: {e!s}")
            await event.send(MessageChain([Plain("呜喵…本喵没能画好二维码…再试一次好不好嘛？ (=;ω;=)")]))
            return
        if not key or not qrimg:
            await event.send(MessageChain([Plain("呜喵…二维码生不出来…本喵的网络胡须好像打结了喵…")]))
            return
        b64 = qrimg.split(",", 1)[-1]
        await event.send(MessageChain([
            Plain(
                "喵~ 请主人打开网易云音乐APP，\n"
                "扫一扫下面这张「喵之契约」！\n"
                "扫完在手机上点「确认登录」，\n"
                "本喵会竖着尾巴等好消息的喵~ (=^･ω･^=)"
            ),
            Image.fromBase64(b64),
        ]))
        self._qr_phase = "scanning"
        self._qr_task = asyncio.create_task(self._poll_qr(key, event))

    @filter.command("logins")
    async def login_status_handler(self, event: AstrMessageEvent):
        self._disable_llm(event)
        if not event.is_admin():
            await event.send(MessageChain([Plain("喵？项圈状态只有主人能看喵~ 不给看不给看，哼喵 (｀ω´)")]))
            return
        cookie = self.config.get("cookie", "")
        if not cookie:
            await event.send(MessageChain([Plain("本喵还没戴上项圈（未登录）喵…\n主人用 /login 扫一下「喵之契约」，给喵系上铃铛吧~")]))
            return
        try:
            data = await self.api.login_status(cookie)
            profile = data.get("data", {}).get("profile")
            if profile:
                try:
                    vip_type = int(profile.get("vipType", 0) or 0)
                except (TypeError, ValueError):
                    vip_type = 0
                try:
                    red_vip = int(profile.get("redVipLevel", 0) or 0)
                except (TypeError, ValueError):
                    red_vip = 0
                if vip_type == 110:
                    vip = "黑胶SVIP（项圈都镶钻了喵！）"
                elif red_vip >= 7:
                    vip = "黑胶SVIP（项圈都镶钻了喵！）"
                elif vip_type == 10:
                    vip = "黑胶VIP（连毛色都亮了喵）"
                elif vip_type == 11:
                    vip = "试用黑胶VIP（借来的项圈喵）"
                elif vip_type > 0:
                    vip = f"VIP（vipType={vip_type}）"
                else:
                    vip = "普通账号"
                await event.send(MessageChain([Plain(
                    "咕噜咕噜~ 项圈好好的，本喵登录着呢喵！\n"
                    f"  昵称：{profile.get('nickname', '未知')}\n"
                    f"  类型：{vip}"
                )]))
            else:
                await event.send(MessageChain([Plain(
                    "呜喵…项圈松掉了，登录失效了喵…\n"
                    "主人再帮本喵扫一次 /login 好不好嘛？ (=;ω;=)"
                )]))
        except Exception as e:
            logger.error(f"Netease Music plugin: login status error: {e!s}")
            await event.send(MessageChain([Plain("呜喵…本喵检查项圈的时候打了个喷嚏…稍后再看看好不好嘛？")]))

    async def _poll_qr(self, key: str, event: AstrMessageEvent):
        last_code = None
        try:
            for _ in range(60):
                await asyncio.sleep(3)
                try:
                    res = await self.api.qr_check(key)
                except Exception as e:
                    logger.warning(f"Netease Music plugin: qr_check error: {e!s}")
                    continue
                code = res.get("code")
                if code == 801:
                    pass
                elif code == 802:
                    if last_code != 802:
                        self._qr_phase = "confirmed"
                        await event.send(MessageChain([Plain(
                            "喵！本喵看到主人扫到了！\n"
                            "快在手机上点「确认登录」，\n"
                            "尾巴都已经翘起来等着啦！ (=^･ω･^=)"
                        )]))
                elif code == 803:
                    cookie = res.get("cookie", "")
                    if cookie:
                        self.config["cookie"] = cookie
                        self._save_cookie(cookie)
                    self._vip_cache = None
                    self._qr_phase = "success"
                    await event.send(MessageChain([Plain(
                        "契约达成，登录成功啦喵！\n"
                        "以后想听什么歌，摇摇喵的铃铛就好啦~ (=￣ω￣=)"
                    )]))
                    return
                elif code == 800:
                    self._qr_phase = "expired"
                    await event.send(MessageChain([Plain(
                        "呜喵…「喵之契约」过期了喵…\n"
                        "重新用 /login 画一张吧~"
                    )]))
                    return
                last_code = code
            self._qr_phase = "expired"
            await event.send(MessageChain([Plain("喵呜…等太久了，本喵蜷成一团睡着了…契约也过期了喵…重新 /login 一下吧~")]))
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Netease Music plugin: QR polling error: {e!s}")

    @filter.regex(r"^[/／]?\s*[pP]\s*\d+$", priority=1000)
    async def page_handler(self, event: AstrMessageEvent):
        text = self._get_plain_text(event)
        logger.info(f"Netease Music plugin: page_handler raw={text!r}")

        m = re.match(r"^[/／]?\s*[pP]\s*(\d+)$", text)
        if not m:
            return

        session_id = event.get_session_id()
        if session_id not in self.waiting_users:
            logger.info(f"Netease Music plugin: page_handler no session {session_id}")
            return
        us = self.waiting_users[session_id]
        if time.time() > us.get("expire", 0):
            logger.info("Netease Music plugin: page_handler session expired")
            return
        self._disable_llm(event)

        stage = us.get("stage")
        page = int(m.group(1))
        if page < 1:
            page = 1

        # 点歌与下载的歌曲列表共用同一套翻页逻辑
        if stage in ("song", "track"):
            us["page"] = page
            if stage == "song":
                us["expire"] = time.time() + POINT_SONG_IDLE_SECONDS
                self._start_song_timer(session_id)
            else:
                us["expire"] = time.time() + DOWNLOAD_IDLE_SECONDS
                us["busy"] = False
            self._track_user_message(event)
            await self._render_list_page(event, session_id)
            if stage == "track":
                self._start_download_timer(session_id)
            return

    @filter.regex(r"^[\d\s,，]+$", priority=999)
    async def number_selection_handler(self, event: AstrMessageEvent):
        session_id = event.get_session_id()
        if session_id not in self.waiting_users:
            return
        us = self.waiting_users[session_id]
        if time.time() > us.get("expire", 0):
            return
        self._disable_llm(event)

        nums = [int(n) for n in re.findall(r"\d+", self._get_plain_text(event))]
        if not nums:
            return

        stage = us.get("stage", "song")

        if stage == "type":
            num = nums[0]
            if num not in (1, 2):
                return
            event.stop_event()
            self._stop_song_timer(session_id)
            self.waiting_users.pop(session_id, None)
            if num == 1:
                await self.search_songs_and_show(event, us["keyword"])
            else:
                await self.search_artists_and_show(event, us["keyword"])
            # 新列表出来之后再撤回旧提示与用户指令，避免出现空窗
            await self._recall(event, us.get("msg_id"))
            await self._recall_user_message(event)
            return

        if stage == "artist":
            num = nums[0]
            artists = us.get("artists") or []
            if not (1 <= num <= len(artists)):
                return
            event.stop_event()
            self._stop_song_timer(session_id)
            self.waiting_users.pop(session_id, None)
            await self.show_artist_top_songs(event, artists[num - 1])
            await self._recall(event, us.get("msg_id"))
            await self._recall_user_message(event)
            return

        if stage == "song":
            songs = us.get("items") or []
            # 点歌一次只能选一首：只取用户输入的第一个数字
            num = nums[0]
            if not (1 <= num <= len(songs)):
                return
            event.stop_event()
            self._stop_song_timer(session_id)
            self.waiting_users.pop(session_id, None)
            await self._play_selected_songs(event, songs, [num], us)
            return

        if stage == "dl_type":
            num = nums[0]
            if num not in (1, 2):
                return
            event.stop_event()
            keyword = us.get("keyword", "")
            self._stop_download_timer(session_id)
            self.waiting_users.pop(session_id, None)
            if num == 1:
                await self._show_download_artists(event, keyword)
            else:
                await self._show_download_songs(event, keyword)
            await self._recall(event, us.get("msg_id"))
            await self._recall_user_message(event)
            return

        if stage == "dl_artist":
            num = nums[0]
            artists = us.get("artists") or []
            if not (1 <= num <= len(artists)):
                return
            event.stop_event()
            self._stop_download_timer(session_id)
            self.waiting_users.pop(session_id, None)
            await self._start_download_artist_tracks(event, artists[num - 1])
            await self._recall(event, us.get("msg_id"))
            await self._recall_user_message(event)
            return

        if stage == "playlist":
            num = nums[0]
            playlists = us.get("playlists") or []
            if not (1 <= num <= len(playlists)):
                return
            event.stop_event()
            self._stop_download_timer(session_id)
            self.waiting_users.pop(session_id, None)
            self._track_user_message(event)
            pl = playlists[num - 1]
            await self.show_playlist_tracks(event, pl.get("id"), pl.get("name", ""))
            return

        if stage == "track":
            tracks = us.get("items") or []
            event.stop_event()
            self._track_user_message(event)
            # 批量下载期间暂停倒计时，避免下载到一半被超时收尾
            us["busy"] = True
            us["expire"] = time.time() + DOWNLOAD_IDLE_SECONDS
            self._stop_download_timer(session_id)
            try:
                for num in nums:
                    if 1 <= num <= len(tracks):
                        await self.download_selected_track(event, tracks, num)
            finally:
                if self.waiting_users.get(session_id) is us:
                    us["busy"] = False
                    us["expire"] = time.time() + DOWNLOAD_IDLE_SECONDS
                    self._start_download_timer(session_id)
            return

    @filter.regex(r"^\*$", priority=999)
    async def cancel_selection_handler(self, event: AstrMessageEvent):
        session_id = event.get_session_id()
        if session_id not in self.waiting_users:
            return
        us = self.waiting_users[session_id]
        if time.time() > us.get("expire", 0):
            return
        self._disable_llm(event)
        self._stop_song_timer(session_id)
        self._stop_download_timer(session_id)
        self.waiting_users.pop(session_id, None)

        list_msg_ids = list(us.get("list_msg_ids") or [])
        if not list_msg_ids and us.get("msg_id"):
            list_msg_ids = [us.get("msg_id")]

        await self._recall_ids(event, list_msg_ids)
        await self._recall_all_pending(event)
        await self._recall_user_message(event)
        await event.send(MessageChain([Plain(
            "好啦~ 本喵收爪下班，蜷回猫窝啦~\n"
            "想听歌随时摇铃铛叫喵哦！ (ฅ'ω'ฅ)"
        )]))

    @filter.event_message_type(filter.EventMessageType.ALL, priority=-1)
    async def llm_guard_handler(self, event: AstrMessageEvent):
        """点歌/下载会话进行中时，拦截所有未匹配消息，避免误触 LLM。"""
        session_id = event.get_session_id()
        us = self.waiting_users.get(session_id)
        if not us:
            return
        if time.time() > us.get("expire", 0):
            return
        self._disable_llm(event)

    async def ask_search_type(self, event: AstrMessageEvent, keyword: str):
        session_id = event.get_session_id()
        # 先停掉可能残留的旧看门狗，避免它误伤新会话
        self._stop_song_timer(session_id)
        text = (
            f"喵~ 主人想听「{keyword}」呀！本喵的耳朵唰地竖起来了！\n"
            "要怎么去找呢？回复数字告诉喵：\n"
            "  1. 搜歌曲（去唱片堆里刨一刨）\n"
            "  2. 搜歌手（竖起胡须闻一闻）"
        )
        msg_id = await self._send_track(event, MessageChain([Plain(text)]))
        self.waiting_users[session_id] = {
            "stage": "type",
            "keyword": keyword,
            "expire": time.time() + POINT_SONG_IDLE_SECONDS,
            "msg_id": msg_id,
            "_event": event,
        }
        self._start_song_timer(session_id)

    async def search_songs_and_show(self, event: AstrMessageEvent, keyword: str):
        try:
            songs = await self.api.search_songs(keyword, self._search_limit())
        except Exception as e:
            logger.error(f"Netease Music plugin: search songs error: {e!s}")
            await event.send(MessageChain([Plain(
                "呜喵…本喵和音乐服务器的线断掉了…\n"
                "主人看看 API 服务是不是睡着了？\n"
                "拍醒它再让喵跑一趟嘛~ (=;ω;=)"
            )]))
            return
        if not songs:
            await event.send(MessageChain([Plain(
                f"呜…本喵把唱片堆都刨遍了，\n"
                f"也没找到「{keyword}」这首歌喵…\n"
                "是不是名字打错了呀？再给喵一次机会嘛~ (=;ω;=)"
            )]))
            return
        header = f"「{keyword}」的歌，本喵都叼回来啦"
        await self._show_song_list(event, songs, header, page=1)

    async def search_artists_and_show(self, event: AstrMessageEvent, keyword: str):
        try:
            artists = await self.api.search_artists(keyword, self._search_limit())
        except Exception as e:
            logger.error(f"Netease Music plugin: search artists error: {e!s}")
            await event.send(MessageChain([Plain(
                "呜喵…本喵和音乐服务器的线断掉了…\n"
                "主人看看 API 服务是不是睡着了？\n"
                "拍醒它再让喵跑一趟嘛~ (=;ω;=)"
            )]))
            return
        if not artists:
            await event.send(MessageChain([Plain(
                f"呜…本喵闻遍了整个院子，\n"
                f"也没找到「{keyword}」这位歌手喵…\n"
                "换个名字再试试好不好？ (=;ω;=)"
            )]))
            return
        lines = [f"闻着味儿找到 {len(artists)} 位歌手喵！主人想听谁家的歌？"]
        for i, ar in enumerate(artists, 1):
            name = ar.get("name", "")
            alias = " / ".join(ar.get("alias") or [])
            music_size = ar.get("musicSize", 0)
            alias_str = f"（{alias}）" if alias else ""
            size_str = f"（{music_size} 首）" if music_size else ""
            lines.append(f"  {i}. {name}{alias_str} {size_str}".rstrip())
        msg_id = await self._send_track(event, MessageChain([Plain("\n".join(lines))]))
        session_id = event.get_session_id()
        self.waiting_users[session_id] = {
            "stage": "artist",
            "artists": artists,
            "expire": time.time() + POINT_SONG_IDLE_SECONDS,
            "msg_id": msg_id,
            "_event": event,
        }
        self._start_song_timer(session_id)

    async def show_artist_top_songs(self, event: AstrMessageEvent, artist: Dict[str, Any]):
        artist_id = artist.get("id")
        artist_name = artist.get("name", "")
        try:
            songs = await self.api.get_artist_top_songs(artist_id, self._search_limit())
        except Exception as e:
            logger.error(f"Netease Music plugin: top songs error: {e!s}")
            await event.send(MessageChain([Plain("呜喵…本喵去叼热门歌曲的路上滑了一跤…再试一次好不好嘛~")]))
            return
        if not songs:
            await event.send(MessageChain([Plain(f"呜喵…「{artist_name}」还没把歌唱给本喵听过喵… (=;ω;=)")]))
            return
        header = f"「{artist_name}」的拿手好歌，都排好队啦"
        await self._show_song_list(event, songs, header, page=1)

    async def _show_song_list(self, event: AstrMessageEvent, items: List[Dict[str, Any]],
                              header: str, page: int = 1):
        session_id = event.get_session_id()
        prev_us = self.waiting_users.get(session_id) or {}
        list_msg_ids = list(prev_us.get("list_msg_ids") or [])
        self.waiting_users[session_id] = {
            "stage": "song",
            "items": items,
            "title": header,
            "page": page,
            "expire": time.time() + POINT_SONG_IDLE_SECONDS,
            "msg_id": None,
            "list_msg_ids": list_msg_ids,
            "_event": event,
        }
        self._start_song_timer(session_id)
        await self._render_list_page(event, session_id)

    async def _render_list_page(self, event: AstrMessageEvent, session_id: str):
        """点歌 / 下载共用的歌曲列表渲染：全局编号 + 翻页提示。"""
        us = self.waiting_users.get(session_id)
        if not us:
            return
        items = us.get("items") or []
        title = (us.get("title") or "歌曲列表").strip()
        page = us.get("page", 1)
        page_size = self._page_size()

        total = len(items)
        total_pages = max(1, (total + page_size - 1) // page_size)
        if page < 1:
            page = 1
        if page > total_pages:
            page = total_pages

        start = (page - 1) * page_size
        end = min(start + page_size, total)

        if us.get("stage") == "track":
            select_hint = "爪爪回复数字下载（可多个，如 1 2 3）"
        else:
            select_hint = "爪爪回复数字点歌（一次一首）"
        lines = [
            f"{title} · 共 {total} 首",
            f"第 {page}/{total_pages} 页 · 第 {start + 1}-{end} 首",
            select_hint,
            "p2 或 /p2 翻页 · 回复 * 让本喵收工",
        ]
        for i, song in enumerate(items[start:end], start + 1):
            name = song.get("name", "")
            artists = self._song_artists(song)
            dur_ms = self._song_duration(song)
            dur_str = f"{dur_ms // 60000}:{(dur_ms % 60000) // 1000:02d}" if dur_ms else ""
            line = f"  {i}. {name}"
            if artists:
                line += f" - {artists}"
            if dur_str:
                line += f" [{dur_str}]"
            lines.append(line)

        chain = MessageChain([Plain("\n".join(lines))])
        if us.get("stage") == "track":
            msg_id = await self._send_download_msg(event, chain)
        else:
            msg_id = await self._send_track(event, chain)
            if msg_id:
                us.setdefault("list_msg_ids", []).append(int(msg_id))
        us["page"] = page
        us["msg_id"] = msg_id

    async def _start_download_search(self, event: AstrMessageEvent, keyword: str):
        self._track_user_message(event)
        text = (
            f"喵~ 主人想把「{keyword}」抱回家呀！\n"
            "回复数字告诉本喵去哪里找：\n"
            "  1. 搜歌手（竖起胡须闻一闻）\n"
            "  2. 搜歌曲（去唱片堆里刨一刨）"
        )
        msg_id = await self._send_download_msg(event, MessageChain([Plain(text)]))
        session_id = event.get_session_id()
        self.waiting_users[session_id] = {
            "stage": "dl_type",
            "keyword": keyword,
            "expire": time.time() + DOWNLOAD_IDLE_SECONDS,
            "busy": False,
            "msg_id": msg_id,
            "_event": event,
        }
        self._start_download_timer(session_id)

    async def _show_download_artists(self, event: AstrMessageEvent, keyword: str):
        try:
            artists = await self.api.search_artists(keyword, self._search_limit())
        except Exception as e:
            logger.error(f"Netease Music plugin: search artists error: {e!s}")
            await event.send(MessageChain([Plain("呜喵…本喵闻歌手的时候出了点岔子…稍后再让喵闻一次嘛~")]))
            return
        if not artists:
            await event.send(MessageChain([Plain(f"呜喵…本喵没闻到「{keyword}」这位歌手的味道喵… (=;ω;=)")]))
            return

        self._track_user_message(event)
        lines = [f"闻着味儿找到 {len(artists)} 位歌手喵！主人要把谁家的歌抱回家？"]
        for i, ar in enumerate(artists, 1):
            name = ar.get("name", "")
            alias = " / ".join(ar.get("alias") or [])
            music_size = ar.get("musicSize", 0)
            alias_str = f"（{alias}）" if alias else ""
            size_str = f"（{music_size} 首）" if music_size else ""
            lines.append(f"  {i}. {name}{alias_str} {size_str}".rstrip())
        lines.append("\n回复 * 本喵就收爪停下喵~")

        msg_id = await self._send_download_msg(event, MessageChain([Plain("\n".join(lines))]))
        session_id = event.get_session_id()
        self.waiting_users[session_id] = {
            "stage": "dl_artist",
            "artists": artists,
            "expire": time.time() + DOWNLOAD_IDLE_SECONDS,
            "busy": False,
            "msg_id": msg_id,
            "_event": event,
        }
        self._start_download_timer(session_id)

    async def _show_download_songs(self, event: AstrMessageEvent, keyword: str):
        try:
            songs = await self.api.search_songs(keyword, self._search_limit())
        except Exception as e:
            logger.error(f"Netease Music plugin: search songs error: {e!s}")
            await event.send(MessageChain([Plain("呜喵…本喵刨歌曲堆的时候出了点岔子…稍后再试嘛~")]))
            return
        if not songs:
            await event.send(MessageChain([Plain(f"呜喵…本喵没刨到「{keyword}」相关的歌喵… (=;ω;=)")]))
            return
        await self._enter_download_tracks(event, songs, f"「{keyword}」的歌，本喵刨出来了")

    async def _start_download_artist_tracks(self, event: AstrMessageEvent, artist: Dict[str, Any]):
        artist_id = artist.get("id")
        artist_name = artist.get("name", "")
        try:
            songs = await self.api.get_artist_top_songs(artist_id, self._search_limit())
        except Exception as e:
            logger.error(f"Netease Music plugin: top songs error: {e!s}")
            await event.send(MessageChain([Plain("呜喵…本喵去叼热门歌曲的路上滑了一跤…再试一次好不好嘛~")]))
            return
        if not songs:
            await event.send(MessageChain([Plain(f"呜喵…「{artist_name}」还没把歌唱给本喵听过喵… (=;ω;=)")]))
            return
        await self._enter_download_tracks(event, songs, f"「{artist_name}」的拿手好歌")

    async def _enter_download_tracks(self, event: AstrMessageEvent, tracks: List[Dict[str, Any]], title: str):
        session_id = event.get_session_id()
        self._track_user_message(event)
        self.waiting_users[session_id] = {
            "stage": "track",
            "items": tracks,
            "title": title,
            "page": 1,
            "expire": time.time() + DOWNLOAD_IDLE_SECONDS,
            "busy": False,
            "msg_id": None,
            "_event": event,
        }
        await self._render_list_page(event, session_id)
        self._start_download_timer(session_id)

    async def show_user_playlists(self, event: AstrMessageEvent):
        cookie = self.config.get("cookie", "")
        try:
            status = await self.api.login_status(cookie)
            profile = (status.get("data") or {}).get("profile")
            if not profile or not profile.get("userId"):
                await event.send(MessageChain([Plain(
                    "呜喵…项圈松了，登录失效了喵…\n"
                    "主人再帮本喵扫一次 /login 好不好嘛？ (=;ω;=)"
                )]))
                return
            uid = profile["userId"]
            playlists = await self.api.get_user_playlists(uid, cookie, limit=50)
        except Exception as e:
            logger.error(f"Netease Music plugin: user playlists error: {e!s}")
            await event.send(MessageChain([Plain("呜喵…本喵翻歌单仓库时被门夹了爪爪…稍后再试试嘛~")]))
            return

        if not playlists:
            await event.send(MessageChain([Plain(
                "喵呜~ 主人的歌单仓库空空如也，\n"
                "连一颗猫粮都翻不出来喵…\n"
                "主人先去攒点歌，本喵帮您看着家~ (=^･ω･^=)"
            )]))
            return

        self._track_user_message(event)
        lines = [f"喵呜~ 主人的歌单小仓库里有 {len(playlists)} 个歌单喵！回复数字让本喵去叼~"]
        for i, pl in enumerate(playlists, 1):
            name = pl.get("name", "未命名歌单")
            count = pl.get("trackCount", 0)
            lines.append(f"  {i}. {name}（{count} 首）")
        lines.append("\n回复 * 本喵就收爪停下喵~")

        msg_id = await self._send_download_msg(event, MessageChain([Plain("\n".join(lines))]))
        session_id = event.get_session_id()
        self.waiting_users[session_id] = {
            "stage": "playlist",
            "playlists": playlists,
            "expire": time.time() + DOWNLOAD_IDLE_SECONDS,
            "busy": False,
            "msg_id": msg_id,
            "_event": event,
        }
        self._start_download_timer(session_id)

    async def show_playlist_tracks(self, event: AstrMessageEvent, playlist_id: int, playlist_name: str = ""):
        cookie = self.config.get("cookie", "")
        try:
            if not playlist_name:
                detail = await self.api.get_playlist_detail(playlist_id, cookie)
                playlist_name = (detail or {}).get("name") or f"歌单 {playlist_id}"
            tracks = await self.api.get_playlist_tracks(playlist_id, self._max_fetch(), cookie)
        except Exception as e:
            logger.error(f"Netease Music plugin: playlist tracks error: {e!s}")
            await event.send(MessageChain([Plain("呜喵…本喵翻歌单的时候爪爪打滑了…稍后再试试嘛~")]))
            return

        if not tracks:
            await event.send(MessageChain([Plain(f"呜喵…歌单「{playlist_name}」里空空的，没有能叼的歌喵… (=;ω;=)")]))
            return

        self._track_user_message(event)
        session_id = event.get_session_id()
        self.waiting_users[session_id] = {
            "stage": "track",
            "items": tracks,
            "title": f"歌单「{playlist_name}」",
            "page": 1,
            "expire": time.time() + DOWNLOAD_IDLE_SECONDS,
            "busy": False,
            "msg_id": None,
            "_event": event,
        }
        await self._render_list_page(event, session_id)
        self._start_download_timer(session_id)

    @staticmethod
    def _metadata_args(title: str, artists: str, album: str, lyrics: str = "") -> List[str]:
        args = [
            "-metadata", f"title={title}",
            "-metadata", f"artist={artists}",
            "-metadata", f"album={album}",
        ]
        if lyrics:
            # ffmpeg 会按容器写入：MP3 -> USLT / FLAC -> LYRICS / M4A -> ©lyr
            args.extend(["-metadata", f"lyrics={lyrics}"])
        return args

    async def _fetch_lyrics_for_embedding(self, song_id: int) -> str:
        """获取歌词（原文 + 翻译），失败时静默返回空字符串，不影响下载。"""
        try:
            data = await self.api.get_lyrics(song_id, self.config.get("cookie", ""))
        except Exception as e:
            logger.warning(f"Netease Music plugin: get lyrics failed: {e!s}")
            return ""
        if not isinstance(data, dict):
            return ""

        original = ""
        for key in ("lrc", "klyric", "yrc"):
            node = data.get(key)
            if isinstance(node, dict):
                lyric = node.get("lyric")
                if isinstance(lyric, str) and lyric.strip():
                    original = lyric
                    break

        translation = ""
        node = data.get("tlyric")
        if isinstance(node, dict):
            lyric = node.get("lyric")
            if isinstance(lyric, str):
                translation = lyric

        if not original:
            original = translation
            translation = ""
        if not original:
            return ""

        merged = _merge_lrc_lyrics(original, translation)
        merged = merged.replace("\x00", "").strip()
        # 防止极端超长歌词撑爆 Windows 命令行参数上限
        if len(merged) > 30000:
            merged = merged[:30000]
        return merged

    async def _embed_tags(self, audio_path: str, title: str, artists: str, album: str,
                          cover_path: Optional[str], ext: str, lyrics: str = "") -> bool:
        if not self._ffmpeg_path:
            return False

        # ffmpeg 7.x 的 -metadata lyrics 在 MP3 中会写成 TXXX(USLT)，
        # 播放器不识别；MP3 改为 ffmpeg 处理完后直接写标准 USLT 帧。
        is_mp3 = ext.lower() == ".mp3"
        ffmpeg_lyrics = "" if (lyrics and is_mp3) else lyrics
        metadata_args = self._metadata_args(title, artists, album, ffmpeg_lyrics)

        if cover_path and os.path.isfile(cover_path):
            tagged_with_cover = audio_path + ".tagged_cover" + ext
            cmd_cover = [
                self._ffmpeg_path,
                "-i", audio_path,
                "-i", cover_path,
                "-map", "0:a",
                "-map", "1:v",
                "-c:a", "copy",
                "-c:v", "mjpeg",
            ]
            if ext.lower() == ".mp3":
                cmd_cover += ["-id3v2_version", "3"]
            cmd_cover += [
                *metadata_args,
                "-metadata:s:v", "title=Album cover",
                "-metadata:s:v", "comment=Cover (front)",
                "-y", tagged_with_cover,
            ]
            try:
                process = await asyncio.create_subprocess_exec(
                    *cmd_cover, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                )
                _, stderr = await process.communicate()
                if process.returncode == 0 and os.path.exists(tagged_with_cover) and os.path.getsize(tagged_with_cover) > 0:
                    os.replace(tagged_with_cover, audio_path)
                    if lyrics and is_mp3 and not _write_mp3_uslt(audio_path, lyrics):
                        return False
                    return True
                else:
                    err = stderr.decode(errors="ignore") if stderr else "unknown"
                    logger.warning(f"Netease Music plugin: cover embed failed: {err[:200]}")
                    if os.path.exists(tagged_with_cover):
                        try:
                            os.remove(tagged_with_cover)
                        except Exception:
                            pass
            except Exception as e:
                logger.warning(f"Netease Music plugin: cover embed error: {e!s}")

        tagged_text = audio_path + ".tagged_text" + ext
        cmd_text = [
            self._ffmpeg_path,
            "-i", audio_path,
            *metadata_args,
            "-codec", "copy",
        ]
        if ext.lower() == ".mp3":
            cmd_text += ["-id3v2_version", "3"]
        cmd_text += ["-y", tagged_text]
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd_text, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            _, stderr = await process.communicate()
            if process.returncode == 0 and os.path.exists(tagged_text) and os.path.getsize(tagged_text) > 0:
                os.replace(tagged_text, audio_path)
                if lyrics and is_mp3 and not _write_mp3_uslt(audio_path, lyrics):
                    return False
                return True
            else:
                err = stderr.decode(errors="ignore") if stderr else "unknown"
                logger.warning(f"Netease Music plugin: text tag failed: {err[:200]}")
        except Exception as e:
            logger.warning(f"Netease Music plugin: text tag error: {e!s}")
        finally:
            try:
                if os.path.exists(tagged_text):
                    os.remove(tagged_text)
            except Exception:
                pass

        return False

    async def download_selected_track(self, event: AstrMessageEvent, tracks: List[Dict[str, Any]], num: int):
        if not (1 <= num <= len(tracks)):
            await self._send_download_msg(event, MessageChain([Plain(f"喵？列表里没有第 {num} 首喵…主人再对对编号嘛？ (｀ω´)")]))
            return

        selected = tracks[num - 1]
        song_id = selected.get("id")
        if not song_id:
            await self._send_download_msg(event, MessageChain([Plain("呜喵…这首歌没有ID，本喵叼不动它喵… (=;ω;=)")]))
            return

        temp_original: Optional[str] = None
        temp_cover: Optional[str] = None
        try:
            song_details = await self.api.get_song_details(song_id)
            if not song_details:
                raise ValueError("无法获取歌曲详细信息。")

            try:
                audio_info = await self.api.get_audio_url(song_id, "standard", self.config.get("cookie", ""))
            except Exception as e:
                logger.warning(f"Netease Music plugin: get_audio_url error: {e!s}")
                audio_info = None
            if not audio_info or not audio_info.get("url"):
                await self._send_download_msg(event, MessageChain([Plain(
                    f"喵~ 第 {num} 首暂时叼不到链接…本喵先跳过它啦~"
                )]))
                return

            audio_url = audio_info["url"]
            title = song_details.get("name", "")
            artists = " / ".join(a["name"] for a in song_details.get("ar", []))
            album = song_details.get("al", {}).get("name", "未知专辑")
            cover_url = song_details.get("al", {}).get("picUrl", "")
            ext = os.path.splitext(urllib.parse.urlparse(audio_url).path)[1] or ".mp3"
            temp_original = os.path.join(self.temp_dir, f"{song_id}_{int(time.time())}{ext}")

            await self._send_download_msg(event, MessageChain([Plain(
                f"正在下载第 {num} 首：{title}\n请稍等一下喵~"
            )]))

            download_ok = await self.api.download_audio(audio_url, temp_original)
            if not download_ok:
                await self._send_download_msg(event, MessageChain([Plain(
                    f"呜喵…第 {num} 首搬运失败了…是不是网线被别的猫咬了呀？ (=;ω;=)"
                )]))
                return

            if cover_url and self._ffmpeg_path:
                try:
                    cover_bytes = await self.api.download_image(cover_url)
                    if cover_bytes:
                        temp_cover = os.path.join(self.temp_dir, f"{song_id}_{int(time.time())}_cover.jpg")
                        with open(temp_cover, "wb") as f:
                            f.write(cover_bytes)
                except Exception as e:
                    logger.warning(f"Netease Music plugin: cover download failed: {e!s}")
                    temp_cover = None

            lyrics = await self._fetch_lyrics_for_embedding(song_id)
            tagged = await self._embed_tags(temp_original, title, artists, album, temp_cover, ext, lyrics)

            file_name = f"{title} - {artists}{ext}"
            try:
                await event.send(MessageChain([File(file=temp_original, name=file_name)]))
                lyrics_note = "（歌词已内嵌）" if (lyrics and tagged) else ""
                await self._send_download_msg(event, MessageChain([Plain(
                    f"第 {num} 首完成：{title} - {artists}\n"
                    f"文件已发送喵~{lyrics_note}"
                )]))
            except Exception as e:
                logger.warning(f"Netease Music plugin: send file failed: {e!s}")
                await self._send_download_msg(event, MessageChain([Plain(
                    f"呜喵…第 {num} 首文件递不出去…\n"
                    "可能被群门口的门卫拦住了喵… (=;ω;=)"
                )]))

        except Exception as e:
            logger.error(f"Netease Music plugin: download failed: {e!s}", exc_info=True)
            await self._send_download_msg(event, MessageChain([Plain(
                f"呜…搬运第 {num} 首的时候，\n"
                f"本喵摔了个猫跟头喵…\n"
                f"({type(e).__name__}: {e!s})"
            )]))
        finally:
            for fpath in (temp_original, temp_cover):
                if fpath and os.path.exists(fpath):
                    try:
                        os.remove(fpath)
                    except Exception as e:
                        logger.warning(f"Netease Music plugin: remove failed: {e!s}")

    async def _play_selected_songs(self, event: AstrMessageEvent, songs: List[Dict[str, Any]],
                                   nums: List[int], us: Dict[str, Any]):
        """点歌选定的歌曲逐首下载发送；所有文件与语音都发完之后，才统一撤回中间消息。"""
        user_msg_id = self._get_event_message_id(event)

        recall_ids: List[Optional[int]] = []
        for mid in (us.get("list_msg_ids") or []):
            recall_ids.append(mid)
        if not recall_ids and us.get("msg_id"):
            recall_ids.append(us.get("msg_id"))

        results = []
        carry_msg_id: Optional[int] = None
        if len(nums) > 1:
            carry_msg_id = await self._send_track(event, MessageChain([Plain(
                f"喵呜~ 主人一口气点了 {len(nums)} 首！\n"
                "本喵一趟只能叼一只，现在开始来回跑腿，\n"
                "主人摸摸头等一等嘛~ (=^･ω･^=)"
            )]))
        for num in nums:
            result = await self._play_one_song(event, songs, num)
            results.append(result)
            recall_ids.extend(result["recall"])

        # 所有文件（含语音）发送完毕，此时才统一撤回，不提前撤回
        pending = self.download_pending_recall.pop(event.get_session_id(), [])
        recall_ids.extend(pending)
        recall_ids.append(user_msg_id)
        if carry_msg_id:
            recall_ids.append(carry_msg_id)
        await self._recall_ids(event, recall_ids)

        sent = [r for r in results if r["ok"]]
        if sent:
            lines = ["全部叼完啦！这次的战利品喵："]
            for idx, r in enumerate(results, 1):
                note = f" {r['note']}" if r["note"] else ""
                lines.append(f"  {idx}. {r['title']}{note}")
            lines.append("本喵开心得原地咕噜咕噜~ (=^･ω･^=)")
            await event.send(MessageChain([Plain("\n".join(lines))]))
        else:
            await event.send(MessageChain([Plain(
                "呜喵…这次一件战利品都没叼回来…\n"
                "可能被群限制了，稍后再放本喵出门一趟好不好？ (=;ω;=)"
            )]))

    async def _play_one_song(self, event: AstrMessageEvent, songs: List[Dict[str, Any]],
                             num: int) -> Dict[str, Any]:
        """下载并发送一首歌的文件与语音。

        返回 {"title", "recall", "note", "ok"}：recall 为需要稍后撤回的消息ID，
        note 为附加说明（为空表示文件和语音都发送成功）。
        """
        recall_mids: List[Optional[int]] = []
        selected = songs[num - 1]
        song_id = selected.get("id")
        title_guess = selected.get("name") or f"第 {num} 首"

        def _result(title: str, note: str, ok: bool) -> Dict[str, Any]:
            return {"title": title, "recall": recall_mids, "note": note, "ok": ok}

        if not song_id:
            return _result(title_guess, "（找不到ID，本喵叼不动喵）", False)

        temp_original: Optional[str] = None
        temp_wav: Optional[str] = None
        try:
            song_details = await self.api.get_song_details(song_id)
            if not song_details:
                raise ValueError("无法获取歌曲详细信息。")

            quality_chain = await self._quality_chain()

            audio_info = None
            for q in quality_chain:
                try:
                    info = await self.api.get_audio_url(song_id, q, self.config.get("cookie", ""))
                except Exception as e:
                    logger.warning(f"Netease Music plugin: get_audio_url({q}) error: {e!s}")
                    continue
                if info and info.get("url"):
                    audio_info = info
                    break
            if not audio_info:
                return _result(title_guess, "（拿不到链接，空手而归喵）", False)
            audio_url = audio_info["url"]

            title = song_details.get("name", "")
            artists = " / ".join(a["name"] for a in song_details.get("ar", []))
            album = song_details.get("al", {}).get("name", "未知专辑")
            cover_url = song_details.get("al", {}).get("picUrl", "")
            duration_ms = song_details.get("dt", 0)
            dur_str = f"{duration_ms // 60000}:{(duration_ms % 60000) // 1000:02d}"
            display = f"{title} - {artists}".rstrip(" -")

            ext = os.path.splitext(urllib.parse.urlparse(audio_url).path)[1] or ".mp3"
            temp_original = os.path.join(self.temp_dir, f"{song_id}_{int(time.time())}{ext}")
            dl_msg_id = await self._send_track(event, MessageChain([Plain(
                f"喵~ 本喵去叼第 {num} 首「{title}」了，\n尾巴摇啊摇，稍等一下下喵~"
            )]))
            recall_mids.append(dl_msg_id)

            download_ok = await self.api.download_audio(audio_url, temp_original)
            if not download_ok:
                return _result(display, "（搬运失败了喵 T_T）", False)

            voice_note = ""
            if not self._ffmpeg_path:
                voice_note = "（没找到 ffmpeg，只叼得动文件喵）"
            else:
                temp_wav = os.path.join(self.temp_dir, f"{song_id}_{int(time.time())}.wav")
                convert_cmd = [
                    self._ffmpeg_path,
                    "-i", temp_original,
                    "-vn",
                    "-ac", "1",
                    "-ar", "24000",
                    "-acodec", "pcm_s16le",
                    "-sample_fmt", "s16",
                    "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
                    "-y", temp_wav
                ]
                process = await asyncio.create_subprocess_exec(
                    *convert_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                )
                stdout, stderr = await process.communicate()
                if process.returncode != 0:
                    logger.error("Netease Music plugin: ffmpeg convert failed")
                    temp_wav = None
                    voice_note = "（语音没磨好，只有文件喵）"

            info_msg_id = await self._send_song_with_cover(
                event, num, title, artists, album, dur_str, cover_url,
                audio_info.get("level", self.config.get("quality", ""))
            )
            recall_mids.append(info_msg_id)

            file_sent = False
            file_name = f"{title} - {artists}{ext}"
            for attempt in (1, 2):
                try:
                    await event.send(MessageChain([File(file=temp_original, name=file_name)]))
                    file_sent = True
                    break
                except Exception as e:
                    logger.warning(f"Netease Music plugin: send file failed (attempt {attempt}): {e!s}")
                    if attempt == 1:
                        # 第一次失败：文件可能损坏，重新下载并重新内嵌标签后再试一次
                        try:
                            if temp_original and os.path.exists(temp_original):
                                os.remove(temp_original)
                        except Exception:
                            pass
                        temp_original = os.path.join(self.temp_dir, f"{song_id}_{int(time.time())}_retry{ext}")
                        redownload_ok = await self.api.download_audio(audio_url, temp_original)
                        if redownload_ok:
                            await self._embed_tags(temp_original, title, artists, album, None, ext)
                        else:
                            logger.warning("Netease Music plugin: re-download failed, give up retry")
                            break

            voice_sent = False
            if temp_wav and os.path.exists(temp_wav) and os.path.getsize(temp_wav) > 0:
                for attempt in (1, 2):
                    try:
                        record_comp = Record.fromFileSystem(temp_wav)
                        await event.send(MessageChain([record_comp]))
                        voice_sent = True
                        break
                    except Exception as e:
                        logger.error(f"Failed to send Record (attempt {attempt}): {e!s}")
            elif not file_sent and self._ffmpeg_path and temp_original and os.path.exists(temp_original):
                # 文件发不出去、语音又没转出来：补转一次语音，保证主人至少能听到
                try:
                    temp_wav = os.path.join(self.temp_dir, f"{song_id}_{int(time.time())}_voice.wav")
                    fallback_cmd = [
                        self._ffmpeg_path,
                        "-i", temp_original,
                        "-vn",
                        "-ac", "1",
                        "-ar", "24000",
                        "-acodec", "pcm_s16le",
                        "-sample_fmt", "s16",
                        "-y", temp_wav
                    ]
                    process = await asyncio.create_subprocess_exec(
                        *fallback_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                    )
                    await process.communicate()
                    if process.returncode == 0 and os.path.exists(temp_wav) and os.path.getsize(temp_wav) > 0:
                        try:
                            record_comp = Record.fromFileSystem(temp_wav)
                            await event.send(MessageChain([record_comp]))
                            voice_sent = True
                        except Exception as e:
                            logger.error(f"Failed to send fallback Record: {e!s}")
                except Exception as e:
                    logger.error(f"Failed to convert fallback voice: {e!s}")

            link_sent = False
            if not file_sent:
                voice_status = "语音已经发出" if voice_sent else "语音也没能发出"
                try:
                    await event.send(MessageChain([Plain(
                        f"「{display}」的文件没能递出去喵…\n"
                        f"{voice_status}，主人可以先点播放链接听：\n"
                        f"https://music.163.com/song?id={song_id}"
                    )]))
                    link_sent = True
                except Exception as e:
                    logger.warning(f"Netease Music plugin: send fallback link failed: {e!s}")

            if file_sent and voice_sent:
                note = ""
            elif file_sent:
                note = voice_note or "（语音没递出去，只有文件喵）"
            elif voice_sent and link_sent:
                note = "（文件没发出去，已发语音和播放链接喵）"
            elif link_sent:
                note = "（文件/语音没发出去，已发播放链接喵）"
            elif voice_sent:
                note = "（文件没发出去，只发了语音喵）"
            else:
                note = "（文件、语音和链接都没递出去喵 T_T）"
            return _result(display, note, file_sent or voice_sent or link_sent)

        except Exception as e:
            logger.error(f"Netease Music plugin: play failed: {e!s}", exc_info=True)
            await event.send(MessageChain([Plain(
                f"呜…叼第 {num} 首的时候，本喵摔了一跤喵…\n"
                f"({type(e).__name__}: {e!s})"
            )]))
            return _result(title_guess, "（出错了，已跳过喵 T_T）", False)
        finally:
            for fpath in (temp_original, temp_wav):
                if fpath and os.path.exists(fpath):
                    try:
                        os.remove(fpath)
                    except Exception as e:
                        logger.warning(f"Netease Music plugin: remove failed: {e!s}")

    async def _send_song_with_cover(self, event: AstrMessageEvent, num: int, title: str, artists: str,
                                     album: str, dur_str: str, cover_url: str, level: str = "") -> Optional[int]:
        level_display = QUALITY_NAMES.get(level, level or "")
        if level == "jymaster":
            level_display += "（顶配）"
        elif level == "lossless":
            level_display += "（无损）"
        detail_lines = [f"第 {num} 首 · {title}"]
        byline = "｜".join(part for part in (artists, album) if part)
        if byline:
            detail_lines.append(byline)
        meta_line = f"时长 {dur_str}"
        if level_display:
            meta_line += f"｜音质 {level_display}"
        detail_lines.append(meta_line)
        info_components = [Plain("\n".join(detail_lines))]
        image_data = await self.api.download_image(cover_url)
        if image_data:
            info_components.append(Image.fromBase64(base64.b64encode(image_data).decode()))
        return await self._send_track(event, MessageChain(info_components))
