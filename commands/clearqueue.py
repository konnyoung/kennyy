import discord
from discord.ext import commands
from discord import app_commands
import wavelink

from commands import player_is_ready, resolve_wavelink_player


class ClearQueueCommands(commands.Cog):
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

    async def _send_interaction_message(
        self,
        interaction: discord.Interaction,
        *,
        content: str | None = None,
        embed: discord.Embed | None = None,
        ephemeral: bool = False,
    ) -> None:
        if content is None and embed is None:
            return

        try:
            if interaction.response.is_done():
                await interaction.followup.send(
                    content=content,
                    embed=embed,
                    ephemeral=ephemeral,
                )
            else:
                await interaction.response.send_message(
                    content=content,
                    embed=embed,
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
            embed = discord.Embed(
                title=self._translate(
                    interaction,
                    "commands.common.embeds.error_title",
                    default="❌ Erro",
                ),
                description=self._translate(
                    interaction,
                    "commands.common.errors.bot_not_connected_full",
                    default="O bot não está conectado a um canal de voz.",
                ),
                color=0xff0000,
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

    @app_commands.command(name="clearqueue", description="Clear every track in the queue")
    async def clearqueue(self, interaction: discord.Interaction):
        """Limpa toda a fila de reprodução"""
        player = await self._player_or_error(interaction)
        if not player:
            return

        if player.queue.count == 0:
            embed = discord.Embed(
                title=self._translate(
                    interaction,
                    "commands.clearqueue.empty.title",
                    default="Fila Vazia",
                ),
                description=self._translate(
                    interaction,
                    "commands.clearqueue.empty.description",
                    default="Não há músicas na fila para limpar.",
                ),
                color=0xffff00
            )

            if player.current:
                embed.add_field(
                    name=self._translate(
                        interaction,
                        "commands.clearqueue.empty.current_label",
                        default="🎵 Música Atual",
                    ),
                    value=self._translate(
                        interaction,
                        "commands.clearqueue.empty.current_value",
                        default=f"**{player.current.title}** continua tocando\n(Use /stop para parar completamente)",
                        title=player.current.title,
                    ),
                    inline=False
                )

            return await self._send_interaction_message(interaction, embed=embed)

        cleared_count = player.queue.count
        player.queue.clear()

        embed = discord.Embed(
            title=self._translate(
                interaction,
                "commands.clearqueue.success.title",
                default="🗑️ Fila Limpa",
            ),
            color=0x00ff00
        )

        embed.add_field(
            name=self._translate(
                interaction,
                "commands.clearqueue.success.removed_label",
                default="Músicas Removidas",
            ),
            value=self._translate(
                interaction,
                "commands.clearqueue.success.removed_value",
                default=f"{cleared_count}",
                count=cleared_count,
            ),
            inline=True
        )
        embed.add_field(
            name=self._translate(
                interaction,
                "commands.clearqueue.success.status_label",
                default="Status da Fila",
            ),
            value=self._translate(
                interaction,
                "commands.clearqueue.success.status_value",
                default="✅ Limpa",
            ),
            inline=True
        )
        embed.add_field(
            name=self._translate(
                interaction,
                "commands.clearqueue.success.current_label",
                default="Música Atual",
            ),
            value=self._translate(
                interaction,
                "commands.clearqueue.success.current_value",
                default="Continua tocando" if player.current else "Nenhuma",
                status="continua" if player.current else "nenhuma",
            ),
            inline=True
        )

        if player.current:
            author = player.current.author or self._translate(
                interaction,
                "commands.common.labels.unknown_author",
                default="Desconhecido",
            )
            embed.add_field(
                name=self._translate(
                    interaction,
                    "commands.clearqueue.success.now_playing_label",
                    default="🎵 Tocando Agora",
                ),
                value=self._translate(
                    interaction,
                    "commands.clearqueue.success.now_playing_value",
                    default=f"**{player.current.title}**\nPor: {author}",
                    title=player.current.title,
                    author=author,
                ),
                inline=False
            )

        embed.set_footer(
            text=self._translate(
                interaction,
                "commands.clearqueue.success.footer",
                default="Use /play para adicionar novas músicas à fila",
            )
        )
        await self._send_interaction_message(interaction, embed=embed)


async def setup(bot):
    await bot.add_cog(ClearQueueCommands(bot))