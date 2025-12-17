# Kennyy 🎶 - your Discord music bot :D

<p align="center">
   <a href="https://discord.com/api/oauth2/authorize?client_id=920133124095098881&permissions=2150648832&scope=bot%20applications.commands">
      <img src="https://img.shields.io/badge/Add%20Kenny%20to%20your%20server-5865F2?style=for-the-badge&logo=discord&logoColor=white" alt="Add Kenny to your server">
   </a>
</p>

Kennyy is a 'opun sosu' multilingual Discord music bot built with `discord.py`, Wavelink, and Lavalink. It delivers reliable playback and a polished slash-command experience with advanced features and extensive language support.

## Feature Highlights
- **Seamless music playback** with queue controls, filters, search, and LRCLIB-powered lyrics (including live sync when timed data is available).
- **Multi-node failover system** supporting up to 10 Lavalink nodes with intelligent health monitoring and automatic failover.
- **Advanced node protection** that preserves active playback sessions during health checks and failover events.
- **Autocomplete search** with aggressive timeouts to avoid Discord "Unknown interaction" errors.
- **Automatic channel-status** presence updates, lonely-listener auto-pause, and configurable loop/shuffle modes.
- **Persistent guild preferences** for language and bot presence stored in MongoDB.
- **Comprehensive logging system** with localized log embeds sent to a dedicated channel, including guild join/leave, music start, and error tracking.
- **Localized responses** in 10 languages: Portuguese (Brazil), Portuguese (Portugal), English, Spanish, French, Italian, Japanese, Turkish, and Russian.
- **Owner-only admin suite** with remote presence management and log control.
- **Advanced audio filters** including bass boost with 3 intensity levels, nightcore, karaoke, and 8D audio effects.
- **Optional proxy support** for SOCKS5/HTTP proxies (useful for Cloudflare WARP on VPS environments).

## Project Structure
```
├── index.py              # Bot entrypoint, event handlers, MongoDB + Lavalink bootstrap
├── commands/             # Slash command cogs (play, queue, search, filters, admin, logger, etc.)
│   ├── play.py          # Main playback command with volume, skip, pause, stop controls
│   ├── queue.py         # Queue management (view, skipto, clear, shuffle, remove)
│   ├── search.py        # Interactive search with dropdown selection
│   ├── filter.py        # Audio filters (bass boost, nightcore, karaoke, 8D)
│   ├── admin.py         # Owner-only controls (presence, status, log management)
│   ├── logger.py        # Localized logging system for guild events and errors
│   ├── language.py      # Guild language preference management
│   ├── lyrics.py        # Lavalink-powered lyrics with live synchronization
│   └── shared_player.py # Shared player state and utilities
├── locales/              # Translation dictionaries (pt, pt-pt, en, es, fr, it, ja, tr, ru)
├── data/presence.json    # Legacy presence fallback (MongoDB preferred)
├── requirements.txt      # Python dependencies
└── README.md             # You are here
```

## Requirements
- Python 3.12 or newer
- Lavalink server (Java 17+, configured nodes via environment variables)
- MongoDB Atlas or self-hosted MongoDB (SRV connection string recommended)
- Discord application with a bot token and slash-command privileges

## Setup
1. **Clone the repository** and change into the project folder.
2. **Create a virtual environment** (optional but recommended):
   ```powershell
   py -3.12 -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```
3. **Install dependencies**:
   ```powershell
   python -m pip install -r requirements.txt
   ```
4. **Configure environment variables**:
   - Copy `.env.example` to `.env`.
   - Replace placeholder credentials with your own values:
     ```dotenv
     # Discord Bot Configuration
     DISCORD_TOKEN=your_bot_token_here
     BOT_OWNER_IDS=123456789012345678,987654321098765432  # Comma-separated user IDs for bot owners
     
     # MongoDB Configuration
     MONGODB_URI=mongodb+srv://user:password@cluster.mongodb.net/kenny?retryWrites=true&w=majority
     
     # Logging (Optional)
     LOG_CHANNEL_ID=1234567890123456  # Discord channel ID for bot event logs
     
     # Lavalink Node 1 (Required)
     LAVALINK_NODE1_HOST=lava.example.com
     LAVALINK_NODE1_NAME=Primary
     LAVALINK_NODE1_PORT=443
     LAVALINK_NODE1_PASSWORD=youshallnotpass
     LAVALINK_NODE1_SECURE=true
     
     # Lavalink Node 2 (Optional - for failover)
     LAVALINK_NODE2_HOST=backup.example.com
     LAVALINK_NODE2_NAME=Backup
     LAVALINK_NODE2_PORT=2333
     LAVALINK_NODE2_PASSWORD=anotherpassword
     LAVALINK_NODE2_SECURE=false
     
     # Additional nodes 3-10 follow the same pattern
     # LAVALINK_NODE3_HOST=...
     # LAVALINK_NODE3_NAME=...
     # LAVALINK_NODE3_PORT=...
     # LAVALINK_NODE3_PASSWORD=...
     # LAVALINK_NODE3_SECURE=true/false
     ```
   - **Multi-node support**: Configure up to 10 nodes (`LAVALINK_NODE1_*` through `LAVALINK_NODE10_*`) for redundancy and load balancing.
   - **Required variables**: `DISCORD_TOKEN`, `MONGODB_URI`, `BOT_OWNER_IDS`, and at least one Lavalink node configuration.
   - **Optional variables**: `LOG_CHANNEL_ID` enables the logging system to send localized embeds for guild events and errors.
