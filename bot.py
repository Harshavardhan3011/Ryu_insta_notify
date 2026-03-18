import asyncio
import json
import logging
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from time import monotonic, sleep
from urllib.parse import urlsplit, urlunsplit

import discord
import requests
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv
from yt_dlp import DownloadError, YoutubeDL


load_dotenv()

INSTAGRAM_URL_PATTERN = re.compile(r"https?://(?:www\.)?instagram\.com/[^\s]+", re.IGNORECASE)
SHORTCODE_PATTERN = re.compile(r'"shortcode":"(.*?)"')
CODE_PATTERN = re.compile(r'"code":"(.*?)"')
MEDIA_ID_PATTERN = re.compile(r"instagram://media\?id=(\d+)")
EDGE_SHORTCODE_PATTERN = re.compile(r'"edge_owner_to_timeline_media".*?"shortcode":"([^\"]+)"', re.DOTALL)
ANSI_ESCAPE_PATTERN = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")
VALID_USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9._]+$")
RESERVED_PATHS = {"p", "reel", "tv", "stories", "explore", "accounts", "direct"}

DOWNLOAD_DIR = Path("downloads")
CONFIG_FILE = Path("insta_config.json")

DEFAULT_FILE_LIMIT_BYTES = 8 * 1024 * 1024
RECENT_URL_TTL_SECONDS = 120
MAX_LINKS_PER_MESSAGE = 3
DOWNLOAD_TIMEOUT_SECONDS = int(os.getenv("DOWNLOAD_TIMEOUT_SECONDS", "180"))
MAX_DOWNLOAD_ATTEMPTS = int(os.getenv("MAX_DOWNLOAD_ATTEMPTS", "3"))
YTDLP_SOCKET_TIMEOUT = int(os.getenv("YTDLP_SOCKET_TIMEOUT", "15"))
YTDLP_RETRIES = int(os.getenv("YTDLP_RETRIES", "2"))
REQUESTS_TIMEOUT_SECONDS = int(os.getenv("REQUESTS_TIMEOUT_SECONDS", "20"))
AUTO_UPLOAD_NEW_POST_MEDIA = os.getenv("AUTO_UPLOAD_NEW_POST_MEDIA", "true").strip().lower() in (
    "1",
    "true",
    "yes",
)
GUILD_ID_RAW = os.getenv("GUILD_ID", "").strip()
GUILD_ID = int(GUILD_ID_RAW) if GUILD_ID_RAW.isdigit() else 0
YTDLP_USER_AGENT = os.getenv(
    "YTDLP_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
)

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
if not DISCORD_TOKEN:
    raise ValueError("DISCORD_TOKEN is missing. Add it to your .env file or hosting environment.")
USE_COOKIES = os.getenv("USE_COOKIES", "true").strip().lower() in ("1", "true", "yes")
COOKIE_BROWSER = os.getenv("COOKIE_BROWSER", "chrome").strip().lower()


class ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "stage"):
            record.stage = "general"
        return True


