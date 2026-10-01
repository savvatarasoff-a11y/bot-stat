"""Скачивание видео из TikTok по ссылке (через yt-dlp, без водяного знака, если TikTok его отдаёт)."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import tempfile
from dataclasses import dataclass

log = logging.getLogger(__name__)

LINK = re.compile(r"https?://(?:[a-z]+\.)?tiktok\.com/[^\s<>\"']+", re.IGNORECASE)
MAX_SIZE = 50 * 1024 * 1024      # больше Bot API отправить не даёт
MAX_PARALLEL = 3                 # одновременных скачиваний на весь бот


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
        "format": "best[ext=mp4][filesize<50M]/best[ext=mp4]/best",
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
    if os.path.getsize(path) > MAX_SIZE:
        raise DownloadError("Видео больше 50 МБ, Telegram не даёт боту отправить такой файл.")
    return Video(path=path, title=(info or {}).get("title") or "",
                 width=info.get("width"), height=info.get("height"),
                 duration=int(info["duration"]) if info.get("duration") else None)


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
