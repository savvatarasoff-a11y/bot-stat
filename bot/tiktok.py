"""Скачивание видео из TikTok по ссылке (через yt-dlp, без водяного знака, если TikTok его отдаёт)."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass

log = logging.getLogger(__name__)

LINK = re.compile(r"https?://(?:[a-z]+\.)?tiktok\.com/[^\s<>\"']+", re.IGNORECASE)
MAX_SIZE = 50 * 1024 * 1024      # больше Bot API отправить не даёт
MAX_PARALLEL = 3                 # одновременных скачиваний на весь бот
# H.264 Telegram на телефонах играет плавно; H.265 (его TikTok отдаёт часто) — с рывками
FORMAT = ("best[vcodec^=h264][filesize<?50M]/best[vcodec^=avc][filesize<?50M]"
          "/best[ext=mp4][filesize<?50M]/best[ext=mp4]/best")
FFMPEG_TIMEOUT = 300


class DownloadError(Exception):
    """Текст ошибки показывается пользователю."""


@dataclass
class Video:
    path: str
    title: str
    width: int | None = None
    height: int | None = None
    duration: int | None = None

    def cleanup(self) -> None:
        shutil.rmtree(os.path.dirname(self.path), ignore_errors=True)


def find_link(text: str | None) -> str | None:
    m = LINK.search(text or "")
    return m.group(0).rstrip(".,;!?)") if m else None


def _download(url: str, folder: str) -> Video:
    import yt_dlp

    opts = {
        "outtmpl": os.path.join(folder, "video.%(ext)s"),
        "format": FORMAT,
        "max_filesize": MAX_SIZE,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except yt_dlp.utils.DownloadError as e:
        log.info("TikTok: не скачалось %s: %s", url, e)
        raise DownloadError("Не удалось скачать видео. Проверьте ссылку: видео должно быть открытым.") from e
    files = [os.path.join(folder, f) for f in os.listdir(folder) if not f.endswith(".part")]
    if not files:
        raise DownloadError("Видео больше 50 МБ, Telegram не даёт боту отправить такой файл.")
    path = max(files, key=os.path.getsize)
    info = info or {}
    video = Video(path=path, title=info.get("title") or "", width=info.get("width"), height=info.get("height"),
                  duration=int(info["duration"]) if info.get("duration") else None)
    video = prepare(video)
    if os.path.getsize(video.path) > MAX_SIZE:
        raise DownloadError("Видео больше 50 МБ, Telegram не даёт боту отправить такой файл.")
    return video


def probe(path: str) -> dict | None:
    """Кодек, размеры и длительность видео через ffprobe; None — не получилось."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name,width,height,pix_fmt:format=duration", "-of", "json", path],
            capture_output=True, text=True, timeout=60, check=True).stdout
        data = json.loads(out)
        stream = data["streams"][0]
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, IndexError):
        return None
    duration = (data.get("format") or {}).get("duration")
    return {"codec": stream.get("codec_name"), "pix_fmt": stream.get("pix_fmt"),
            "width": stream.get("width"), "height": stream.get("height"),
            "duration": round(float(duration)) if duration else None}


def prepare(video: Video) -> Video:
    """Готовит файл к плавному воспроизведению в Telegram.

    Не H.264 (или не yuv420p) — перекодируем в H.264; иначе только переносим индекс (moov)
    в начало файла, чтобы видео начинало играть сразу. Без ffmpeg отдаём как есть."""
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        log.warning("TikTok: ffmpeg не установлен, видео отправляется без обработки")
        return video
    meta = probe(video.path)
    if meta is None:
        return video
    out = os.path.join(os.path.dirname(video.path), "ready.mp4")
    if meta["codec"] == "h264" and meta["pix_fmt"] in ("yuv420p", "yuvj420p", None):
        codecs = ["-c", "copy"]
    else:
        codecs = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
                  "-c:a", "aac", "-b:a", "128k"]
    try:
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", video.path, "-map", "0:v:0", "-map", "0:a:0?",
                        *codecs, "-movflags", "+faststart", out],
                       capture_output=True, timeout=FFMPEG_TIMEOUT, check=True)
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("TikTok: ffmpeg не справился с %s: %s", video.path, e)
        return video
    os.remove(video.path)
    ready = probe(out) or meta
    return Video(path=out, title=video.title, width=ready["width"] or video.width,
                 height=ready["height"] or video.height, duration=ready["duration"] or video.duration)


class TikTokDownloader:
    def __init__(self, parallel: int = MAX_PARALLEL):
        self.sem = asyncio.Semaphore(parallel)

    async def download(self, url: str) -> Video:
        """Скачивает во временную папку; после отправки вызвать video.cleanup()."""
        async with self.sem:
            folder = tempfile.mkdtemp(prefix="tiktok_")
            try:
                return await asyncio.to_thread(_download, url, folder)
            except BaseException:
                shutil.rmtree(folder, ignore_errors=True)
                raise