def setup_logger() -> logging.Logger:
    logger = logging.getLogger("ryu_insta_notify")
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s [%(levelname)s] [%(stage)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    handler.addFilter(ContextFilter())
    logger.addHandler(handler)
    return logger


LOGGER = setup_logger()


def log_event(level: int, stage: str, message: str):
    LOGGER.log(level, message, extra={"stage": stage})


class ErrorCode(str, Enum):
    INVALID_URL = "INVALID_URL"
    PRIVATE_CONTENT = "PRIVATE_CONTENT"
    DOWNLOAD_FAILED = "DOWNLOAD_FAILED"
    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    DISCORD_UPLOAD_FAILED = "DISCORD_UPLOAD_FAILED"


@dataclass
class BotError:
    code: ErrorCode
    user_message: str
    technical_message: str


@dataclass
class DownloadResult:
    file_path: Path | None
    error: BotError | None

    @property
    def ok(self) -> bool:
        return self.file_path is not None and self.error is None


def clean_error_text(error: Exception | str) -> str:
    return ANSI_ESCAPE_PATTERN.sub("", str(error)).strip()


def normalize_instagram_url(url: str) -> str:
    parsed = urlsplit(url.strip())
    normalized = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    return normalized.rstrip("/") + "/"


def canonical_profile_url(username: str) -> str:
    return f"https://www.instagram.com/{username}/"


def extract_username_from_url(url: str) -> tuple[str, str]:
    normalized_url = normalize_instagram_url(url)
    parsed = urlsplit(normalized_url)

    if parsed.netloc.lower() not in {"instagram.com", "www.instagram.com"}:
        raise ValueError("URL must be an Instagram profile URL.")

    path_parts = [part for part in parsed.path.split("/") if part]
    if not path_parts:
        raise ValueError("Profile URL is missing username.")

    username = path_parts[0]
    if username.lower() in RESERVED_PATHS:
        raise ValueError("URL must point to a profile, not a post/reel/story path.")
    if not VALID_USERNAME_PATTERN.fullmatch(username):
        raise ValueError("Instagram username in URL is invalid.")

    return username, canonical_profile_url(username)


def ensure_config_file():
    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text("{}\n", encoding="utf-8")


def load_config() -> dict[str, dict[str, int | str | None]]:
    ensure_config_file()
    try:
        raw = CONFIG_FILE.read_text(encoding="utf-8").strip()
        if not raw:
            return {}
        data = json.loads(raw)
        if not isinstance(data, dict):
            return {}

        cleaned: dict[str, dict[str, int | str | None]] = {}
        for username, details in data.items():
            if not isinstance(details, dict):
                continue
            channel_id = details.get("channel_id")
            last_post_id = details.get("last_post_id", None)

            parsed_channel_id = 0
            if isinstance(channel_id, int):
                parsed_channel_id = channel_id
            elif isinstance(channel_id, str) and channel_id.isdigit():
                parsed_channel_id = int(channel_id)

            if parsed_channel_id > 0:
                normalized_last_post_id = str(last_post_id) if last_post_id is not None else None
                if normalized_last_post_id is not None and not is_valid_shortcode(normalized_last_post_id):
                    normalized_last_post_id = None
                cleaned[str(username)] = {
                    "channel_id": parsed_channel_id,
                    "last_post_id": normalized_last_post_id,
                }
        return cleaned
    except (OSError, json.JSONDecodeError) as exc:
        log_event(logging.WARNING, "error", f"Failed reading insta_config.json: {clean_error_text(exc)}")
        return {}


def save_config(config: dict[str, dict[str, int | str | None]]):
    CONFIG_FILE.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")


def is_valid_shortcode(value: str | None) -> bool:
    if not value:
        return False
    shortcode = value.strip()
    if len(shortcode) <= 5:
        return False
    lowered = shortcode.lower()
    if lowered in {"en_us", "en-gb", "en", "us", "english"}:
        return False
    if lowered.startswith("en_") or lowered.startswith("en-"):
        return False
    return True


def extract_shortcode_from_html(html: str) -> str | None:
    """
    Extract Instagram post shortcode from HTML using proper JSON parsing.
    
    Attempts:
    1. Parse JSON from <script> tags
    2. Navigate to edge_owner_to_timeline_media.edges[0].node.shortcode
    3. Validate shortcode (>5 chars, not language codes)
    4. Fallback to regex if JSON parsing fails
    """
    # Attempt 1: Safe JSON parsing from script tags
    try:
        script_pattern = re.compile(
            r'<script[^>]*type="application/ld\+json"[^>]*>([^<]+)</script>',
            re.DOTALL
        )
        script_match = script_pattern.search(html)
        if not script_match:
            # Try alternative script extraction
            script_pattern = re.compile(
                r'<script[^>]*>\s*window\._sharedData\s*=\s*({.*?});\s*</script>',
                re.DOTALL
            )
            script_match = script_pattern.search(html)
        
        if script_match:
            try:
                data = json.loads(script_match.group(1))
                
                # Navigate to shortcode in profile timeline
                if isinstance(data, dict):
                    # Check for entry_data path
                    if 'entry_data' in data:
                        entry_data = data['entry_data']
                        if 'ProfilePage' in entry_data and entry_data['ProfilePage']:
                            profile_page = entry_data['ProfilePage'][0]
                            if 'graphql' in profile_page and 'user' in profile_page['graphql']:
                                user = profile_page['graphql']['user']
                                if 'edge_owner_to_timeline_media' in user:
                                    timeline_media = user['edge_owner_to_timeline_media']
                                    if 'edges' in timeline_media and timeline_media['edges']:
                                        first_edge = timeline_media['edges'][0]
                                        if 'node' in first_edge and 'shortcode' in first_edge['node']:
                                            shortcode = str(first_edge['node']['shortcode']).strip()
                                            if is_valid_shortcode(shortcode):
                                                log_event(logging.DEBUG, "scraper", f"[JSON] Extracted shortcode: {shortcode}")
                                                return shortcode
            except (json.JSONDecodeError, TypeError, KeyError, IndexError) as e:
                log_event(logging.DEBUG, "scraper", f"[JSON] Parse failed: {clean_error_text(e)}")
    except Exception as e:
        log_event(logging.DEBUG, "scraper", f"[JSON] Exception: {clean_error_text(e)}")

    # Attempt 2: Regex fallback - only look for "shortcode" in quotes
    try:
        # Look specifically for shortcode in quotes
        shortcode_pattern = re.compile(r'"shortcode":"([^"]+)"')
        matches = shortcode_pattern.findall(html)
        
        for match in matches:
            shortcode = match.strip()
            if is_valid_shortcode(shortcode):
                log_event(logging.DEBUG, "scraper", f"[REGEX] Extracted shortcode: {shortcode}")
                return shortcode
    except Exception as e:
        log_event(logging.DEBUG, "scraper", f"[REGEX] Exception: {clean_error_text(e)}")

    log_event(logging.WARNING, "scraper", "Failed to extract valid shortcode from HTML")
    return None


def fetch_latest_post_api(username: str) -> tuple[str | None, str | None, BotError | None]:
    """Fetch latest post via Instagram web_profile_info API endpoint."""
    endpoint = f"https://www.instagram.com/api/v1/users/web_profile_info/?username={username}"
    session = requests.Session()
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.instagram.com/",
        "Connection": "keep-alive",
    }
    headers.update({"X-IG-App-ID": "936619743392459"})
    session.headers.update(headers)

    try:
        # Basic pacing to reduce request bursts/rate-limit likelihood.
        sleep(2)
        response = session.get(endpoint, timeout=REQUESTS_TIMEOUT_SECONDS)
        response.raise_for_status()
        payload = response.json()

        edges = (
            payload.get("data", {})
            .get("user", {})
            .get("edge_owner_to_timeline_media", {})
            .get("edges", [])
        )
        if not edges:
            return None, None, make_error(ErrorCode.DOWNLOAD_FAILED, "API returned no timeline edges")

        node = edges[0].get("node", {}) if isinstance(edges[0], dict) else {}
        shortcode = str(node.get("shortcode") or "").strip()
        if not is_valid_shortcode(shortcode):
            return None, None, make_error(ErrorCode.DOWNLOAD_FAILED, f"API returned invalid shortcode: {shortcode}")

        post_url = f"https://www.instagram.com/p/{shortcode}/"
        log_event(logging.INFO, "fetch", f"[api] Fetched post {shortcode} for {username}")
        return shortcode, post_url, None
    except requests.RequestException as exc:
        technical = clean_error_text(exc)
        log_event(logging.WARNING, "fetch", f"[api] Request failed for {username}: {technical}")
        return None, None, make_error(ErrorCode.DOWNLOAD_FAILED, technical)
    except (ValueError, json.JSONDecodeError) as exc:
        technical = clean_error_text(exc)
        log_event(logging.WARNING, "fetch", f"[api] JSON parse failed for {username}: {technical}")
        return None, None, make_error(ErrorCode.DOWNLOAD_FAILED, technical)
    except Exception as exc:
        technical = clean_error_text(exc)
        log_event(logging.WARNING, "fetch", f"[api] Unexpected error for {username}: {technical}")
        return None, None, make_error(ErrorCode.DOWNLOAD_FAILED, technical)
    finally:
        session.close()


