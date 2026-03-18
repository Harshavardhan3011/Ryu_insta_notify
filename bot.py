import asyncio
import os
import re
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from yt_dlp import YoutubeDL


INSTAGRAM_URL_PATTERN = re.compile(r"https?://(?:www\.)?instagram\.com/[^\s]+", re.IGNORECASE)
ANSI_ESCAPE_PATTERN = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")
DOWNLOAD_DIR = Path("downloads")

load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

if not DISCORD_TOKEN:
    raise ValueError("DISCORD_TOKEN is missing. Add it to your .env file or hosting environment.")

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
commands_synced = False


def normalize_instagram_url(video_url: str) -> str:
    parsed = urlsplit(video_url.strip())
    normalized = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    return normalized.rstrip("/") + "/"


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
    }

    with YoutubeDL(resolver_opts) as ydl:
        info = ydl.extract_info(normalized_url, download=False)

    candidates = [
        info.get("webpage_url"),
        info.get("original_url"),
        info.get("url"),
    ]

    for candidate in candidates:
        if not candidate:
            continue
        candidate_url = normalize_instagram_url(candidate)
        if is_supported_media_url(candidate_url):
            return candidate_url

    raise ValueError(
        "Could not resolve a direct Instagram reel/post URL. Send a reel/post link, or a share link that points to one."
    )


def clean_error_text(error: Exception) -> str:
    return ANSI_ESCAPE_PATTERN.sub("", str(error)).strip()


def resolve_downloaded_file(info: dict, ydl: YoutubeDL) -> Path:
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

    raise FileNotFoundError("Downloaded file was not found on disk.")


def download_with_options(video_url: str, ydl_opts: dict) -> Path:
    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(video_url, download=True)
        return resolve_downloaded_file(info, ydl)


def download_instagram_video(video_url: str) -> Path:
    DOWNLOAD_DIR.mkdir(exist_ok=True)

    normalized_url = resolve_instagram_media_url(video_url)

    ydl_opts = {
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "outtmpl": str(DOWNLOAD_DIR / "%(id)s.%(ext)s"),
        "noplaylist": True,
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
    }

    try:
        return download_with_options(normalized_url, ydl_opts)
    except Exception as first_error:
        browser = os.getenv("INSTAGRAM_COOKIES_FROM_BROWSER", "").strip().lower()
        if browser:
            retry_opts = dict(ydl_opts)
            retry_opts["cookiesfrombrowser"] = (browser,)
            try:
                return download_with_options(normalized_url, retry_opts)
            except Exception as retry_error:
                raise RuntimeError(
                    f"{clean_error_text(first_error)} | Retry with browser cookies failed: {clean_error_text(retry_error)}"
                ) from retry_error
        raise


async def send_instagram_video(channel: discord.abc.Messageable, video_url: str):
    status_message = await channel.send(f"Downloading video from {video_url}...")

    downloaded_path = None
    try:
        downloaded_path = await asyncio.to_thread(download_instagram_video, video_url)
        await channel.send(file=discord.File(downloaded_path))
    except Exception as exc:
        error_text = clean_error_text(exc)
        if "instagram:user" in error_text.lower():
            error_text = "This looks like a profile URL. Send a direct reel/post URL or a share link that opens a reel/post."
        await channel.send(f"Failed to download this Instagram link. Error: {error_text}")
    finally:
        if downloaded_path and downloaded_path.exists():
            downloaded_path.unlink()
        try:
            await status_message.delete()
        except Exception:
            pass


@bot.event
async def on_ready():
    global commands_synced
    if not commands_synced:
        await bot.tree.sync()
        commands_synced = True
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    match = INSTAGRAM_URL_PATTERN.search(message.content)
    if not match:
        await bot.process_commands(message)
        return

    video_url = match.group(0)
    await send_instagram_video(message.channel, video_url)

    await bot.process_commands(message)


@bot.tree.command(name="insta", description="Download an Instagram video and post it in this channel")
@app_commands.describe(url="Instagram reel or post URL")
async def insta(interaction: discord.Interaction, url: str):
    await interaction.response.send_message(f"Starting download for: {url}")

    if "instagram.com" not in url.lower():
        await interaction.followup.send("Please provide a valid Instagram URL.")
        return

    if interaction.channel is None:
        await interaction.followup.send("Could not access this channel.")
        return

    await send_instagram_video(interaction.channel, url)

if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)