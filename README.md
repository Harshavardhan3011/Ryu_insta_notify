# Ryu_insta_notify

Discord bot that watches messages for Instagram links, downloads media with yt-dlp, posts the media back to the channel, and deletes local files after upload.

## Features

- Detects Instagram links in normal messages.
- Supports the slash command /insta.
- Resolves share/profile-style links to direct media links when possible.
- Retries downloads with fallback quality profiles.
- Uses browser cookies fallback when configured.
- Adapts to ffmpeg availability at runtime.
- Avoids re-processing duplicate URLs in a short window.
- Checks Discord upload size limits before upload.
- Cleans up files in all success/failure paths.

## Local Setup

1. Create and activate a virtual environment.

	Windows PowerShell:

	```powershell
	python -m venv venv
	.\venv\Scripts\Activate.ps1
	```

2. Install dependencies.

	```powershell
	pip install -r requirements.txt
	```

3. Install ffmpeg (recommended for best quality merge support).

	Windows steps:
	- Download ffmpeg build from a trusted source.
	- Extract it, for example to C:\ffmpeg.
	- Add C:\ffmpeg\bin to your PATH.
	- Open a new terminal and verify:

	```powershell
	ffmpeg -version
	```

	If ffmpeg is missing, the bot still works using progressive single-file formats.

4. Add environment variables to .env.

	```env
	DISCORD_TOKEN=your_actual_token_here
	INSTAGRAM_COOKIES_FROM_BROWSER=chrome
	DOWNLOAD_TIMEOUT_SECONDS=180
	MAX_DOWNLOAD_ATTEMPTS=3
	YTDLP_SOCKET_TIMEOUT=15
	YTDLP_RETRIES=2
	YTDLP_USER_AGENT=Mozilla/5.0 ...
	```

	Notes:
	- INSTAGRAM_COOKIES_FROM_BROWSER is optional. Use chrome, edge, firefox, etc.
	- Do not commit .env.

5. Run the bot.

	```powershell
	python bot.py
	```

6. Keep yt-dlp updated.

	```powershell
	python -m pip install -U yt-dlp
	```

## How It Works

- Event flow:
	- on_message detects one or more Instagram links.
	- Each URL is normalized, deduplicated, and processed safely.
	- yt-dlp tries multiple quality profiles with retries/fallbacks.
	- The bot checks Discord upload size limits before sending.
	- On success, file is uploaded and local file is deleted.
	- On failure, a clean user-facing error message is sent.

## Files

- bot.py: Discord bot implementation.
- requirements.txt: Python dependencies.
- .gitignore: ignores secrets and local artifacts.
- .env: local secret token file (never commit).

## GitHub

Push this project to a private repository.

```powershell
git add .
git commit -m "Initial Discord Instagram notifier bot"
git push origin main
```

## Render Deployment

Deploy as a Background Worker (not a web service).

- Build Command: pip install -r requirements.txt
- Start Command: python bot.py
- Environment Variables:
	- DISCORD_TOKEN=your_actual_token_here
	- INSTAGRAM_COOKIES_FROM_BROWSER=chrome (optional)
	- DOWNLOAD_TIMEOUT_SECONDS=180 (optional)
	- MAX_DOWNLOAD_ATTEMPTS=3 (optional)
	- YTDLP_SOCKET_TIMEOUT=15 (optional)
	- YTDLP_RETRIES=2 (optional)

This keeps the bot running continuously.

## Railway / VPS Notes

- Railway:
	- Use a worker/background process with start command python bot.py.
	- Add the same environment variables used for Render.

- VPS:
	- Install Python, ffmpeg, and project dependencies.
	- Run bot with a process manager (systemd, pm2, supervisor) for auto-restart.

## Troubleshooting

- Instagram extraction errors:
	- Update yt-dlp.
	- Try direct reel/post links.
	- Configure INSTAGRAM_COOKIES_FROM_BROWSER.

- Upload failed due to size:
	- Bot already attempts lower-quality fallbacks.
	- If still too large, use shorter or lower-resolution media.

- Message detection not working:
	- Ensure Message Content Intent is enabled in Discord Developer Portal.
	- Ensure bot has Send Messages and Attach Files permission.