def make_error(code: ErrorCode, technical_message: str) -> BotError:
    mapping = {
        ErrorCode.INVALID_URL: "Invalid Instagram URL.",
        ErrorCode.PRIVATE_CONTENT: "Instagram content appears private or login-protected.",
        ErrorCode.DOWNLOAD_FAILED: "Instagram download failed.",
        ErrorCode.FILE_TOO_LARGE: "Video too large to upload.",
        ErrorCode.DISCORD_UPLOAD_FAILED: "Discord upload failed.",
    }
    return BotError(code=code, user_message=mapping[code], technical_message=technical_message)


def classify_download_error(raw_message: str) -> ErrorCode:
    msg = raw_message.lower()
    if "invalid" in msg or "not a valid url" in msg:
        return ErrorCode.INVALID_URL
    if "private" in msg or "login" in msg or "forbidden" in msg or "cookie" in msg:
        return ErrorCode.PRIVATE_CONTENT
    return ErrorCode.DOWNLOAD_FAILED


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def get_quality_profiles() -> list[str]:
    if ffmpeg_available():
        return [
            "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "best[height<=720][ext=mp4]/best[height<=720]/best",
            "best[height<=480][ext=mp4]/best[height<=480]/best",
        ]
    return [
        "best[ext=mp4]/best",
        "best[height<=720][ext=mp4]/best[height<=720]/best",
        "best[height<=480][ext=mp4]/best[height<=480]/best",
    ]


def is_supported_media_url(url: str) -> bool:
    path = urlsplit(url).path.lower()
    return any(part in path for part in ("/p/", "/reel/", "/tv/"))


def resolve_instagram_media_url(url: str) -> str:
    normalized_url = normalize_instagram_url(url)
    if is_supported_media_url(normalized_url):
        return normalized_url

    opts = {
        "quiet": True,
        "skip_download": True,
        "extract_flat": True,
        "socket_timeout": YTDLP_SOCKET_TIMEOUT,
        "retries": YTDLP_RETRIES,
        "http_headers": {"User-Agent": YTDLP_USER_AGENT},
    }
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(normalized_url, download=False)

    candidates = [info.get("webpage_url"), info.get("original_url"), info.get("url")]
    for candidate in candidates:
        if not candidate:
            continue
        candidate_url = normalize_instagram_url(candidate)
        if is_supported_media_url(candidate_url):
            return candidate_url
    raise ValueError("Could not resolve direct Instagram post URL.")


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

    fallback = file_path.with_suffix(".mp4")
    if fallback.exists():
        return fallback

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


def build_ydl_download_options(fmt: str, run_id: str) -> dict:
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
    return opts


def get_discord_file_limit(channel: discord.abc.Messageable) -> int:
    guild = getattr(channel, "guild", None)
    limit = getattr(guild, "filesize_limit", None)
    if isinstance(limit, int) and limit > 0:
        return limit
    return DEFAULT_FILE_LIMIT_BYTES