5. **Launch Lavalink** with the same host/password definition used above.
6. **Run the bot**:
   ```bash
   # Normal startup
   python index.py
   
   # With proxy (WARP/SOCKS5/HTTP)
   python index.py --proxy socks5://127.0.0.1:40000
   
   # View available options
   python index.py --help
   ```
   
## Slash Command Overview
| Command | Description | Notable Details |
| --- | --- | --- |
| `/play` | Queue tracks or playlists with autocomplete. | Rebuilds player if Lavalink reconnects, provides now-playing embeds with interactive controls.
| `/queue` | Display and control the queue. | Interactive buttons for skip, shuffle, loop, and jump-to-track.
| `/search` | Interactive track search via dropdown. | Cancellable view prevents stale interactions.
| `/filter` | Apply audio filters. | Bass boost levels, nightcore, karaoke, 8D, reset - all with interactive buttons.
| `/volume` | Adjust playback volume. | Range: 0-150%, includes interactive +/- buttons.
| `/seek` | Jump to a specific timestamp. | Supports time formats like `1:30` or `90`.
| `/skipto` | Jump to a specific position in queue. | Skips all tracks between current and target.
| `/clearqueue` | Remove upcoming tracks. | Leaves the current track playing.
| `/lyrics` | Show lyrics for the current track. | Pulls from Lavalink LavaLyrics; auto-syncs timed lines. Enable `lrcLib` (or other providers) for best results.
| `/language` | Set guild language. | Restricted to administrators; persisted in MongoDB. Supports 10 languages.
| `/ping` | Health diagnostics. | Reports Discord latency plus per-node Lavalink status.
| `/admin` | Owner-only controls. | Manage presence, status, and logging system (enable/disable/status).
| `/help` | Display available commands. | Localized command list.

## Localization Workflow
- Guild language preferences are stored in collection `guild_languages` (document: `{guild_id: int, language: str}`).
- Translation strings live in `locales/*.json`; keys mirror command paths (e.g., `commands.play.embed.title`).
- **Supported languages (10 total)**:
  - 🇧🇷 Portuguese (Brazil) - `pt.json`
  - 🇵🇹 Portuguese (Portugal) - `pt-pt.json`
  - 🇬🇧 English - `en.json`
  - 🇪🇸 Spanish - `es.json`
  - 🇫🇷 French - `fr.json`
  - 🇮🇹 Italian - `it.json`
  - 🇯🇵 Japanese - `ja.json`
  - 🇹🇷 Turkish - `tr.json`
  - 🇷🇺 Russian - `ru.json`
- To add new languages, duplicate an existing JSON file, translate values, and ensure the filename matches the locale code.
- The translation system includes fallback support - if a key is missing in a locale, it falls back to the default text provided in the code.

## MongoDB Details
- Connection string must include the target database in the path (e.g., `/kenny`).
- Collections created automatically:
  - `guild_languages` — one document per guild storing language preference.
  - `bot_presence` — single document storing status/activity payload.
  - `logs_collection` — global log configuration (enabled/disabled state).
- The bot validates the  at startup with a `ping` and falls back to default messages if the connection fails.
- All guild-specific settings (language) and global settings (presence, logs) are persisted in MongoDB.

## Lavalink Failover & Health Monitoring
- **Multi-node support**: Configure up to 10 nodes via `LAVALINK_NODE{n}_*` environment variables.
- **Intelligent health checks**: Automated monitoring every 30 seconds with 8-second timeout using `/stats` endpoint.
- **Active playback protection**: Nodes with active players are never forcibly disconnected during health checks.
- **Automatic failover**: When a node fails, the bot immediately switches to the next available node without interrupting playback.
- **Node blacklisting**: Failed nodes are temporarily blacklisted for 2 minutes to prevent reconnection loops.
- **Load balancing**: New players are assigned to the node with the least active players.
- `connect_lavalink` ensures dead sessions are closed and nodes reconnect gracefully.
- Autocomplete and playback functions automatically promote to the next available node.

## Proxy Support
The bot supports optional SOCKS5 and HTTP proxies, useful for VPS environments with Cloudflare WARP or other proxy services.

### Usage
```bash
# Start with SOCKS5 proxy (Cloudflare WARP)
python index.py --proxy socks5://127.0.0.1:40000

# Start with HTTP proxy
python index.py --proxy http://proxy.example.com:8080

# Normal startup (no proxy)
python index.py
```

