# Ryu_insta_notify

Discord bot that watches messages for Instagram links, downloads the video with yt-dlp, posts the video back to the channel, and deletes the local file afterward.

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

3. Add your bot token to .env.

	```env
	DISCORD_TOKEN=your_actual_token_here
	```

4. Run the bot.

	```powershell
	python bot.py
	```

## How It Works

- The bot listens to every message.
- If it finds a URL containing instagram.com, it downloads the media with yt-dlp.
- The downloaded file is sent back to the same channel.
- The local file is deleted immediately after sending.

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
- Environment Variable: DISCORD_TOKEN=your_actual_token_here

This keeps the bot running continuously.