def fetch_latest_post_ytdlp_with_cookies(username: str) -> tuple[str | None, str | None, BotError | None]:
    """Fetch using yt-dlp with browser cookies for authenticated access."""
    profile_url = canonical_profile_url(username)
    opts = {
        "quiet": True,
        "skip_download": True,
        "extract_flat": True,
        "noplaylist": False,
        "playlistend": 1,
        "socket_timeout": YTDLP_SOCKET_TIMEOUT,
        "retries": YTDLP_RETRIES,
        "http_headers": {"User-Agent": YTDLP_USER_AGENT},
        "cookiesfrombrowser": (COOKIE_BROWSER,),
    }
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(profile_url, download=False)
        entries = info.get("entries") if isinstance(info, dict) else None
        if not entries:
            return None, None, make_error(ErrorCode.DOWNLOAD_FAILED, "yt-dlp (cookies) returned no entries")
        first = entries[0]
        if not isinstance(first, dict):
            return None, None, make_error(ErrorCode.DOWNLOAD_FAILED, "yt-dlp (cookies) entry payload is invalid")

        post_id = str(first.get("id") or "").strip()
        if not post_id:
            return None, None, make_error(ErrorCode.DOWNLOAD_FAILED, "yt-dlp (cookies) did not return post id")

        post_url = first.get("url") or first.get("webpage_url") or first.get("original_url")
        if isinstance(post_url, str) and post_url.startswith("/"):
            post_url = f"https://www.instagram.com{post_url}"
        if isinstance(post_url, str) and post_url.startswith("http"):
            normalized_post = normalize_instagram_url(post_url)
        else:
            normalized_post = f"https://www.instagram.com/p/{post_id}/"

        log_event(logging.INFO, "fetch", f"[cookies] Fetched post {post_id} for {username}")
        return post_id, normalized_post, None
    except DownloadError as exc:
        technical = clean_error_text(exc)
        log_event(logging.DEBUG, "fetch", f"[cookies] Failed for {username}: {technical}")
        return None, None, make_error(classify_download_error(technical), technical)
    except Exception as exc:
        technical = clean_error_text(exc)
        log_event(logging.DEBUG, "fetch", f"[cookies] Exception for {username}: {technical}")
        return None, None, make_error(ErrorCode.DOWNLOAD_FAILED, technical)


def fetch_latest_post_ytdlp_no_cookies(username: str) -> tuple[str | None, str | None, BotError | None]:
    """Fetch using yt-dlp without cookies (public session)."""
    profile_url = canonical_profile_url(username)
    opts = {
        "quiet": True,
        "skip_download": True,
        "extract_flat": True,
        "noplaylist": False,
        "playlistend": 1,
        "socket_timeout": YTDLP_SOCKET_TIMEOUT,
        "retries": YTDLP_RETRIES,
        "http_headers": {"User-Agent": YTDLP_USER_AGENT},
    }
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(profile_url, download=False)
        entries = info.get("entries") if isinstance(info, dict) else None
        if not entries:
            return None, None, make_error(ErrorCode.DOWNLOAD_FAILED, "yt-dlp returned no entries")
        first = entries[0]
        if not isinstance(first, dict):
            return None, None, make_error(ErrorCode.DOWNLOAD_FAILED, "yt-dlp entry payload is invalid")

        post_id = str(first.get("id") or "").strip()
        if not post_id:
            return None, None, make_error(ErrorCode.DOWNLOAD_FAILED, "yt-dlp did not return post id")

        post_url = first.get("url") or first.get("webpage_url") or first.get("original_url")
        if isinstance(post_url, str) and post_url.startswith("/"):
            post_url = f"https://www.instagram.com{post_url}"
        if isinstance(post_url, str) and post_url.startswith("http"):
            normalized_post = normalize_instagram_url(post_url)
        else:
            normalized_post = f"https://www.instagram.com/p/{post_id}/"

        log_event(logging.INFO, "fetch", f"[no-cookies] Fetched post {post_id} for {username}")
        return post_id, normalized_post, None
    except DownloadError as exc:
        technical = clean_error_text(exc)
        log_event(logging.DEBUG, "fetch", f"[no-cookies] Failed for {username}: {technical}")
        return None, None, make_error(classify_download_error(technical), technical)
    except Exception as exc:
        technical = clean_error_text(exc)
        log_event(logging.DEBUG, "fetch", f"[no-cookies] Exception for {username}: {technical}")
        return None, None, make_error(ErrorCode.DOWNLOAD_FAILED, technical)


def fetch_latest_post_ytdlp(username: str) -> tuple[str | None, str | None, BotError | None]:
    profile_url = canonical_profile_url(username)
    opts = {
        "quiet": True,
        "skip_download": True,
        "extract_flat": True,
        "noplaylist": False,
        "playlistend": 1,
        "socket_timeout": YTDLP_SOCKET_TIMEOUT,
        "retries": YTDLP_RETRIES,
        "http_headers": {"User-Agent": YTDLP_USER_AGENT},
    }
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(profile_url, download=False)
        entries = info.get("entries") if isinstance(info, dict) else None
        if not entries:
            return None, None, make_error(ErrorCode.DOWNLOAD_FAILED, "yt-dlp returned no entries")
        first = entries[0]
        if not isinstance(first, dict):
            return None, None, make_error(ErrorCode.DOWNLOAD_FAILED, "yt-dlp entry payload is invalid")

        post_id = str(first.get("id") or "").strip()
        if not post_id:
            return None, None, make_error(ErrorCode.DOWNLOAD_FAILED, "yt-dlp did not return post id")

        post_url = first.get("url") or first.get("webpage_url") or first.get("original_url")
        if isinstance(post_url, str) and post_url.startswith("/"):
            post_url = f"https://www.instagram.com{post_url}"
        if isinstance(post_url, str) and post_url.startswith("http"):
            normalized_post = normalize_instagram_url(post_url)
        else:
            normalized_post = f"https://www.instagram.com/p/{post_id}/"

        log_event(logging.INFO, "yt-dlp success", f"Fetched latest post for {username}")
        return post_id, normalized_post, None
    except DownloadError as exc:
        technical = clean_error_text(exc)
        log_event(logging.WARNING, "yt-dlp failure", f"{username}: {technical}")
        return None, None, make_error(classify_download_error(technical), technical)
    except Exception as exc:
        technical = clean_error_text(exc)
        log_event(logging.WARNING, "yt-dlp failure", f"{username}: {technical}")
        return None, None, make_error(ErrorCode.DOWNLOAD_FAILED, technical)