### Notes
- Proxy affects Discord connections (WebSocket Gateway, REST API) and bot HTTP requests.
- Proxy does NOT affect Lavalink server connections (configure separately in Lavalink's `application.yml` if needed).
- Commonly used with Cloudflare WARP on Linux VPS to avoid regional restrictions.

## Live Lyrics
- The `/lyrics` command now requests lyrics exclusively from the LRCLIB API and renders them in sync when timestamps are present.
- For the most stable experience keep Spotify and YouTube lyric providers enabled in `application.yml` under `plugins.lavasrc.lyrics-sources`.
- Optional providers (Deezer, Yandex, LRCLib, etc.) can supplement coverage; configure their required tokens or cookies before enabling them.
- The bot highlights the current line in the embed while the track is playing and reverts to the full lyrics once playback stops or the task finishes.

## Quality-of-Life Features
- **Lonely channel detection**: pauses playback after 2 minutes alone and disconnects if nobody returns.
- **Rich console dashboard** using `rich` for real-time logging, Lavalink status, and node uptime/downtime tracking.
- **Embedded now-playing card** is deduplicated to avoid spam when tracks change or skip.
- **Comprehensive logging system**: localized log embeds for guild join/leave, music start, and errors sent to configured channel.
- **Optimized logging**: Minimal console output with only critical information (no verbose health check spam).
- **Error tracking**: all playback errors, Lavalink failures, and command errors are logged with full context.
- **Interactive controls**: all playback commands include button-based controls for easy interaction.
- **Auto-pause on empty channel**: bot automatically pauses when alone and resumes when someone joins.
- **Fallback search**: automatically switches to alternative sources when primary fails.
- **Bass boost levels**: Choose between low, medium, and high intensity bass boost via interactive buttons.
- **Node status monitoring**: Real-time uptime/downtime tracking displayed in console panel.

## Development Tips
- Run `python -m compileall index.py` or `ruff`/`black` if you add linters to ensure syntax and formatting.
- Avoid hardcoding user-facing strings; use the translation helper (`bot.translate`).
- Add new commands under `commands/` and expose them via `async def setup(bot)`.
- When modifying locale files, validate JSON formatting with `python -m json.tool locales/en.json`.

## Troubleshooting
- **Bot stays silent**: Verify Lavalink is reachable (check `/ping` and console output) and the node password matches.
- **Slash commands missing**: Ensure the bot has `applications.commands` scope and that sync logs show success on startup.
- **MongoDB errors**: Confirm the URI includes credentials and database name; Atlas requires IP allow-listing.
- **Lyrics not found**: Ensure at least one LavaLyrics provider is enabled (e.g., Spotify or YouTube). Some sources require valid credentials/cookies.
- **Logs not appearing**: Set `LOG_CHANNEL_ID` in `.env` and use `/admin logs status` to verify the system is enabled.
- **Translation issues**: Ensure the locale file exists in `locales/` directory and is valid JSON. Check console for loading errors.
- **Player not responding**: Verify Lavalink connection is active with `/ping` and check for error logs in the configured log channel.
- **Node keeps disconnecting**: Check health check logs (30s interval, 8s timeout). Nodes with high latency may need timeout adjustment in code.
- **Infinite reconnection loop**: Fixed in latest version. Nodes are now correctly identified during failover and blacklisted for 2 minutes.
- **Music stops when node goes offline**: Nodes with active players are protected and won't be forcibly disconnected during health checks.
- **Proxy not working**: Ensure the proxy service is running and accessible. Test with `curl --proxy <proxy_url> https://discord.com`.

## WARP auto-reconnect (Linux only)
Some YouTube IP blocks cause Lavalink to return `fault` with message `Something broke when playing the track.`. The bot can auto-run Cloudflare WARP to rotate IPs and retry the track after 5s. This only works on Linux hosts where `warp-cli` is available.

### Install WARP
```bash
sudo apt update
sudo apt install cloudflare-warp -y
sudo warp-cli --accept-tos register
sudo warp-cli --accept-tos set-mode warp
sudo warp-cli --accept-tos connect
```

### Permissions for the reconnect script
- The bot runs `warp-cli --accept-tos disconnect` ➜ `sleep 1` ➜ `warp-cli --accept-tos connect`.
- Logs are written to `/var/log/warp-reconnect.log`; if unwritable, it falls back to `/tmp/warp-reconnect.log`.
- Make `/var/log/warp-reconnect.log` writable by the bot user (safer than sudoers):
   ```bash
   sudo touch /var/log/warp-reconnect.log
   sudo chown <bot_user>:<bot_group> /var/log/warp-reconnect.log
   sudo chmod 640 /var/log/warp-reconnect.log
   ```
- If `warp-cli` requires root, either run the bot as root (not recommended) or add a sudoers exception for `/usr/bin/warp-cli`.

### How it behaves
1) On the specific Lavalink fault, the bot logs the error, fires the WARP reconnect script, sends a localized embed “Reconectando o áudio…”, waits 5s, and retries the same track.
2) If the retry fails, normal fallback/queue-finish logic resumes.
3) Works only on Linux; no-op on Windows/macOS.

## Contributing
Pull requests are welcome! Please accompany significant features with translation updates, add or update tests if applicable.
---

## Translations
- Portuguese (Brazil & Portugal): konnyoung
- Turkish: Twixier
- Other languages: Community contributions welcome!

Made with ❤️ for the Discord community. Enjoy the music!







