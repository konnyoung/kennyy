import discord
from discord.ext import commands
from discord import app_commands
import wavelink
import re
import asyncio
import difflib

from commands import iter_wavelink_nodes, player_is_ready, resolve_wavelink_player


AUTOCOMPLETE_TIMEOUT_SECONDS = 1.5


class MusicControlView(discord.ui.View):
    """View com botões de controle de música"""

    def __init__(self, bot, player: wavelink.Player | None = None, *, guild_id: int | None = None):
        super().__init__(timeout=None)  # Sem timeout para controles persistentes
        self.bot = bot
        resolved_guild_id = guild_id
        if resolved_guild_id is None and player and getattr(player, "guild", None):
            resolved_guild_id = player.guild.id
        self._guild_id = resolved_guild_id
        self._volume_reset_tasks: dict[str, asyncio.Task] = {}

        initial_mode = None
        if player:
            if hasattr(player, "loop_mode_override"):
                initial_mode = player.loop_mode_override
            elif getattr(player, "queue", None):
                initial_mode = player.queue.mode
        self._apply_loop_button_state(initial_mode)
        self._update_play_pause_button(player)

    def _translate(self, interaction: discord.Interaction, key: str, **kwargs) -> str:
        translator = getattr(self.bot, "translate", None)
        guild_id = interaction.guild.id if interaction.guild else None
        default = kwargs.get("default")
        if translator:
            return translator(key, guild_id=guild_id, **kwargs)
        return default if default is not None else key

    def _translate_static(self, key: str, *, default: str | None = None, **kwargs) -> str:
        translator = getattr(self.bot, "translate", None)
        if translator:
            return translator(key, guild_id=self._guild_id, default=default, **kwargs)
        return default if default is not None else key

    def _apply_loop_button_state(self, mode: wavelink.QueueMode | None) -> None:
        button = getattr(self, "loop_button", None)
        if not isinstance(button, discord.ui.Button):
            return

        if mode is None:
            mode = wavelink.QueueMode.normal

        if mode is wavelink.QueueMode.loop:
            button.style = discord.ButtonStyle.success
            label = self._translate_static(
                "commands.play.loop.button.track",
                default="Loop: Track",
            )
        elif mode is wavelink.QueueMode.loop_all:
            button.style = discord.ButtonStyle.primary
            label = self._translate_static(
                "commands.play.loop.button.queue",
                default="Loop: Queue",
            )
        else:
            button.style = discord.ButtonStyle.secondary
            label = self._translate_static(
                "commands.play.loop.button.off",
                default="Loop: Off",
            )

        button.label = label
        button.emoji = "🔁"

    def _update_play_pause_button(self, player: wavelink.Player | None) -> None:
        """Alterna o emoji do botão play/pause conforme o estado atual."""
        button: discord.ui.Button | None = None
        for child in self.children:
            if isinstance(child, discord.ui.Button) and child.custom_id == "play_pause":
                button = child
                break

        if button is None:
            return

        is_paused = bool(getattr(player, "paused", False))
        button.emoji = "▶️" if is_paused else "⏸️"

    def _cancel_volume_reset(self, key: str) -> None:
        task = self._volume_reset_tasks.pop(key, None)
        if task and not task.done():
            task.cancel()

    def _schedule_volume_reset(
        self,
        key: str,
        button: discord.ui.Button,
        message: discord.Message | None,
    ) -> None:
        self._cancel_volume_reset(key)

        async def _reset():
            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                return

            button.label = None
            if message:
                try:
                    await message.edit(view=self)
                except Exception:
                    pass

        task = asyncio.create_task(_reset())
        self._volume_reset_tasks[key] = task

    async def _show_volume_feedback(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
        new_volume: int,
    ) -> None:
        message = interaction.message
        button.label = f"{new_volume}%"

        try:
            if not interaction.response.is_done():
                await interaction.response.edit_message(view=self)
            elif message:
                await message.edit(view=self)
        except discord.NotFound:
            return
        except discord.HTTPException:
            return

        custom_id = getattr(button, "custom_id", None)
        if custom_id:
            self._schedule_volume_reset(custom_id, button, message)

    async def _send_ephemeral(
        self,
        interaction: discord.Interaction,
        content: str | None = None,
        *,
        embed: discord.Embed | None = None,
    ) -> None:
        if content is None and embed is None:
            return

        try:
            if not interaction.response.is_done():
                try:
                    await interaction.response.defer(ephemeral=True, thinking=False)
                except discord.NotFound:
                    return
                except discord.HTTPException:
                    pass

            await interaction.followup.send(content, embed=embed, ephemeral=True)
        except (discord.NotFound, discord.HTTPException):
            return

    async def _ensure_control_access(
        self,
        interaction: discord.Interaction,
        player: discord.VoiceClient | wavelink.Player | None,
    ) -> wavelink.Player | None:
        guild = interaction.guild
        resolved_player: wavelink.Player | None = player if isinstance(player, wavelink.Player) else None

        if resolved_player is None and guild is not None:
            resolved_player = resolve_wavelink_player(self.bot, guild)

        if not player_is_ready(resolved_player):
            message = self._translate(interaction, "commands.common.errors.bot_not_connected")
            await self._send_ephemeral(interaction, message)
            return None

        user_channel = getattr(interaction.user.voice, "channel", None)
        if user_channel is None:
            message = self._translate(interaction, "commands.play.errors.user_not_in_voice")
            await self._send_ephemeral(interaction, message)
            return None

        player_channel = getattr(resolved_player, "channel", None)
        if player_channel is None and guild is not None:
            bot_voice_state = getattr(guild.me, "voice", None)
            player_channel = getattr(bot_voice_state, "channel", None)

        if player_channel is None and getattr(resolved_player, "connected", False):
            player_channel = user_channel

        if player_channel is None:
            message = self._translate(interaction, "commands.common.errors.bot_not_connected")
            await self._send_ephemeral(interaction, message)
            return None

        if player_channel.id != user_channel.id:
            message = self._translate(
                interaction,
                "commands.play.errors.same_voice_channel",
                default="Você precisa estar no mesmo canal de voz que o bot para usar estes controles!",
            )
            await self._send_ephemeral(interaction, message)
            return None

        if getattr(resolved_player, "channel", None) is None:
            try:
                resolved_player.channel = player_channel  # type: ignore[assignment]
            except Exception:
                pass

        return resolved_player

    @discord.ui.button(emoji="🔉", style=discord.ButtonStyle.secondary, custom_id="volume_down")
    async def volume_down_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Botão para diminuir volume"""
        raw_player = interaction.guild.voice_client
        player = await self._ensure_control_access(interaction, raw_player)
        if not player:
            return

        if not player.current:
            message = self._translate(interaction, "commands.common.errors.no_track")
            await self._send_ephemeral(interaction, message)
            return

        current_volume = player.volume
        new_volume = max(current_volume - 10, 0)  # Diminui 10%, mínimo 0%
        await player.set_volume(new_volume)
        await self._show_volume_feedback(interaction, button, new_volume)

    @discord.ui.button(emoji="🔊", style=discord.ButtonStyle.secondary, custom_id="volume_up")
    async def volume_up_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Botão para aumentar volume"""
        raw_player = interaction.guild.voice_client
        player = await self._ensure_control_access(interaction, raw_player)
        if not player:
            return

        if not player.current:
            message = self._translate(interaction, "commands.common.errors.no_track")
            await self._send_ephemeral(interaction, message)
            return

        current_volume = player.volume
        new_volume = min(current_volume + 10, 150)  # Aumenta 10%, máximo 150%
        await player.set_volume(new_volume)
        await self._show_volume_feedback(interaction, button, new_volume)

    @discord.ui.button(emoji="⏮️", style=discord.ButtonStyle.secondary, custom_id="previous")
    async def previous_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Botão para música anterior"""
        raw_player = interaction.guild.voice_client
        player = await self._ensure_control_access(interaction, raw_player)
        if not player:
            return

        if not player.current:
            message = self._translate(interaction, "commands.common.errors.no_track")
            await self._send_ephemeral(interaction, message)
            return

        # Reinicia a música atual se passou de 10 segundos
        if player.position > 10000:  # 10 segundos em ms
            await player.seek(0)
            message = self._translate(interaction, "commands.play.previous.restarted")
            await self._send_ephemeral(interaction, message)
        else:
            message = self._translate(interaction, "commands.play.previous.none")
            await self._send_ephemeral(interaction, message)

    @discord.ui.button(emoji="⏯️", style=discord.ButtonStyle.primary, custom_id="play_pause")
    async def play_pause_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Botão para pausar/retomar"""
        raw_player = interaction.guild.voice_client
        player = await self._ensure_control_access(interaction, raw_player)
        if not player:
            return

        if not player.current:
            message = self._translate(interaction, "commands.common.errors.no_track")
            await self._send_ephemeral(interaction, message)
            return

        if player.paused:
            await player.pause(False)
        else:
            await player.pause(True)

        self._update_play_pause_button(player)

        try:
            embed_message = getattr(player, "current_embed_message", None)
            new_embed = None
            current_track = getattr(player, "current", None)
            if embed_message and current_track:
                build_embed = getattr(self.bot, "_build_now_playing_embed", None)
                if callable(build_embed):
                    new_embed = build_embed(player, current_track)

            if not interaction.response.is_done():
                await interaction.response.edit_message(view=self, embed=new_embed)
            elif embed_message:
                await embed_message.edit(view=self, embed=new_embed)
            else:
                await interaction.edit_original_response(view=self)
        except discord.NotFound:
            pass
        except discord.HTTPException:
            pass

    @discord.ui.button(emoji="⏹️", style=discord.ButtonStyle.danger, custom_id="stop")
    async def stop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Botão para parar reprodução"""
        raw_player = interaction.guild.voice_client
        player = await self._ensure_control_access(interaction, raw_player)
        if not player:
            return

        try:
            await self.bot._clear_now_playing_message(player)
        except Exception as exc:
            print(f"Falha ao limpar estado de reprodução antes de desconectar: {exc}")

        await player.disconnect()
        message = self._translate(interaction, "commands.play.stop.success")
        await self._send_ephemeral(interaction, message)

        # Desabilita todos os botões
        for item in self.children:
            item.disabled = True

        try:
            await interaction.edit_original_response(view=self)
        except:
            pass

    @discord.ui.button(emoji="⏭️", style=discord.ButtonStyle.secondary, custom_id="skip")
    async def skip_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Botão para pular música"""
        raw_player = interaction.guild.voice_client
        player = await self._ensure_control_access(interaction, raw_player)
        if not player:
            return

        if not player.playing:
            message = self._translate(interaction, "commands.common.errors.no_track")
            await self._send_ephemeral(interaction, message)
            return

        try:
            await player.skip(force=True)
        except Exception:
            await player.stop()

        message = self._translate(interaction, "commands.play.skip.success")
        await self._send_ephemeral(interaction, message)

    @discord.ui.button(emoji="🔀", style=discord.ButtonStyle.secondary, custom_id="shuffle")
    async def shuffle_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Botão para embaralhar fila"""
        raw_player = interaction.guild.voice_client
        player = await self._ensure_control_access(interaction, raw_player)
        if not player:
            return

        if player.queue.is_empty:
            message = self._translate(interaction, "commands.play.shuffle.empty")
            await self._send_ephemeral(interaction, message)
            return

        player.queue.shuffle()
        message = self._translate(
            interaction,
            "commands.play.shuffle.success",
            count=player.queue.count,
        )
        await self._send_ephemeral(interaction, message)

    @discord.ui.button(emoji="🔁", style=discord.ButtonStyle.secondary, custom_id="loop", row=1)
    async def loop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Botão para alternar modos de loop"""
        raw_player = interaction.guild.voice_client
        player = await self._ensure_control_access(interaction, raw_player)
        if not player:
            return

        current_mode = getattr(player.queue, "mode", wavelink.QueueMode.normal)

        if current_mode is wavelink.QueueMode.normal:
            new_mode = wavelink.QueueMode.loop
            response_key = "commands.play.loop.responses.track"
            response_default = "🔂 Loop da música atual ativado!"
        elif current_mode is wavelink.QueueMode.loop:
            new_mode = wavelink.QueueMode.loop_all
            response_key = "commands.play.loop.responses.queue"
            response_default = "🔁 Loop da fila ativado!"
        else:
            new_mode = wavelink.QueueMode.normal
            response_key = "commands.play.loop.responses.off"
            response_default = "🔁 Loop desativado."

        player.queue.mode = new_mode
        player.loop_mode_override = new_mode
        if interaction.guild:
            self._guild_id = interaction.guild.id
        self._apply_loop_button_state(new_mode)

        try:
            if not interaction.response.is_done():
                await interaction.response.edit_message(view=self)
            else:
                await interaction.edit_original_response(view=self)
        except discord.NotFound:
            pass
        except discord.HTTPException:
            pass

    @discord.ui.button(emoji="📜", style=discord.ButtonStyle.secondary, custom_id="play_lyrics", row=1)
    async def lyrics_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Mostra a letra da música atual"""
        raw_player = interaction.guild.voice_client
        player = await self._ensure_control_access(interaction, raw_player)
        if not player:
            return

        if not player.current:
            message = self._translate(
                interaction,
                "commands.common.errors.no_track",
                default="❌ Não há música tocando!",
            )
            await self._send_ephemeral(interaction, message)
            return

        lyrics_cog = self.bot.get_cog("LyricsCommands")
        if lyrics_cog is None:
            message = self._translate(
                interaction,
                "commands.lyrics.errors.feature_unavailable",
                default="❌ Letras indisponíveis no momento.",
            )
            await self._send_ephemeral(interaction, message)
            return

        try:
            await lyrics_cog.handle_lyrics_interaction(interaction, ephemeral=False, player=player)
        except Exception as exc:
            print(f"Falha ao exibir letras pela view de reprodução: {exc}")
            message = self._translate(
                interaction,
                "commands.lyrics.errors.feature_unavailable",
                default="❌ Erro ao buscar letras.",
            )
            await self._send_ephemeral(interaction, message)


class PlayCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def _translate(self, interaction: discord.Interaction | None, key: str, **kwargs) -> str:
        translator = getattr(self.bot, "translate", None)
        guild_id = None
        if interaction and interaction.guild:
            guild_id = interaction.guild.id
        if translator:
            return translator(key, guild_id=guild_id, **kwargs)
        return key

    def _translate_guild(self, guild: discord.Guild | None, key: str, **kwargs) -> str:
        translator = getattr(self.bot, "translate", None)
        guild_id = guild.id if guild else None
        if translator:
            return translator(key, guild_id=guild_id, **kwargs)
        return key

    def _error_embed(
        self,
        interaction: discord.Interaction,
        title_key: str,
        description_key: str,
        *,
        color: int = 0xff0000,
        **kwargs,
    ) -> discord.Embed:
        title = self._translate(interaction, title_key, **kwargs)
        description = self._translate(interaction, description_key, **kwargs)
        return discord.Embed(title=title, description=description, color=color)

    async def _send_interaction_message(
        self,
        interaction: discord.Interaction,
        *,
        content: str | None = None,
        embed: discord.Embed | None = None,
        ephemeral: bool = False,
        view: discord.ui.View | None = None,
    ) -> None:
        if content is None and embed is None:
            return

        view_param = view if view is not None else discord.utils.MISSING

        try:
            if interaction.response.is_done():
                await interaction.followup.send(
                    content=content,
                    embed=embed,
                    ephemeral=ephemeral,
                    view=view_param,
                )
            else:
                await interaction.response.send_message(
                    content=content,
                    embed=embed,
                    ephemeral=ephemeral,
                    view=view_param,
                )
        except (discord.NotFound, discord.HTTPException):
            return

    async def _player_or_error(
        self,
        interaction: discord.Interaction,
        *,
        ephemeral: bool = False,
    ) -> wavelink.Player | None:
        player = resolve_wavelink_player(self.bot, interaction.guild)

        if not player_is_ready(player):
            embed = self._error_embed(
                interaction,
                "commands.common.embeds.error_title",
                "commands.common.errors.bot_not_connected_full",
            )
            await self._send_interaction_message(interaction, embed=embed, ephemeral=ephemeral)
            return None

        if getattr(player, "text_channel", None) is None and interaction.channel:
            try:
                player.text_channel = interaction.channel
            except Exception:
                pass

        return player

    def _ensure_loop_mode_attr(self, player: wavelink.Player | None) -> None:
        if not isinstance(player, wavelink.Player):
            return

        try:
            queue_mode = getattr(player.queue, "mode", wavelink.QueueMode.normal)
        except Exception:
            queue_mode = wavelink.QueueMode.normal

        loop_mode = getattr(player, "loop_mode_override", queue_mode)
        player.loop_mode_override = loop_mode

        try:
            player.queue.mode = loop_mode
        except Exception:
            pass

    async def _send_autocomplete_choices(
        self,
        interaction: discord.Interaction,
        choices: list[app_commands.Choice[str]],
    ) -> list[app_commands.Choice[str]]:
        is_expired = getattr(interaction, "is_expired", None)
        if callable(is_expired):
            try:
                if is_expired():
                    return []
            except Exception:
                pass

        if interaction.response.is_done():
            return []

        try:
            await interaction.response.autocomplete(choices)
        except discord.NotFound:
            return []
        except discord.HTTPException as exc:
            print(f"Falha ao responder autocomplete: {exc}")
        except Exception as exc:
            print(f"Erro inesperado ao responder autocomplete: {exc}")
        return []

    def _normalize_search_text(self, text: str) -> str:
        """Normaliza texto para comparação de busca."""
        text = text.lower()
        text = re.sub(r"\(.*?\)|\[.*?\]", " ", text)
        text = re.sub(r"[^a-z0-9çãõáàâêéíóôúüñ\s]", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text

    def _score_track_match(self, query: str, track: wavelink.Playable) -> float:
        """Retorna uma pontuação de aderência do track ao termo buscado."""
        query_norm = self._normalize_search_text(query)
        title_raw = getattr(track, "title", "") or ""
        author_raw = getattr(track, "author", "") or ""

        title_norm = self._normalize_search_text(title_raw)
        author_norm = self._normalize_search_text(author_raw)

        query_tokens = set(query_norm.split())
        title_tokens = set(title_norm.split())
        author_tokens = set(author_norm.split())

        title_sim = difflib.SequenceMatcher(None, query_norm, title_norm).ratio()

        token_overlap = 0.0
        if query_tokens:
            token_overlap = len(query_tokens & title_tokens) / len(query_tokens)

        starts_with_bonus = 0.15 if title_norm.startswith(query_norm) else 0.0
        exact_bonus = 0.25 if title_norm == query_norm else 0.0

        author_bonus = 0.0
        if query_tokens and author_tokens:
            author_bonus = 0.1 * (len(query_tokens & author_tokens) / len(author_tokens))

        penalty = 0.0
        remix_markers = {"live", "remix", "cover", "karaoke"}
        if title_tokens & remix_markers and not (query_tokens & remix_markers):
            penalty += 0.1

        score = (title_sim * 0.6) + (token_overlap * 0.25) + starts_with_bonus + exact_bonus + author_bonus - penalty
        return score

    def _pick_best_track(self, query: str, tracks: list[wavelink.Playable]) -> wavelink.Playable:
        """Seleciona a faixa com melhor aderência ao termo de busca."""
        if not tracks:
            raise ValueError("Lista de tracks vazia")

        scored = []
        for track in tracks:
            try:
                score = self._score_track_match(query, track)
            except Exception:
                score = 0.0
            scored.append((score, track))

        scored.sort(key=lambda item: item[0], reverse=True)
        return scored[0][1]

    def is_url(self, string):
        """Verifica se a string é uma URL válida"""
        url_pattern = re.compile(
            r'^https?://'
            r'(?:(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+[A-Z]{2,6}\.?|'
            r'localhost|'
            r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})'
            r'(?::\d+)?'
            r'(?:/?|[/?]\S+)$', re.IGNORECASE)
        return url_pattern.match(string)

    def _strip_search_prefix(self, query: str) -> str:
        """Remove prefixos yt/ytm/scsearch duplicados para evitar consultas inválidas."""
        prefixes = ("ytsearch:", "ytmsearch:", "scsearch:", "spsearch:", "dzsearch:", "amsearch:")
        for prefix in prefixes:
            if query.lower().startswith(prefix):
                return query[len(prefix):]
        return query

    async def _search_with_fallback(
        self,
        interaction: discord.Interaction,
        query: str,
        *,
        is_url: bool = False,
        timeout: float | None = None,
        max_attempts: int | None = None,
        provider: str | None = None,
    ) -> wavelink.Playlist | list[wavelink.Playable] | None:
        """Tenta buscar tracks em múltiplos prefixos (YouTube/YouTube Music/SoundCloud)."""

        async def _fetch(identifier: str):
            try:
                return await wavelink.Pool.fetch_tracks(identifier)
            except Exception:
                return await interaction.client.search_with_failover(identifier)

        async def _run_attempts(attempts: list[str]):
            last_exc: Exception | None = None
            for attempt in attempts:
                try:
                    coro = _fetch(attempt)
                    tracks = await asyncio.wait_for(coro, timeout=timeout) if timeout else await coro
                except Exception as exc:
                    last_exc = exc
                    continue

                if not tracks:
                    continue

                if isinstance(tracks, wavelink.Playlist):
                    if getattr(tracks, "tracks", None):
                        return tracks
                    continue

                playable_cls = getattr(wavelink, "Playable", None)
                if playable_cls and isinstance(tracks, playable_cls):
                    return [tracks]

                try:
                    tracks_list = list(tracks)
                except TypeError:
                    tracks_list = []

                if tracks_list:
                    return tracks_list
            if last_exc:
                raise last_exc
            return None

        attempts: list[str]
        clean_query = self._strip_search_prefix(query)
        provider_effective = provider or "spotify"  # padrão: Spotify
        user_selected_provider = provider is not None
        if is_url:
            attempts = [query]
        else:
            quoted = f'"{clean_query}"'
            if provider_effective == "youtube":
                attempts = [
                    f"ytsearch:{clean_query}",
                    f"ytsearch:{quoted}",
                    f"ytsearch:{clean_query} audio",
                ]
            elif provider_effective == "ytmusic":
                attempts = [
                    f"ytmsearch:{clean_query}",
                    f"ytmsearch:{quoted}",
                    f"ytmsearch:{clean_query} audio",
                ]
            elif provider_effective == "soundcloud":
                attempts = [
                    f"scsearch:{clean_query}",
                    f"scsearch:{quoted}",
                ]
            elif provider_effective == "deezer":
                attempts = [
                    f"dzsearch:{clean_query}",
                    f"dzsearch:{quoted}",
                ]
            elif provider_effective == "spotify":
                attempts = [
                    f"spsearch:{clean_query}",
                    f"spsearch:{quoted}",
                ]
            elif provider_effective == "applemusic":
                attempts = [
                    f"amsearch:{clean_query}",
                    f"amsearch:{quoted}",
                ]
            else:
                attempts = [
                    f"ytmsearch:{clean_query}",
                    f"ytmsearch:{quoted}",
                    f"ytmsearch:{clean_query} audio",
                    f"ytsearch:{clean_query}",
                    f"ytsearch:{quoted}",
                    f"ytsearch:{clean_query} audio",
                    f"scsearch:{clean_query}",
                    f"scsearch:{quoted}",
                    f"spsearch:{clean_query}",
                    f"spsearch:{quoted}",
                ]

            default_attempts = [
                f"spsearch:{clean_query}",
                f"spsearch:{quoted}",
                f"dzsearch:{clean_query}",
                f"dzsearch:{quoted}",
                f"amsearch:{clean_query}",
                f"amsearch:{quoted}",
                f"ytmsearch:{clean_query}",
                f"ytmsearch:{quoted}",
                f"ytmsearch:{clean_query} audio",
                f"ytsearch:{clean_query}",
                f"ytsearch:{quoted}",
                f"ytsearch:{clean_query} audio",
                f"scsearch:{clean_query}",
                f"scsearch:{quoted}",
            ]

        if max_attempts is not None:
            attempts = attempts[:max_attempts]

        # Primeira passada: conforme provider efetivo
        first_result = await _run_attempts(attempts)
        if first_result or is_url:
            return first_result

        # Fallback amplo se não achou nada
        fallback_attempts = default_attempts if max_attempts is None else default_attempts[:max_attempts]
        return await _run_attempts(fallback_attempts)



    def format_time_from_ms(self, milliseconds):
        """Formata tempo de milissegundos para MM:SS"""
        if milliseconds is None or milliseconds == 0:
            return "00:00"
        seconds = int(milliseconds / 1000)
        minutes = seconds // 60
        seconds = seconds % 60
        return f"{minutes:02d}:{seconds:02d}"

    async def _force_destroy_remote_player(self, guild_id: int | None) -> None:
        if guild_id is None:
            return

        for node in iter_wavelink_nodes():
            identifier = getattr(node, "identifier", "?")

            players_cache = getattr(node, "_players", None)
            if isinstance(players_cache, dict):
                players_cache.pop(guild_id, None)

            managed_players = getattr(node, "players", None)
            if isinstance(managed_players, dict):
                managed_players.pop(guild_id, None)

            session_id = getattr(node, "session_id", None)
            status = getattr(node, "status", None)

            if session_id is None or status != wavelink.NodeStatus.CONNECTED:
                continue

            try:
                await node._destroy_player(guild_id)
            except wavelink.LavalinkException as exc:
                if getattr(exc, "status", None) == 404:
                    continue
                print(f"Falha ao destruir player remoto no nó {identifier}: {exc}")
            except Exception as exc:
                print(f"Erro inesperado ao destruir player remoto no nó {identifier}: {exc}")

    async def _cleanup_failed_voice_connection(self, guild: discord.Guild | None) -> None:
        if guild is None:
            return

        # Limpeza é um reset de sessão: não manter afinidade de node.
        try:
            if hasattr(self.bot, "_clear_session_node_affinity"):
                self.bot._clear_session_node_affinity(getattr(guild, "id", None))
        except Exception:
            pass

        processed: set[int] = set()

        voice_client = guild.voice_client
        if voice_client is not None:
            processed.add(id(voice_client))
            try:
                await voice_client.disconnect(force=True)
            except TypeError:
                try:
                    await voice_client.disconnect()
                except Exception as exc:
                    print(f"Falha ao desconectar voice_client padrão: {exc}")
            except Exception as exc:
                print(f"Falha ao desconectar voice_client: {exc}")

        for node in list(wavelink.Pool.nodes.values()):
            try:
                player = node.get_player(guild.id)
            except Exception:
                player = None

            if player is None or id(player) in processed:
                continue

            processed.add(id(player))
            try:
                await player.disconnect()
            except Exception as exc:
                print(f"Falha ao limpar player Wavelink preso: {exc}")

        try:
            await guild.change_voice_state(channel=None, self_mute=False, self_deaf=False)
        except discord.HTTPException:
            pass
        except Exception as exc:
            print(f"Falha ao limpar estado de voz do guild: {exc}")

        try:
            await self._force_destroy_remote_player(getattr(guild, "id", None))
        except Exception as exc:
            print(f"Falha ao destruir player remoto durante limpeza: {exc}")

        await asyncio.sleep(0.25)

    async def _handle_voice_connect_issue(
        self,
        interaction: discord.Interaction,
        channel: discord.abc.Connectable,
        error: Exception,
        attempt: int,
        max_attempts: int,
        attempted_node_id: str | None = None,
    ) -> None:
        guild = interaction.guild
        channel_name = getattr(channel, "name", str(getattr(channel, "id", "?")))
        
        print(
            f"Falha ao conectar ao canal de voz '{channel_name}' (tentativa {attempt}/{max_attempts}): {error}"
        )

        await self._cleanup_failed_voice_connection(guild)

        # Verifica se há outros nodes disponíveis
        other_nodes_available = False
        if attempted_node_id:
            other_nodes_available = any(
                node.status == wavelink.NodeStatus.CONNECTED
                for node in wavelink.Pool.nodes.values()
                if getattr(node, "identifier", None) != attempted_node_id
            )
        
        if other_nodes_available:
            await asyncio.sleep(0.3)
        else:
            print(f"⚠️ Nenhum outro node disponível - aguardando antes de tentar novamente")
            # Aguarda um pouco mais quando não há alternativas
            await asyncio.sleep(1.5)
            
            await asyncio.sleep(1)

    async def _connect_player_with_retry(
        self,
        interaction: discord.Interaction,
        channel: discord.abc.Connectable,
        *,
        attempts: int = 2,
    ) -> wavelink.Player:
        last_error: Exception | None = None

        try:
            await self.bot.ensure_lavalink_connected()
        except Exception as ensure_exc:
            print(f"Falha ao validar nós Lavalink antes de conectar: {ensure_exc}")
        
        # Verifica se há pelo menos um node realmente disponível
        available_nodes = [
            node for node in wavelink.Pool.nodes.values()
            if node.status == wavelink.NodeStatus.CONNECTED
        ]
        
        if not available_nodes:
            raise RuntimeError("Nenhum node Lavalink disponível para conexão")
        
        print(f"ℹ️ {len(available_nodes)} node(s) disponível(is) para conexão")

        excluded_nodes = set()  # Nodes que falharam e devem ser evitados nos retries
        
        for attempt in range(1, attempts + 1):
            attempted_node_id = None
            
            try:
                # Seleciona node para esta tentativa
                preferred_node_id = None
                # Só usa afinidade na primeira tentativa; em retry, permite qualquer node disponível
                if attempt == 1:
                    try:
                        if interaction.guild is not None:
                            preferred_node_id = getattr(self.bot, "_session_node_affinity", {}).get(interaction.guild.id)
                    except Exception:
                        preferred_node_id = None

                # Filtra nodes disponíveis (exclui nodes que falharam)
                usable_nodes = [
                    n for n in wavelink.Pool.nodes.values()
                    if n.status == wavelink.NodeStatus.CONNECTED and n.identifier not in excluded_nodes
                ]
                
                if not usable_nodes:
                    print(f"⚠️ Nenhum node disponível para tentativa {attempt}/{attempts} (todos excluídos ou offline)")
                    raise RuntimeError("Nenhum node Lavalink disponível após exclusões")

                # Escolhe qual node usar
                selected_node = None
                
                # Se há afinidade válida e o node está disponível, usa ele
                if preferred_node_id and preferred_node_id not in excluded_nodes:
                    try:
                        preferred_node = wavelink.Pool.get_node(preferred_node_id)
                        if preferred_node.status == wavelink.NodeStatus.CONNECTED:
                            selected_node = preferred_node
                            print(f"🎯 Usando node com afinidade: {preferred_node_id}")
                    except Exception:
                        pass
                
                # Se não tem afinidade ou node preferido não está disponível, escolhe o com menos players
                if selected_node is None:
                    # Ordena por quantidade de players (menos players = menos carga)
                    usable_nodes.sort(key=lambda n: len(getattr(n, 'players', {})))
                    selected_node = usable_nodes[0]
                    player_count = len(getattr(selected_node, 'players', {}))
                    print(f"🔍 Selecionado node com menos carga: {selected_node.identifier} ({player_count} player(s) ativos)")

                connect_timeout = 6.0
                
                # Cria player com o node selecionado explicitamente
                def _player_factory(client: discord.Client, ch: discord.abc.Connectable):
                    return wavelink.Player(client, ch, nodes=[selected_node])

                player = await channel.connect(cls=_player_factory, self_deaf=True, reconnect=True, timeout=connect_timeout)
                
                # Após conexão bem-sucedida, identifica qual node foi usado
                try:
                    node = getattr(player, "node", None)
                    if node:
                        attempted_node_id = getattr(node, "identifier", None)
                        player_count = len(node.players) if node else 0
                        print(f"✅ Conectado no node: {attempted_node_id} ({player_count} player(s) ativo(s))")
                except Exception:
                    pass
                    
            except (
                wavelink.ChannelTimeoutException,
                wavelink.InvalidChannelStateException,
                asyncio.TimeoutError,
            ) as exc:
                last_error = exc
                
                # Verifica se o erro é realmente de permissão (não timeout do node)
                is_permission_error = False
                error_msg = str(exc).lower()
                
                if "permission" in error_msg or "forbidden" in error_msg:
                    is_permission_error = True
                    print(f"⚠️ Erro de permissão detectado ao conectar: {exc}")
                
                # Se for erro de permissão, não marca node como falho
                if is_permission_error:
                    await self._cleanup_failed_voice_connection(interaction.guild)
                    raise RuntimeError(f"Sem permissão para conectar ao canal de voz: {exc}")
                
                # Tenta identificar qual node causou o timeout através do voice_client parcial
                try:
                    guild = interaction.guild
                    voice_client = guild.voice_client if guild else None
                    if voice_client and hasattr(voice_client, "node"):
                        node = getattr(voice_client, "node", None)
                        if node:
                            attempted_node_id = getattr(node, "identifier", None)
                            print(f"🎯 Timeout no node: {attempted_node_id}")
                except Exception:
                    pass
                
                # Se não conseguiu identificar pelo voice_client, usa o node que foi explicitamente selecionado
                if not attempted_node_id and selected_node:
                    attempted_node_id = selected_node.identifier
                    print(f"🎯 Timeout no node selecionado: {attempted_node_id}")
                
                # Adiciona o node à lista de exclusão para evitar nas próximas tentativas
                if attempted_node_id:
                    excluded_nodes.add(attempted_node_id)
                
                # Limpa afinidade do node problemático para tentar outro na próxima vez
                if attempted_node_id and interaction.guild:
                    try:
                        affinity = getattr(self.bot, "_session_node_affinity", {})
                        if affinity.get(interaction.guild.id) == attempted_node_id:
                            affinity.pop(interaction.guild.id, None)
                            print(f"🔄 Removida afinidade com node '{attempted_node_id}' para permitir failover")
                    except Exception:
                        pass
                
                # Marca o node como falho apenas se NÃO for erro de permissão
                if attempted_node_id:
                    try:
                        await self.bot.mark_node_as_failed(attempted_node_id)
                    except Exception as mark_exc:
                        print(f"⚠️ Erro ao marcar node como falho: {mark_exc}")
                
                # Limpa conexões e aguarda antes da próxima tentativa
                await self._handle_voice_connect_issue(interaction, channel, exc, attempt, attempts, attempted_node_id)
                
                # Se ainda houver tentativas, verifica se há outros nodes disponíveis
                if attempt < attempts:
                    try:
                        # Conta quantos nodes estão conectados
                        connected_nodes = sum(
                            1 for node in wavelink.Pool.nodes.values()
                            if node.status == wavelink.NodeStatus.CONNECTED
                        )
                        
                        if connected_nodes == 0:
                            print(f"⚠️ Nenhum node disponível para nova tentativa (tentativa {attempt + 1}/{attempts})")
                        else:
                            print(f"🔄 Tentando novamente com nodes disponíveis ({connected_nodes} online)...")
                    except Exception:
                        pass
                
                continue
            except discord.ClientException as exc:
                last_error = exc
                await self._cleanup_failed_voice_connection(interaction.guild)
                await asyncio.sleep(1)
                continue
            except Exception as exc:
                last_error = exc
                break
            else:
                # Marca o player com a session_id atual do nó para rastreamento
                try:
                    node = getattr(player, "node", None)
                    if node:
                        session_id = getattr(node, "session_id", None)
                        if session_id:
                            player._session_id = session_id
                except Exception:
                    pass
                return player  # Conectou com sucesso

        if last_error is not None:
            raise last_error

        raise RuntimeError("Falha desconhecida ao conectar ao canal de voz.")

    async def _rebuild_player(
        self,
        interaction: discord.Interaction,
        player: wavelink.Player,
        channel: discord.abc.Connectable,
    ) -> wavelink.Player:
        """Reconstrói o player garantindo que a sessão no Lavalink seja recriada."""

        if channel is None:
            raise RuntimeError("Canal de voz alvo não encontrado para reconstruir o player.")

        guild_name = interaction.guild.name if interaction.guild else "Desconhecido"
        print(f"🔁 Reconstruindo player Lavalink para {guild_name} (sessão expirada ou inválida).")

        queue_items = list(player.queue) if not player.queue.is_empty else []
        try:
            queue_mode = player.queue.mode
        except Exception:
            queue_mode = wavelink.QueueMode.normal
        loop_mode = getattr(player, "loop_mode_override", queue_mode)

        auto_queue_items: list[wavelink.Playable] = []
        if hasattr(player, "auto_queue") and player.auto_queue and not player.auto_queue.is_empty:
            auto_queue_items = list(player.auto_queue)

        autoplay_mode = player.autoplay
        inactive_timeout = player.inactive_timeout
        volume = player.volume
        text_channel = getattr(player, "text_channel", None) or interaction.channel
        current_embed_message = getattr(player, "current_embed_message", None)

        try:
            await player.disconnect()
        except Exception:
            pass

        try:
            await self._cleanup_failed_voice_connection(interaction.guild)
        except Exception as cleanup_exc:
            print(f"Falha ao limpar estado de voz antes de reconstruir player: {cleanup_exc}")

        try:
            guild_id = interaction.guild.id if interaction.guild else None
            await self._force_destroy_remote_player(guild_id)
        except Exception as destroy_exc:
            print(f"Falha ao remover player remoto antes de reconstruir: {destroy_exc}")

        new_player = await self._connect_player_with_retry(interaction, channel)
        new_player.queue.mode = loop_mode
        new_player.loop_mode_override = loop_mode
        new_player.autoplay = autoplay_mode

        if inactive_timeout is not None:
            new_player.inactive_timeout = inactive_timeout

        if text_channel:
            new_player.text_channel = text_channel

        if current_embed_message:
            new_player.current_embed_message = current_embed_message

        for item in queue_items:
            try:
                await new_player.queue.put_wait(item)
            except Exception as exc:
                print(f"Falha ao restaurar item da fila durante reconstrução: {exc}")

        for item in auto_queue_items:
            try:
                await new_player.auto_queue.put_wait(item)
            except Exception as exc:
                print(f"Falha ao restaurar item da auto queue durante reconstrução: {exc}")

        if volume != 100:
            await new_player.set_volume(volume)
        try:
            new_player.queue.mode = loop_mode
        except Exception:
            pass
        return new_player

    async def _ensure_active_player(self, interaction: discord.Interaction) -> wavelink.Player:
        """Garante que existe um player válido e com sessão ativa no Lavalink."""

        guild = interaction.guild
        if guild is None:
            raise RuntimeError("Interação sem guild associada.")

        user_channel = getattr(interaction.user.voice, "channel", None)
        if user_channel is None:
            raise RuntimeError("Usuário não está em um canal de voz válido.")

        player: wavelink.Player | None = resolve_wavelink_player(self.bot, guild)

        if player:
            needs_rebuild = False
            node = getattr(player, "node", None)

            if not node or node.status != wavelink.NodeStatus.CONNECTED:
                needs_rebuild = True
            elif not player.connected:
                needs_rebuild = True
            else:
                # Verifica se a sessão do player é válida comparando com a sessão atual do nó
                player_session = getattr(player, "_session_id", None)
                node_session = getattr(node, "session_id", None)
                
                # Se o player não tem session_id ou ela não bate com a do nó, precisa rebuild
                if player_session is None or (node_session and player_session != node_session):
                    print(f"⚠️ Player sem session válida (player: {player_session}, nó: {node_session}). Rebuild necessário.")
                    needs_rebuild = True
                else:
                    # Valida se o player ainda existe no servidor Lavalink
                    try:
                        info = await node.fetch_player_info(guild.id)
                    except wavelink.LavalinkException as exc:
                        status = getattr(exc, "status", None)
                        reason = str(getattr(exc, "error", "")).lower()
                        if status in {401, 402, 403, 404, 410} or "session" in reason:
                            info = None
                        else:
                            raise
                    except wavelink.NodeException:
                        info = None
                    except Exception as exc:
                        # Qualquer outro erro ao validar - força rebuild
                        print(f"⚠️ Erro ao validar player info: {exc}")
                        info = None

                    if info is None:
                        needs_rebuild = True

            if needs_rebuild:
                player = await self._rebuild_player(interaction, player, user_channel)
        else:
            await self._cleanup_failed_voice_connection(guild)
            await self._force_destroy_remote_player(guild.id)
            player = await self._connect_player_with_retry(interaction, user_channel)

        if player.channel and player.channel.id != user_channel.id:
            try:
                await player.move_to(user_channel)
            except Exception:
                player = await self._rebuild_player(interaction, player, user_channel)

        player.text_channel = interaction.channel
        self._ensure_loop_mode_attr(player)
        return player

    async def _safe_play(
        self,
        interaction: discord.Interaction,
        player: wavelink.Player,
        track: wavelink.Playable,
    ) -> wavelink.Player:
        """Tenta iniciar a reprodução e reconstrói a sessão em casos recuperáveis."""

        def _should_rebuild_from_exception(error: Exception) -> bool:
            recoverable_statuses = {400, 401, 402, 403, 404, 410}

            if isinstance(error, wavelink.LavalinkException):
                status = getattr(error, "status", None)
                if status in recoverable_statuses:
                    return True

                reason = str(getattr(error, "error", "")).lower()
                if "session" in reason or "player" in reason:
                    return True
                return False

            if isinstance(error, wavelink.NodeException):
                status = getattr(error, "status", None)
                return status in recoverable_statuses or status is None

            message = str(error).lower()
            if "session" in message and "not" in message:
                return True
            if "player" in message and "not found" in message:
                return True
            return False

        try:
            await player.play(track)
            self._ensure_loop_mode_attr(player)
            return player
        except Exception as exc:
            if not _should_rebuild_from_exception(exc):
                raise

            target_channel = getattr(interaction.user.voice, "channel", None)
            if target_channel is None:
                raise

            print(f"Reconstruindo player após erro ao iniciar reprodução: {exc}")
            rebuilt_player = await self._rebuild_player(interaction, player, target_channel)
            await rebuilt_player.play(track)
            self._ensure_loop_mode_attr(rebuilt_player)
            return rebuilt_player

    @app_commands.command(name="play", description="Play a track or playlist")
    @app_commands.describe(
        query="Song name, YouTube/Spotify/Deezer/Apple Music URL, or search term",
        service="Service to search (default: Spotify)",
    )
    @app_commands.choices(
        service=[
            app_commands.Choice(name="Spotify (Default)", value="spotify"),
            app_commands.Choice(name="Deezer", value="deezer"),
            app_commands.Choice(name="Apple Music", value="applemusic"),
            app_commands.Choice(name="YouTube Music", value="ytmusic"),
            app_commands.Choice(name="YouTube", value="youtube"),
            app_commands.Choice(name="SoundCloud", value="soundcloud"),
        ]
    )
    async def play(
        self,
        interaction: discord.Interaction,
        query: str,
        service: app_commands.Choice[str] | None = None,
    ):
        """Comando para reproduzir música"""
        # Verifica se o usuário está em um canal de voz ANTES de defer (mais rápido)
        if not interaction.user.voice:
            embed = self._error_embed(
                interaction,
                "commands.common.embeds.error_title",
                "commands.play.errors.user_not_in_voice",
            )
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        # Verifica permissões do bot no canal de voz
        user_channel = interaction.user.voice.channel
        bot_member = interaction.guild.me if interaction.guild else None
        
        if bot_member and user_channel:
            permissions = user_channel.permissions_for(bot_member)
            
            if not permissions.view_channel:
                embed = self._error_embed(
                    interaction,
                    "commands.common.embeds.error_title",
                    "commands.play.errors.no_view_permission",
                )
                channel_label = self._translate(interaction, "commands.play.errors.channel_label", default="Channel")
                embed.add_field(
                    name=channel_label,
                    value=user_channel.mention,
                    inline=False
                )
                return await interaction.response.send_message(embed=embed, ephemeral=True)
            
            if not permissions.connect:
                embed = self._error_embed(
                    interaction,
                    "commands.common.embeds.error_title",
                    "commands.play.errors.no_connect_permission",
                )
                channel_label = self._translate(interaction, "commands.play.errors.channel_label", default="Channel")
                embed.add_field(
                    name=channel_label,
                    value=user_channel.mention,
                    inline=False
                )
                return await interaction.response.send_message(embed=embed, ephemeral=True)
            
            if not permissions.speak:
                embed = self._error_embed(
                    interaction,
                    "commands.common.embeds.error_title",
                    "commands.play.errors.no_speak_permission",
                )
                channel_label = self._translate(interaction, "commands.play.errors.channel_label", default="Channel")
                embed.add_field(
                    name=channel_label,
                    value=user_channel.mention,
                    inline=False
                )
                return await interaction.response.send_message(embed=embed, ephemeral=True)
        
        # Defer somente após validação básica
        try:
            await interaction.response.defer()
        except (discord.NotFound, discord.HTTPException) as e:
            print(f"⚠️ Erro ao fazer defer: {e}")
            pass

        # Envia embed de "Pesquisando..." IMEDIATAMENTE
        searching_text = self._translate(interaction, "commands.play.searching", default="Searching...")
        searching_embed = discord.Embed(
            description=f"# <a:unadance:1450689460307230760> {searching_text}",
            color=0x5284FF
        )
        searching_msg = await interaction.followup.send(embed=searching_embed)

        try:
            lavalink_ok = await self.bot.ensure_lavalink_connected()
        except Exception as e:
            embed = self._error_embed(
                interaction,
                "commands.common.embeds.error_title",
                "commands.play.errors.lavalink_validation",
                error=e,
            )
            
            # Envia log do erro
            if hasattr(self.bot, 'logger') and self.bot.logger:
                try:
                    guild_name = interaction.guild.name if interaction.guild else "DM"
                    guild_id = interaction.guild.id if interaction.guild else None
                    await self.bot.logger.log_error(
                        error_type="Lavalink Connection",
                        error_message=str(e),
                        guild_name=guild_name,
                        guild_id=guild_id,
                        additional_info=f"Comando: /play\nQuery: {query}\nUsuário: {interaction.user}"
                    )
                except Exception as log_exc:
                    print(f"Erro ao enviar log de falha do Lavalink: {log_exc}")
            
            return await interaction.followup.send(embed=embed)

        if not lavalink_ok:
            embed = self._error_embed(
                interaction,
                "commands.play.errors.lavalink_unavailable.title",
                "commands.play.errors.lavalink_unavailable.description",
            )
            
            # Envia log do erro
            if hasattr(self.bot, 'logger') and self.bot.logger:
                try:
                    guild_name = interaction.guild.name if interaction.guild else "DM"
                    guild_id = interaction.guild.id if interaction.guild else None
                    await self.bot.logger.log_error(
                        error_type="Lavalink Unavailable",
                        error_message="Nenhum nó Lavalink disponível",
                        guild_name=guild_name,
                        guild_id=guild_id,
                        additional_info=f"Comando: /play\nQuery: {query}\nUsuário: {interaction.user}"
                    )
                except Exception as log_exc:
                    print(f"Erro ao enviar log de Lavalink indisponível: {log_exc}")
            
            return await interaction.followup.send(embed=embed)

        # IMPORTANTE: Conecta na call PRIMEIRO e só depois busca a música.
        # Isso garante que se houver failover de node durante a conexão, 
        # a busca será feita com o node correto e os resultados serão consistentes.
        guild = interaction.guild
        had_voice_client = bool(guild and isinstance(getattr(guild, "voice_client", None), wavelink.Player))

        is_url = self.is_url(query)
        provider = service.value if service else None

        # Primeiro: garantir player conectado
        try:
            player = await self._ensure_active_player(interaction)
        except Exception as e:
            embed = self._error_embed(
                interaction,
                "commands.common.embeds.error_title",
                "commands.play.errors.connect_voice",
                error=e,
            )
            return await interaction.followup.send(embed=embed)

        # Segundo: buscar música (agora que sabemos qual node está sendo usado)
        try:
            tracks = await self._search_with_fallback(
                interaction,
                query,
                is_url=is_url,
                provider=provider,
            )
            # Deleta mensagem de pesquisa após encontrar
            try:
                await searching_msg.delete()
            except:
                pass
        except Exception as e:
            # Se este comando acabou de conectar o bot (não havia voice_client),
            # não deixa ele preso na call quando a busca falha.
            if not had_voice_client:
                try:
                    if isinstance(player, wavelink.Player):
                        await player.disconnect()
                except Exception:
                    pass

            embed = self._error_embed(
                interaction,
                "commands.common.embeds.error_title",
                "commands.play.errors.search_failure",
                error=e,
            )

            # Envia log do erro
            if hasattr(self.bot, 'logger') and self.bot.logger:
                try:
                    guild_name = interaction.guild.name if interaction.guild else "DM"
                    guild_id = interaction.guild.id if interaction.guild else None
                    error_msg = str(e)
                    await self.bot.logger.log_error(
                        error_type="Search/Load Track",
                        error_message=error_msg,
                        guild_name=guild_name,
                        guild_id=guild_id,
                        additional_info=f"Query: {query}\nUsuário: {interaction.user}"
                    )
                except Exception as log_exc:
                    print(f"Erro ao enviar log de falha de busca: {log_exc}")

            return await interaction.followup.send(embed=embed)

        if not tracks:
            embed = self._error_embed(
                interaction,
                "commands.play.errors.not_found.title",
                "commands.play.errors.not_found.description",
            )
            return await interaction.followup.send(embed=embed)

        try:
            # Se for uma playlist
            if isinstance(tracks, wavelink.Playlist):
                added_count = 0
                
                # Inicializa o dicionário de requesters se não existir
                if not hasattr(player, "_track_requesters"):
                    player._track_requesters = {}
                
                for track in tracks.tracks:
                    # Define quem solicitou a música
                    track.requester = interaction.user
                    
                    # Armazena também em um dicionário personalizado
                    track_id = getattr(track, "identifier", None) or getattr(track, "encoded", None)
                    if track_id:
                        player._track_requesters[track_id] = interaction.user
                    
                    await player.queue.put_wait(track)
                    added_count += 1

                self._ensure_loop_mode_attr(player)

                title = self._translate(interaction, "commands.play.playlist.added.title")
                description = self._translate(
                    interaction,
                    "commands.play.playlist.added.description",
                    name=tracks.name,
                    count=added_count,
                )

                embed = discord.Embed(title=title, description=description, color=0x00ff00)

                if hasattr(tracks, 'artwork') and tracks.artwork:
                    embed.set_thumbnail(url=tracks.artwork)

                stats_name = self._translate(interaction, "commands.play.playlist.added.stats_name")
                stats_value = self._translate(
                    interaction,
                    "commands.play.playlist.added.stats_value",
                    queue_count=player.queue.count,
                )
                embed.add_field(name=stats_name, value=stats_value, inline=True)

                await interaction.followup.send(embed=embed, ephemeral=True)

                # Se não está tocando, inicia
                if not player.playing:
                    player.text_channel = interaction.channel
                    next_track = await player.queue.get_wait()
                    try:
                        player = await self._safe_play(interaction, player, next_track)
                    except Exception as e:
                        embed = self._error_embed(
                            interaction,
                            "commands.common.embeds.error_title",
                            "commands.play.errors.playlist_start",
                            error=e,
                        )
                        return await interaction.followup.send(embed=embed)
            else:
                # Garante lista e escolhe o melhor resultado
                tracks_list: list[wavelink.Playable]
                if isinstance(tracks, wavelink.Playable):
                    tracks_list = [tracks]
                else:
                    try:
                        tracks_list = list(tracks)
                    except TypeError:
                        tracks_list = []

                if not tracks_list:
                    embed = self._error_embed(
                        interaction,
                        "commands.play.errors.not_found.title",
                        "commands.play.errors.not_found.description",
                    )
                    return await interaction.followup.send(embed=embed)

                try:
                    track = self._pick_best_track(query, tracks_list)
                except Exception:
                    track = tracks_list[0]
                
                # Inicializa o dicionário de requesters se não existir
                if not hasattr(player, "_track_requesters"):
                    player._track_requesters = {}
                
                # Define quem solicitou a música
                track.requester = interaction.user
                
                # Armazena também em um dicionário personalizado
                track_id = getattr(track, "identifier", None) or getattr(track, "encoded", None)
                if track_id:
                    player._track_requesters[track_id] = interaction.user

                # Se não está tocando nada, toca imediatamente
                if not player.playing:
                    try:
                        player = await self._safe_play(interaction, player, track)
                    except Exception as e:
                        embed = self._error_embed(
                            interaction,
                            "commands.common.embeds.error_title",
                            "commands.play.errors.play_start",
                            error=e,
                        )
                        return await interaction.followup.send(embed=embed)

                    player.text_channel = interaction.channel
                else:
                    # Adiciona à fila
                    await player.queue.put_wait(track)
                    self._ensure_loop_mode_attr(player)

                    embed = discord.Embed(
                        title=self._translate(interaction, "commands.play.queue.added.title"),
                        description=self._translate(
                            interaction,
                            "commands.play.queue.added.description",
                            title=track.title,
                        ),
                        color=0x0099ff
                    )

                    position_label = self._translate(
                        interaction,
                        "commands.play.queue.added.position_label",
                    )
                    duration_label = self._translate(interaction, "commands.common.labels.duration")
                    requester_label = self._translate(interaction, "commands.play.now_playing.requested_by")

                    embed.add_field(name=position_label, value=f"#{player.queue.count}", inline=True)
                    embed.add_field(
                        name=duration_label,
                        value=self.format_time_from_ms(track.length),
                        inline=True,
                    )
                    embed.add_field(name=requester_label, value=interaction.user.mention, inline=True)

                    if hasattr(track, 'artwork') and track.artwork:
                        embed.set_thumbnail(url=track.artwork)

                    # Calcula tempo estimado até esta música tocar
                    queue_duration = sum(t.length for t in player.queue if t.length)
                    if player.current and player.current.length:
                        remaining_current = player.current.length - player.position
                        queue_duration += remaining_current

                    embed.add_field(
                        name=self._translate(interaction, "commands.play.queue.added.eta_label"),
                        value=self.format_time_from_ms(queue_duration),
                        inline=True
                    )

                    embed.set_footer(
                        text=self._translate(
                            interaction,
                            "commands.play.queue.added.footer",
                            count=player.queue.count,
                        )
                    )

                    await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            embed = self._error_embed(
                interaction,
                "commands.common.embeds.error_title",
                "commands.play.errors.process_failure",
                error=e,
            )
            await interaction.followup.send(embed=embed)

    @app_commands.command(name="volume", description="Control playback volume")
    @app_commands.describe(level="Nível do volume (0-150%)")
    async def volume(self, interaction: discord.Interaction, level: int):
        """Comando para controlar volume diretamente"""
        player = await self._player_or_error(interaction)
        if not player:
            return

        if level < 0 or level > 150:
            embed = self._error_embed(
                interaction,
                "commands.play.volume.command.invalid_title",
                "commands.play.volume.command.invalid_description",
            )
            return await self._send_interaction_message(interaction, embed=embed)

        old_volume = player.volume
        await player.set_volume(level)

        embed = discord.Embed(
            title=self._translate(interaction, "commands.play.volume.command.title"),
            description=self._translate(
                interaction,
                "commands.play.volume.command.description",
                old=old_volume,
                new=level,
            ),
            color=0x00ff00
        )

        # Indicador visual do volume
        volume_bar_length = 20
        filled_length = int((level / 150) * volume_bar_length)
        volume_bar = '█' * filled_length + '░' * (volume_bar_length - filled_length)

        embed.add_field(
            name=self._translate(interaction, "commands.play.volume.command.level_label"),
            value=self._translate(
                interaction,
                "commands.play.volume.command.level_value",
                level=level,
                bar=volume_bar,
            ),
            inline=False
        )

        if level > 100:
            embed.add_field(
                name=self._translate(interaction, "commands.play.volume.warning.title"),
                value=self._translate(interaction, "commands.play.volume.warning.description"),
                inline=False
            )
        elif level == 0:
            embed.add_field(
                name=self._translate(interaction, "commands.play.volume.command.silenced_title"),
                value=self._translate(interaction, "commands.play.volume.command.silenced_description"),
                inline=False
            )

        # Adiciona controles de volume
        view = MusicControlView(
            self.bot,
            player=player,
            guild_id=interaction.guild.id if interaction.guild else None,
        )
        await self._send_interaction_message(interaction, embed=embed, view=view)

    @app_commands.command(name="skip", description="Skip the current track")
    async def skip(self, interaction: discord.Interaction):
        """Pula a música atual"""
        player = await self._player_or_error(interaction)
        if not player:
            return

        if not player.playing:
            embed = self._error_embed(
                interaction,
                "commands.common.embeds.error_title",
                "commands.common.errors.no_track_full",
            )
            return await self._send_interaction_message(interaction, embed=embed)

        skipped_track = player.current.title if player.current else self._translate(
            interaction,
            "commands.common.labels.unknown_track",
        )
        try:
            await player.skip(force=True)
        except Exception:
            await player.stop()

        embed = discord.Embed(
            title=self._translate(interaction, "commands.play.skip.embed_title"),
            description=self._translate(
                interaction,
                "commands.play.skip.embed_description",
                track=skipped_track,
            ),
            color=0x00ff00
        )

        if not player.queue.is_empty:
            next_track = list(player.queue)[0]
            embed.add_field(
                name=self._translate(interaction, "commands.play.skip.next_label"),
                value=self._translate(
                    interaction,
                    "commands.play.skip.next_value",
                    title=next_track.title,
                    author=next_track.author or self._translate(interaction, "commands.common.labels.unknown_author"),
                ),
                inline=False
            )

        await self._send_interaction_message(interaction, embed=embed)

    @app_commands.command(name="stop", description="Stop playback and clear the queue")
    async def stop(self, interaction: discord.Interaction):
        """Para a música e limpa a fila"""
        player = await self._player_or_error(interaction)
        if not player:
            return

        try:
            await self.bot._clear_now_playing_message(player)
        except Exception as exc:
            print(f"Falha ao limpar estado de reprodução antes de desconectar: {exc}")

        await player.disconnect()

        embed = discord.Embed(
            title=self._translate(interaction, "commands.play.stop.embed_title"),
            description=self._translate(interaction, "commands.play.stop.embed_description"),
            color=0x00ff00
        )
        embed.set_footer(
            text=self._translate(interaction, "commands.play.stop.footer")
        )
        await self._send_interaction_message(interaction, embed=embed)

    @app_commands.command(name="pause", description="Pause or resume playback")
    async def pause(self, interaction: discord.Interaction):
        """Pausa ou retoma a música"""
        player = await self._player_or_error(interaction)
        if not player:
            return

        if not player.current:
            embed = self._error_embed(
                interaction,
                "commands.common.embeds.error_title",
                "commands.common.errors.no_track_full",
            )
            return await self._send_interaction_message(interaction, embed=embed)

        if player.paused:
            await player.pause(False)
            embed = discord.Embed(
                title=self._translate(interaction, "commands.play.pause.resumed_title"),
                description=self._translate(
                    interaction,
                    "commands.play.pause.resumed_description",
                    title=player.current.title,
                ),
                color=0x00ff00
            )
        else:
            await player.pause(True)
            embed = discord.Embed(
                title=self._translate(interaction, "commands.play.pause.paused_title"),
                description=self._translate(
                    interaction,
                    "commands.play.pause.paused_description",
                    title=player.current.title,
                ),
                color=0xffff00
            )

        await self._send_interaction_message(interaction, embed=embed)

    @app_commands.command(name="seek", description="Seek to a specific position in the current track (e.g. 2m2s, 90s, 1:30)")
    @app_commands.describe(time="Tempo para ir (ex: 2m2s, 90s, 1:30, 150)")
    async def seek(self, interaction: discord.Interaction, time: str):
        """Comando para pular para um tempo específico da música atual."""
        player = await self._player_or_error(interaction, ephemeral=True)
        if not player:
            return

        if not player.current:
            message = self._translate(interaction, "commands.play.seek.errors.no_track")
            return await self._send_interaction_message(interaction, content=message, ephemeral=True)

        def parse_time(timestr):
            # Aceita formatos: 1:30, 2m2s, 90s, 150, 1m, 1h2m3s
            timestr = timestr.strip().lower()
            if re.match(r"^\d+:\d{1,2}$", timestr):
                # Formato 1:30
                parts = timestr.split(":")
                return int(parts[0]) * 60 * 1000 + int(parts[1]) * 1000
            total_ms = 0
            matches = re.findall(r"(\d+)(h|m|s)", timestr)
            if matches:
                for value, unit in matches:
                    value = int(value)
                    if unit == "h":
                        total_ms += value * 3600 * 1000
                    elif unit == "m":
                        total_ms += value * 60 * 1000
                    elif unit == "s":
                        total_ms += value * 1000
                return total_ms
            # Só número (segundos)
            if timestr.isdigit():
                return int(timestr) * 1000
            raise ValueError(self._translate(interaction, "commands.play.seek.errors.format"))

        try:
            ms = parse_time(time)
            if ms < 0 or ms > player.current.length:
                message = self._translate(
                    interaction,
                    "commands.play.seek.errors.out_of_range",
                    max_seconds=player.current.length // 1000,
                )
                return await self._send_interaction_message(interaction, content=message, ephemeral=True)
            await player.seek(ms)
            success_message = self._translate(
                interaction,
                "commands.play.seek.success",
                timestamp=time,
            )
            await self._send_interaction_message(interaction, content=success_message, ephemeral=True)
        except Exception as e:
            error_message = self._translate(
                interaction,
                "commands.play.seek.errors.generic",
                error=e,
            )
            await self._send_interaction_message(interaction, content=error_message, ephemeral=True)


async def setup(bot):
    await bot.add_cog(PlayCommands(bot))
    # Registra a view de controles como persistente (botões continuam após restart)
    bot.add_view(MusicControlView(bot))