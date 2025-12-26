"""
Cog para restaurar fila após desconexão de node.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import discord
import wavelink
from discord import app_commands
from discord.ext import commands

if TYPE_CHECKING:
    from index import MusicBot


class ResumeQueueCog(commands.Cog):
    """Comando para restaurar fila salva após queda de node."""

    def __init__(self, bot: "MusicBot") -> None:
        self.bot = bot

    def _translate(
        self,
        interaction: discord.Interaction,
        key: str,
        *,
        default: str = "",
        **kwargs,
    ) -> str:
        guild_id = interaction.guild.id if interaction.guild else None
        return self.bot.translate(key, guild_id=guild_id, default=default, **kwargs)

    def _error_embed(self, interaction: discord.Interaction, key: str, **kwargs) -> discord.Embed:
        """Cria embed de erro com título e descrição traduzidos."""
        error_title = self._translate(
            interaction,
            "commands.common.embeds.error_title",
            default="❌ Error",
        )
        message = self._translate(interaction, key, default=key, **kwargs)
        return discord.Embed(title=error_title, description=message, color=0xFF0000)

    async def _cleanup_voice_state(self, guild: discord.Guild) -> None:
        """Limpa estado de voz corrompido."""
        try:
            if guild.voice_client:
                await guild.voice_client.disconnect(force=True)
        except Exception:
            pass
        
        try:
            if guild.me and guild.me.voice:
                await guild.me.move_to(None)
        except Exception:
            pass

    async def _get_or_create_player(
        self,
        interaction: discord.Interaction,
        voice_channel: discord.VoiceChannel | discord.StageChannel,
    ) -> wavelink.Player | None:
        """Obtém player existente ou cria um novo usando a mesma lógica do /play."""
        guild = interaction.guild
        player: wavelink.Player | None = guild.voice_client  # type: ignore

        # Verifica se player existente está em node saudável
        if player is not None:
            player_node = getattr(player, "node", None)
            if player_node:
                node_id = getattr(player_node, "identifier", None)
                if node_id and self.bot.is_node_blacklisted(node_id):
                    print(f"[ResumeQueue] Player existente em node na blacklist ({node_id}), destruindo...")
                    try:
                        await player.disconnect()
                    except Exception:
                        pass
                    await self._cleanup_voice_state(guild)
                    player = None

        if player is not None:
            return player

        # Limpa qualquer estado de voz corrompido antes de criar novo player
        await self._cleanup_voice_state(guild)

        # Obtém nodes disponíveis (não na blacklist)
        current_time = asyncio.get_event_loop().time()
        usable_nodes = []
        
        for node in wavelink.Pool.nodes.values():
            node_id = getattr(node, "identifier", None)
            if not node_id:
                continue
            
            # Ignora nodes na blacklist
            if self.bot.is_node_blacklisted(node_id):
                continue
            
            # Ignora nodes não conectados
            if node.status != wavelink.NodeStatus.CONNECTED:
                continue
            
            usable_nodes.append(node)

        if not usable_nodes:
            print("[ResumeQueue] Nenhum node Lavalink disponível")
            return None

        print(f"[ResumeQueue] {len(usable_nodes)} node(s) disponível(is)")

        # Ordena por quantidade de players (menos players = menos carga)
        usable_nodes.sort(key=lambda n: len(getattr(n, 'players', {})))
        selected_node = usable_nodes[0]
        player_count = len(getattr(selected_node, 'players', {}))
        print(f"[ResumeQueue] Selecionado node: {selected_node.identifier} ({player_count} player(s) ativos)")

        # Tenta conectar com retry em diferentes nodes
        last_error = None
        tried_nodes = set()

        for attempt in range(len(usable_nodes)):
            if selected_node.identifier in tried_nodes:
                # Pega próximo node não tentado
                remaining = [n for n in usable_nodes if n.identifier not in tried_nodes]
                if not remaining:
                    break
                selected_node = remaining[0]
            
            tried_nodes.add(selected_node.identifier)

            try:
                connect_timeout = 10.0  # Timeout mais curto para retry mais rápido

                # Cria player com o node selecionado explicitamente
                def _player_factory(client: discord.Client, ch: discord.abc.Connectable):
                    return wavelink.Player(client, ch, nodes=[selected_node])

                player = await voice_channel.connect(
                    cls=_player_factory, 
                    self_deaf=True, 
                    reconnect=True, 
                    timeout=connect_timeout
                )
                
                player.autoplay = wavelink.AutoPlayMode.disabled
                
                # Confirma conexão
                node = getattr(player, "node", None)
                if node:
                    print(f"[ResumeQueue] ✅ Conectado no node: {node.identifier}")
                
                return player

            except (
                wavelink.ChannelTimeoutException,
                wavelink.InvalidChannelStateException,
                asyncio.TimeoutError,
            ) as exc:
                last_error = exc
                print(f"[ResumeQueue] ⚠️ Falha no node {selected_node.identifier}: {exc}")
                
                # Limpa estado antes de tentar próximo node
                await self._cleanup_voice_state(guild)
                
                # Se ainda há nodes para tentar, continua
                remaining = [n for n in usable_nodes if n.identifier not in tried_nodes]
                if remaining:
                    selected_node = remaining[0]
                    print(f"[ResumeQueue] Tentando próximo node: {selected_node.identifier}")
                    continue
                break

            except Exception as exc:
                last_error = exc
                print(f"[ResumeQueue] ❌ Erro inesperado: {exc}")
                await self._cleanup_voice_state(guild)
                break

        print(f"[ResumeQueue] Falha ao criar player após tentar {len(tried_nodes)} node(s): {last_error}")
        return None

    @app_commands.command(name="resumequeue", description="Resume the last saved queue after a disconnection")
    async def resumequeue(self, interaction: discord.Interaction) -> None:
        """Restaura a última fila salva após queda de node."""
        await interaction.response.defer()

        # Verificar se usuário está em canal de voz
        if not interaction.user.voice or not interaction.user.voice.channel:
            voice_required = self._translate(
                interaction,
                "commands.common.errors.voice_required",
                default="You need to be in a voice channel!",
            )
            embed = self._error_embed(interaction, "commands.common.errors.voice_required")
            embed.description = voice_required
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        voice_channel = interaction.user.voice.channel
        guild_id = interaction.guild.id

        # Verificar se existe cache
        if not self.bot.queue_cache.has_cache(guild_id):
            title = self._translate(
                interaction,
                "commands.resumequeue.no_cache_title",
                default="No saved queue",
            )
            description = self._translate(
                interaction,
                "commands.resumequeue.no_cache_description",
                default="There's no saved queue to resume.\n\nThe queue cache expires after 1 hour or when the queue finishes naturally.",
            )
            embed = discord.Embed(
                title=f"<a:panic:1451081526522417252> {title}",
                description=description,
                color=0xFF6B6B,
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        cached_tracks = self.bot.queue_cache.get_queue(guild_id)
        if not cached_tracks:
            title = self._translate(
                interaction,
                "commands.resumequeue.no_cache_title",
                default="No saved queue",
            )
            description = self._translate(
                interaction,
                "commands.resumequeue.no_cache_description",
                default="There's no saved queue to resume.",
            )
            embed = discord.Embed(
                title=f"<a:panic:1451081526522417252> {title}",
                description=description,
                color=0xFF6B6B,
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        # Verificar permissões no canal de voz
        bot_member = interaction.guild.me
        permissions = voice_channel.permissions_for(bot_member)

        if not permissions.connect or not permissions.speak:
            perm_title = self._translate(
                interaction,
                "commands.play.errors.no_connect_permission",
                default="I don't have permission to connect to this voice channel!",
            )
            embed = discord.Embed(
                title="<a:dance_teto:1451252227133018374> Missing Permissions",
                description=perm_title,
                color=0xFF0000,
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        # Verificar se há node disponível
        has_healthy_node = self.bot.has_healthy_node()
        if not has_healthy_node:
            title = self._translate(
                interaction,
                "commands.play.errors.lavalink_error_title",
                default="Connection issue",
            )
            description = self._translate(
                interaction,
                "commands.play.errors.lavalink_error",
                default="Our servers are unstable at the moment! Please try again in a few moments.",
            )
            embed = discord.Embed(
                title=f"<:crymeru:1453534083983474867> {title}",
                description=description,
                color=0xFF6B6B,
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        # Embed de carregamento
        loading_text = self._translate(
            interaction,
            "commands.resumequeue.loading",
            default="Restoring queue...",
        )
        loading_embed = discord.Embed(
            description=f"# <a:unadance:1450689460307230760> {loading_text}",
            color=0x5284FF,
        )
        await interaction.followup.send(embed=loading_embed)

        # Criar ou obter player
        player = await self._get_or_create_player(interaction, voice_channel)
        if not player:
            title = self._translate(
                interaction,
                "commands.play.errors.lavalink_error_title",
                default="Connection issue",
            )
            description = self._translate(
                interaction,
                "commands.play.errors.lavalink_error",
                default="Couldn't connect to audio server.",
            )
            embed = discord.Embed(
                title=f"<:crymeru:1453534083983474867> {title}",
                description=description,
                color=0xFF6B6B,
            )
            await interaction.edit_original_response(embed=embed)
            return

        # Guardar text_channel
        player.text_channel = interaction.channel

        # Restaurar músicas
        added_count = 0
        failed_count = 0
        first_track = None

        for cached in cached_tracks:
            try:
                # Usar URI ou busca por título
                search_query = cached.get("uri") or cached.get("identifier") or f"{cached.get('title', '')} {cached.get('author', '')}"
                if not search_query or not search_query.strip():
                    failed_count += 1
                    continue

                # Pequeno delay entre buscas
                if added_count > 0:
                    await asyncio.sleep(0.15)

                # Buscar track
                tracks = await wavelink.Playable.search(search_query)
                if tracks:
                    track = tracks[0]
                    track.requester = interaction.user  # type: ignore
                    player.queue.put(track)
                    added_count += 1

                    if first_track is None:
                        first_track = track
                else:
                    failed_count += 1

            except Exception as e:
                print(f"[ResumeQueue] Falha ao resolver track: {cached.get('title', 'unknown')} - {e}")
                failed_count += 1

                # Se for erro de Lavalink, para de tentar
                if "lavalink" in str(e).lower() or "node" in str(e).lower():
                    break

        if added_count == 0:
            title = self._translate(
                interaction,
                "commands.resumequeue.failed_title",
                default="Failed to resume",
            )
            description = self._translate(
                interaction,
                "commands.resumequeue.failed_description",
                default="I couldn't load any tracks from the saved queue.",
            )
            embed = discord.Embed(
                title=f"<a:panic:1451081526522417252> {title}",
                description=description,
                color=0xFF6B6B,
            )
            await interaction.edit_original_response(embed=embed)

            # Desconectar player se criamos um novo
            try:
                if player and not player.playing:
                    await player.disconnect()
            except Exception:
                pass
            return

        # Limpar cache após restaurar
        self.bot.queue_cache.clear_queue(guild_id)

        # Começar a tocar se não estiver tocando
        if not player.playing and not player.paused:
            await player.play(player.queue.get())

        # Embed de sucesso
        title = self._translate(
            interaction,
            "commands.resumequeue.success_title",
            default="Queue restored!",
        )
        description = self._translate(
            interaction,
            "commands.resumequeue.success_description",
            default="**{count}** track(s) have been added to the queue.",
            count=added_count,
        )
        first_track_label = self._translate(
            interaction,
            "commands.resumequeue.first_track",
            default="First track",
        )
        queue_size_label = self._translate(
            interaction,
            "commands.resumequeue.queue_size",
            default="Queue size",
        )

        embed = discord.Embed(
            title=f"<a:dance_teto:1451252227133018374> {title}",
            description=description,
            color=0x57F287,
        )
        embed.add_field(
            name=first_track_label,
            value=f"**{first_track.title}**" if first_track else "Unknown",
            inline=True,
        )
        embed.add_field(
            name=queue_size_label,
            value=str(len(player.queue) + (1 if player.current else 0)),
            inline=True,
        )

        if first_track:
            artwork = getattr(first_track, "artwork", None) or getattr(first_track, "artworkUrl", None)
            if artwork:
                embed.set_thumbnail(url=artwork)

        embed.timestamp = discord.utils.utcnow()

        await interaction.edit_original_response(embed=embed)


async def setup(bot: "MusicBot") -> None:
    await bot.add_cog(ResumeQueueCog(bot))
