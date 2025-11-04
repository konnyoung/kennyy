import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import re
import wavelink
from wavelink.exceptions import LavalinkException, NodeException


class LyricsCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._sync_tasks: dict[int, asyncio.Task] = {}

    def _translate(self, interaction, key, default="Translation missing", **kwargs):
        return self.bot.translate(key, guild_id=interaction.guild_id, default=default, **kwargs)

    async def fetch_lyrics(self, player: wavelink.Player, track: wavelink.Playable) -> dict | None:
        return await self._fetch_from_lavalink(player, track)

    async def _fetch_from_lavalink(self, player: wavelink.Player, track: wavelink.Playable) -> dict | None:
        node = getattr(player, "node", None)
        if node is None:
            return None

        try:
            payload = await node.send(
                path="v4/lyrics",
                params={
                    "track": track.encoded,
                    "skipTrackSource": "true",
                },
            )
        except LavalinkException as exc:
            if exc.status != 404:
                print(f"Erro ao buscar letras via Lavalink: {exc}")
            return None
        except NodeException as exc:
            print(f"Falha de comunicação com Lavalink ao buscar letras: {exc}")
            return None
        except Exception as exc:  # noqa: BLE001
            print(f"Erro inesperado ao buscar letras via Lavalink: {exc}")
            return None

        if not isinstance(payload, dict):
            return None

        text = payload.get("text")
        raw_lines = payload.get("lines")
        aggregated_lines: list[str] = []
        timed_lines: list[dict[str, int | str | None]] = []

        if isinstance(raw_lines, list):
            for item in raw_lines:
                if not isinstance(item, dict):
                    continue

                line_text = item.get("line") or ""
                timestamp = item.get("timestamp")
                duration = item.get("duration")

                if line_text:
                    aggregated_lines.append(line_text)

                if isinstance(timestamp, (int, float)):
                    timed_lines.append(
                        {
                            "timestamp": int(timestamp),
                            "line": line_text,
                            "duration": int(duration) if isinstance(duration, (int, float)) else None,
                        }
                    )

        if not text and aggregated_lines:
            text = "\n".join(aggregated_lines)

        if timed_lines:
            timed_lines.sort(key=lambda entry: entry["timestamp"])

        cleaned = self._clean_lyrics_text(text) if text else None
        if cleaned is None and timed_lines:
            collected = [entry["line"].strip() for entry in timed_lines if entry.get("line")]
            cleaned = "\n".join(filter(None, collected)) or None

        if cleaned is None and not timed_lines:
            return None
        cleaned = cleaned or ""

        source_name = payload.get("sourceName") or "LavaLyrics"
        provider = payload.get("provider")
        source_label = f"{source_name} ({provider})" if provider else source_name

        return {
            "title": track.title,
            "artist": track.author,
            "lyrics": cleaned,
            "thumbnail": getattr(track, "artwork", None),
            "url": getattr(track, "uri", None),
            "source": source_label,
            "timed_lines": timed_lines,
        }

    def _clean_lyrics_text(self, text: str) -> str:
        normalized = text.replace('\r\n', '\n').replace('\r', '\n')
        normalized = re.sub(r'\n{3,}', '\n\n', normalized)
        lines = [line.rstrip() for line in normalized.split('\n')]
        cleaned_lines = [line for line in lines if line.strip()]
        return '\n'.join(cleaned_lines).strip()

    def _create_embed(
        self,
        interaction: discord.Interaction,
        track: wavelink.Playable,
        lyrics_data: dict,
        description: str,
    ) -> discord.Embed:
        description = description or self._translate(
            interaction,
            "commands.lyrics.embed.empty",
            default="Letra indisponível no momento."
        )
        if len(description) > 4000:
            description = description[:3997] + "..."

        embed_title = self._translate(
            interaction,
            "commands.lyrics.embed.title",
            default="🎵 {title}",
            title=lyrics_data.get("title") or track.title
        )

        embed = discord.Embed(
            title=embed_title,
            description=description,
            color=0xffff64,
            url=lyrics_data.get("url") or None
        )
        embed.set_author(
            name=lyrics_data.get("artist") or track.author,
            icon_url=lyrics_data.get("thumbnail") or None
        )
        embed.set_footer(
            text=self._translate(
                interaction,
                "commands.lyrics.embed.footer",
                default="Fonte: {source} • Música atual: {track}",
                track=track.title,
                source=lyrics_data.get("source", "?")
            )
        )
        return embed

    def _find_line_index(self, lines: list[dict], position_ms: int) -> int | None:
        if not lines:
            return None

        index: int | None = None
        for idx, entry in enumerate(lines):
            timestamp = entry.get("timestamp")
            if not isinstance(timestamp, int):
                continue

            if position_ms + 250 >= timestamp:
                index = idx
            else:
                break

        if index is None and isinstance(lines[0].get("timestamp"), int) and position_ms < lines[0]["timestamp"]:
            return 0

        return index

    def _render_timed_snippet(self, lines: list[dict], current_index: int | None, window: int = 2) -> str | None:
        if not lines:
            return None

        if current_index is None:
            current_index = 0

        current_index = max(0, min(current_index, len(lines) - 1))
        start = max(0, current_index - window)
        end = min(len(lines), current_index + window + 1)

        snippet_lines: list[str] = []
        for idx in range(start, end):
            line_text = (lines[idx].get("line") or "").strip()
            if not line_text:
                continue
            if idx == current_index:
                snippet_lines.append(f"**→ {line_text} ←**")
            else:
                snippet_lines.append(line_text)

        joined = "\n".join(snippet_lines).strip()
        return joined or None

    def _cancel_sync_task(self, guild_id: int) -> None:
        task = self._sync_tasks.pop(guild_id, None)
        if task:
            task.cancel()

    async def _sync_lyrics_task(
        self,
        interaction: discord.Interaction,
        message: discord.Message,
        player: wavelink.Player,
        track: wavelink.Playable,
        lyrics_data: dict,
    ) -> None:
        guild_id = interaction.guild_id
        timed_lines = [entry for entry in lyrics_data.get("timed_lines", []) if isinstance(entry, dict)]
        if not timed_lines:
            return

        last_index: int | None = None
        was_cancelled = False

        try:
            while True:
                current_track = getattr(player, "current", None)
                if not current_track or current_track.identifier != track.identifier:
                    break

                position_ms = getattr(player, "position", 0) or 0
                current_index = self._find_line_index(timed_lines, position_ms)

                if current_index is not None and current_index != last_index:
                    snippet = self._render_timed_snippet(timed_lines, current_index)
                    if snippet:
                        embed = self._create_embed(interaction, track, lyrics_data, snippet)
                        try:
                            await message.edit(embed=embed)
                        except discord.NotFound:
                            break
                        except discord.HTTPException as exc:
                            print(f"Não foi possível atualizar letra sincronizada: {exc}")
                        last_index = current_index

                await asyncio.sleep(1)
        except asyncio.CancelledError:
            was_cancelled = True
            raise
        finally:
            if guild_id is not None:
                self._sync_tasks.pop(guild_id, None)
            if not was_cancelled and lyrics_data.get("lyrics"):
                try:
                    embed = self._create_embed(interaction, track, lyrics_data, lyrics_data["lyrics"])
                    await message.edit(embed=embed)
                except (discord.NotFound, discord.HTTPException):
                    pass

    @app_commands.command(name="lyrics", description="Show the lyrics for the current track")
    async def lyrics(self, interaction: discord.Interaction):
        await interaction.response.defer()
        
        # Verifica se há música tocando
        if not interaction.guild:
            return await interaction.followup.send("❌ Este comando só funciona em servidores.", ephemeral=True)
        
        player: wavelink.Player = interaction.guild.voice_client
        
        if not player or not player.current:
            embed = discord.Embed(
                title=self._translate(
                    interaction,
                    "commands.lyrics.errors.no_track.title",
                    default="❌ Nenhuma música tocando"
                ),
                description=self._translate(
                    interaction,
                    "commands.lyrics.errors.no_track.description",
                    default="Não há música tocando no momento. Use /play para tocar algo primeiro!"
                ),
                color=0xff0000
            )
            return await interaction.followup.send(embed=embed)
        
        current_track = player.current

        lyrics_data = await self.fetch_lyrics(player, current_track)

        if not lyrics_data:
            embed = discord.Embed(
                title=self._translate(
                    interaction,
                    "commands.lyrics.errors.not_found.title",
                    default="❌ Não encontrado"
                ),
                description=self._translate(
                    interaction,
                    "commands.lyrics.errors.not_found.description",
                    default="Não consegui encontrar a letra para: **{query}**",
                    query=f"{current_track.title} - {current_track.author}"
                ),
                color=0xff0000
            )
            return await interaction.followup.send(embed=embed)
        
        timed_lines = [entry for entry in lyrics_data.get("timed_lines", []) if isinstance(entry, dict)]
        position_ms = getattr(player, "position", 0) or 0
        current_index = self._find_line_index(timed_lines, position_ms) if timed_lines else None
        snippet = self._render_timed_snippet(timed_lines, current_index) if timed_lines else None

        description = snippet or lyrics_data.get("lyrics") or self._translate(
            interaction,
            "commands.lyrics.embed.empty",
            default="Letra indisponível no momento."
        )

        embed = self._create_embed(interaction, current_track, lyrics_data, description)
        message = await interaction.followup.send(embed=embed)

        if timed_lines and interaction.guild_id is not None:
            self._cancel_sync_task(interaction.guild_id)
            task = self.bot.loop.create_task(
                self._sync_lyrics_task(
                    interaction=interaction,
                    message=message,
                    player=player,
                    track=current_track,
                    lyrics_data=lyrics_data,
                )
            )
            self._sync_tasks[interaction.guild_id] = task

    def cog_unload(self) -> None:
        for task in self._sync_tasks.values():
            task.cancel()
        self._sync_tasks.clear()


async def setup(bot):
    await bot.add_cog(LyricsCommands(bot))
