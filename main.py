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
from typing import Dict, Any, Optional, List

try:
    import imageio_ffmpeg
except ImportError:
    imageio_ffmpeg = None

from astrbot.api import star, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core.message.message_event_result import MessageChain
from astrbot.api.message_components import Plain, Image, Record, File


# --- Helpers ---
def _find_ffmpeg() -> Optional[str]:
    """优先使用 imageio-ffmpeg 内置的 ffmpeg，否则回退到系统 PATH"""
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


# --- API Wrapper ---
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

    async def get_song_details(self, song_id: int) -> Optional[Dict[str, Any]]:
        url = f"{self.base_url}/song/detail?ids={str(song_id)}"
        async with self.session.get(url) as r:
            r.raise_for_status()
            data = await r.json()
            return data["songs"][0] if data.get("songs") else None

    async def get_audio_url(self, song_id: int, quality: str, cookie: str = "") -> Optional[Dict[str, Any]]:
        # 浏览器复制的 cookie 值已带 %XX 编码，不能二次编码；仅清理分号后的空格
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


# --- Main Plugin Class ---
class Main(star.Star):
    def __init__(self, context, config: Optional[Dict[str, Any]] = None):
        super().__init__(context)
        self.config = config or {}
        self.config.setdefault("api_url", "http://127.0.0.1:3000")
        self.config.setdefault("quality", "lossless")
        self.config.setdefault("search_limit", 5)
        self.config.setdefault("cookie", "")

        self.waiting_users: Dict[str, Dict[str, Any]] = {}
        self.song_cache: Dict[str, List[Dict[str, Any]]] = {}

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
            # 第 137 行：确保字符串完整，无换行
            logger.warning("Netease Music plugin: ffmpeg not found. Install imageio-ffmpeg or system ffmpeg for voice.")

        self.cleanup_task: Optional[asyncio.Task] = None
        self.temp_dir = os.path.join(tempfile.gettempdir(), "netease_music_plugin")
        os.makedirs(self.temp_dir, exist_ok=True)

        # 扫码登录状态
        self._qr_task: Optional[asyncio.Task] = None
        self._qr_phase: str = "idle"  # idle / scanning / confirmed / success / expired
        self._cookie_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "netease_login.json")
        self._load_saved_cookie()

    def _load_saved_cookie(self):
        """从本地文件恢复扫码登录得到的 cookie（配置里已填则不覆盖）"""
        if self.config.get("cookie"):
            return
        try:
            if os.path.isfile(self._cookie_path):
                with open(self._cookie_path, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                if saved.get("cookie"):
                    self.config["cookie"] = saved["cookie"]
                    logger.info("Netease Music plugin: Restored login cookie from local file.")
        except Exception as e:
            logger.warning(f"Netease Music plugin: Failed to load saved cookie: {e!s}")

    def _save_cookie(self, cookie: str):
        try:
            with open(self._cookie_path, "w", encoding="utf-8") as f:
                json.dump({"cookie": cookie}, f)
        except Exception as e:
            logger.warning(f"Netease Music plugin: Failed to save cookie: {e!s}")

    # --- Lifecycle Hooks ---
    async def initialize(self):
        self.cleanup_task = asyncio.create_task(self._periodic_cleanup())
        logger.info("Netease Music plugin: Background cleanup task started.")

    async def terminate(self):
        if self.cleanup_task and not self.cleanup_task.done():
            self.cleanup_task.cancel()
            try:
                await self.cleanup_task
            except asyncio.CancelledError:
                logger.info("Netease Music plugin: Background cleanup task cancelled.")
        if self.http_session and not self.http_session.closed:
            await self.http_session.close()
            logger.info("Netease Music plugin: HTTP session closed.")

    async def _periodic_cleanup(self):
        while True:
            await asyncio.sleep(60)
            now = time.time()
            expired_sessions = []
            for session_id, user_session in self.waiting_users.items():
                if user_session["expire"] < now:
                    expired_sessions.append((session_id, user_session["key"]))
            if expired_sessions:
                logger.info(f"Netease Music plugin: Cleaning up {len(expired_sessions)} expired session(s).")
                for session_id, cache_key in expired_sessions:
                    if session_id in self.waiting_users:
                        del self.waiting_users[session_id]
                    if cache_key in self.song_cache:
                        del self.song_cache[cache_key]
            try:
                for filename in os.listdir(self.temp_dir):
                    filepath = os.path.join(self.temp_dir, filename)
                    if os.path.isfile(filepath):
                        mtime = os.path.getmtime(filepath)
                        if now - mtime > 3600:
                            os.remove(filepath)
                            logger.debug(f"Netease Music plugin: Removed old temp file {filepath}")
            except Exception as e:
                logger.warning(f"Netease Music plugin: Error cleaning temp files: {e!s}")

    # --- Event Handlers ---
    @filter.command("163music", alias={"music", "听歌"})
    async def cmd_handler(self, event: AstrMessageEvent, keyword: str = ""):
        if not keyword.strip():
            await event.send(MessageChain([Plain("主人，请告诉我您想听什么歌喵~ 例如：/163music Lemon")]))
            return
        await self.search_and_show(event, keyword.strip())

    @filter.regex(r"(?i)^(来.?一首|播放|听.?听|点歌|唱.?一首|来.?首)\s*([^\s].+?)(的歌|的歌曲|的音乐|歌|曲)?$")
    async def natural_language_handler(self, event: AstrMessageEvent):
        match = re.search(r"(?i)^(来.?一首|播放|听.?听|点歌|唱.?一首|来.?首)\s*([^\s].+?)(的歌|的歌曲|的音乐|歌|曲)?$", event.message_str)
        if match:
            keyword = match.group(2).strip()
            if keyword:
                event.stop_event()
                await self.search_and_show(event, keyword)

    @filter.command("login")
    async def login_handler(self, event: AstrMessageEvent):
        if not event.is_admin():
            await event.send(MessageChain([Plain("喵？登录是主人的专属指令哦~")]))
            return
        if self._qr_task and not self._qr_task.done():
            self._qr_task.cancel()
        try:
            key = await self.api.qr_key()
            qrimg = await self.api.qr_create(key) if key else None
        except Exception as e:
            logger.error(f"Netease Music plugin: Failed to get QR code. Error: {e!s}")
            await event.send(MessageChain([Plain("呜喵...获取登录二维码失败，请检查API服务喵...")]))
            return
        if not key or not qrimg:
            await event.send(MessageChain([Plain("呜喵...二维码生成失败了喵...")]))
            return
        b64 = qrimg.split(",", 1)[-1]
        await event.send(MessageChain([
            Plain("请主人用网易云音乐APP扫描下面的二维码登录喵~\n有效期内有效，扫码后记得在手机上确认哦"),
            Image.fromBase64(b64),
        ]))
        self._qr_phase = "scanning"
        self._qr_task = asyncio.create_task(self._poll_qr(key, event))

    @filter.command("logins")
    async def login_status_handler(self, event: AstrMessageEvent):
        if not event.is_admin():
            await event.send(MessageChain([Plain("喵？这个指令只有主人能用哦~")]))
            return
        cookie = self.config.get("cookie", "")
        if not cookie:
            await event.send(MessageChain([Plain("还没有登录喵~ 主人可以用 /login 扫码登录")]))
            return
        try:
            data = await self.api.login_status(cookie)
            profile = data.get("data", {}).get("profile")
            if profile:
                vip = "黑胶VIP" if profile.get("vipType", 0) > 0 else "普通用户"
                await event.send(MessageChain([Plain(
                    f"✅ 已登录喵~\n昵称：{profile.get('nickname', '未知')}\n账号类型：{vip}"
                )]))
            else:
                await event.send(MessageChain([Plain("⚠️ 登录状态已失效，请主人重新用 /login 扫码登录喵")]))
        except Exception as e:
            logger.error(f"Netease Music plugin: Failed to check login status. Error: {e!s}")
            await event.send(MessageChain([Plain("呜喵...查询登录状态失败，请检查API服务喵...")]))

    async def _poll_qr(self, key: str, event: AstrMessageEvent):
        last_code = None
        try:
            for _ in range(60):  # 最多轮询约3分钟
                await asyncio.sleep(3)
                try:
                    res = await self.api.qr_check(key)
                except Exception as e:
                    logger.warning(f"Netease Music plugin: qr_check error: {e!s}")
                    continue
                code = res.get("code")
                if code == 801:  # 等待扫码
                    pass
                elif code == 802:  # 已扫码，待确认
                    if last_code != 802:
                        self._qr_phase = "confirmed"
                        await event.send(MessageChain([Plain("检测到主人扫码啦~ 请在手机上点击确认登录喵！")]))
                elif code == 803:  # 登录成功
                    cookie = res.get("cookie", "")
                    if cookie:
                        self.config["cookie"] = cookie
                        self._save_cookie(cookie)
                    self._qr_phase = "success"
                    await event.send(MessageChain([Plain("🎉 登录成功喵！已保存登录状态，可以点歌啦~")]))
                    return
                elif code == 800:  # 二维码过期
                    self._qr_phase = "expired"
                    await event.send(MessageChain([Plain("二维码已过期喵... 请重新使用 /login 获取")]))
                    return
                last_code = code
            self._qr_phase = "expired"
            await event.send(MessageChain([Plain("等待扫码超时了喵... 请重新使用 /login")]))
        except asyncio.CancelledError:
            logger.info("Netease Music plugin: QR polling cancelled.")
        except Exception as e:
            logger.error(f"Netease Music plugin: QR polling error: {e!s}")

    @filter.regex(r"^\d+$", priority=999)
    async def number_selection_handler(self, event: AstrMessageEvent):
        session_id = event.get_session_id()
        if session_id not in self.waiting_users:
            return
        user_session = self.waiting_users[session_id]
        if time.time() > user_session["expire"]:
            return
        try:
            num = int(event.message_str.strip())
        except ValueError:
            return
        limit = self.config.get("search_limit", 5)
        if not (1 <= num <= limit):
            return
        event.stop_event()
        del self.waiting_users[session_id]
        await self.play_selected_song(event, user_session["key"], num)

    # --- Core Logic ---
    async def search_and_show(self, event: AstrMessageEvent, keyword: str):
        try:
            songs = await self.api.search_songs(keyword, self.config["search_limit"])
        except Exception as e:
            logger.error(f"Netease Music plugin: API search failed. Error: {e!s}")
            await event.send(MessageChain([Plain("呜喵...和音乐服务器的连接断掉了...主人，请检查一下API服务是否正常运行喵？")]))
            return
        if not songs:
            await event.send(MessageChain([Plain(f"对不起主人...我...我没能找到「{keyword}」这首歌喵... T_T")]))
            return
        cache_key = f"{event.get_session_id()}_{int(time.time())}"
        self.song_cache[cache_key] = songs
        response_lines = [f"主人，我为您找到了 {len(songs)} 首歌曲喵！请回复数字告诉我您想听哪一首~"]
        for i, song in enumerate(songs, 1):
            artists = " / ".join(a["name"] for a in song.get("artists", []))
            album = song.get("album", {}).get("name", "未知专辑")
            duration_ms = song.get("duration", 0)
            dur_str = f"{duration_ms//60000}:{(duration_ms%60000)//1000:02d}"
            response_lines.append(f"{i}. {song['name']} - {artists} 《{album}》 [{dur_str}]")
        await event.send(MessageChain([Plain("\n".join(response_lines))]))
        self.waiting_users[event.get_session_id()] = {"key": cache_key, "expire": time.time() + 60}

    async def play_selected_song(self, event: AstrMessageEvent, cache_key: str, num: int):
        if cache_key not in self.song_cache:
            await event.send(MessageChain([Plain("喵呜~ 主人选择得太久了，搜索结果已经凉掉了哦，请重新点歌吧~")]))
            return

        songs = self.song_cache[cache_key]
        if not (1 <= num <= len(songs)):
            await event.send(MessageChain([Plain("主人，您输入的数字不对哦，请选择列表里的歌曲编号喵~")]))
            return

        selected_song = songs[num - 1]
        song_id = selected_song["id"]
        temp_original = None
        temp_wav = None

        try:
            song_details = await self.api.get_song_details(song_id)
            if not song_details:
                raise ValueError("无法获取歌曲详细信息。")

            quality_chain = [self.config["quality"]]
            for fallback in ("lossless", "exhigh", "standard"):
                if fallback not in quality_chain:
                    quality_chain.append(fallback)

            audio_info = None
            for q in quality_chain:
                try:
                    info = await self.api.get_audio_url(song_id, q, self.config.get("cookie", ""))
                except Exception as e:
                    logger.warning(f"Netease Music plugin: get_audio_url(level={q}) error: {e!s}")
                    continue
                if info and info.get("url"):
                    audio_info = info
                    break
            if not audio_info:
                await event.send(MessageChain([Plain("喵~ 暂时无法获取这首歌曲的播放链接，请稍后再试喵...")]))
                return
            audio_url = audio_info["url"]
            src_br = audio_info.get("br", 0)
            src_type = audio_info.get("type", "unknown")
            logger.info(
                f"Netease Music plugin: source audio type={src_type}, "
                f"bitrate={src_br/1000:.0f}kbps, level={self.config['quality']}"
            )
            if src_br and src_br < 320000:
                logger.warning(
                    f"Netease Music plugin: source bitrate only {src_br/1000:.0f}kbps. "
                    "Cookie/会员缺失可能导致源音质被降级，语音会变浑。"
                )

            title = song_details.get("name", "")
            artists = " / ".join(a["name"] for a in song_details.get("ar", []))
            album = song_details.get("al", {}).get("name", "未知专辑")
            cover_url = song_details.get("al", {}).get("picUrl", "")
            duration_ms = song_details.get("dt", 0)
            dur_str = f"{duration_ms//60000}:{(duration_ms%60000)//1000:02d}"

            ext = os.path.splitext(urllib.parse.urlparse(audio_url).path)[1] or ".mp3"
            temp_original = os.path.join(self.temp_dir, f"{song_id}_{int(time.time())}{ext}")
            await event.send(MessageChain([Plain("正在为主人下载歌曲喵，请稍等片刻~")]))

            download_ok = await self.api.download_audio(audio_url, temp_original)
            if not download_ok:
                await event.send(MessageChain([Plain("喵~ 歌曲下载失败了，可能是网络问题，请稍后再试喵...")]))
                return

            if not self._ffmpeg_path:
                await event.send(MessageChain([Plain("🔊 未找到 ffmpeg，无法转换为语音。将仅发送文件。")]))
            else:
                temp_wav = os.path.join(self.temp_dir, f"{song_id}_{int(time.time())}.wav")
                convert_cmd = [
                    self._ffmpeg_path,
                    "-i", temp_original,
                    "-vn",                          # 丢弃视频流
                    "-ac", "1",                     # 单声道
                    "-ar", "24000",                 # 24kHz：与 QQ 语音(SILK)宽带编码对齐，重采样损伤最小
                    "-acodec", "pcm_s16le",         # 16bit 无损 PCM
                    "-sample_fmt", "s16",
                    "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",  # 响度归一化，小音量/闷响问题明显改善
                    "-y", temp_wav
                ]
                process = await asyncio.create_subprocess_exec(
                    *convert_cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, stderr = await process.communicate()
                if process.returncode != 0:
                    error_msg = stderr.decode() if stderr else "未知错误"
                    logger.error(f"Netease Music plugin: ffmpeg conversion failed: {error_msg}")
                    await event.send(MessageChain([Plain("🔊 音频转换失败，将仅发送文件。")]))
                    temp_wav = None

            await self._send_song_with_cover(event, num, title, artists, album, dur_str, cover_url, audio_info.get("level", self.config['quality']))

            file_name = f"{title} - {artists}{ext}"
            await event.send(MessageChain([File(file=temp_original, name=file_name)]))

            if temp_wav and os.path.exists(temp_wav) and os.path.getsize(temp_wav) > 0:
                try:
                    record_comp = Record.fromFileSystem(temp_wav)
                    await event.send(MessageChain([record_comp]))
                    logger.info(f"Record sent successfully: {temp_wav}")
                except Exception as e:
                    logger.error(f"Failed to send Record: {e!s}")
            else:
                if temp_wav is None:
                    pass
                else:
                    logger.warning("Record file invalid, skipping.")

            await event.send(MessageChain([Plain("🎵 音频文件已发送，请查收喵~")]))

        except Exception as e:
            logger.error(f"Netease Music plugin: Failed to play song {song_id}. Error: {e!s}", exc_info=True)
            await event.send(MessageChain([Plain(f"呜...播放歌曲的时候失败了喵... ({type(e).__name__}: {e!s}")]))
        finally:
            for fpath in (temp_original, temp_wav):
                if fpath and os.path.exists(fpath):
                    try:
                        os.remove(fpath)
                        logger.debug(f"Netease Music plugin: Removed temp file {fpath}")
                    except Exception as e:
                        logger.warning(f"Netease Music plugin: Failed to remove {fpath}: {e!s}")
            if cache_key in self.song_cache:
                del self.song_cache[cache_key]

    async def _send_song_with_cover(self, event: AstrMessageEvent, num: int, title: str, artists: str, album: str, dur_str: str, cover_url: str, level: str = ""):
        detail_text = f"""遵命，主人！为您播放第 {num} 首歌曲~

♪ 歌名：{title}
🎤 歌手：{artists}
💿 专辑：{album}
⏳ 时长：{dur_str}
✨ 音质：{level}

请主人享用喵~
"""
        info_components = [Plain(detail_text)]
        image_data = await self.api.download_image(cover_url)
        if image_data:
            info_components.append(Image.fromBase64(base64.b64encode(image_data).decode()))
        await event.send(MessageChain(info_components))