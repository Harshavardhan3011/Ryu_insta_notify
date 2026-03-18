# Ryu_insta_notify

Discord bot that supports manual Instagram media relay and dynamic profile monitoring with Discord notifications.

## Features

- **Cookie-Enhanced Hybrid Fetch System**: 4-tier fallback strategy for maximum reliability
  - Tier 1: yt-dlp with browser cookies (logged-in access)
  - Tier 2: yt-dlp without cookies (public access)
  - Tier 3: HTML scraping fallback
  - Tier 4: Fail-safe (skip cycle gracefully)
- Detects Instagram links in normal messages.
- Supports the slash command /insta.
- Supports dynamic monitor setup with /setnotify.
- Supports /removenotify and /listnotify.
- Resolves share/profile-style links to direct media links when possible.
- Retries downloads with fallback quality profiles.
- Adapts to ffmpeg availability at runtime.
- Avoids re-processing duplicate URLs in a short window.
- Checks Discord upload size limits before upload.
- Cleans up files in all success/failure paths.
- Background monitor loop runs every 5 minutes.
- Tracks multiple Instagram profiles from insta_config.json.
- Sends @everyone notification to each profile's configured channel when a new post is detected.
- Comprehensive logging showing which fetch method succeeded.

## Authentication: Browser Cookies Setup

To enable cookie-based authentication for more reliable Instagram access:

### Option A: Automatic (Recommended)

1. **Browser Setup**:
   - Open Chrome with the Instagram account you want to monitor
   - Log in to Instagram
   - Keep Chrome running (cookies need to be accessible)

2. **Bot Configuration**:
   - Set `USE_COOKIES=true` in .env
   - Set `COOKIE_BROWSER=chrome` in .env (supports: chrome, edge, firefox, safari)

3. **Run Bot**:
   ```powershell
   python bot.py
   ```
   The bot will automatically use Chrome's stored cookies for Instagram authentication.

### Option B: Disable Cookies (Fallback Only)

If you didn't log in or don't want to use cookies:

```env
USE_COOKIES=false
```

The bot will still work using the 3-tier public fallback system (yt-dlp only → HTML scrape → error).

### Important Notes:

- Cookies provide authenticated access, allowing the bot to monitor private profiles (if you have access)
- Cookies need to stay current; refresh your browser login periodically if monitoring stops
- If cookies expire, the bot automatically falls back to yt-dlp and scraping
- No passwords are stored; only browser cookies are used

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

	This also installs requests for fallback HTML scraping.

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
	# Required
	DISCORD_TOKEN=your_actual_token_here
	
	# Optional: Cookie-based authentication
	USE_COOKIES=true
	COOKIE_BROWSER=chrome
	
	# Optional: Monitoring behavior
	MONITOR_INTERVAL_MINUTES=5
	AUTO_UPLOAD_NEW_POST_MEDIA=true
	
	# Optional: Performance tuning
	DOWNLOAD_TIMEOUT_SECONDS=180
	MAX_DOWNLOAD_ATTEMPTS=3
	YTDLP_SOCKET_TIMEOUT=15
	YTDLP_RETRIES=2
	YTDLP_USER_AGENT=Mozilla/5.0 ...
	
	# Optional: Fast command sync for development
	GUILD_ID=
	```

	Notes:
	- USE_COOKIES: Enable browser cookie authentication (default: true)
	- COOKIE_BROWSER: Browser to extract cookies from (default: chrome, options: edge, firefox, safari)
	- GUILD_ID: Optional server ID for fast command sync during development
	- Do not commit .env.
	- On first profile setup, latest post id is stored in insta_config.json without sending notification.

5. Run the bot.

	```powershell
	python bot.py
	```

6. Keep yt-dlp updated.

	```powershell
	python -m pip install -U yt-dlp
	```

## How It Works

### Fetch Strategy (4-Tier Hybrid System)

The bot uses an intelligent fallback system for maximum reliability:

**Tier 1: yt-dlp with Browser Cookies** (if USE_COOKIES=true)
- Uses Chrome cookies for authenticated Instagram access
- Best for monitoring private profiles
- Falls back if cookies are expired or not available

**Tier 2: yt-dlp without Cookies**
- Public-only access to Instagram profiles
- Works for all public profiles and doesn't require cookies
- Falls back if yt-dlp extraction fails

**Tier 3: HTML Scraping Fallback**
- Direct HTML fetch with regex extraction
- Multi-pattern extraction (code, shortcode, media ID)
- Works even when yt-dlp is blocked

**Tier 4: Fail-Safe**
- Skips profile for current cycle
- Retries next cycle
- Never crashes the bot

### Event Flow

- on_message detects one or more Instagram links.
- Each URL is normalized, deduplicated, and processed safely.
- yt-dlp tries multiple quality profiles with retries/fallbacks.
- The bot checks Discord upload size limits before sending.
- On success, file is uploaded and local file is deleted.

### Monitoring Flow (Every 5 Minutes)

- Load all configured profiles from insta_config.json.
- For each profile:
  - **Fetch latest post** using 4-tier hybrid system (see above)
  - **Compare** latest post ID with stored last_post_id
  - **On change**: send @everyone notification with post URL in configured channel
  - **Optionally** download and upload media
  - **Persist** new ID in insta_config.json
- If profile was newly added, initial post ID is stored and no notification is sent.
- On any failure, profile is skipped for the cycle and retried next cycle.

## Slash Commands

- /insta url:https://www.instagram.com/reel/ABC123/
	- Download an Instagram post/reel and upload it to the current channel.
	- Validates the URL contains instagram.com.
	- Automatically handles quality fallbacks.
	- Cleans up downloaded files after upload.

- /notifyall url:https://www.instagram.com/reel/ABC123/
	- Send an Instagram post notification to everyone in the channel.
	- Sends immediate ephemeral acknowledgment (only you see it).
	- Posts public notification with @everyone mention + post link.
	- Optional: add `download:True` to download and upload the video.
	- Example with download: `/notifyall url:https://www.instagram.com/reel/ABC123/ download:True`
	- Useful for instant announcements of Instagram posts to the team.

