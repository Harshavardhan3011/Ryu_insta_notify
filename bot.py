import asyncio
import logging
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from time import monotonic
from urllib.parse import urlsplit, urlunsplit

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from yt_dlp import DownloadError, YoutubeDL


INSTAGRAM_URL_PATTERN = re.compile(r"https?://(?:www\.)?instagram\.com/[^\s]+", re.IGNORECASE)
ANSI_ESCAPE_PATTERN = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")
DOWNLOAD_DIR = Path("downloads")
DEFAULT_FILE_LIMIT_BYTES = 8 * 1024 * 1024
RECENT_URL_TTL_SECONDS = 120
MAX_LINKS_PER_MESSAGE = 3
DOWNLOAD_TIMEOUT_SECONDS = int(os.getenv("DOWNLOAD_TIMEOUT_SECONDS", "180"))
MAX_DOWNLOAD_ATTEMPTS = int(os.getenv("MAX_DOWNLOAD_ATTEMPTS", "3"))
YTDLP_SOCKET_TIMEOUT = int(os.getenv("YTDLP_SOCKET_TIMEOUT", "15"))
YTDLP_RETRIES = int(os.getenv("YTDLP_RETRIES", "2"))
YTDLP_USER_AGENT = os.getenv(
    "YTDLP_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
)


class ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "stage"):
            record.stage = "general"
        if not hasattr(record, "url_id"):
            record.url_id = "-"
        return True


