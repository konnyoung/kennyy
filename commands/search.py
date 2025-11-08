import discord
from discord.ext import commands
from discord import app_commands
import wavelink

from commands import player_is_ready, resolve_wavelink_player


class MusicControlView(discord.ui.View):
    """View com botões de controle de música"""

    def __init__(self, bot, player: wavelink.Player | None = None, *, guild_id: int | None = None):
        super().__init__(timeout=None)
        self.bot = bot
        resolved_guild_id = guild_id
        if resolved_guild_id is None and player and getattr(player, "guild", None):
            resolved_guild_id = player.guild.id
        self._guild_id = resolved_guild_id

        initial_mode = None
        if player:
            if hasattr(player, "loop_mode_override"):
                initial_mode = player.loop_mode_override
            elif getattr(player, "queue", None):
                initial_mode = player.queue.mode
        self._apply_loop_button_state(initial_mode)

    def _translate(
        self,
        interaction: discord.Interaction,
        key: str,
        *,
        default: str | None = None,
        **kwargs,
    ) -> str:
        translator = getattr(self.bot, "translate", None)
        guild_id = interaction.guild.id if interaction.guild else None
        if translator:
            return translator(key, guild_id=guild_id, default=default, **kwargs)
        return default if default is not None else key

    def _translate_static(
        self,
        key: str,
        *,
        default: str | None = None,
        **kwargs,
    ) -> str:
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

    async def _send_ephemeral(self, interaction: discord.Interaction, message: str | None) -> None:
        if not message:
            return

        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

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
            message = self._translate(
                interaction,
                "commands.common.errors.bot_not_connected",
                default="❌ Bot não conectado!",
            )
            await self._send_ephemeral(interaction, message)
            return None

        user_channel = getattr(interaction.user.voice, "channel", None)
        if user_channel is None:
            message = self._translate(
                interaction,
                "commands.play.errors.user_not_in_voice",
                default="Você precisa estar em um canal de voz!",
            )
            await self._send_ephemeral(interaction, message)
            return None

        player_channel = getattr(resolved_player, "channel", None)
        if player_channel is None and guild is not None:
            bot_voice_state = getattr(guild.me, "voice", None)
            player_channel = getattr(bot_voice_state, "channel", None)

        if player_channel is None and getattr(resolved_player, "connected", False):
            player_channel = user_channel

        if user_channel.id != player_channel.id:
            message = self._translate(
                interaction,
                "commands.play.errors.same_voice_channel",
                default="Você precisa estar no mesmo canal de voz que o bot para usar estes controles!",
            )
            await self._send_ephemeral(interaction, message)
            return None

        if getattr(resolved_player, "channel", None) is None:
            try:
                resolved_player.channel = player_channel or user_channel  # type: ignore[assignment]
            except Exception:
                pass

        return resolved_player

    @discord.ui.button(emoji="🔁", style=discord.ButtonStyle.secondary, custom_id="search_loop", row=1)
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

        message = self._translate(
            interaction,
            response_key,
            default=response_default,
        )

        await self._send_ephemeral(interaction, message or response_default)

        try:
            await interaction.message.edit(view=self)
        except Exception:
            pass

    @discord.ui.button(emoji="⏮️", style=discord.ButtonStyle.secondary, custom_id="search_previous", row=0)
    async def previous_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Botão para música anterior"""
        raw_player = interaction.guild.voice_client
        player = await self._ensure_control_access(interaction, raw_player)
        if not player:
            return

        if not player.current:
            await self._send_ephemeral(
                interaction,
                self._translate(
                    interaction,
                    "commands.common.errors.no_track",
                    default="❌ Não há música tocando!",
                ),
            )
            return

        if player.position > 10000:
            await player.seek(0)
            await self._send_ephemeral(
                interaction,
                self._translate(
                    interaction,
                    "commands.play.previous.restarted",
                    default="⏮️ Música reiniciada!",
                ),
            )
        else:
            await self._send_ephemeral(
                interaction,
                self._translate(
                    interaction,
                    "commands.play.previous.none",
                    default="⏮️ Não há música anterior!",
                ),
            )

    @discord.ui.button(emoji="⏯️", style=discord.ButtonStyle.primary, custom_id="search_play_pause", row=0)
    async def play_pause_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Botão para pausar/retomar"""
        raw_player = interaction.guild.voice_client
        player = await self._ensure_control_access(interaction, raw_player)
        if not player:
            return

        if not player.current:
            await self._send_ephemeral(
                interaction,
                self._translate(
                    interaction,
                    "commands.common.errors.no_track",
                    default="❌ Não há música tocando!",
                ),
            )
            return

        if player.paused:
            await player.pause(False)
            await self._send_ephemeral(
                interaction,
                self._translate(
                    interaction,
                    "commands.play.toggle.resumed",
                    default="▶️ Reprodução retomada!",
                ),
            )
        else:
            await player.pause(True)
            await self._send_ephemeral(
                interaction,
                self._translate(
                    interaction,
                    "commands.play.toggle.paused",
                    default="⏸️ Reprodução pausada!",
                ),
            )

    @discord.ui.button(emoji="⏹️", style=discord.ButtonStyle.danger, custom_id="search_stop", row=0)
    async def stop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Botão para parar reprodução"""
        raw_player = interaction.guild.voice_client
        player = await self._ensure_control_access(interaction, raw_player)
        if not player:
            return

        await player.disconnect()
        await self._send_ephemeral(
            interaction,
            self._translate(
                interaction,
                "commands.play.stop.success",
                default="⏹️ Reprodução parada e fila limpa!",
            ),
        )

        for item in self.children:
            item.disabled = True

        try:
            await interaction.edit_original_response(view=self)
        except:
            pass

    @discord.ui.button(emoji="⏭️", style=discord.ButtonStyle.secondary, custom_id="search_skip", row=0)
    async def skip_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Botão para pular música"""
        raw_player = interaction.guild.voice_client
        player = await self._ensure_control_access(interaction, raw_player)
        if not player:
            return

        if not player.playing:
            await self._send_ephemeral(
                interaction,
                self._translate(
                    interaction,
                    "commands.common.errors.no_track",
                    default="❌ Não há música tocando!",
                ),
            )
            return

        try:
            await player.skip(force=True)
        except Exception:
            await player.stop()

        await self._send_ephemeral(
            interaction,
            self._translate(
                interaction,
                "commands.play.skip.success",
                default="⏭️ Música pulada!",
            ),
        )

    @discord.ui.button(emoji="🔀", style=discord.ButtonStyle.secondary, custom_id="search_shuffle", row=0)
    async def shuffle_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Botão para embaralhar fila"""
        raw_player = interaction.guild.voice_client
        player = await self._ensure_control_access(interaction, raw_player)
        if not player:
            return

        if player.queue.is_empty:
            await self._send_ephemeral(
                interaction,
                self._translate(
                    interaction,
                    "commands.play.shuffle.empty",
                    default="❌ Não há músicas na fila para embaralhar!",
                ),
            )
            return

        player.queue.shuffle()
        await self._send_ephemeral(
            interaction,
            self._translate(
                interaction,
                "commands.play.shuffle.success",
                default=f"🔀 Fila embaralhada! ({player.queue.count} músicas)",
                count=player.queue.count,
            ),
        )


class SearchDropdown(discord.ui.Select):
    """Dropdown para seleção de músicas da busca"""
    def __init__(self, bot, user, tracks):
        self.bot = bot
        self.user = user
        self.tracks = tracks
        self._guild_id = getattr(user.guild, "id", None)

        def t(key: str, *, default: str | None = None, **kwargs) -> str:
            translator = getattr(self.bot, "translate", None)
            if translator:
                return translator(key, guild_id=self._guild_id, default=default, **kwargs)
            return default if default is not None else key

        # Cria opções do dropdown
        options = []
        for i, track in enumerate(tracks[:25]):  # Máximo 25 opções do Discord
            duration = self.format_time_from_ms(track.length) if track.length else "N/A"
            title = track.title[:50] if track.title else t(
                "commands.common.labels.unknown_track",
                default="Título desconhecido",
            )
            author = (track.author[:40] if track.author else t(
                "commands.common.labels.unknown_author",
                default="Desconhecido",
            ))

            label = f"{i + 1}. {title}"
            description = t(
                "commands.search.dropdown.option_description",
                default=f"👤 {author} • ⏱️ {duration}",
                author=author,
                duration=duration,
            )

            options.append(discord.SelectOption(
                label=label,
                description=description,
                value=str(i)
            ))

        super().__init__(
            placeholder=t(
                "commands.search.dropdown.placeholder",
                default="🎵 Selecione uma música para tocar...",
            ),
            min_values=1,
            max_values=1,
            options=options
        )

    def format_time_from_ms(self, milliseconds):
        """Formata tempo de milissegundos para MM:SS"""
        if milliseconds is None or milliseconds == 0:
            return "00:00"
        seconds = int(milliseconds / 1000)
        minutes = seconds // 60
        seconds = seconds % 60
        return f"{minutes:02d}:{seconds:02d}"

    async def callback(self, interaction: discord.Interaction):
        """Callback quando usuário seleciona uma música"""
        if interaction.user.id != self.user.id:
            translator = getattr(self.bot, "translate", None)
            message = "❌ Apenas quem executou o comando pode usar este menu!"
            if translator:
                message = translator(
                    "commands.search.dropdown.errors.only_requester",
                    guild_id=interaction.guild.id if interaction.guild else self._guild_id,
                    default=message,
                )
            await interaction.response.send_message(message, ephemeral=True)
            return

        try:
            # Pega a música selecionada
            selected_index = int(self.values[0])
            track = self.tracks[selected_index]

            # Verifica se o usuário está em um canal de voz
            if not interaction.user.voice:
                translator = getattr(self.bot, "translate", None)
                message = "❌ Você precisa estar em um canal de voz para tocar música!"
                if translator:
                    message = translator(
                        "commands.search.dropdown.errors.user_not_in_voice",
                        guild_id=interaction.guild.id if interaction.guild else self._guild_id,
                        default=message,
                    )
                await interaction.response.send_message(message, ephemeral=True)
                return

            guild = interaction.guild
            player = resolve_wavelink_player(self.bot, guild) if guild else None

            if not player_is_ready(player):
                try:
                    voice_channel = interaction.user.voice.channel
                except AttributeError:
                    translator = getattr(self.bot, "translate", None)
                    message = "❌ Você precisa estar em um canal de voz para tocar música!"
                    if translator:
                        message = translator(
                            "commands.search.dropdown.errors.user_not_in_voice",
                            guild_id=guild.id if guild else self._guild_id,
                            default=message,
                        )
                    await interaction.response.send_message(message, ephemeral=True)
                    return

                try:
                    player = await voice_channel.connect(cls=wavelink.Player)
                except Exception as e:
                    translator = getattr(self.bot, "translate", None)
                    message = f"❌ Erro ao conectar ao canal de voz: {e}"
                    if translator:
                        message = translator(
                            "commands.search.dropdown.errors.connect_voice",
                            guild_id=guild.id if guild else self._guild_id,
                            default=message,
                            error=e,
                        )
                    await interaction.response.send_message(message, ephemeral=True)
                    return
            else:
                existing_channel = getattr(player, "channel", None)
                user_channel = getattr(interaction.user.voice, "channel", None)
                if existing_channel and user_channel and existing_channel.id != user_channel.id:
                    translator = getattr(self.bot, "translate", None)
                    message = "❌ Você precisa estar no mesmo canal de voz que o bot!"
                    if translator:
                        message = translator(
                            "commands.play.errors.same_voice_channel",
                            guild_id=guild.id if guild else self._guild_id,
                            default=message,
                        )
                    await interaction.response.send_message(message, ephemeral=True)
                    return

            # Define o canal de texto para mensagens automáticas (fim da playlist)
            try:
                player.text_channel = interaction.channel
            except Exception:
                pass

            # Se já está tocando algo, adiciona à fila ou substitui
            if player.playing:
                await player.queue.put_wait(track)

                embed = discord.Embed(
                    title=self._translate_message(interaction, "commands.play.queue.added.title", default="➕ Adicionado à Fila"),
                    description=self._translate_message(
                        interaction,
                        "commands.play.queue.added.description",
                        default=f"**{track.title}**",
                        title=track.title,
                    ),
                    color=0x0099ff
                )

                embed.add_field(
                    name=self._translate_message(interaction, "commands.common.labels.artist", default="👤 Artista"),
                    value=track.author or self._translate_message(
                        interaction,
                        "commands.common.labels.unknown_author",
                        default="Desconhecido",
                    ),
                    inline=True
                )
                embed.add_field(
                    name=self._translate_message(
                        interaction,
                        "commands.play.queue.added.position_label",
                        default="📋 Posição na fila",
                    ),
                    value=self._translate_message(
                        interaction,
                        "commands.play.queue.added.position_value",
                        default=f"#{player.queue.count}",
                        position=player.queue.count,
                    ),
                    inline=True
                )
                embed.add_field(
                    name=self._translate_message(
                        interaction,
                        "commands.play.queue.added.duration_label",
                        default="⏱️ Duração",
                    ),
                    value=self.format_time_from_ms(track.length),
                    inline=True
                )

                if hasattr(track, 'artwork') and track.artwork:
                    embed.set_thumbnail(url=track.artwork)

                embed.set_footer(
                    text=self._translate_message(
                        interaction,
                        "commands.search.dropdown.footer.queue",
                        default="🔍 Selecionado da busca",
                    )
                )

                confirmation_message = self._translate_message(
                    interaction,
                    "commands.search.dropdown.queue_confirmation",
                    default="➕ **{title}** adicionada à fila na posição #{position}",
                    title=track.title,
                    position=player.queue.count,
                )

                await interaction.response.edit_message(
                    content=confirmation_message,
                    embed=None,
                    view=None,
                )
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await player.play(track)

                embed = discord.Embed(
                    title=self._translate_message(
                        interaction,
                        "commands.play.now_playing.title",
                        default="🎵 Tocando Agora",
                    ),
                    description=self._translate_message(
                        interaction,
                        "commands.play.now_playing.description",
                        default=f"**{track.title}**",
                        title=track.title,
                    ),
                    color=0x00ff00
                )

                embed.add_field(
                    name=self._translate_message(interaction, "commands.common.labels.artist", default="👤 Artista"),
                    value=track.author or self._translate_message(
                        interaction,
                        "commands.common.labels.unknown_author",
                        default="Desconhecido",
                    ),
                    inline=True
                )
                embed.add_field(
                    name=self._translate_message(
                        interaction,
                        "commands.common.labels.duration",
                        default="⏱️ Duração",
                    ),
                    value=self.format_time_from_ms(track.length),
                    inline=True
                )
                embed.add_field(
                    name=self._translate_message(
                        interaction,
                        "commands.play.now_playing.volume_label",
                        default="🔊 Volume",
                    ),
                    value=f"{player.volume}%",
                    inline=True
                )

                if hasattr(track, 'artwork') and track.artwork:
                    embed.set_thumbnail(url=track.artwork)

                embed.set_footer(
                    text=self._translate_message(
                        interaction,
                        "commands.search.dropdown.footer.play",
                        default="🔍 Selecionado da busca",
                    )
                )

                confirmation_message = self._translate_message(
                    interaction,
                    "commands.search.dropdown.play_confirmation",
                    default="▶️ Tocando agora: **{title}**",
                    title=track.title,
                )

                await interaction.response.edit_message(
                    content=confirmation_message,
                    embed=None,
                    view=None,
                )
                await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            if not interaction.response.is_done():
                translator = getattr(self.bot, "translate", None)
                message = f"❌ Erro ao reproduzir música: {e}"
                if translator:
                    message = translator(
                        "commands.search.dropdown.errors.play",
                        guild_id=interaction.guild.id if interaction.guild else self._guild_id,
                        default=message,
                        error=e,
                    )
                await interaction.response.send_message(message, ephemeral=True)

    def _translate_message(
        self,
        interaction: discord.Interaction,
        key: str,
        *,
        default: str | None = None,
        **kwargs,
    ) -> str:
        translator = getattr(self.bot, "translate", None)
        guild_id = interaction.guild.id if interaction.guild else self._guild_id
        if translator:
            return translator(key, guild_id=guild_id, default=default, **kwargs)
        return default if default is not None else key


class SearchView(discord.ui.View):
    """View que contém o dropdown e botão de cancelar"""
    def __init__(self, bot, user, tracks, query):
        super().__init__(timeout=300)  # 5 minutos de timeout
        self.bot = bot
        self.user = user
        self.query = query
        self._guild_id = getattr(user.guild, "id", None)

        # Adiciona o dropdown
        self.add_item(SearchDropdown(bot, user, tracks))
        self._localize_components()

    def _translate(
        self,
        interaction: discord.Interaction | None,
        key: str,
        *,
        default: str | None = None,
        **kwargs,
    ) -> str:
        translator = getattr(self.bot, "translate", None)
        guild_id = self._guild_id
        if interaction and interaction.guild:
            guild_id = interaction.guild.id
        if translator:
            return translator(key, guild_id=guild_id, default=default, **kwargs)
        return default if default is not None else key

    def _localize_components(self) -> None:
        for item in self.children:
            if isinstance(item, discord.ui.Button) and item.custom_id == "search_cancel":
                item.label = self._translate(None, "commands.search.view.cancel.label", default=item.label)

    @discord.ui.button(label="❌ Cancelar", style=discord.ButtonStyle.secondary, row=1, custom_id="search_cancel")
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Botão para cancelar a busca"""
        if interaction.user.id != self.user.id:
            message = self._translate(
                interaction,
                "commands.search.view.cancel.errors.only_requester",
                default="❌ Apenas quem executou o comando pode cancelar!",
            )
            await interaction.response.send_message(message, ephemeral=True)
            return

        embed = discord.Embed(
            title=self._translate(
                interaction,
                "commands.search.view.cancel.embed.title",
                default="🚫 Busca Cancelada",
            ),
            description=self._translate(
                interaction,
                "commands.search.view.cancel.embed.description",
                default=f"Busca por **{self.query}** foi cancelada.",
                query=self.query,
            ),
            color=0xff0000
        )

        await interaction.response.edit_message(embed=embed, view=None)

    async def on_timeout(self):
        """Chamado quando a view expira"""
        for item in self.children:
            item.disabled = True


class SearchCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

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

    @app_commands.command(name="search", description="Search for tracks and choose which one to play")
    @app_commands.describe(query="Termo de busca ou URL para encontrar músicas")
    async def search(self, interaction: discord.Interaction, query: str):
        """Comando de busca com dropdown de seleção"""
        await interaction.response.defer()

        # Verifica se o usuário está em um canal de voz
        if not interaction.user.voice:
            embed = discord.Embed(
                title=self._translate(
                    interaction,
                    "commands.common.embeds.error_title",
                    default="❌ Erro",
                ),
                description=self._translate(
                    interaction,
                    "commands.search.errors.user_not_in_voice",
                    default="Você precisa estar em um canal de voz para usar este comando!",
                ),
                color=0xff0000
            )
            return await interaction.followup.send(embed=embed)

        try:
            # Realiza a busca
            if query.startswith(('http://', 'https://')):
                tracks = await interaction.client.search_with_failover(query)
            else:
                tracks = await interaction.client.search_with_failover(f"ytsearch:{query}")

            if not tracks:
                embed = discord.Embed(
                    title=self._translate(
                        interaction,
                        "commands.search.errors.no_results.title",
                        default="❌ Nenhum Resultado",
                    ),
                    description=self._translate(
                        interaction,
                        "commands.search.errors.no_results.description",
                        default=f"Não foram encontradas músicas para: **{query}**",
                        query=query,
                    ),
                    color=0xff0000
                )
                embed.add_field(
                    name=self._translate(
                        interaction,
                        "commands.search.errors.no_results.tips_label",
                        default="💡 Dicas",
                    ),
                    value=self._translate(
                        interaction,
                        "commands.search.errors.no_results.tips_value",
                        default="• Tente termos mais específicos\n• Verifique a ortografia\n• Use nome do artista + música",
                    ),
                    inline=False
                )
                return await interaction.followup.send(embed=embed)

            # Se for playlist, pega algumas músicas dela
            if isinstance(tracks, wavelink.Playlist):
                playlist_tracks = tracks.tracks[:25]  # Primeiras 25 músicas da playlist

                embed = discord.Embed(
                    title=self._translate(
                        interaction,
                        "commands.search.playlist.title",
                        default="🔍 Resultados da Busca - Playlist",
                    ),
                    description=self._translate(
                        interaction,
                        "commands.search.playlist.description",
                        default=f"**{tracks.name}**\nSelecione uma música da playlist:",
                        name=tracks.name,
                    ),
                    color=0x0099ff
                )

                if hasattr(tracks, 'artwork') and tracks.artwork:
                    embed.set_thumbnail(url=tracks.artwork)

                embed.add_field(
                    name=self._translate(
                        interaction,
                        "commands.search.playlist.info_label",
                        default="📋 Informações",
                    ),
                    value=self._translate(
                        interaction,
                        "commands.search.playlist.info_value",
                        default=f"Total de músicas: {len(tracks.tracks)}\nExibindo: {len(playlist_tracks)}",
                        total=len(tracks.tracks),
                        showing=len(playlist_tracks),
                    ),
                    inline=True
                )

                tracks = playlist_tracks
            else:
                # Lista de músicas individuais
                embed = discord.Embed(
                    title=self._translate(
                        interaction,
                        "commands.search.results.title",
                        default="🔍 Resultados da Busca",
                    ),
                    description=self._translate(
                        interaction,
                        "commands.search.results.description",
                        default=f"Encontrados **{len(tracks)}** resultados para: **{query}**\nSelecione uma música:",
                        count=len(tracks),
                        query=query,
                    ),
                    color=0x0099ff
                )

                embed.add_field(
                    name=self._translate(
                        interaction,
                        "commands.search.results.howto_label",
                        default="💡 Como usar",
                    ),
                    value=self._translate(
                        interaction,
                        "commands.search.results.howto_value",
                        default="Use o menu abaixo para selecionar a música desejada",
                    ),
                    inline=False
                )

            # Mostra primeiras 3 músicas como prévia
            preview_list = []
            for i, track in enumerate(tracks[:3]):
                duration = self.format_time_from_ms(track.length) if track.length else "N/A"
                preview_list.append(
                    self._translate(
                        interaction,
                        "commands.search.results.preview_item",
                        default=f"`{i + 1}.` **{track.title[:40]}**\n👤 {track.author or 'Desconhecido'} • ⏱️ {duration}",
                        index=i + 1,
                        title=track.title[:40],
                        author=track.author or self._translate(
                            interaction,
                            "commands.common.labels.unknown_author",
                            default="Desconhecido",
                        ),
                        duration=duration,
                    )
                )

            if len(tracks) > 3:
                preview_list.append(
                    self._translate(
                        interaction,
                        "commands.search.results.preview_more",
                        default=f"... e mais **{len(tracks) - 3}** música(s)",
                        remaining=len(tracks) - 3,
                    )
                )

            embed.add_field(
                name=self._translate(
                    interaction,
                    "commands.search.results.preview_label",
                    default="🎵 Prévia dos Resultados",
                ),
                value="\n\n".join(preview_list),
                inline=False
            )

            embed.set_footer(
                text=self._translate(
                    interaction,
                    "commands.search.results.footer",
                    default="💡 Use o dropdown abaixo para selecionar • Expira em 5 minutos",
                )
            )

            # Cria view com dropdown
            view = SearchView(self.bot, interaction.user, tracks[:25], query)

            await interaction.followup.send(embed=embed, view=view)

        except Exception as e:
            embed = discord.Embed(
                title=self._translate(
                    interaction,
                    "commands.common.embeds.error_title",
                    default="❌ Erro",
                ),
                description=self._translate(
                    interaction,
                    "commands.search.errors.generic",
                    default=f"Ocorreu um erro ao buscar: {e}",
                    error=e,
                ),
                color=0xff0000
            )
            await interaction.followup.send(embed=embed)

    def format_time_from_ms(self, milliseconds):
        """Formata tempo de milissegundos para MM:SS"""
        if milliseconds is None or milliseconds == 0:
            return "00:00"
        seconds = int(milliseconds / 1000)
        minutes = seconds // 60
        seconds = seconds % 60
        return f"{minutes:02d}:{seconds:02d}"


async def setup(bot):
    await bot.add_cog(SearchCommands(bot))