- /setnotify channel:#channel url:https://www.instagram.com/dragon__up/
	- Validates Instagram profile URL.
	- Extracts username and normalizes URL.
	- Adds or updates profile notification config.
	- On first add, stores latest post id without sending a notification.

- /removenotify url:https://www.instagram.com/dragon__up/
	- Removes profile from monitoring.

- /listnotify
	- Lists all currently configured profiles and target channels.

## Files

- bot.py: Discord bot implementation.
- insta_config.json: persisted profile monitor configuration.
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
	- USE_COOKIES=true (optional, enable browser cookies)
	- COOKIE_BROWSER=chrome (optional, default chrome)
	- DOWNLOAD_TIMEOUT_SECONDS=180 (optional)
	- MAX_DOWNLOAD_ATTEMPTS=3 (optional)
	- YTDLP_SOCKET_TIMEOUT=15 (optional)
	- YTDLP_RETRIES=2 (optional)

**Note**: On Render, if USE_COOKIES=true but Chrome is not available, the bot will automatically fall back to Tier 2 (yt-dlp without cookies) and continue working.

This keeps the bot running continuously.

## Railway / VPS Notes

- Railway:
	- Use a worker/background process with start command python bot.py.
	- Add the same environment variables used for Render.

- VPS:
	- Install Python, ffmpeg, and project dependencies.
	- Run bot with a process manager (systemd, pm2, supervisor) for auto-restart.

## Troubleshooting

### Cookie Authentication Issues

- **Cookies not working**:
  - Ensure you're logged into Instagram in Chrome
  - Keep Chrome running while the bot operates
  - Try refreshing your login if cookies become stale
  - Check that COOKIE_BROWSER matches your browser (chrome, edge, firefox, safari)

- **Cookie extraction permission errors**:
  - Chrome may require permission to access cookies
  - Try closing and reopening Chrome
  - On Linux: Install `gnome-keyring` for cookie access
  - Disable cookies with USE_COOKIES=false as fallback

### Instagram Extraction Errors

- **Profile extraction still failing after adding cookies**:
  - Update yt-dlp: `yt-dlp -U`
  - Try accessing the profile in Chrome first
  - The bot will automatically fall back to scraping

- If scraping fails too:
  - Instagram may have changed HTML structure
  - Check yt-dlp GitHub for updates
  - File an issue if the problem persists

### Upload Failed Due to Size

- Bot already attempts lower-quality fallbacks.
- If still too large, use shorter or lower-resolution media.

### Message Detection Not Working

- Ensure Message Content Intent is enabled in Discord Developer Portal.
- Ensure bot has Send Messages and Attach Files permission.

### Monitor Notifications Not Sent

- Ensure profile was added via /setnotify.
- Ensure bot can mention everyone and send messages in target channel.
- Check terminal logs to see which fetch method failed

## External Setup Commands

A) Install ffmpeg on Windows and add to PATH:
- Download ffmpeg, extract to a folder (example: C:\\ffmpeg)
- Add C:\\ffmpeg\\bin to PATH
- Verify:

```powershell
ffmpeg -version
```

B) Update yt-dlp:

```powershell
yt-dlp -U
```

C) Install requests (if needed manually):

```powershell
pip install requests
```

D) Run bot:

```powershell
python bot.py
```