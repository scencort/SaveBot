import asyncio
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path

import aiohttp
import yt_dlp
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode, ChatAction
from aiogram.filters import CommandStart
from aiogram.types import BufferedInputFile, Message
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не задан. Создай файл .env и положи туда BOT_TOKEN=...")

# HTTP-прокси: явный PROXY из .env побеждает, иначе берём HTTPS_PROXY/HTTP_PROXY/ALL_PROXY.
# Нужно, если Telegram заблокирован у провайдера и трафик идёт через локальный VPN-клиент
# (Hiddify, WARP, Clash и т.п., обычно слушают на 127.0.0.1:NNNN).
PROXY = (
    os.getenv("PROXY")
    or os.getenv("HTTPS_PROXY")
    or os.getenv("HTTP_PROXY")
    or os.getenv("ALL_PROXY")
)

# Источник cookies для yt-dlp (нужен когда YouTube/Instagram требуют логин).
# COOKIES_FILE предпочтительнее: путь к экспортированному cookies.txt (формат Netscape) —
# работает независимо от того, открыт ли браузер. Иначе COOKIES_BROWSER:
# chrome / edge / firefox / brave / opera / chromium / vivaldi / safari — браузер должен быть
# залогинен в сервисе, и на Windows часто требует, чтобы он был полностью закрыт.
COOKIES_FILE = os.getenv("COOKIES_FILE")
COOKIES_BROWSER = os.getenv("COOKIES_BROWSER") or os.getenv("YT_COOKIES_BROWSER")

TIKTOK_RE = re.compile(
    r"https?://(?:www\.|vm\.|vt\.|m\.)?tiktok\.com/\S+",
    re.IGNORECASE,
)
YOUTUBE_RE = re.compile(
    r"https?://(?:www\.|m\.)?youtube\.com/(?:shorts/|watch\?v=)\S+"
    r"|https?://youtu\.be/\S+",
    re.IGNORECASE,
)
INSTAGRAM_RE = re.compile(
    r"https?://(?:www\.)?instagram\.com/(?:reel|reels|p|tv|share/reel)/\S+",
    re.IGNORECASE,
)
ANY_LINK_RE = re.compile(
    "|".join(p.pattern for p in (TIKTOK_RE, YOUTUBE_RE, INSTAGRAM_RE)),
    re.IGNORECASE,
)

TIKWM_API = "https://www.tikwm.com/api/"
MAX_TELEGRAM_UPLOAD = 50 * 1024 * 1024  # 50 МБ — лимит обычной отправки

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("tiktok-bot")

bot_session = AiohttpSession(proxy=PROXY) if PROXY else AiohttpSession()
bot = Bot(
    token=BOT_TOKEN,
    session=bot_session,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher()


# -------- TikTok через tikwm (быстро, без водяного знака) --------

async def _resolve_short_url(session: aiohttp.ClientSession, url: str) -> str:
    """vm.tiktok.com / vt.tiktok.com короткие ссылки редиректят на полный URL."""
    try:
        async with session.head(url, allow_redirects=True, timeout=15, proxy=PROXY) as resp:
            return str(resp.url)
    except Exception:
        return url


async def _tikwm_info(session: aiohttp.ClientSession, url: str) -> dict:
    payload = {"url": url, "hd": 1}
    async with session.post(TIKWM_API, data=payload, timeout=30, proxy=PROXY) as resp:
        resp.raise_for_status()
        data = await resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"tikwm error: {data.get('msg', 'unknown')}")
    return data["data"]


async def _download_bytes(session: aiohttp.ClientSession, url: str) -> bytes:
    async with session.get(url, timeout=120, proxy=PROXY) as resp:
        resp.raise_for_status()
        return await resp.read()


async def download_tiktok(url: str) -> tuple[bytes, str, str, str | None]:
    """→ (video_bytes, title, author, fallback_direct_url)"""
    async with aiohttp.ClientSession(
        headers={"User-Agent": "Mozilla/5.0 (compatible; TikTokBot/1.0)"}
    ) as session:
        full_url = await _resolve_short_url(session, url)
        info = await _tikwm_info(session, full_url)
        video_url = info.get("hdplay") or info.get("play")
        if not video_url:
            raise RuntimeError("Не нашёл прямую ссылку на видео")
        video_bytes = await _download_bytes(session, video_url)

    title = (info.get("title") or "tiktok").strip()
    author = (info.get("author") or {}).get("nickname") or ""
    return video_bytes, title, author, video_url


# -------- YouTube Shorts / Instagram Reels через yt-dlp --------