def fetch_latest_post_scrape(username: str) -> tuple[str | None, str | None, BotError | None]:
    profile_url = canonical_profile_url(username)
    session = requests.Session()
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.instagram.com/",
        "Connection": "keep-alive",
    }
    session.headers.update(headers)

    try:
        response = session.get(profile_url, timeout=REQUESTS_TIMEOUT_SECONDS)
        response.raise_for_status()
        shortcode = extract_shortcode_from_html(response.text)
        if not shortcode:
            return None, None, make_error(ErrorCode.DOWNLOAD_FAILED, "No valid shortcode found in profile HTML")

        post_url = f"https://www.instagram.com/p/{shortcode}/"
        log_event(logging.INFO, "fetch", f"[scrape] Fetched post {shortcode} for {username}")
        return shortcode, post_url, None
    except requests.RequestException as exc:
        technical = clean_error_text(exc)
        log_event(logging.DEBUG, "fetch", f"[scrape] Failed for {username}: {technical}")
        return None, None, make_error(ErrorCode.DOWNLOAD_FAILED, technical)
    except Exception as exc:
        technical = clean_error_text(exc)
        log_event(logging.DEBUG, "fetch", f"[scrape] Exception for {username}: {technical}")
        return None, None, make_error(ErrorCode.DOWNLOAD_FAILED, technical)
    finally:
        session.close()


def get_latest_post(username: str) -> tuple[str | None, str | None, BotError | None]:
    """
    Hybrid fetch strategy with authenticated API first:
    1. Instagram API endpoint via requests.Session
    2. yt-dlp with cookies (if enabled)
    3. yt-dlp without cookies
    4. HTML scraping fallback
    5. Fail safe (return error)
    """
    # Tier 1: Instagram API endpoint
    api_id, api_url, api_err = fetch_latest_post_api(username)
    if api_err is None and api_id and api_url:
        print(f"[✓ api] {username}: {api_id}")
        return api_id, api_url, None
    print(f"[✗ api] {username}: {api_err.technical_message if api_err else 'unknown'}")

    # Tier 2: Try with cookies (if enabled)
    if USE_COOKIES:
        post_id, post_url, err = fetch_latest_post_ytdlp_with_cookies(username)
        if err is None and post_id and post_url:
            print(f"[✓ cookies] {username}: {post_id}")
            return post_id, post_url, None
        print(f"[✗ cookies] {username}: {err.technical_message if err else 'unknown'}")
    else:
        err = None

    # Tier 3: Try yt-dlp without cookies
    post_id, post_url, err = fetch_latest_post_ytdlp_no_cookies(username)
    if err is None and post_id and post_url:
        print(f"[✓ yt-dlp] {username}: {post_id}")
        return post_id, post_url, None
    print(f"[✗ yt-dlp] {username}: {err.technical_message if err else 'unknown'}")

    # Tier 4: Fall back to scraping
    fallback_id, fallback_url, fallback_err = fetch_latest_post_scrape(username)
    if fallback_err is None and fallback_id and fallback_url:
        print(f"[✓ scrape] {username}: {fallback_id}")
        return fallback_id, fallback_url, None
    print(f"[✗ scrape] {username}: {fallback_err.technical_message if fallback_err else 'unknown'}")

    # Tier 5: All methods failed
    final_error = fallback_err or err or api_err or make_error(ErrorCode.DOWNLOAD_FAILED, "all fetch methods exhausted")
    print(f"[✗ FAILED] {username}: All fetch methods failed")
    return None, None, final_error


def download_instagram_video(url: str, max_bytes: int) -> DownloadResult:
    DOWNLOAD_DIR.mkdir(exist_ok=True)

    try:
        normalized_url = resolve_instagram_media_url(url)
    except Exception as exc:
        return DownloadResult(None, make_error(ErrorCode.INVALID_URL, clean_error_text(exc)))

    profiles = get_quality_profiles()
    last_error: BotError | None = None
    too_large = False

    for attempt, fmt in enumerate(profiles[:MAX_DOWNLOAD_ATTEMPTS], start=1):
        run_id = uuid.uuid4().hex[:10]
        log_event(logging.INFO, "download_start", f"Attempt {attempt} with format {fmt}")
        try:
            with YoutubeDL(build_ydl_download_options(fmt, run_id)) as ydl:
                info = ydl.extract_info(normalized_url, download=True)
                file_path = resolve_downloaded_file(info, ydl, run_id)

            size = file_path.stat().st_size
            if size > max_bytes:
                too_large = True
                file_path.unlink(missing_ok=True)
                continue

            return DownloadResult(file_path, None)
        except DownloadError as exc:
            cleanup_partial_files(run_id)
            technical = clean_error_text(exc)
            last_error = make_error(classify_download_error(technical), technical)
            log_event(logging.WARNING, "download_end", technical)
        except Exception as exc:
            cleanup_partial_files(run_id)
            technical = clean_error_text(exc)
            last_error = make_error(ErrorCode.DOWNLOAD_FAILED, technical)
            log_event(logging.WARNING, "download_end", technical)

    if too_large:
        return DownloadResult(None, make_error(ErrorCode.FILE_TOO_LARGE, "All quality profiles exceeded upload limit"))
    return DownloadResult(None, last_error or make_error(ErrorCode.DOWNLOAD_FAILED, "unknown failure"))


