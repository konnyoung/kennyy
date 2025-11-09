import discord
from discord.ext import commands
from discord import app_commands
import wavelink
import asyncio
import math

from commands import player_is_ready, resolve_wavelink_player


class QueueControlView(discord.ui.View):
    """View com botões de controle e paginação para o comando queue"""

    def __init__(self, bot, player: wavelink.Player | None, *, per_page: int = 10):
        super().__init__(timeout=None)
        self.bot = bot
        self.player: wavelink.Player | None = player
        self.per_page = max(1, per_page)
        self.page = 0
        self.message: discord.Message | None = None

        self._apply_loop_button_state()

    def attach_message(self, message: discord.Message) -> None:
        self.message = message

    def _translate(
        self,
        interaction: discord.Interaction | None,
        key: str,
        *,
        default: str | None = None,
        **kwargs,
    ) -> str:
        translator = getattr(self.bot, "translate", None)
        guild_id = None
        if interaction and interaction.guild:
            guild_id = interaction.guild.id
        elif self.player and getattr(self.player, "guild", None):
            guild_id = self.player.guild.id
        if translator:
            return translator(key, guild_id=guild_id, default=default, **kwargs)
        return default if default is not None else key

    def _translate_static(self, key: str, *, default: str | None = None, **kwargs) -> str:
        translator = getattr(self.bot, "translate", None)
        guild_id = None
        if self.player and getattr(self.player, "guild", None):
            guild_id = self.player.guild.id
        if translator:
            return translator(key, guild_id=guild_id, default=default, **kwargs)
        return default if default is not None else key

    def _coerce_player(self, interaction: discord.Interaction | None) -> wavelink.Player | None:
        if interaction and interaction.guild:
            candidate = resolve_wavelink_player(self.bot, interaction.guild)
            if player_is_ready(candidate):
                self.player = candidate

        if not player_is_ready(self.player):
            self._apply_loop_button_state(wavelink.QueueMode.normal)
            return None

        self._apply_loop_button_state()
        return self.player

    def _apply_loop_button_state(self, mode: wavelink.QueueMode | None = None) -> None:
        button = getattr(self, "loop_button", None)
        if not isinstance(button, discord.ui.Button):
            return

        if mode is None:
            mode = wavelink.QueueMode.normal
            if self.player:
                bot_getter = getattr(self.bot, "_get_loop_mode", None)
                if callable(bot_getter):
                    try:
                        mode = bot_getter(self.player)
                    except Exception:
                        mode = wavelink.QueueMode.normal
                if mode is wavelink.QueueMode.normal:
                    queue_mode = wavelink.QueueMode.normal
                    if getattr(self.player, "queue", None):
                        queue_mode = getattr(self.player.queue, "mode", wavelink.QueueMode.normal)
                    mode = getattr(self.player, "loop_mode_override", queue_mode)

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

    def _update_pagination_buttons(self, page_count: int) -> None:
        try:
            self.prev_page_button.disabled = page_count <= 1 or self.page <= 0
            self.next_page_button.disabled = page_count <= 1 or self.page >= page_count - 1
        except AttributeError:
            pass

    async def create_queue_embed(self, player: wavelink.Player | None = None) -> discord.Embed:
        player = player or self.player
        guild = getattr(player, "guild", None)
        translator = getattr(self.bot, "translate", None)

        def t(key: str, *, default: str | None = None, **kwargs) -> str:
            if translator:
                return translator(
                    key,
                    guild_id=getattr(guild, "id", None),
                    default=default,
                    **kwargs,
                )
            return default if default is not None else key

        if not player:
            return discord.Embed(
                title=t("commands.queue.embed.title", default="Fila de Reprodução"),
                description=t(
                    "commands.common.errors.bot_not_connected_full",
                    default="O bot não está conectado a um canal de voz.",
                ),
                color=0xff0000,
            )

        queue_items = list(player.queue)
        queue_count = len(queue_items)

        if queue_count == 0 and not player.current:
            embed = discord.Embed(
                title=t("commands.queue.embed.title", default="Fila de Reprodução"),
                description=t("commands.queue.embed.empty_description", default="Nenhuma música na fila"),
                color=0xffff00,
                timestamp=discord.utils.utcnow(),
            )
            embed.set_footer(
                text=t(
                    "commands.queue.embed.footer",
                    default="Atualização em tempo real • Use /skipto para pular para uma música específica",
                )
            )
            self._update_pagination_buttons(1)
            return embed

        page_count = max(1, math.ceil(queue_count / self.per_page))
        if self.page >= page_count:
            self.page = page_count - 1
        if self.page < 0:
            self.page = 0
        self._update_pagination_buttons(page_count)

        embed = discord.Embed(
            title=t("commands.queue.embed.title", default="Fila de Reprodução"),
            color=0x0099ff,
            timestamp=discord.utils.utcnow(),
        )

        # Música atual
        if player.current:
            current_time = self.bot.format_time(player.position)
            total_time = self.bot.format_time(player.current.length)
            progress_percent = min(player.position / player.current.length, 1.0) if player.current.length > 0 else 0

            bar_length = 25
            filled_length = int(bar_length * progress_percent)
            bar = '█' * filled_length + '░' * (bar_length - filled_length)

            status_text = t(
                "commands.queue.embed.status.playing" if player.playing else "commands.queue.embed.status.paused",
                default="Reproduzindo" if player.playing else "Pausado",
            )

            if hasattr(player.current, "artwork") and player.current.artwork:
                embed.set_thumbnail(url=player.current.artwork)

            embed.add_field(
                name=t("commands.queue.embed.now_playing", default="▶ Tocando Agora"),
                value=f"**{player.current.title}**",
                inline=False,
            )

            embed.add_field(
                name=t("commands.common.labels.artist", default="Artista"),
                value=player.current.author or t("commands.common.labels.unknown_author", default="Desconhecido"),
                inline=True,
            )

            embed.add_field(
                name=t("commands.common.labels.duration", default="Duração"),
                value=total_time,
                inline=True,
            )

            embed.add_field(
                name=t("commands.queue.embed.status_label", default="Status"),
                value=status_text,
                inline=True,
            )

            embed.add_field(
                name=t("commands.queue.embed.progress_label", default="Progresso"),
                value=f"`{current_time}` {bar} `{total_time}`",
                inline=False,
            )

        # Página atual da fila
        start = self.page * self.per_page
        end = start + self.per_page
        page_items = queue_items[start:end]

        queue_lines: list[str] = []
        for idx, track in enumerate(page_items, start=start + 1):
            duration = self.bot.format_time(track.length) if track.length else "N/A"
            title = track.title if len(track.title) <= 45 else f"{track.title[:42]}…"
            queue_lines.append(
                t(
                    "commands.queue.embed.queue.item",
                    default=f"`{idx}.` {title} `({duration})`",
                    index=idx,
                    title=title,
                    duration=duration,
                )
            )

        if queue_lines:
            embed.add_field(
                name=t("commands.queue.embed.queue_label", default="Próximas na Fila"),
                value="\n".join(queue_lines),
                inline=False,
            )
        else:
            embed.add_field(
                name=t("commands.queue.embed.queue_label", default="Próximas na Fila"),
                value=t("commands.queue.embed.empty_queue", default="Nenhuma música na fila"),
                inline=False,
            )

        total_queue_duration = sum(track.length or 0 for track in queue_items)
        remaining_current = 0
        if player.current and player.current.length:
            remaining_current = max(player.current.length - player.position, 0)
        total_remaining = total_queue_duration + remaining_current

        embed.add_field(
            name=t("commands.queue.embed.total_label", default="Total de Músicas"),
            value=t("commands.queue.embed.total_value", default=str(queue_count), count=queue_count),
            inline=True,
        )

        embed.add_field(
            name=t("commands.queue.embed.queue_time_label", default="Tempo da Fila"),
            value=self.bot.format_time(total_queue_duration) if total_queue_duration > 0 else t(
                "commands.queue.embed.empty_queue_time",
                default="00:00",
            ),
            inline=True,
        )

        embed.add_field(
            name=t("commands.queue.embed.remaining_time_label", default="Tempo Restante"),
            value=self.bot.format_time(total_remaining) if total_remaining > 0 else t(
                "commands.queue.embed.empty_queue_time",
                default="00:00",
            ),
            inline=True,
        )

        embed.add_field(
            name=t("commands.queue.embed.page_label", default="Página"),
            value=f"{self.page + 1}/{page_count}",
            inline=True,
        )

        embed.set_footer(
            text=t(
                "commands.queue.embed.footer",
                default="Atualização em tempo real • Use /skipto para pular para uma música específica",
            )
        )
        return embed

    async def _edit_with_latest(self, interaction: discord.Interaction | None = None) -> None:
        player = self._coerce_player(interaction)
        embed = await self.create_queue_embed(player)
        if interaction is not None:
            if not interaction.response.is_done():
                await interaction.response.edit_message(embed=embed, view=self)
            else:
                await interaction.message.edit(embed=embed, view=self)
        elif self.message is not None:
            await self.message.edit(embed=embed, view=self)

    @discord.ui.button(emoji="⏮️", style=discord.ButtonStyle.secondary, custom_id="queue_previous")
    async def previous_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = self._coerce_player(interaction)

        if not player or not player.current:
            message = self._translate(
                interaction,
                "commands.common.errors.no_track",
                default="❌ Não há música tocando!",
            )
            await interaction.response.send_message(message, ephemeral=True)
            return

        if player.position > 10000:
            await player.seek(0)
            message = self._translate(
                interaction,
                "commands.play.previous.restarted",
                default="⏮️ Música reiniciada!",
            )
            await interaction.response.send_message(message, ephemeral=True)
        else:
            message = self._translate(
                interaction,
                "commands.play.previous.none",
                default="⏮️ Não há música anterior!",
            )
            await interaction.response.send_message(message, ephemeral=True)

    @discord.ui.button(emoji="⏯️", style=discord.ButtonStyle.primary, custom_id="queue_play_pause")
    async def play_pause_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = self._coerce_player(interaction)

        if not player or not player.current:
            message = self._translate(
                interaction,
                "commands.common.errors.no_track",
                default="❌ Não há música tocando!",
            )
            await interaction.response.send_message(message, ephemeral=True)
            return

        if player.paused:
            await player.pause(False)
            message = self._translate(
                interaction,
                "commands.play.toggle.resumed",
                default="▶️ Reprodução retomada!",
            )
            await interaction.response.send_message(message, ephemeral=True)
        else:
            await player.pause(True)
            message = self._translate(
                interaction,
                "commands.play.toggle.paused",
                default="⏸️ Reprodução pausada!",
            )
            await interaction.response.send_message(message, ephemeral=True)

    @discord.ui.button(emoji="⏹️", style=discord.ButtonStyle.danger, custom_id="queue_stop")
    async def stop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = self._coerce_player(interaction)

        if not player:
            message = self._translate(
                interaction,
                "commands.common.errors.bot_not_connected",
                default="❌ Bot não conectado!",
            )
            await interaction.response.send_message(message, ephemeral=True)
            return

        await player.disconnect()
        message = self._translate(
            interaction,
            "commands.play.stop.success",
            default="⏹️ Reprodução parada e fila limpa!",
        )
        await interaction.response.send_message(message, ephemeral=True)

        for item in self.children:
            item.disabled = True

        await self._edit_with_latest()

    @discord.ui.button(emoji="⏭️", style=discord.ButtonStyle.secondary, custom_id="queue_skip")
    async def skip_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = self._coerce_player(interaction)

        if not player or not player.current:
            message = self._translate(
                interaction,
                "commands.common.errors.no_track",
                default="❌ Não há música tocando!",
            )
            await interaction.response.send_message(message, ephemeral=True)
            return

        try:
            await player.skip(force=True)
        except Exception:
            await player.stop()

        message = self._translate(
            interaction,
            "commands.play.skip.success",
            default="⏭️ Música pulada!",
        )
        await interaction.response.send_message(message, ephemeral=True)
        await self._edit_with_latest()

    @discord.ui.button(emoji="🔁", style=discord.ButtonStyle.secondary, custom_id="queue_loop")
    async def loop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = self._coerce_player(interaction)

        if not player:
            message = self._translate(
                interaction,
                "commands.common.errors.bot_not_connected",
                default="❌ Bot não conectado!",
            )
            await interaction.response.send_message(message, ephemeral=True)
            return

        current_mode = getattr(getattr(player, "queue", None), "mode", wavelink.QueueMode.normal)

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

        apply_loop_mode = getattr(self.bot, "_apply_loop_mode", None)
        if callable(apply_loop_mode):
            try:
                apply_loop_mode(player, new_mode)
            except Exception:
                player.queue.mode = new_mode
                setattr(player, "loop_mode_override", new_mode)
        else:
            player.queue.mode = new_mode
            setattr(player, "loop_mode_override", new_mode)
        self._apply_loop_button_state(new_mode)

        message = self._translate(
            interaction,
            response_key,
            default=response_default,
        )

        if not interaction.response.is_done():
            await interaction.response.send_message(message, ephemeral=True)
        else:
            await interaction.followup.send(message, ephemeral=True)

        try:
            if interaction.message:
                await interaction.message.edit(view=self)
        except Exception:
            pass

        await self._edit_with_latest()

    @discord.ui.button(emoji="📜", style=discord.ButtonStyle.secondary, custom_id="queue_lyrics", row=1)
    async def lyrics_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = self._coerce_player(interaction)

        async def respond(message: str) -> None:
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(message, ephemeral=True)
                else:
                    await interaction.response.send_message(message, ephemeral=True)
            except (discord.NotFound, discord.HTTPException):
                pass

        if not player:
            message = self._translate(
                interaction,
                "commands.common.errors.bot_not_connected",
                default="❌ Bot não conectado!",
            )
            await respond(message)
            return

        if not player.current:
            message = self._translate(
                interaction,
                "commands.common.errors.no_track",
                default="❌ Não há música tocando!",
            )
            await respond(message)
            return

        lyrics_cog = self.bot.get_cog("LyricsCommands")
        if lyrics_cog is None:
            message = self._translate(
                interaction,
                "commands.lyrics.errors.feature_unavailable",
                default="❌ Letras indisponíveis no momento.",
            )
            await respond(message)
            return

        try:
            await lyrics_cog.handle_lyrics_interaction(interaction, ephemeral=False, player=player)
        except Exception as exc:
            print(f"Falha ao exibir letras pela view da fila: {exc}")
            message = self._translate(
                interaction,
                "commands.lyrics.errors.feature_unavailable",
                default="❌ Erro ao buscar letras.",
            )
            await respond(message)

    @discord.ui.button(emoji="🔄", style=discord.ButtonStyle.secondary, custom_id="queue_refresh")
    async def refresh_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await self._edit_with_latest(interaction)

    @discord.ui.button(emoji="◀️", style=discord.ButtonStyle.secondary, custom_id="queue_prev_page", row=1)
    async def prev_page_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.page > 0:
            self.page -= 1
        await self._edit_with_latest(interaction)

    @discord.ui.button(emoji="▶️", style=discord.ButtonStyle.secondary, custom_id="queue_next_page", row=1)
    async def next_page_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = self._coerce_player(interaction)
        queue_length = len(list(player.queue)) if player else 0
        page_count = max(1, math.ceil(queue_length / self.per_page)) if queue_length else 1
        if self.page < page_count - 1:
            self.page += 1
        await self._edit_with_latest(interaction)


class QueueCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.active_queue_messages = {}

    def _translate(
        self,
        interaction: discord.Interaction | None,
        key: str,
        *,
        default: str | None = None,
        **kwargs,
    ) -> str:
        translator = getattr(self.bot, "translate", None)
        guild_id = None
        if interaction and interaction.guild:
            guild_id = interaction.guild.id
        if translator:
            return translator(key, guild_id=guild_id, default=default, **kwargs)
        return default if default is not None else key

    def _translate_guild(self, guild: discord.Guild | None, key: str, *, default: str | None = None, **kwargs) -> str:
        translator = getattr(self.bot, "translate", None)
        guild_id = guild.id if guild else None
        if translator:
            return translator(key, guild_id=guild_id, default=default, **kwargs)
        return default if default is not None else key

    def _error_embed(
        self,
        interaction: discord.Interaction,
        title_key: str,
        description_key: str,
        *,
        title_default: str,
        description_default: str,
        color: int = 0xff0000,
        **kwargs,
    ) -> discord.Embed:
        title = self._translate(interaction, title_key, default=title_default, **kwargs)
        description = self._translate(
            interaction,
            description_key,
            default=description_default,
            **kwargs,
        )
        return discord.Embed(title=title, description=description, color=color)

    async def _send_interaction_message(
        self,
        interaction: discord.Interaction,
        *,
        content: str | None = None,
        embed: discord.Embed | None = None,
        view: discord.ui.View | None = None,
        ephemeral: bool = False,
    ) -> None:
        if content is None and embed is None:
            return

        view_param = view if view is not None else discord.utils.MISSING

        try:
            if interaction.response.is_done():
                await interaction.followup.send(
                    content=content,
                    embed=embed,
                    view=view_param,
                    ephemeral=ephemeral,
                )
            else:
                await interaction.response.send_message(
                    content=content,
                    embed=embed,
                    view=view_param,
                    ephemeral=ephemeral,
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
                title_default="❌ Erro",
                description_default="O bot não está conectado a um canal de voz.",
            )
            await self._send_interaction_message(
                interaction,
                embed=embed,
                ephemeral=ephemeral,
            )
            return None

        if getattr(player, "text_channel", None) is None and interaction.channel:
            try:
                player.text_channel = interaction.channel
            except Exception:
                pass

        return player

    async def _refresh_active_views(self, player: wavelink.Player | None) -> None:
        if not player or not getattr(player, "guild", None):
            return

        guild_id = player.guild.id
        for view in list(self.active_queue_messages.values()):
            if isinstance(view, QueueControlView) and view.message and view.player and getattr(view.player, "guild", None):
                if view.player.guild.id != guild_id:
                    continue
                try:
                    await view._edit_with_latest()
                except discord.NotFound:
                    continue
                except Exception as exc:
                    print(f"Erro ao atualizar visão de fila ativa: {exc}")

    async def update_queue_display(self, player: wavelink.Player, view: QueueControlView):
        """Atualiza a exibição da fila em tempo real"""
        while player and (player.current or not player.queue.is_empty):
            try:
                await view._edit_with_latest()
                await asyncio.sleep(10)
            except discord.NotFound:
                break
            except Exception as e:
                print(f"Erro ao atualizar fila: {e}")
                break

        if view.message and view.message.id in self.active_queue_messages:
            del self.active_queue_messages[view.message.id]

    @app_commands.command(name="queue", description="Show the playback queue with interactive controls")
    async def queue(self, interaction: discord.Interaction):
        """Mostra a fila de músicas com layout organizado em grid"""
        await interaction.response.defer()

        player = await self._player_or_error(interaction)
        if not player:
            return

        view = QueueControlView(self.bot, player)
        embed = await view.create_queue_embed(player)
        message = await interaction.followup.send(embed=embed, view=view)
        view.attach_message(message)

        self.active_queue_messages[message.id] = view

        if player.current or not player.queue.is_empty:
            asyncio.create_task(self.update_queue_display(player, view))

    @app_commands.command(name="skipto", description="Jump to a specific position in the queue")
    @app_commands.describe(position="Posição da música na fila (1, 2, 3...)")
    async def skipto(self, interaction: discord.Interaction, position: int):
        player = await self._player_or_error(interaction, ephemeral=True)
        if not player:
            return

        queue_items = list(player.queue)

        if not queue_items:
            embed = self._error_embed(
                interaction,
                "commands.common.embeds.error_title",
                "commands.queue.skipto.errors.empty",
                title_default="❌ Erro",
                description_default="Não há músicas na fila para pular.",
            )
            return await self._send_interaction_message(interaction, embed=embed, ephemeral=True)

        if position < 1 or position > len(queue_items):
            embed = self._error_embed(
                interaction,
                "commands.queue.skipto.errors.invalid_title",
                "commands.queue.skipto.errors.invalid_description",
                title_default="Posição inválida",
                description_default=f"Use um número entre 1 e {len(queue_items)}.",
                max_position=len(queue_items),
            )
            return await self._send_interaction_message(interaction, embed=embed, ephemeral=True)

        target_index = position - 1
        target_track = queue_items[target_index]
        remaining_tracks = queue_items[target_index + 1 :]

        original_queue = queue_items.copy()

        player.queue.clear()
        for track in remaining_tracks:
            await player.queue.put_wait(track)

        try:
            await player.play(target_track)
        except Exception as exc:  # noqa: BLE001
            player.queue.clear()
            for track in original_queue:
                await player.queue.put_wait(track)

            embed = self._error_embed(
                interaction,
                "commands.common.embeds.error_title",
                "commands.queue.skipto.errors.generic",
                title_default="❌ Erro",
                description_default=f"Não consegui pular para a posição solicitada: {exc}",
                error=exc,
            )
            return await self._send_interaction_message(interaction, embed=embed, ephemeral=True)

        embed = discord.Embed(
            title=self._translate(
                interaction,
                "commands.queue.skipto.success.title",
                default="Pulando para a música",
            ),
            description=self._translate(
                interaction,
                "commands.queue.skipto.success.description",
                default="⏭️ Pulando para **{title}**",
                title=target_track.title,
            ),
            color=0x00ff00,
        )

        embed.add_field(
            name=self._translate(
                interaction,
                "commands.common.labels.title",
                default="Título",
            ),
            value=target_track.title,
            inline=True,
        )

        embed.add_field(
            name=self._translate(
                interaction,
                "commands.common.labels.artist",
                default="Artista",
            ),
            value=target_track.author or self._translate(
                interaction,
                "commands.common.labels.unknown_author",
                default="Desconhecido",
            ),
            inline=True,
        )

        embed.add_field(
            name=self._translate(
                interaction,
                "commands.common.labels.position",
                default="Posição",
            ),
            value=str(position),
            inline=True,
        )

        await self._send_interaction_message(interaction, embed=embed)
        await self._refresh_active_views(player)

        bot = getattr(self, "bot", None)
        if bot and hasattr(bot, "_apply_track_start_effects"):
            try:
                await bot._apply_track_start_effects(player, target_track)
            except Exception as exc:
                print(f"Falha ao aplicar efeitos de início após skipto: {exc}")

    @app_commands.command(name="clear", description="Clear all upcoming tracks from the queue")
    async def clear(self, interaction: discord.Interaction):
        """Limpa a fila de músicas"""
        player = await self._player_or_error(interaction)
        if not player:
            return

        if player.queue.is_empty:
            embed = self._error_embed(
                interaction,
                "commands.common.embeds.error_title",
                "commands.queue.clear.errors.empty",
                title_default="❌ Erro",
                description_default="Não há músicas na fila para limpar.",
            )
            return await self._send_interaction_message(interaction, embed=embed)

        cleared_count = player.queue.count
        player.queue.clear()

        embed = discord.Embed(
            title=self._translate(
                interaction,
                "commands.queue.clear.success.title",
                default="Fila Limpa",
            ),
            color=0x00ff00
        )

        # Grid de informações
        embed.add_field(
            name=self._translate(
                interaction,
                "commands.queue.clear.success.removed_label",
                default="Músicas Removidas",
            ),
            value=self._translate(
                interaction,
                "commands.queue.clear.success.removed_value",
                default=f"{cleared_count}",
                count=cleared_count,
            ),
            inline=True
        )

        if player.current:
            current_title = player.current.title
            if len(current_title) > 30:
                current_title = current_title[:30] + "..."

            embed.add_field(
                name=self._translate(
                    interaction,
                    "commands.queue.labels.current_track",
                    default="Música Atual",
                ),
                value=self._translate(
                    interaction,
                    "commands.queue.clear.success.current_value",
                    default="Continua tocando",
                ),
                inline=True
            )

            embed.add_field(
                name=self._translate(
                    interaction,
                    "commands.common.labels.title",
                    default="Título",
                ),
                value=self._translate(
                    interaction,
                    "commands.queue.clear.success.title_value",
                    default=current_title,
                    title=current_title,
                ),
                inline=True
            )

        await self._send_interaction_message(interaction, embed=embed)
        await self._refresh_active_views(player)

    @app_commands.command(name="shuffle", description="Shuffle the current queue")
    async def shuffle(self, interaction: discord.Interaction):
        """Embaralha a fila"""
        player = await self._player_or_error(interaction)
        if not player:
            return

        if player.queue.is_empty:
            embed = self._error_embed(
                interaction,
                "commands.common.embeds.error_title",
                "commands.queue.shuffle.errors.empty",
                title_default="❌ Erro",
                description_default="Não há músicas suficientes na fila para embaralhar.",
            )
            return await self._send_interaction_message(interaction, embed=embed)

        player.queue.shuffle()

        embed = discord.Embed(
            title=self._translate(
                interaction,
                "commands.queue.shuffle.success.title",
                default="Fila Embaralhada",
            ),
            color=0x00ff00
        )

        # Grid de informações
        embed.add_field(
            name=self._translate(
                interaction,
                "commands.queue.shuffle.success.shuffled_label",
                default="Músicas Embaralhadas",
            ),
            value=self._translate(
                interaction,
                "commands.queue.shuffle.success.shuffled_value",
                default=f"{player.queue.count}",
                count=player.queue.count,
            ),
            inline=True
        )

        if not player.queue.is_empty:
            next_title = list(player.queue)[0].title if not player.queue.is_empty else "—"
            if len(next_title) > 25:
                next_title_display = next_title[:25] + "..."
            else:
                next_title_display = next_title

            embed.add_field(
                name=self._translate(
                    interaction,
                    "commands.queue.shuffle.success.next_track_label",
                    default="Próxima Música",
                ),
                value=self._translate(
                    interaction,
                    "commands.queue.shuffle.success.next_track_value",
                    default=next_title_display,
                    title=next_title_display,
                ),
                inline=True
            )
            embed.add_field(
                name=self._translate(
                    interaction,
                    "commands.queue.shuffle.success.status_label",
                    default="Status",
                ),
                value=self._translate(
                    interaction,
                    "commands.queue.shuffle.success.status_value",
                    default="Ordem alterada",
                ),
                inline=True
            )

        await self._send_interaction_message(interaction, embed=embed)
        await self._refresh_active_views(player)

    @app_commands.command(name="remove", description="Remove a specific track from the queue")
    @app_commands.describe(position="Posição da música na fila (1, 2, 3...)")
    async def remove(self, interaction: discord.Interaction, position: int):
        """Remove uma música da fila por posição"""
        player = await self._player_or_error(interaction)
        if not player:
            return

        if player.queue.is_empty:
            embed = self._error_embed(
                interaction,
                "commands.common.embeds.error_title",
                "commands.queue.remove.errors.empty",
                title_default="❌ Erro",
                description_default="Não há músicas na fila para remover.",
            )
            return await self._send_interaction_message(interaction, embed=embed)

        q = list(player.queue)
        if position < 1 or position > len(q):
            embed = self._error_embed(
                interaction,
                "commands.queue.remove.errors.invalid_title",
                "commands.queue.remove.errors.invalid_description",
                title_default="Posição Inválida",
                description_default=f"Use um número entre 1 e {len(q)}.",
                max_position=len(q),
            )
            return await self._send_interaction_message(interaction, embed=embed)

        removed_track = q.pop(position - 1)

        player.queue.clear()
        for t in q:
            await player.queue.put_wait(t)

        removed_title = removed_track.title
        if len(removed_title) > 30:
            removed_title = removed_title[:30] + "..."

        embed = discord.Embed(
            title=self._translate(
                interaction,
                "commands.queue.remove.success.title",
                default="Música Removida",
            ),
            color=0x00ff00
        )

        embed.add_field(
            name=self._translate(
                interaction,
                "commands.queue.remove.success.track_label",
                default="Música",
            ),
            value=self._translate(
                interaction,
                "commands.queue.remove.success.track_value",
                default=removed_title,
                title=removed_title,
            ),
            inline=True
        )
        embed.add_field(
            name=self._translate(
                interaction,
                "commands.common.labels.artist",
                default="Artista",
            ),
            value=removed_track.author or self._translate(
                interaction,
                "commands.common.labels.unknown_author",
                default="Desconhecido",
            ),
            inline=True
        )
        embed.add_field(
            name=self._translate(
                interaction,
                "commands.common.labels.position",
                default="Posição",
            ),
            value=self._translate(
                interaction,
                "commands.queue.remove.success.position_value",
                default=f"{position}",
                position=position,
            ),
            inline=True
        )
        embed.add_field(
            name=self._translate(
                interaction,
                "commands.queue.remove.success.remaining_label",
                default="Fila Restante",
            ),
            value=self._translate(
                interaction,
                "commands.queue.remove.success.remaining_value",
                default=f"{player.queue.count} música(s)",
                count=player.queue.count,
            ),
            inline=True
        )

        await self._send_interaction_message(interaction, embed=embed)
        await self._refresh_active_views(player)


async def setup(bot):
    await bot.add_cog(QueueCommands(bot))

