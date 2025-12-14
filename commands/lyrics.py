import asyncio
import re

import aiohttp
import discord
import wavelink
from discord import app_commands
from discord.ext import commands


LRCLIB_API_BASE = "https://lrclib.net/api"
_TIMESTAMP_REGEX = re.compile(r"\[(\d{1,2}):(\d{2})(?:\.(\d{1,3}))?\]")


class LyricsStopView(discord.ui.View):
    def __init__(self, lyrics_cog: 'LyricsCommands', guild_id: int, channel_id: int, interaction: discord.Interaction):
        super().__init__(timeout=300)  # 5 minutos de timeout
        self.lyrics_cog = lyrics_cog
        self.guild_id = guild_id
        self.channel_id = channel_id
        
        # Atualiza o label do botão baseado no locale do usuário
        self.stop_lyrics.label = lyrics_cog._translate(
            interaction,
            "commands.lyrics.stop_button",
            default="🛑 Parar"
        )

    @discord.ui.button(style=discord.ButtonStyle.secondary)
    async def stop_lyrics(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Verifica se o usuário tem permissão para parar (mesmo servidor)
        if interaction.guild_id != self.guild_id:
            await interaction.response.send_message(
                "❌ Você não pode parar as letras de outro servidor.", 
                ephemeral=True
            )
            return

        # Para a tarefa de sincronização
        self.lyrics_cog._cancel_sync_task(self.guild_id)
        
        # Remove o canal da lista de ativos
        if self.channel_id in self.lyrics_cog._active_lyrics_channels:
            self.lyrics_cog._active_lyrics_channels.pop(self.channel_id, None)

        # Apaga a embed de letras e mostra mensagem de confirmação
        try:
            await interaction.response.send_message(
                self.lyrics_cog._translate(
                    interaction,
                    "commands.lyrics.stopped",
                    default="⏹️ Sincronização de letras interrompida."
                ),
                ephemeral=True
            )
            # Apaga a mensagem original com as letras
            await interaction.message.delete()
        except (discord.NotFound, discord.HTTPException):
            pass

    async def on_timeout(self):
        # Remove automaticamente após timeout
        try:
            self.lyrics_cog._cancel_sync_task(self.guild_id)
        except Exception:
            pass
        if self.channel_id in self.lyrics_cog._active_lyrics_channels:
            self.lyrics_cog._active_lyrics_channels.pop(self.channel_id, None)


class LyricsCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._sync_tasks: dict[int, asyncio.Task] = {}
        self._active_lyrics_channels: dict[int, int] = {}  # channel_id -> guild_id

    def _translate(self, interaction, key, default="Translation missing", **kwargs):
        return self.bot.translate(key, guild_id=interaction.guild_id, default=default, **kwargs)
    
    def cleanup_guild_lyrics(self, guild_id: int) -> None:
        """Limpa letras ativas quando o bot desconecta do servidor."""
        self._cancel_sync_task(guild_id)

    async def fetch_lyrics(self, player: wavelink.Player, track: wavelink.Playable) -> dict | None:
        return await self._fetch_from_lrclib(track)

    async def _fetch_from_lrclib(self, track: wavelink.Playable) -> dict | None:
        if track is None:
            return None

        payload: dict | None = None

        isrc = self._extract_track_isrc(track)
        if isrc:
            payload = await self._lrclib_request("get", {"isrc": isrc})

        if payload is None:
            for params in self._build_lrclib_queries(track):
                results = await self._lrclib_request("search", params) or []
                if not isinstance(results, list):
                    continue
                payload = self._select_lrclib_result(results, track)
                if payload is not None:
                    break

        if not isinstance(payload, dict):
            return None

        timed_lines = self._parse_synced_lyrics(payload.get("syncedLyrics"), getattr(track, "length", None))

        text = payload.get("plainLyrics")
        cleaned = self._clean_lyrics_text(text) if text else None
        if cleaned is None and timed_lines:
            collected = [entry["line"].strip() for entry in timed_lines if entry.get("line")]
            cleaned = "\n".join(filter(None, collected)) or None

        if cleaned is None and not timed_lines:
            return None
        cleaned = cleaned or ""

        language = payload.get("language")
        source_hint = payload.get("syncedLyricsSource") or payload.get("plainLyricsSource") or "LRCLib"
        if language:
            source_label = f"{source_hint} [{language}]"
        else:
            source_label = str(source_hint)

        return {
            "title": payload.get("trackName") or track.title,
            "artist": payload.get("artistName") or track.author,
            "lyrics": cleaned,
            "thumbnail": getattr(track, "artwork", None),
            "url": getattr(track, "uri", None),
            "source": source_label,
            "timed_lines": timed_lines,
        }

    async def _lrclib_request(self, endpoint: str, params: dict[str, str | int]) -> dict | list | None:
        url = f"{LRCLIB_API_BASE}/{endpoint}"
        timeout = aiohttp.ClientTimeout(total=12)
        headers = {"User-Agent": "KennyMusicBot/1.0 (+https://github.com/)"}

        try:
            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                async with session.get(url, params=params) as response:
                    if response.status == 404:
                        return None
                    if response.status != 200:
                        body = await response.text()
                        print(f"LRCLib request failed ({response.status}): {body[:200]}")
                        return None
                    return await response.json(content_type=None)
        except asyncio.TimeoutError:
            print(f"Timeout ao consultar LRCLib em {endpoint} com {params}")
        except aiohttp.ClientError as exc:
            print(f"Falha HTTP ao consultar LRCLib: {exc}")
        except Exception as exc:  # noqa: BLE001
            print(f"Erro inesperado durante consulta ao LRCLib: {exc}")
        return None

    def _build_lrclib_queries(self, track: wavelink.Playable) -> list[dict[str, str | int]]:
        title = getattr(track, "title", "") or ""
        artist = getattr(track, "author", "") or ""
        duration_ms = getattr(track, "length", None)
        duration_seconds = int(round(duration_ms / 1000)) if isinstance(duration_ms, (int, float)) and duration_ms > 0 else None

        queries: list[dict[str, str | int]] = []
        seen: set[tuple[str, str, int | None]] = set()

        def add_query(track_name: str, artist_name: str) -> None:
            query_key = (track_name, artist_name, duration_seconds)
            if query_key in seen:
                return
            seen.add(query_key)
            payload: dict[str, str | int] = {
                "track_name": track_name,
                "artist_name": artist_name,
            }
            if duration_seconds:
                payload["duration"] = duration_seconds
            queries.append(payload)

        add_query(title.strip(), artist.strip())

        normalized_title = self._sanitize_metadata(title)
        normalized_artist = self._sanitize_metadata(artist)
        add_query(normalized_title, normalized_artist)

        alt_artist = self._strip_feature_credit(normalized_artist)
        if alt_artist != normalized_artist:
            add_query(normalized_title, alt_artist)

        if " - " in normalized_title:
            main_title = normalized_title.split(" - ", 1)[0].strip()
            add_query(main_title, alt_artist)

        add_query(normalized_title, "")
        return queries

    def _select_lrclib_result(self, results: list[dict], track: wavelink.Playable) -> dict | None:
        valid: list[dict] = []
        for entry in results:
            if not isinstance(entry, dict):
                continue
            if not entry.get("syncedLyrics") and not entry.get("plainLyrics"):
                continue
            valid.append(entry)

        if not valid:
            return None

        title_target = (getattr(track, "title", "") or "").casefold()
        artist_target = (getattr(track, "author", "") or "").casefold()
        duration_ms = getattr(track, "length", None)
        duration_target = int(round(duration_ms / 1000)) if isinstance(duration_ms, (int, float)) and duration_ms > 0 else None

        for entry in valid:
            entry_title = str(entry.get("trackName", "")).casefold()
            entry_artist = str(entry.get("artistName", "")).casefold()
            if entry_title == title_target and (not artist_target or artist_target in entry_artist or entry_artist in artist_target):
                return entry

        if duration_target is not None:
            valid.sort(key=lambda item: abs(duration_target - int(item.get("duration") or duration_target)))

        return valid[0]

    def _parse_synced_lyrics(self, synced: str | None, track_length_ms: int | None) -> list[dict[str, int | str | None]]:
        if not synced:
            return []

        timed_entries: list[dict[str, int | str | None]] = []
        for raw_line in synced.splitlines():
            matches = list(_TIMESTAMP_REGEX.finditer(raw_line))
            if not matches:
                continue

            lyric_text = _TIMESTAMP_REGEX.sub("", raw_line).strip()
            if not lyric_text:
                continue

            for match in matches:
                minutes = int(match.group(1))
                seconds = int(match.group(2))
                fraction = (match.group(3) or "0")[:3]
                fraction = fraction.ljust(3, "0")
                millis = int(fraction)

                timestamp = minutes * 60000 + seconds * 1000 + millis
                timed_entries.append({
                    "timestamp": timestamp,
                    "line": lyric_text,
                })

        timed_entries.sort(key=lambda item: item["timestamp"])

        for index, entry in enumerate(timed_entries):
            next_timestamp: int | None = None
            if index + 1 < len(timed_entries):
                next_timestamp = timed_entries[index + 1]["timestamp"]
            elif isinstance(track_length_ms, int) and track_length_ms > entry["timestamp"]:
                next_timestamp = track_length_ms

            if isinstance(next_timestamp, int):
                entry["duration"] = max(0, next_timestamp - entry["timestamp"])
            else:
                entry["duration"] = None

        return timed_entries

    def _extract_track_isrc(self, track: wavelink.Playable) -> str | None:
        isrc = getattr(track, "isrc", None)
        if isinstance(isrc, str) and isrc.strip():
            return isrc.strip()

        info = getattr(track, "info", None)
        if isinstance(info, dict):
            candidate = info.get("isrc") or info.get("isrcCode")
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        return None

    def _sanitize_metadata(self, value: str) -> str:
        if not value:
            return ""
        cleaned = re.sub(r"\s*\([^)]*\)", "", value)
        cleaned = re.sub(r"\s*\[[^]]*\]", "", cleaned)
        cleaned = re.sub(r"\s*\{[^}]*\}", "", cleaned)
        cleaned = cleaned.replace("–", "-")
        return cleaned.strip()

    def _strip_feature_credit(self, artist: str) -> str:
        if not artist:
            return ""
        parts = re.split(r"\s+(?:feat\.|featuring|ft\.)\s+", artist, maxsplit=1, flags=re.IGNORECASE)
        primary = parts[0].strip()
        return primary

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
        
        # Remove qualquer canal ativo deste servidor quando a tarefa é cancelada
        channels_to_remove = [
            channel_id for channel_id, stored_guild_id in self._active_lyrics_channels.items()
            if stored_guild_id == guild_id
        ]
        for channel_id in channels_to_remove:
            self._active_lyrics_channels.pop(channel_id, None)

    async def handle_lyrics_interaction(
        self,
        interaction: discord.Interaction,
        *,
        ephemeral: bool,
        player: wavelink.Player | None = None,
    ) -> None:
        guild = interaction.guild

        if guild is None:
            message = self._translate(
                interaction,
                "commands.lyrics.errors.guild_only",
                default="❌ Este comando só funciona em servidores.",
            )
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(message, ephemeral=True)
                else:
                    await interaction.response.send_message(message, ephemeral=True)
            except (discord.NotFound, discord.HTTPException):
                pass
            return

        # Verifica se já há letras ativas neste canal
        channel_id = interaction.channel_id
        if not ephemeral and channel_id in self._active_lyrics_channels:
            # Verifica se a tarefa de sync realmente existe e está rodando
            guild_id = self._active_lyrics_channels[channel_id]
            sync_task = self._sync_tasks.get(guild_id)

            # Se a "sessão" é do mesmo servidor, preferimos auto-recuperar:
            # cancela o sync anterior e permite reiniciar as letras no mesmo canal.
            if interaction.guild_id is not None and guild_id == interaction.guild_id:
                try:
                    self._cancel_sync_task(guild_id)
                except Exception:
                    pass
                self._active_lyrics_channels.pop(channel_id, None)
                sync_task = None

            # Se o bot nem está mais em voz, limpa estado preso
            try:
                guild = interaction.guild
                voice_client = getattr(guild, "voice_client", None) if guild else None
                if voice_client is None or not getattr(voice_client, "connected", False):
                    self._active_lyrics_channels.pop(channel_id, None)
                    self._cancel_sync_task(guild_id)
                    sync_task = None
            except Exception:
                pass
            
            # Se não há tarefa ou a tarefa já terminou, limpa o canal da lista
            if sync_task is None or sync_task.done():
                self._active_lyrics_channels.pop(channel_id, None)
            else:
                # Tarefa realmente existe e está ativa, mostra aviso
                warning_message = self._translate(
                    interaction,
                    "commands.lyrics.warnings.already_active",
                    default="⚠️ Já há letras sendo exibidas neste canal.",
                )
                try:
                    if interaction.response.is_done():
                        await interaction.followup.send(warning_message, ephemeral=True)
                    else:
                        await interaction.response.send_message(warning_message, ephemeral=True)
                except (discord.NotFound, discord.HTTPException):
                    pass
                return

        resolved_player: wavelink.Player | None = player or guild.voice_client
        current_track = getattr(resolved_player, "current", None)

        if resolved_player is None or current_track is None:
            embed = discord.Embed(
                title=self._translate(
                    interaction,
                    "commands.lyrics.errors.no_track.title",
                    default="❌ Nenhuma música tocando",
                ),
                description=self._translate(
                    interaction,
                    "commands.lyrics.errors.no_track.description",
                    default="Não há música tocando no momento. Use /play para tocar algo primeiro!",
                ),
                color=0xff0000,
            )
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(embed=embed, ephemeral=ephemeral)
                else:
                    await interaction.response.send_message(embed=embed, ephemeral=ephemeral)
            except (discord.NotFound, discord.HTTPException):
                pass
            return

        searching_embed = discord.Embed(
            title=self._translate(
                interaction,
                "commands.lyrics.searching.title",
                default="🎶 Procurando letra",
            ),
            description=self._translate(
                interaction,
                "commands.lyrics.searching.description",
                default="Procurando a letra de **{song}**...",
                song=current_track.title,
            ),
            color=0x5865F2,
        )
        searching_embed.set_footer(
            text=self._translate(
                interaction,
                "commands.lyrics.searching.footer",
                default="Isso pode demorar alguns segundos.",
            )
        )

        message: discord.Message | None = None
        try:
            if interaction.response.is_done():
                message = await interaction.followup.send(embed=searching_embed, ephemeral=ephemeral)
            else:
                await interaction.response.send_message(embed=searching_embed, ephemeral=ephemeral)
                message = await interaction.original_response()
        except (discord.NotFound, discord.HTTPException):
            message = None

        lyrics_data = await self.fetch_lyrics(resolved_player, current_track)

        guild_id = interaction.guild_id
        if guild_id is not None:
            self._cancel_sync_task(guild_id)

        if not lyrics_data:
            embed = discord.Embed(
                title=self._translate(
                    interaction,
                    "commands.lyrics.errors.not_found.title",
                    default="❌ Não encontrado",
                ),
                description=self._translate(
                    interaction,
                    "commands.lyrics.errors.not_found.description",
                    default="Não consegui encontrar a letra para: **{query}**",
                    query=f"{current_track.title} - {current_track.author}",
                ),
                color=0xff0000,
            )
            try:
                if message is not None:
                    await message.edit(embed=embed)
                elif interaction.response.is_done():
                    await interaction.followup.send(embed=embed, ephemeral=ephemeral)
                else:
                    await interaction.response.send_message(embed=embed, ephemeral=ephemeral)
            except (discord.NotFound, discord.HTTPException):
                pass
            return

        timed_lines = [entry for entry in lyrics_data.get("timed_lines", []) if isinstance(entry, dict)]
        position_ms = getattr(resolved_player, "position", 0) or 0
        current_index = self._find_line_index(timed_lines, position_ms) if timed_lines else None
        snippet = self._render_timed_snippet(timed_lines, current_index) if timed_lines else None

        description = snippet or lyrics_data.get("lyrics") or self._translate(
            interaction,
            "commands.lyrics.embed.empty",
            default="Letra indisponível no momento.",
        )

        final_embed = self._create_embed(interaction, current_track, lyrics_data, description)

        # Cria a view com botão de parar se há letras sincronizadas e não é ephemeral
        stop_view = None
        if timed_lines and guild_id is not None and not ephemeral:
            stop_view = LyricsStopView(self, guild_id, channel_id, interaction)

        try:
            if message is not None:
                await message.edit(embed=final_embed, view=stop_view)
            elif interaction.response.is_done():
                await interaction.followup.send(embed=final_embed, view=stop_view, ephemeral=ephemeral)
            else:
                await interaction.response.send_message(embed=final_embed, view=stop_view, ephemeral=ephemeral)
        except (discord.NotFound, discord.HTTPException):
            return

        # Marca o canal como ativo se não for ephemeral
        if not ephemeral:
            self._active_lyrics_channels[channel_id] = guild_id

        if timed_lines and guild_id is not None and message is not None:
            task = self.bot.loop.create_task(
                self._sync_lyrics_task(
                    interaction=interaction,
                    message=message,
                    player=resolved_player,
                    track=current_track,
                    lyrics_data=lyrics_data,
                    channel_id=channel_id,
                    ephemeral=ephemeral,
                )
            )
            self._sync_tasks[guild_id] = task

    async def _sync_lyrics_task(
        self,
        interaction: discord.Interaction,
        message: discord.Message,
        player: wavelink.Player,
        track: wavelink.Playable,
        lyrics_data: dict,
        channel_id: int,
        ephemeral: bool,
    ) -> None:
        guild_id = interaction.guild_id
        timed_lines = [entry for entry in lyrics_data.get("timed_lines", []) if isinstance(entry, dict)]
        if not timed_lines:
            return

        last_index: int | None = None
        consecutive_edit_failures = 0
        was_cancelled = False

        try:
            while True:
                current_track = getattr(player, "current", None)
                if not current_track or current_track.identifier != track.identifier:
                    break

                # Se o bot desconectou do canal de voz, encerra o sync
                if not getattr(player, "connected", True):
                    break

                # Se não está mais tocando (fila acabou/desconectou), encerra o sync
                if not getattr(player, "playing", True) and not getattr(player, "paused", False):
                    break

                position_ms = getattr(player, "position", 0) or 0
                current_index = self._find_line_index(timed_lines, position_ms)

                if current_index is not None and current_index != last_index:
                    snippet = self._render_timed_snippet(timed_lines, current_index)
                    if snippet:
                        embed = self._create_embed(interaction, track, lyrics_data, snippet)
                        try:
                            await message.edit(embed=embed)
                            consecutive_edit_failures = 0
                        except discord.NotFound:
                            break
                        except discord.HTTPException as exc:
                            print(f"Não foi possível atualizar letra sincronizada: {exc}")
                            consecutive_edit_failures += 1
                            if consecutive_edit_failures >= 3:
                                break
                        last_index = current_index

                await asyncio.sleep(1)
        except asyncio.CancelledError:
            was_cancelled = True
            raise
        finally:
            if guild_id is not None:
                self._sync_tasks.pop(guild_id, None)
            # Remove o canal da lista de ativos quando as letras terminam
            if not ephemeral and channel_id in self._active_lyrics_channels:
                self._active_lyrics_channels.pop(channel_id, None)
            if not was_cancelled and lyrics_data.get("lyrics"):
                try:
                    embed = self._create_embed(interaction, track, lyrics_data, lyrics_data["lyrics"])
                    await message.edit(embed=embed)
                except (discord.NotFound, discord.HTTPException):
                    pass

    @app_commands.command(name="lyrics", description="Show the lyrics for the current track")
    async def lyrics(self, interaction: discord.Interaction):
        await self.handle_lyrics_interaction(interaction, ephemeral=False)

    def cog_unload(self) -> None:
        for task in self._sync_tasks.values():
            task.cancel()
        self._sync_tasks.clear()
        self._active_lyrics_channels.clear()


async def setup(bot):
    await bot.add_cog(LyricsCommands(bot))