async def notify_discord(username: str, channel_id: int, post_url: str) -> bool:
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            fetched = await bot.fetch_channel(channel_id)
            channel = fetched if isinstance(fetched, (discord.TextChannel, discord.Thread)) else None
        except Exception as exc:
            log_event(logging.ERROR, "notify", f"Could not resolve channel {channel_id}: {clean_error_text(exc)}")
            print(f"  ✗ Resolve channel failed: {channel_id}")
            return False

    if channel is None:
        log_event(logging.ERROR, "notify", f"Channel {channel_id} not found")
        print(f"  ✗ Channel {channel_id} not found")
        return False

    try:
        allowed_mentions = discord.AllowedMentions(everyone=True)
        await channel.send(
            f"📸 **New Instagram post from {username}!** @everyone\n{post_url}",
            allowed_mentions=allowed_mentions
        )
        log_event(logging.INFO, "notify", f"Notification sent to {channel_id} for {username}")
        print(f"  ✓ Notification message sent to #{channel.name} (@everyone mention included)")
    except Exception as exc:
        log_event(logging.ERROR, "notify", f"Notification send failed: {clean_error_text(exc)}")
        print(f"  ✗ Notification send failed: {clean_error_text(exc)}")
        return False

    if not AUTO_UPLOAD_NEW_POST_MEDIA:
        return True

    result: DownloadResult | None = None
    try:
        await asyncio.sleep(2)
        file_limit = get_discord_file_limit(channel)
        print(f"  Attempting to download and upload media (limit: {file_limit} bytes)...")
        result = await asyncio.wait_for(
            asyncio.to_thread(download_instagram_video, post_url, file_limit),
            timeout=DOWNLOAD_TIMEOUT_SECONDS,
        )
        if not result.ok or result.file_path is None:
            err = result.error or make_error(ErrorCode.DOWNLOAD_FAILED, "unknown download error")
            log_event(logging.WARNING, "notify", f"Media upload skipped: {err.technical_message}")
            print(f"  ℹ Media upload skipped: {err.user_message}")
            return True

        await channel.send(file=discord.File(result.file_path))
        print(f"  ✓ Media uploaded successfully")
        return True
    except Exception as exc:
        log_event(logging.WARNING, "notify", f"Media upload failed: {clean_error_text(exc)}")
        print(f"  ℹ Media upload failed (notification text still sent): {clean_error_text(exc)}")
        return True
    finally:
        if result and result.file_path and result.file_path.exists():
            result.file_path.unlink(missing_ok=True)


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


intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
recent_urls: dict[str, float] = {}
HELP_MESSAGE = (
    "👑 **Ryu Insta Notify Bot Commands**\n\n"
    "**/insta**\n"
    "→ Download an Instagram post/reel URL into this channel\n"
    "Example: `/insta url:https://www.instagram.com/reel/ABC123/`\n\n"
    "**/notifyall**\n"
    "→ Send an Instagram post notification to @everyone\n"
    "Example: `/notifyall url:https://www.instagram.com/reel/ABC123/`\n\n"
    "**/setnotify**\n"
    "→ Set a channel and Instagram profile to monitor\n"
    "Example: `/setnotify channel:#updates url:https://www.instagram.com/dragon__up/`\n\n"
    "**/removenotify**\n"
    "→ Remove a tracked profile\n"
    "Example: `/removenotify url:https://www.instagram.com/dragon__up/`\n\n"
    "**/listnotify**\n"
    "→ Show all tracked Instagram profiles\n\n"
    "⚡ This bot automatically monitors configured profiles and sends alerts when new posts are uploaded."
)


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
    feedback = await channel.send("⏳ Processing your Instagram link...")
    result: DownloadResult | None = None

    try:
        file_limit = get_discord_file_limit(channel)
        result = await asyncio.wait_for(
            asyncio.to_thread(download_instagram_video, url, file_limit),
            timeout=DOWNLOAD_TIMEOUT_SECONDS,
        )
        if not result.ok or result.file_path is None:
            err = result.error or make_error(ErrorCode.DOWNLOAD_FAILED, "unknown downloader failure")
            await channel.send(f"❌ Failed: {err.user_message}")
            return

        await channel.send(file=discord.File(result.file_path))
        await channel.send("✅ Uploaded successfully")
    except asyncio.TimeoutError:
        await channel.send("❌ Failed: Download timed out. Please try again.")
    except Exception as exc:
        await channel.send(f"❌ Failed: {clean_error_text(exc)}")
    finally:
        if result and result.file_path and result.file_path.exists():
            result.file_path.unlink(missing_ok=True)
        try:
            await feedback.delete()
        except Exception:
            pass


