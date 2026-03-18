import asyncio
import os
import re
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from yt_dlp import YoutubeDL


INSTAGRAM_URL_PATTERN = re.compile(r"https?://(?:www\.)?instagram\.com/[^\s]+", re.IGNORECASE)
DOWNLOAD_DIR = Path("downloads")

load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

if not DISCORD_TOKEN:
    raise ValueError("DISCORD_TOKEN is missing. Add it to your .env file or hosting environment.")

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
commands_synced = False


def download_instagram_video(video_url: str) -> Path:
    DOWNLOAD_DIR.mkdir(exist_ok=True)

    ydl_opts = {
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "outtmpl": str(DOWNLOAD_DIR / "%(id)s.%(ext)s"),
        "noplaylist": True,
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
    }

    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(video_url, download=True)
        file_path = Path(ydl.prepare_filename(info))

    if file_path.exists():
        return file_path

    fallback_file = file_path.with_suffix(".mp4")
    if fallback_file.exists():
        return fallback_file

    raise FileNotFoundError("Downloaded file was not found on disk.")


async def send_instagram_video(channel: discord.abc.Messageable, video_url: str):
    status_message = await channel.send(f"Downloading video from {video_url}...")

    downloaded_path = None
    try:
        downloaded_path = await asyncio.to_thread(download_instagram_video, video_url)
        await channel.send(file=discord.File(downloaded_path))
    except Exception as exc:
        await channel.send(f"Failed to download this Instagram link. Error: {exc}")
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