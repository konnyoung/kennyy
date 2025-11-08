import discord
from discord.ext import commands
from discord import app_commands
import wavelink
import re
import asyncio

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

        initial_mode = None
        if player:
            if hasattr(player, "loop_mode_override"):
                initial_mode = player.loop_mode_override
            elif getattr(player, "queue", None):
                initial_mode = player.queue.mode
        self._apply_loop_button_state(initial_mode)

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

        title = self._translate(interaction, "commands.play.volume.decrease.title")
        description = self._translate(
            interaction,
            "commands.play.volume.change.description",
            old=current_volume,
            new=new_volume,
        )

        embed = discord.Embed(title=title, description=description, color=0x0099ff)
        await self._send_ephemeral(interaction, embed=embed)

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

        title = self._translate(interaction, "commands.play.volume.increase.title")
        description = self._translate(
            interaction,
            "commands.play.volume.change.description",
            old=current_volume,
            new=new_volume,
        )

        embed = discord.Embed(title=title, description=description, color=0x0099ff)

        if new_volume > 100:
            warning_title = self._translate(interaction, "commands.play.volume.warning.title")
            warning_description = self._translate(
                interaction,
                "commands.play.volume.warning.description",
            )
            embed.add_field(name=warning_title, value=warning_description, inline=False)

        await self._send_ephemeral(interaction, embed=embed)

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
            message = self._translate(interaction, "commands.play.toggle.resumed")
            await self._send_ephemeral(interaction, message)
        else:
            await player.pause(True)
            message = self._translate(interaction, "commands.play.toggle.paused")
            await self._send_ephemeral(interaction, message)

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

        try:
            if interaction.response.is_done():
                await interaction.followup.send(
                    content=content,
                    embed=embed,
                    ephemeral=ephemeral,
                    view=view,
                )
            else:
                await interaction.response.send_message(
                    content=content,
                    embed=embed,
                    ephemeral=ephemeral,
                    view=view,
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

    async def search_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        """Função de autocomplete para buscar músicas"""
        if not current or len(current) < 2:
            return await self._send_autocomplete_choices(
                interaction,
                [
                    app_commands.Choice(
                        name=self._translate(interaction, "commands.play.autocomplete.prompt"),
                        value="",
                    )
                ],
            )

        # Se for uma URL, não mostra sugestões
        if self.is_url(current):
            return await self._send_autocomplete_choices(
                interaction,
                [
                    app_commands.Choice(
                        name=self._translate(
                            interaction,
                            "commands.play.autocomplete.url_detected",
                            value=current[:50],
                        ),
                        value=current,
                    )
                ],
            )

        try:
            # Busca no YouTube com o termo digitado (limita tempo para evitar timeout do Discord)
            search_query = f"ytsearch:{current}"
            tracks = await asyncio.wait_for(
                interaction.client.search_with_failover(search_query),
                timeout=AUTOCOMPLETE_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            return await self._send_autocomplete_choices(
                interaction,
                [
                    app_commands.Choice(
                        name=self._translate(interaction, "commands.play.autocomplete.timeout"),
                        value=current,
                    )
                ],
            )
        except Exception as e:
            print(f"Erro no autocomplete: {e}")
            return await self._send_autocomplete_choices(
                interaction,
                [
                    app_commands.Choice(
                        name=self._translate(
                            interaction,
                            "commands.play.autocomplete.error_choice",
                            value=current,
                        ),
                        value=current,
                    )
                ],
            )

        if tracks is None:
            return await self._send_autocomplete_choices(
                interaction,
                [
                    app_commands.Choice(
                        name=self._translate(interaction, "commands.play.autocomplete.no_results"),
                        value=current,
                    )
                ],
            )

        if isinstance(tracks, wavelink.Playlist):
            tracks = tracks.tracks

        playable_cls = getattr(wavelink, "Playable", None)
        if playable_cls and isinstance(tracks, playable_cls):
            tracks = [tracks]

        if not isinstance(tracks, list):
            try:
                tracks = list(tracks)
            except TypeError:
                tracks = []

        if not tracks:
            return await self._send_autocomplete_choices(
                interaction,
                [
                    app_commands.Choice(
                        name=self._translate(interaction, "commands.play.autocomplete.no_results"),
                        value=current,
                    )
                ],
            )

        # Cria lista de sugestões (máximo 25 por limitação do Discord)
        choices: list[app_commands.Choice[str]] = []
        for track in tracks[:25]:
            track_length = getattr(track, "length", None)
            duration = self.format_time_from_ms(track_length) if track_length else "N/A"
            author_value = getattr(track, "author", None)
            title_value = getattr(track, "title", None)

            author = (
                author_value[:30]
                if author_value
                else self._translate(interaction, "commands.common.labels.unknown_author")
            )
            title = (
                title_value[:50]
                if title_value
                else self._translate(interaction, "commands.play.autocomplete.unknown_title")
            )

            display_name = f"🎵 {title} - {author} ({duration})"
            if len(display_name) > 100:
                display_name = display_name[:97] + "..."

            choice_value = title_value or current
            choices.append(app_commands.Choice(name=display_name, value=choice_value))

        if not choices:
            choices = [
                app_commands.Choice(
                    name=self._translate(interaction, "commands.play.autocomplete.no_results"),
                    value=current,
                )
            ]

        return await self._send_autocomplete_choices(interaction, choices)

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
    ) -> None:
        guild = interaction.guild
        channel_name = getattr(channel, "name", str(getattr(channel, "id", "?")))
        print(
            f"Falha ao conectar ao canal de voz '{channel_name}' (tentativa {attempt}/{max_attempts}): {error}"
        )

        await self._cleanup_failed_voice_connection(guild)

        try:
            await self.bot.ensure_lavalink_connected()
        except Exception as ensure_exc:
            print(f"Erro ao validar Lavalink após falha de voz: {ensure_exc}")

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

        for attempt in range(1, attempts + 1):
            try:
                player = await channel.connect(cls=wavelink.Player, self_deaf=True, reconnect=True)
            except (
                wavelink.ChannelTimeoutException,
                wavelink.InvalidChannelStateException,
                asyncio.TimeoutError,
            ) as exc:
                last_error = exc
                await self._handle_voice_connect_issue(interaction, channel, exc, attempt, attempts)
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

        await asyncio.sleep(0.2)

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
    @app_commands.describe(query="Nome da música, URL do YouTube/Spotify, ou termo de busca")
    @app_commands.autocomplete(query=search_autocomplete)
    async def play(self, interaction: discord.Interaction, query: str):
        """Comando para reproduzir música"""
        try:
            await interaction.response.defer()
        except (discord.NotFound, discord.HTTPException):
            pass

        # Verifica se o usuário está em um canal de voz
        if not interaction.user.voice:
            embed = self._error_embed(
                interaction,
                "commands.common.embeds.error_title",
                "commands.play.errors.user_not_in_voice",
            )
            return await interaction.followup.send(embed=embed)

        try:
            lavalink_ok = await self.bot.ensure_lavalink_connected()
        except Exception as e:
            embed = self._error_embed(
                interaction,
                "commands.common.embeds.error_title",
                "commands.play.errors.lavalink_validation",
                error=e,
            )
            return await interaction.followup.send(embed=embed)

        if not lavalink_ok:
            embed = self._error_embed(
                interaction,
                "commands.play.errors.lavalink_unavailable.title",
                "commands.play.errors.lavalink_unavailable.description",
            )
            return await interaction.followup.send(embed=embed)

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

        # Busca a música
        try:
            if self.is_url(query):
                tracks = await interaction.client.search_with_failover(query)
            else:
                tracks = await interaction.client.search_with_failover(f"ytsearch:{query}")
        except Exception as e:
            embed = self._error_embed(
                interaction,
                "commands.common.embeds.error_title",
                "commands.play.errors.search_failure",
                error=e,
            )
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
                for track in tracks.tracks:
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
                track = tracks[0]

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
                    confirmation = self._translate(
                        interaction,
                        "commands.play.start.playing_now",
                        default="▶️ Tocando agora: **{title}**",
                        title=track.title,
                    )
                    await interaction.followup.send(confirmation, ephemeral=True)
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