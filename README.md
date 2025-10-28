# Kenny Music Bot

Kenny is a multilingual Discord music bot built with `discord.py`, Wavelink, and Lavalink. It delivers reliable playback, a polished slash-command UX, and an optional Flask dashboard while keeping configuration and secrets in environment files.

## Feature Highlights
- Seamless music playback with queue controls, filters, search, and lyrics powered by Lavalink multi-node failover.
- Autocomplete search with aggressive timeouts to avoid Discord "Unknown interaction" errors.
- Automatic channel-status presence updates, lonely-listener auto-pause, and configurable loop/shuffle modes.
- Persistent guild language preferences and bot presence stored in MongoDB.
- Fully translated responses (Português BR/PT, English, Français) with JSON-backed localization.
- Owner-only admin command suite and optional Flask dashboard for remote presence management.

## Project Structure
```
├── index.py              # Bot entrypoint, event handlers, MongoDB + Lavalink bootstrap
├── commands/             # Slash command cogs (play, queue, search, filters, admin, etc.)
├── locales/              # Translation dictionaries (pt, pt-pt, en, fr)
├── web/dashboard.py      # Optional Flask dashboard (loads only if dependencies are present)
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
     LAVALINK_NODE1_HOST=127.0.0.1
     LAVALINK_NODE1_PORT=2333
     LAVALINK_NODE1_PASSWORD=youshallnotpass
     LAVALINK_NODE1_SECURE=false
     ```
   - Additional nodes (`LAVALINK_NODE2_*`, `LAVALINK_NODE3_*`) are optional but recommended for redundancy.
5. **Launch Lavalink** with the same host/password definition used above.
6. **Run the bot**:
   ```powershell
   python index.py
   ```
   
## Slash Command Overview
| Command | Description | Notable Details |
| --- | --- | --- |
| `/play` | Queue tracks or playlists with autocomplete. | Rebuilds player if Lavalink reconnects, provides now-playing embeds.
| `/queue` | Display and control the queue. | Interactive buttons for skip, shuffle, loop, and jump-to-track.
| `/search` | Interactive track search via dropdown. | Cancellable view prevents stale interactions.
| `/filter` | Apply audio filters. | Bass boost levels, nightcore, karaoke, 8D, reset.
| `/clearqueue` | Remove upcoming tracks. | Leaves the current track playing.
| `/lyrics` | Fetch lyrics for the current track. | Uses public APIs with fallback logic.
| `/language` | Set guild language. | Restricted to administrators; persisted in MongoDB.
| `/ping` | Health diagnostics. | Reports Discord latency plus per-node Lavalink status.
| `/admin` | Owner-only presence controls. | Persists status/activity in MongoDB.

## Localization Workflow
- Guild language preferences are stored in collection `guild_languages` (document: `{guild_id: int, language: str}`).
- Translation strings live in `locales/*.json`; keys mirror command paths (e.g., `commands.play.embed.title`).
- To add new languages, duplicate an existing JSON file, translate values, and ensure the filename matches the locale code.

## MongoDB Details
- Connection string must include the target database in the path (e.g., `/kenny`).
- Collections created automatically:
  - `guild_languages` — one document per guild.
  - `bot_presence` — single document storing status/activity payload.
- The bot validates the URI at startup with a `ping` and falls back to Portuguese messages if the connection fails.

## Lavalink Failover
- Configure up to three nodes via `LAVALINK_NODE{n}_*` variables.
- `connect_lavalink` ensures dead sessions are closed and nodes reconnect gracefully.
- Autocomplete and playback functions automatically promote to the next available node.

## Quality-of-Life Features
- Lonely channel detection: pauses playback after 2 minutes alone and disconnects if nobody returns.
- Rich console dashboard using `rich` for real-time logging and Lavalink status.
- Embedded now-playing card is deduplicated to avoid spam when tracks change or skip.

## Development Tips
- Run `python -m compileall index.py` or `ruff`/`black` if you add linters to ensure syntax and formatting.
- Avoid hardcoding user-facing strings; use the translation helper (`bot.translate`).
- Add new commands under `commands/` and expose them via `async def setup(bot)`.
- When modifying locale files, validate JSON formatting with `python -m json.tool locales/en.json`.

## Troubleshooting
- **Bot stays silent**: Verify Lavalink is reachable (check `/ping` and console output) and the node password matches.
- **Slash commands missing**: Ensure the bot has `applications.commands` scope and that sync logs show success on startup.
- **MongoDB errors**: Confirm the URI includes credentials and database name; Atlas requires IP allow-listing.
- **Lyrics not found**: The public APIs may rate-limit; try again later or implement a provider with personal credentials.

## Contributing
Pull requests are welcome! Please accompany significant features with translation updates, add or update tests if applicable.
---

Made with ❤️ for the Discord community. Enjoy the music!