@tasks.loop(seconds=90)
async def instagram_monitor_loop():
    log_event(logging.INFO, "check cycle", "Starting monitor cycle")
    config = load_config()
    profiles = list(config.keys())
    print(f"\n{'='*60}")
    print(f"[MONITOR CYCLE] Loaded {len(profiles)} profile(s): {profiles}")
    print(f"{'='*60}")
    if not config:
        log_event(logging.INFO, "check cycle", "No profiles configured")
        return

    for username, details in list(config.items()):
        await asyncio.sleep(2)
        channel_id = int(details.get("channel_id", 0))
        if channel_id <= 0:
            continue

        try:
            print(f"\n[PROFILE] {username}")
            print(f"  Attempting fetch (USE_COOKIES={USE_COOKIES})...")
            post_id, post_url, err = await asyncio.to_thread(get_latest_post, username)
            if err is not None or not post_id or not post_url:
                technical = err.technical_message if err else "empty post metadata"
                log_event(logging.WARNING, "error", f"{username}: {technical}")
                print(f"  ✗ Fetch failed: {technical}")
                continue

            previous_id = details.get("last_post_id", None)
            print(f"  Current post ID: {post_id}")
            print(f"  Stored post ID: {previous_id}")
            if previous_id is None:
                config[username]["last_post_id"] = post_id
                save_config(config)
                log_event(logging.INFO, "check cycle", f"Initialized {username} with post {post_id}")
                print(f"  [INIT] First run - stored post ID (no notification)")
                continue

            if post_id == str(previous_id):
                print(f"  [SKIP] No new post detected")
                continue

            log_event(logging.INFO, "new post detected", f"{username}: {post_id}")
            print(f"  [NEW] New post detected! ID: {post_id}")
            print(f"  Sending Discord notification...")
            notified = await notify_discord(username, channel_id, post_url)
            if notified:
                config[username]["last_post_id"] = post_id
                save_config(config)
                log_event(logging.INFO, "notification sent", f"{username}: {post_url}")
                print(f"  ✓ Notification sent and config updated")
        except Exception as exc:
            log_event(logging.ERROR, "error", f"{username}: {clean_error_text(exc)}")
            print(f"  ✗ Error: {clean_error_text(exc)}")
    print(f"\n{'='*60}\n")


@instagram_monitor_loop.before_loop
async def before_instagram_monitor_loop():
    await bot.wait_until_ready()


@bot.event
async def on_ready():
    try:
        if GUILD_ID:
            guild_obj = discord.Object(id=GUILD_ID)
            bot.tree.copy_global_to(guild=guild_obj)
            synced = await bot.tree.sync(guild=guild_obj)
            print(f"✅ Synced {len(synced)} commands to guild {GUILD_ID}")
            log_event(logging.INFO, "startup", f"Synced {len(synced)} commands to guild {GUILD_ID}")
        else:
            synced = await bot.tree.sync()
            print(f"✅ Synced {len(synced)} commands")
            log_event(logging.INFO, "startup", f"Synced {len(synced)} slash commands")
    except Exception as exc:
        print(f"❌ Sync Error: {exc}")
        log_event(logging.ERROR, "startup", f"Sync error: {clean_error_text(exc)}")

    print(f"Logged in as {bot.user}")

    ensure_config_file()
    ffmpeg_status = "available" if ffmpeg_available() else "missing"
    log_event(logging.INFO, "startup", f"Logged in as {bot.user} | ffmpeg={ffmpeg_status}")

    if not instagram_monitor_loop.is_running():
        instagram_monitor_loop.start()
        log_event(logging.INFO, "startup", "Started monitor loop (every 90 seconds)")


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    if bot.user in message.mentions:
        await message.channel.send(HELP_MESSAGE)

    links = dedupe_links_in_message(message.content)
    for link in links:
        if seen_recently(link):
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


@bot.tree.command(name="ping", description="Test command")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("Pong ✅")


