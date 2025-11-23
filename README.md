# Kennyy 🎶 - your Discord music bot :D

<p align="center">
   <a href="https://discord.com/api/oauth2/authorize?client_id=920133124095098881&permissions=2150648832&scope=bot%20applications.commands">
      <img src="https://img.shields.io/badge/Add%20Kenny%20to%20your%20server-5865F2?style=for-the-badge&logo=discord&logoColor=white" alt="Add Kenny to your server">
   </a>
</p>

Kennyy is a 'opun sosu' multilingual Discord music bot built with `discord.py`, Wavelink, and Lavalink. It delivers reliable playback and a polished slash-command experience with advanced features and extensive language support.

## Feature Highlights
- **Seamless music playback** with queue controls, filters, search, and LRCLIB-powered lyrics (including live sync when timed data is available).
- **Autocomplete search** with aggressive timeouts to avoid Discord "Unknown interaction" errors.
- **Automatic channel-status** presence updates, lonely-listener auto-pause, and configurable loop/shuffle modes.
- **Persistent guild preferences** for language and bot presence stored in MongoDB.
- **Comprehensive logging system** with localized log embeds sent to a dedicated channel, including guild join/leave, music start, and error tracking.
- **Fully translated responses** in 6 languages (Português BR/PT, English, Français, 日本語, Türkçe) with JSON-backed localization.
- **Owner-only admin suite** with remote presence management and log control.
- **Advanced audio filters** including bass boost, nightcore, karaoke, and 8D audio effects.

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
├── locales/              # Translation dictionaries (pt, pt-pt, en, fr, ja, tr)
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
     DISCORD_TOKEN=your_bot_token
     MONGODB_URI=mongodb+srv://user:pass@host/kenny?retryWrites=true&w=majority
     LOG_CHANNEL_ID=1234567890  # Optional: Discord channel ID for bot logs
     LAVALINK_NODE1_HOST=127.0.0.1
     LAVALINK_NODE1_PORT=2333
     LAVALINK_NODE1_PASSWORD=youshallnotpass
     LAVALINK_NODE1_SECURE=false
     ```
   - Additional nodes (`LAVALINK_NODE2_*`, `LAVALINK_NODE3_*`) are optional but recommended for redundancy.
   - `LOG_CHANNEL_ID` enables the logging system to send localized embeds for guild events and errors.
5. **Launch Lavalink** with the same host/password definition used above.
6. **Run the bot**:
   ```powershell
   python index.py
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
| `/language` | Set guild language. | Restricted to administrators; persisted in MongoDB. Supports 6 languages.
| `/ping` | Health diagnostics. | Reports Discord latency plus per-node Lavalink status.
| `/admin` | Owner-only controls. | Manage presence, status, and logging system (enable/disable/status).
| `/help` | Display available commands. | Localized command list.

## Localization Workflow
- Guild language preferences are stored in collection `guild_languages` (document: `{guild_id: int, language: str}`).
- Translation strings live in `locales/*.json`; keys mirror command paths (e.g., `commands.play.embed.title`).
- **Supported languages**: Portuguese (Brazil), Portuguese (Portugal), English, French, Japanese, Turkish (Thanks Twixier for translating ❤).
- To add new languages, duplicate an existing JSON file, translate values, and ensure the filename matches the locale code (e.g., `es.json` for Spanish).
- The translation system includes fallback support - if a key is missing in a locale, it falls back to the default text provided in the code.

## MongoDB Details
- Connection string must include the target database in the path (e.g., `/kenny`).
- Collections created automatically:
  - `guild_languages` — one document per guild storing language preference.
  - `bot_presence` — single document storing status/activity payload.
  - `logs_collection` — global log configuration (enabled/disabled state).
- The bot validates the  at startup with a `ping` and falls back to default messages if the connection fails.
- All guild-specific settings (language) and global settings (presence, logs) are persisted in MongoDB.

## Lavalink Failover
- Configure up to three nodes via `LAVALINK_NODE{n}_*` variables.
- `connect_lavalink` ensures dead sessions are closed and nodes reconnect gracefully.
- Autocomplete and playback functions automatically promote to the next available node.

## Live Lyrics
- The `/lyrics` command now requests lyrics exclusively from the LRCLIB API and renders them in sync when timestamps are present.
- For the most stable experience keep Spotify and YouTube lyric providers enabled in `application.yml` under `plugins.lavasrc.lyrics-sources`.
- Optional providers (Deezer, Yandex, LRCLib, etc.) can supplement coverage; configure their required tokens or cookies before enabling them.
- The bot highlights the current line in the embed while the track is playing and reverts to the full lyrics once playback stops or the task finishes.

## Quality-of-Life Features
- **Lonely channel detection**: pauses playback after 2 minutes alone and disconnects if nobody returns.
- **Rich console dashboard** using `rich` for real-time logging and Lavalink status.
- **Embedded now-playing card** is deduplicated to avoid spam when tracks change or skip.
- **Comprehensive logging system**: localized log embeds for guild join/leave, music start, and errors sent to configured channel.
- **Error tracking**: all playback errors, Lavalink failures, and command errors are logged with full context.
- **Interactive controls**: all playback commands include button-based controls for easy interaction.
- **Auto-pause on empty channel**: bot automatically pauses when alone and resumes when someone joins.
- **Fallback search**: automatically switches to alternative sources when primary fails.

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

## Contributing
Pull requests are welcome! Please accompany significant features with translation updates, add or update tests if applicable.
---

## Translations
- Portuguese (BR): konnyoung
- Turkish: Twixier

Made with ❤️ for the Discord community. Enjoy the music!