def setup_logger() -> logging.Logger:
    logger = logging.getLogger("ryu_insta_notify")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s [%(levelname)s] [%(stage)s] [%(url_id)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    handler.addFilter(ContextFilter())
    logger.addHandler(handler)
    return logger


LOGGER = setup_logger()


class ErrorCode(str, Enum):
    INVALID_URL = "INVALID_URL"
    PRIVATE_CONTENT = "PRIVATE_CONTENT"
    DOWNLOAD_FAILED = "DOWNLOAD_FAILED"
    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    DISCORD_UPLOAD_FAILED = "DISCORD_UPLOAD_FAILED"


class FailureReason(str, Enum):
    INVALID_LINK = "invalid_link"
    PRIVATE_LOGIN_REQUIRED = "private_or_login_required"
    EXTRACTOR_FAILURE = "extractor_failure"
    NETWORK_ISSUE = "network_issue"
    UNKNOWN = "unknown"


@dataclass
class BotError:
    code: ErrorCode
    user_message: str
    technical_message: str


@dataclass
class DownloadResult:
    file_path: Path | None
    error: BotError | None
    quality_profile: str | None = None

    @property
    def ok(self) -> bool:
        return self.file_path is not None and self.error is None


def normalize_instagram_url(video_url: str) -> str:
    parsed = urlsplit(video_url.strip())
    normalized = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    return normalized.rstrip("/") + "/"


def clean_error_text(error: Exception | str) -> str:
    return ANSI_ESCAPE_PATTERN.sub("", str(error)).strip()


def log_event(level: int, stage: str, url_id: str, message: str):
    LOGGER.log(level, message, extra={"stage": stage, "url_id": url_id})


def url_id_from_url(url: str) -> str:
    path = urlsplit(url).path.strip("/")
    return path.split("/")[-1] if path else "unknown"


def classify_download_error(raw_message: str) -> ErrorCode:
    msg = raw_message.lower()
    if "not a valid url" in msg or "invalid url" in msg:
        return ErrorCode.INVALID_URL
    if "login" in msg or "private" in msg or "forbidden" in msg or "cookie" in msg:
        return ErrorCode.PRIVATE_CONTENT
    return ErrorCode.DOWNLOAD_FAILED


def classify_failure_reason(raw_message: str) -> FailureReason:
    msg = raw_message.lower()
    if "not a valid url" in msg or "invalid url" in msg:
        return FailureReason.INVALID_LINK
    if "login" in msg or "private" in msg or "forbidden" in msg or "cookie" in msg:
        return FailureReason.PRIVATE_LOGIN_REQUIRED
    if "timed out" in msg or "timeout" in msg or "connection" in msg or "network" in msg:
        return FailureReason.NETWORK_ISSUE
    if "unable to extract" in msg or "extractor" in msg:
        return FailureReason.EXTRACTOR_FAILURE
    return FailureReason.UNKNOWN


def make_error(code: ErrorCode, technical_message: str) -> BotError:
    messages = {
        ErrorCode.INVALID_URL: "Invalid Instagram URL. Send a direct reel/post link.",
        ErrorCode.PRIVATE_CONTENT: "This Instagram media appears private or requires login.",
        ErrorCode.DOWNLOAD_FAILED: "Unable to download this Instagram media right now.",
        ErrorCode.FILE_TOO_LARGE: "Video too large to upload. Try a smaller version.",
        ErrorCode.DISCORD_UPLOAD_FAILED: "Upload failed while sending the video to Discord.",
    }
    return BotError(code=code, user_message=messages[code], technical_message=technical_message)


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def get_quality_profiles() -> list[tuple[str, str]]:
    if ffmpeg_available():
        return [
            ("high", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"),
            ("medium", "best[height<=720][ext=mp4]/best[height<=720]/best"),
            ("low", "best[height<=480][ext=mp4]/best[height<=480]/best"),
        ]
    return [
        ("high", "best[ext=mp4]/best"),
        ("medium", "best[height<=720][ext=mp4]/best[height<=720]/best"),
        ("low", "best[height<=480][ext=mp4]/best[height<=480]/best"),
    ]


def is_supported_media_url(video_url: str) -> bool:
    path = urlsplit(video_url).path.lower()
    return any(part in path for part in ("/reel/", "/p/", "/tv/"))


def resolve_instagram_media_url(video_url: str) -> str:
    normalized_url = normalize_instagram_url(video_url)
    if is_supported_media_url(normalized_url):
        return normalized_url

    resolver_opts = {
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": YTDLP_SOCKET_TIMEOUT,
        "retries": YTDLP_RETRIES,
        "http_headers": {"User-Agent": YTDLP_USER_AGENT},
    }

    with YoutubeDL(resolver_opts) as ydl:
        info = ydl.extract_info(normalized_url, download=False)

    candidates = [info.get("webpage_url"), info.get("original_url"), info.get("url")]
    for candidate in candidates:
        if not candidate:
            continue
        candidate_url = normalize_instagram_url(candidate)
        if is_supported_media_url(candidate_url):
            return candidate_url

    raise ValueError("Could not resolve a direct Instagram reel/post URL.")


def resolve_downloaded_file(info: dict, ydl: YoutubeDL, run_id: str) -> Path:
    requested_downloads = info.get("requested_downloads") or []
    for item in requested_downloads:
        filepath = item.get("filepath") or item.get("_filename")
        if filepath:
            path = Path(filepath)
            if path.exists():
                return path

    file_path = Path(ydl.prepare_filename(info))
    if file_path.exists():
        return file_path

    fallback_file = file_path.with_suffix(".mp4")
    if fallback_file.exists():
        return fallback_file

    matches = list(DOWNLOAD_DIR.glob(f"{run_id}_*"))
    if matches:
        return matches[0]

    raise FileNotFoundError("Downloaded file was not found on disk.")


def cleanup_partial_files(run_id: str):
    for path in DOWNLOAD_DIR.glob(f"{run_id}_*"):
        try:
            path.unlink()
        except OSError:
            pass


def build_ydl_options(fmt: str, run_id: str, use_cookies: bool) -> dict:
    opts = {
        "format": fmt,
        "outtmpl": str(DOWNLOAD_DIR / f"{run_id}_%(id)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": YTDLP_SOCKET_TIMEOUT,
        "retries": YTDLP_RETRIES,
        "http_headers": {"User-Agent": YTDLP_USER_AGENT},
    }

    if ffmpeg_available():
        opts["merge_output_format"] = "mp4"

    if use_cookies:
        browser = os.getenv("INSTAGRAM_COOKIES_FROM_BROWSER", "").strip().lower()
        if browser:
            opts["cookiesfrombrowser"] = (browser,)
    return opts


def get_discord_file_limit(channel: discord.abc.Messageable) -> int:
    guild = getattr(channel, "guild", None)
    limit = getattr(guild, "filesize_limit", None)
    if isinstance(limit, int) and limit > 0:
        return limit
    return DEFAULT_FILE_LIMIT_BYTES


def download_instagram_video(url: str, max_bytes: int) -> DownloadResult:
    DOWNLOAD_DIR.mkdir(exist_ok=True)
    url_id = url_id_from_url(url)

    try:
        normalized_url = resolve_instagram_media_url(url)
        log_event(logging.INFO, "normalization", url_id, f"Resolved URL: {normalized_url}")
    except Exception as exc:
        technical = clean_error_text(exc)
        return DownloadResult(file_path=None, error=make_error(ErrorCode.INVALID_URL, technical))

    profiles = get_quality_profiles()
    attempts = 0
    use_cookie_retry = bool(os.getenv("INSTAGRAM_COOKIES_FROM_BROWSER", "").strip())
    had_too_large = False
    last_error_code = ErrorCode.DOWNLOAD_FAILED
    last_technical_message = "Unknown download failure."

    for quality_name, fmt in profiles:
        if attempts >= MAX_DOWNLOAD_ATTEMPTS:
            break
        attempts += 1
        run_id = uuid.uuid4().hex[:10]
        log_event(logging.INFO, "download_start", url_id, f"Attempt {attempts} profile={quality_name} format={fmt}")

        try:
            opts = build_ydl_options(fmt, run_id, use_cookies=False)
            with YoutubeDL(opts) as ydl:
                info = ydl.extract_info(normalized_url, download=True)
                file_path = resolve_downloaded_file(info, ydl, run_id)

            file_size = file_path.stat().st_size
            if file_size > max_bytes:
                file_path.unlink(missing_ok=True)
                log_event(logging.WARNING, "download_end", url_id, f"Downloaded file too large: {file_size} bytes")
                had_too_large = True
                continue

            log_event(logging.INFO, "download_end", url_id, f"Download successful with profile={quality_name}")
            return DownloadResult(file_path=file_path, error=None, quality_profile=quality_name)
        except DownloadError as exc:
            technical = clean_error_text(exc)
            code = classify_download_error(technical)
            reason = classify_failure_reason(technical)
            last_error_code = code
            last_technical_message = technical
            log_event(logging.WARNING, "download_end", url_id, f"Attempt failed ({reason.value}): {technical}")
            cleanup_partial_files(run_id)

            if use_cookie_retry and code in (ErrorCode.PRIVATE_CONTENT, ErrorCode.DOWNLOAD_FAILED):
                cookie_run_id = uuid.uuid4().hex[:10]
                try:
                    log_event(logging.INFO, "download_start", url_id, "Retrying with browser cookies")
                    cookie_opts = build_ydl_options(fmt, cookie_run_id, use_cookies=True)
                    with YoutubeDL(cookie_opts) as ydl:
                        info = ydl.extract_info(normalized_url, download=True)
                        file_path = resolve_downloaded_file(info, ydl, cookie_run_id)

                    file_size = file_path.stat().st_size
                    if file_size > max_bytes:
                        file_path.unlink(missing_ok=True)
                        had_too_large = True
                        continue

                    log_event(logging.INFO, "download_end", url_id, "Cookie retry successful")
                    return DownloadResult(file_path=file_path, error=None, quality_profile=quality_name)
                except Exception as cookie_exc:
                    last_error_code = ErrorCode.DOWNLOAD_FAILED
                    last_technical_message = clean_error_text(cookie_exc)
                    cleanup_partial_files(cookie_run_id)
                    log_event(logging.WARNING, "download_end", url_id, f"Cookie retry failed: {clean_error_text(cookie_exc)}")

            if code == ErrorCode.INVALID_URL:
                return DownloadResult(file_path=None, error=make_error(code, technical))
        except Exception as exc:
            technical = clean_error_text(exc)
            last_error_code = ErrorCode.DOWNLOAD_FAILED
            last_technical_message = technical
            cleanup_partial_files(run_id)
            log_event(logging.ERROR, "download_end", url_id, f"Unexpected downloader error: {technical}")

    if had_too_large:
        return DownloadResult(
            file_path=None,
            error=make_error(
                ErrorCode.FILE_TOO_LARGE,
                "All attempted quality profiles either failed or exceeded Discord upload limit.",
            ),
        )

    return DownloadResult(file_path=None, error=make_error(last_error_code, last_technical_message))


def dedupe_links_in_message(content: str) -> list[str]:
    found = INSTAGRAM_URL_PATTERN.findall(content)
    deduped: list[str] = []
    seen = set()
    for link in found:
        normalized = normalize_instagram_url(link)
        if normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(normalized)
        if len(deduped) >= MAX_LINKS_PER_MESSAGE:
            break
    return deduped


load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
if not DISCORD_TOKEN:
    raise ValueError("DISCORD_TOKEN is missing. Add it to your .env file or hosting environment.")

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
commands_synced = False
recent_urls: dict[str, float] = {}


def seen_recently(url: str) -> bool:
    now = monotonic()
    expired = [k for k, ts in recent_urls.items() if now - ts > RECENT_URL_TTL_SECONDS]
    for key in expired:
        recent_urls.pop(key, None)

    normalized = normalize_instagram_url(url)
    ts = recent_urls.get(normalized)
    if ts and now - ts <= RECENT_URL_TTL_SECONDS:
        return True

    recent_urls[normalized] = now
    return False


async def process_instagram_url(channel: discord.abc.Messageable, url: str):
    url_id = url_id_from_url(url)
    file_limit = get_discord_file_limit(channel)

    feedback = await channel.send("⏳ Processing your Instagram link...")
    log_event(logging.INFO, "detection", url_id, f"Detected URL: {url}")

    result: DownloadResult | None = None
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(download_instagram_video, url, file_limit),
            timeout=DOWNLOAD_TIMEOUT_SECONDS,
        )
        if not result.ok or result.file_path is None:
            err = result.error or make_error(ErrorCode.DOWNLOAD_FAILED, "Unknown downloader failure.")
            await channel.send(f"❌ Failed: {err.user_message}")
            log_event(logging.WARNING, "download_end", url_id, err.technical_message)
            return

        file_size = result.file_path.stat().st_size
        if file_size > file_limit:
            await channel.send("⚠️ Video too large to upload. Try a smaller version.")
            log_event(logging.WARNING, "upload", url_id, f"File too large for Discord: {file_size} bytes")
            return

        await channel.send(file=discord.File(result.file_path))
        await channel.send("✅ Uploaded successfully")
        log_event(logging.INFO, "upload", url_id, "Upload successful")
    except asyncio.TimeoutError:
        await channel.send("❌ Failed: Download timed out. Please try again.")
        log_event(logging.WARNING, "download_end", url_id, "Download timed out")
    except discord.HTTPException as exc:
        err = make_error(ErrorCode.DISCORD_UPLOAD_FAILED, clean_error_text(exc))
        await channel.send(f"❌ Failed: {err.user_message}")
        log_event(logging.ERROR, "upload", url_id, err.technical_message)
    except Exception as exc:
        err = make_error(ErrorCode.DOWNLOAD_FAILED, clean_error_text(exc))
        await channel.send(f"❌ Failed: {err.user_message}")
        log_event(logging.ERROR, "download_end", url_id, err.technical_message)
    finally:
        if result and result.file_path and result.file_path.exists():
            try:
                result.file_path.unlink()
                log_event(logging.INFO, "cleanup", url_id, f"Deleted file {result.file_path.name}")
            except OSError as exc:
                log_event(logging.WARNING, "cleanup", url_id, f"Cleanup failed: {clean_error_text(exc)}")
        try:
            await feedback.delete()
        except Exception:
            pass


@bot.event
async def on_ready():
    global commands_synced
    if not commands_synced:
        await bot.tree.sync()
        commands_synced = True

    ffmpeg_status = "available" if ffmpeg_available() else "missing"
    log_event(logging.INFO, "startup", "-", f"Logged in as {bot.user} | ffmpeg={ffmpeg_status}")


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    links = dedupe_links_in_message(message.content)
    if not links:
        await bot.process_commands(message)
        return

    for link in links:
        if seen_recently(link):
            await message.channel.send("⚠️ This Instagram link was processed recently. Please wait before retrying.")
            continue
        await process_instagram_url(message.channel, link)

    await bot.process_commands(message)


@bot.tree.command(name="insta", description="Download an Instagram video and post it in this channel")
@app_commands.describe(url="Instagram reel or post URL")
async def insta(interaction: discord.Interaction, url: str):
    if interaction.channel is None:
        await interaction.response.send_message("❌ Failed: Could not access this channel.", ephemeral=True)
        return

    if "instagram.com" not in url.lower():
        await interaction.response.send_message("❌ Failed: Please provide a valid Instagram URL.", ephemeral=True)
        return

    await interaction.response.send_message("Queued your request. Processing now...")
    await process_instagram_url(interaction.channel, url)


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)