@bot.tree.command(name="notifyall", description="Send Instagram post notification to everyone")
@app_commands.describe(url="Instagram post or reel URL", download="Download and upload the video (default: False)")
async def notifyall(interaction: discord.Interaction, url: str, download: bool = False):
    """
    Send an Instagram post notification to everyone in the channel.
    
    Usage: /notifyall url:https://www.instagram.com/reel/ABC123/
    Usage: /notifyall url:https://www.instagram.com/reel/ABC123/ download:True
    
    - Validates the URL
    - Sends immediate acknowledgment (ephemeral)
    - Sends notification with @everyone mention
    - Only downloads/uploads video if download=True (default: False)
    - Handles errors gracefully
    """
    if interaction.channel is None:
        await interaction.response.send_message("❌ Failed: Could not access this channel.", ephemeral=True)
        return

    if "instagram.com" not in url.lower():
        await interaction.response.send_message("❌ Failed: Please provide a valid Instagram URL.", ephemeral=True)
        return

    # Immediate acknowledgment (ephemeral - only user sees)
    await interaction.response.send_message("⏳ Processing Instagram link...", ephemeral=True)
    
    log_event(logging.INFO, "command usage", f"/notifyall by {interaction.user.id}: {url} (download={download})")

    # Send notification to channel with @everyone mention
    try:
        allowed_mentions = discord.AllowedMentions(everyone=True)
        await interaction.channel.send(
            f"📸 **New Instagram Post!** @everyone\n{url}",
            allowed_mentions=allowed_mentions
        )
        log_event(logging.INFO, "notifyall", f"Notification sent to {interaction.channel.name}: {url}")
        print(f"[notifyall] Notification sent to #{interaction.channel.name}: {url}")
    except Exception as exc:
        log_event(logging.ERROR, "notifyall", f"Failed to send notification: {clean_error_text(exc)}")
        print(f"[notifyall] Failed to send notification: {clean_error_text(exc)}")
        return

    # Only download and upload video if explicitly requested
    if not download:
        return

    result: DownloadResult | None = None
    try:
        await asyncio.sleep(1)
        file_limit = get_discord_file_limit(interaction.channel)
        print(f"[notifyall] Attempting media download (limit: {file_limit} bytes)...")
        result = await asyncio.wait_for(
            asyncio.to_thread(download_instagram_video, url, file_limit),
            timeout=DOWNLOAD_TIMEOUT_SECONDS,
        )
        if not result.ok or result.file_path is None:
            err = result.error or make_error(ErrorCode.DOWNLOAD_FAILED, "unknown download error")
            log_event(logging.WARNING, "notifyall", f"Media upload skipped: {err.technical_message}")
            print(f"[notifyall] Media upload skipped: {err.user_message}")
            return

        await interaction.channel.send(file=discord.File(result.file_path))
        print(f"[notifyall] Media uploaded successfully")
        log_event(logging.INFO, "notifyall", f"Media uploaded for {url}")
    except asyncio.TimeoutError:
        log_event(logging.WARNING, "notifyall", f"Media download timed out: {url}")
        print(f"[notifyall] Media download timed out")
    except Exception as exc:
        log_event(logging.WARNING, "notifyall", f"Media upload failed: {clean_error_text(exc)}")
        print(f"[notifyall] Media upload failed: {clean_error_text(exc)}")
    finally:
        if result and result.file_path and result.file_path.exists():
            result.file_path.unlink(missing_ok=True)


@bot.tree.command(name="setnotify", description="Set Instagram profile notifications for a channel")
@app_commands.describe(channel="Target Discord channel", url="Instagram profile URL")
async def setnotify(interaction: discord.Interaction, channel: discord.TextChannel, url: str):
    log_event(logging.INFO, "command usage", f"/setnotify by {interaction.user.id}: {url} -> {channel.id}")

    try:
        username, normalized_profile_url = extract_username_from_url(url)
    except ValueError as exc:
        await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    try:
        config = load_config()
        existing = username in config

        if not existing:
            config[username] = {"channel_id": channel.id, "last_post_id": None}
        else:
            config[username]["channel_id"] = channel.id

        save_config(config)
        await interaction.followup.send(f"✅ Now tracking {username} in {channel.mention}", ephemeral=True)

        if existing:
            log_event(logging.INFO, "profile updated", f"{username} -> channel {channel.id}")
            print(f"Updated profile tracking for {username} ({normalized_profile_url})")
        else:
            log_event(logging.INFO, "profile added", f"{username} -> channel {channel.id}")
            print(f"Added profile tracking for {username} ({normalized_profile_url})")
    except Exception as exc:
        error_text = clean_error_text(exc)
        log_event(logging.ERROR, "error", f"/setnotify failed: {error_text}")
        print(f"/setnotify failed: {error_text}")
        await interaction.followup.send("❌ Failed to save notify configuration.", ephemeral=True)


@bot.tree.command(name="removenotify", description="Remove Instagram profile notifications")
@app_commands.describe(url="Instagram profile URL")
async def removenotify(interaction: discord.Interaction, url: str):
    try:
        username, _ = extract_username_from_url(url)
    except ValueError as exc:
        await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
        return

    config = load_config()
    if username not in config:
        await interaction.response.send_message(f"ℹ️ No notify config found for {username}.", ephemeral=True)
        return

    config.pop(username, None)
    save_config(config)
    await interaction.response.send_message(f"✅ Removed notifications for {username}.", ephemeral=True)
    log_event(logging.INFO, "profile removed", f"{username}")


@bot.tree.command(name="listnotify", description="List configured Instagram profile notifications")
async def listnotify(interaction: discord.Interaction):
    config = load_config()
    if not config:
        await interaction.response.send_message("ℹ️ No profiles configured.", ephemeral=True)
        return

    lines = []
    for username, details in config.items():
        channel_id = int(details.get("channel_id", 0))
        last_post_id = str(details.get("last_post_id", "")) or "<none>"
        lines.append(f"- {username}: <#{channel_id}> | last_post_id={last_post_id}")

    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    print(f"Command Error: {error}")
    log_event(logging.ERROR, "error", f"Command Error: {clean_error_text(error)}")

    try:
        if interaction.response.is_done():
            await interaction.followup.send("❌ Error occurred while executing command.", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Error occurred while executing command.", ephemeral=True)
    except Exception:
        pass


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)