def _ytdlp_download_sync(url: str, proxy: str | None) -> tuple[bytes, str, str, str | None]:
    """Скачивает видео в временный файл и возвращает байты + метаданные.
    Запускается в thread executor — yt-dlp синхронный."""
    tmpdir = tempfile.mkdtemp(prefix="ytdlp_")
    try:
        outtmpl = str(Path(tmpdir) / "%(id)s.%(ext)s")
        opts = {
            # mp4 предпочтительно — Telegram сразу показывает превью
            "format": "best[ext=mp4][filesize<50M]/best[ext=mp4]/best",
            "outtmpl": outtmpl,
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "noplaylist": True,
            "merge_output_format": "mp4",
            "restrictfilenames": True,
            # Альтернативные YouTube-клиенты обычно не требуют "Sign in to confirm you're not a bot",
            # в отличие от дефолтного web-клиента, которому YouTube часто шлёт каптчу.
            "extractor_args": {
                "youtube": {
                    "player_client": ["tv_simply", "mweb", "android", "web_safari"],
                }
            },
        }
        if proxy:
            opts["proxy"] = proxy
        if COOKIES_FILE:
            opts["cookiefile"] = COOKIES_FILE
        elif COOKIES_BROWSER:
            opts["cookiesfrombrowser"] = (COOKIES_BROWSER,)

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filepath = ydl.prepare_filename(info)
            if not os.path.exists(filepath):
                # после merge расширение могло смениться
                base, _ = os.path.splitext(filepath)
                for ext in ("mp4", "mkv", "webm", "m4a"):
                    cand = f"{base}.{ext}"
                    if os.path.exists(cand):
                        filepath = cand
                        break

        data = Path(filepath).read_bytes()
        title = (info.get("title") or info.get("id") or "video").strip()
        author = info.get("uploader") or info.get("channel") or info.get("creator") or ""
        direct_url = info.get("url") or info.get("webpage_url")
        return data, title, author, direct_url
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


async def download_via_ytdlp(url: str) -> tuple[bytes, str, str, str | None]:
    return await asyncio.to_thread(_ytdlp_download_sync, url, PROXY)


# -------- Хендлеры --------

@dp.message(CommandStart())
async def on_start(message: Message) -> None:
    await message.answer(
        "Привет! Кинь ссылку на видео — пришлю файл без водяного знака.\n\n"
        "<b>Поддерживается:</b>\n"
        "• TikTok — <code>tiktok.com</code>, <code>vm.tiktok.com</code>, <code>vt.tiktok.com</code>\n"
        "• YouTube Shorts — <code>youtube.com/shorts/...</code>, <code>youtu.be/...</code>\n"
        "• Instagram Reels — <code>instagram.com/reel/...</code> (только публичные)"
    )


@dp.message(F.text.regexp(ANY_LINK_RE))
async def on_link(message: Message) -> None:
    match = ANY_LINK_RE.search(message.text)
    if not match:
        return
    url = match.group(0)

    if TIKTOK_RE.search(url):
        source = "TikTok"
        downloader = download_tiktok
        filename = "tiktok.mp4"
    elif YOUTUBE_RE.search(url):
        source = "YouTube"
        downloader = download_via_ytdlp
        filename = "shorts.mp4"
    elif INSTAGRAM_RE.search(url):
        source = "Instagram"
        downloader = download_via_ytdlp
        filename = "reels.mp4"
    else:
        return

    status = await message.reply(f"Качаю видео из {source}…")
    await message.bot.send_chat_action(message.chat.id, ChatAction.UPLOAD_VIDEO)

    try:
        video_bytes, title, author, fallback_url = await downloader(url)

        if len(video_bytes) > MAX_TELEGRAM_UPLOAD:
            msg = (
                f"Видео слишком большое для отправки "
                f"({len(video_bytes) // 1024 // 1024} МБ, лимит 50 МБ)."
            )
            if fallback_url:
                msg += f"\nПрямая ссылка:\n{fallback_url}"
            await status.edit_text(msg)
            return

        file = BufferedInputFile(video_bytes, filename=filename)
        await message.answer_video(video=file)
        await status.delete()

    except Exception as e:
        log.exception("Ошибка обработки ссылки %s", url)
        err = str(e)
        text = f"Не получилось скачать из {source}: {e}"
        needs_login = (
            "Sign in to confirm" in err
            or "empty media response" in err
            or "login required" in err.lower()
            or "rate-limit" in err.lower()
        )
        if needs_login and not (COOKIES_FILE or COOKIES_BROWSER):
            text += (
                f"\n\n{source} требует авторизацию. Варианты:\n"
                "• <b>На сервере</b> — экспортируй <code>cookies.txt</code> "
                "из браузера (расширение \"Get cookies.txt LOCALLY\"), залей "
                "рядом с ботом и пропиши в .env "
                "<code>COOKIES_FILE=cookies.txt</code>.\n"
                "• <b>На своей машине</b> — пропиши "
                "<code>COOKIES_BROWSER=chrome</code> "
                "(или edge / firefox / brave / vivaldi); браузер должен быть "
                f"залогинен в {source}.\n"
                "После — перезапустить бота."
            )
        await status.edit_text(text)


@dp.message()
async def on_other(message: Message) -> None:
    await message.reply(
        "Пришли ссылку на TikTok, YouTube Shorts или Instagram Reels — "
        "и я верну видео файлом."
    )


async def main() -> None:
    log.info("Бот стартует… (proxy=%s)", PROXY or "off")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
