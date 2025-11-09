import discord
from discord.ext import commands
from discord import app_commands
import wavelink
from typing import Optional

from commands import player_is_ready, resolve_wavelink_player

# Configurações de bassboost baseadas no código TypeScript
BASSBOOST_LEVELS = {
    "high": [
        {"band": 0, "gain": 0.6},
        {"band": 1, "gain": 0.67},
        {"band": 2, "gain": 0.67},
        {"band": 3, "gain": 0.4},
        {"band": 4, "gain": -0.5},
        {"band": 5, "gain": -0.5},
        {"band": 6, "gain": -0.45},
        {"band": 7, "gain": -0.5},
        {"band": 8, "gain": -0.5},
        {"band": 9, "gain": -0.5},
        {"band": 10, "gain": -0.5}
    ],
    "medium": [
        {"band": 0, "gain": 0.35},
        {"band": 1, "gain": 0.23},
        {"band": 2, "gain": 0.26},
        {"band": 3, "gain": 0.25},
        {"band": 4, "gain": -0.1},
        {"band": 5, "gain": -0.15},
        {"band": 6, "gain": -0.15},
        {"band": 7, "gain": -0.15},
        {"band": 8, "gain": -0.15},
        {"band": 9, "gain": -0.15},
        {"band": 10, "gain": -0.15}
    ],
    "low": [
        {"band": 0, "gain": 0.2},
        {"band": 1, "gain": 0.15},
        {"band": 2, "gain": 0.15},
        {"band": 3, "gain": 0.15},
        {"band": 4, "gain": 0.1},
        {"band": 5, "gain": 0.05},
        {"band": 6, "gain": 0},
        {"band": 7, "gain": 0},
        {"band": 8, "gain": -0.15},
        {"band": 9, "gain": -0.15},
        {"band": 10, "gain": -0.15}
    ]
}


class FilterControlView(discord.ui.View):
    """View com botões de controle de filtros"""

    def __init__(self, bot, player):
        super().__init__(timeout=300)
        self.bot = bot
        self.player = player
        self.active_filters = set()

        guild = getattr(player, "guild", None)
        self._guild_id = getattr(guild, "id", None)
        self._localize_components()

    def _translate(
        self,
        key: str,
        *,
        interaction: discord.Interaction | None = None,
        default: str | None = None,
        **kwargs,
    ) -> str:
        translator = getattr(self.bot, "translate", None)
        guild_id = None
        if interaction and interaction.guild:
            guild_id = interaction.guild.id
        elif self._guild_id is not None:
            guild_id = self._guild_id
        if translator:
            return translator(key, guild_id=guild_id, default=default, **kwargs)
        return default if default is not None else key

    def _localize_components(self) -> None:
        label_keys = {
            "filter_bass_boost": "commands.filter.buttons.bass_boost.label",
            "filter_nightcore": "commands.filter.buttons.nightcore.label",
            "filter_karaoke": "commands.filter.buttons.karaoke.label",
            "filter_rotation": "commands.filter.buttons.rotation.label",
            "filter_reset": "commands.filter.buttons.reset.label",
        }

        for item in self.children:
            if isinstance(item, discord.ui.Button) and item.custom_id in label_keys:
                item.label = self._translate(label_keys[item.custom_id], default=item.label)

    @discord.ui.button(emoji="🎵", label="Bass Boost", style=discord.ButtonStyle.secondary, row=0, custom_id="filter_bass_boost")
    async def bass_boost_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Botão para bass boost"""
        try:
            if "bass_boost" in self.active_filters:
                # Remove bass boost
                filters = wavelink.Filters()
                self.active_filters.discard("bass_boost")
                button.style = discord.ButtonStyle.secondary
                message = self._translate(
                    "commands.filter.buttons.bass_boost.deactivated",
                    interaction=interaction,
                    default="🎵 Bass Boost desativado!",
                )
            else:
                filters = wavelink.Filters()
                filters.equalizer.set(bands=BASSBOOST_LEVELS["medium"])

                self.active_filters.add("bass_boost")
                button.style = discord.ButtonStyle.primary
                message = self._translate(
                    "commands.filter.buttons.bass_boost.activated",
                    interaction=interaction,
                    default="🎵 Bass Boost ativado (nível médio)!",
                )

            await self.player.set_filters(filters)
            await interaction.response.send_message(message, ephemeral=True)

        except Exception as e:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    self._translate(
                        "commands.filter.buttons.bass_boost.error",
                        interaction=interaction,
                        default=f"❌ Erro ao aplicar bass boost: {e}",
                        error=e,
                    ),
                    ephemeral=True,
                )

    @discord.ui.button(emoji="⚡", label="Nightcore", style=discord.ButtonStyle.secondary, row=0, custom_id="filter_nightcore")
    async def nightcore_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Botão para nightcore"""
        try:
            if "nightcore" in self.active_filters:
                filters = wavelink.Filters()
                self.active_filters.discard("nightcore")
                button.style = discord.ButtonStyle.secondary
                message = self._translate(
                    "commands.filter.buttons.nightcore.deactivated",
                    interaction=interaction,
                    default="⚡ Nightcore desativado!",
                )
            else:
                filters = wavelink.Filters()
                filters.timescale.set(speed=1.25, pitch=1.3, rate=1.0)

                self.active_filters.add("nightcore")
                button.style = discord.ButtonStyle.primary
                message = self._translate(
                    "commands.filter.buttons.nightcore.activated",
                    interaction=interaction,
                    default="⚡ Nightcore ativado!",
                )

            await self.player.set_filters(filters)
            await interaction.response.send_message(message, ephemeral=True)

        except Exception as e:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    self._translate(
                        "commands.filter.buttons.nightcore.error",
                        interaction=interaction,
                        default=f"❌ Erro ao aplicar nightcore: {e}",
                        error=e,
                    ),
                    ephemeral=True,
                )

    @discord.ui.button(emoji="🎤", label="Karaoke", style=discord.ButtonStyle.secondary, row=0, custom_id="filter_karaoke")
    async def karaoke_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Botão para karaoke"""
        try:
            if "karaoke" in self.active_filters:
                filters = wavelink.Filters()
                self.active_filters.discard("karaoke")
                button.style = discord.ButtonStyle.secondary
                message = self._translate(
                    "commands.filter.buttons.karaoke.deactivated",
                    interaction=interaction,
                    default="🎤 Karaoke desativado!",
                )
            else:
                filters = wavelink.Filters()
                filters.karaoke.set(
                    level=1.0,
                    mono_level=1.0,
                    filter_band=220.0,
                    filter_width=100.0,
                )

                self.active_filters.add("karaoke")
                button.style = discord.ButtonStyle.primary
                message = self._translate(
                    "commands.filter.buttons.karaoke.activated",
                    interaction=interaction,
                    default="🎤 Karaoke ativado!",
                )

            await self.player.set_filters(filters)
            await interaction.response.send_message(message, ephemeral=True)

        except Exception as e:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    self._translate(
                        "commands.filter.buttons.karaoke.error",
                        interaction=interaction,
                        default=f"❌ Erro ao aplicar karaoke: {e}",
                        error=e,
                    ),
                    ephemeral=True,
                )

    @discord.ui.button(emoji="🌀", label="8D Audio", style=discord.ButtonStyle.secondary, row=0, custom_id="filter_rotation")
    async def rotation_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Botão para rotação 8D"""
        try:
            if "rotation" in self.active_filters:
                filters = wavelink.Filters()
                self.active_filters.discard("rotation")
                button.style = discord.ButtonStyle.secondary
                message = self._translate(
                    "commands.filter.buttons.rotation.deactivated",
                    interaction=interaction,
                    default="🌀 8D Audio desativado!",
                )
            else:
                filters = wavelink.Filters()
                filters.rotation.set(rotation_hz=0.2)

                self.active_filters.add("rotation")
                button.style = discord.ButtonStyle.primary
                message = self._translate(
                    "commands.filter.buttons.rotation.activated",
                    interaction=interaction,
                    default="🌀 8D Audio ativado!",
                )

            await self.player.set_filters(filters)
            await interaction.response.send_message(message, ephemeral=True)

        except Exception as e:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    self._translate(
                        "commands.filter.buttons.rotation.error",
                        interaction=interaction,
                        default=f"❌ Erro ao aplicar 8D Audio: {e}",
                        error=e,
                    ),
                    ephemeral=True,
                )

    @discord.ui.button(emoji="🔄", label="Reset", style=discord.ButtonStyle.danger, row=1, custom_id="filter_reset")
    async def reset_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Botão para resetar todos os filtros"""
        try:
            filters = wavelink.Filters()
            await self.player.set_filters(filters)

            self.active_filters.clear()
            for item in self.children:
                if isinstance(item, discord.ui.Button) and item.custom_id != "filter_reset":
                    item.style = discord.ButtonStyle.secondary

            await interaction.response.send_message(
                self._translate(
                    "commands.filter.buttons.reset.success",
                    interaction=interaction,
                    default="🔄 Todos os filtros foram removidos!",
                ),
                ephemeral=True,
            )

        except Exception as e:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    self._translate(
                        "commands.filter.buttons.reset.error",
                        interaction=interaction,
                        default=f"❌ Erro ao resetar filtros: {e}",
                        error=e,
                    ),
                    ephemeral=True,
                )


class FilterCommands(commands.Cog):
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

    @app_commands.command(name="filters", description="Open the audio filter control panel")
    async def filters(self, interaction: discord.Interaction):
        """Comando principal para controle de filtros"""
        player = await self._player_or_error(interaction)
        if not player:
            return

        if not player.current:
            embed = discord.Embed(
                title=self._translate(
                    interaction,
                    "commands.common.embeds.error_title",
                    default="❌ Erro",
                ),
                description=self._translate(
                    interaction,
                    "commands.common.errors.no_track_full",
                    default="Não há música tocando no momento.",
                ),
                color=0xff0000
            )
            return await self._send_interaction_message(interaction, embed=embed)

        embed = discord.Embed(
            title=self._translate(
                interaction,
                "commands.filter.filters.embed.title",
                default="🎛️ Painel de Filtros de Áudio",
            ),
            description=self._translate(
                interaction,
                "commands.filter.filters.embed.description",
                default="Selecione os filtros que deseja aplicar à música:",
            ),
            color=0x0099ff
        )

        embed.add_field(
            name=self._translate(
                interaction,
                "commands.filter.filters.embed.now_playing_label",
                default="🎵 Tocando Agora",
            ),
            value=self._translate(
                interaction,
                "commands.filter.filters.embed.now_playing_value",
                default=f"**{player.current.title}**\nPor: {player.current.author or 'Desconhecido'}",
                title=player.current.title,
                author=player.current.author or self._translate(
                    interaction,
                    "commands.common.labels.unknown_author",
                    default="Desconhecido",
                ),
            ),
            inline=False
        )

        embed.add_field(
            name=self._translate(
                interaction,
                "commands.filter.filters.embed.available_label",
                default="🎛️ Filtros Disponíveis",
            ),
            value=self._translate(
                interaction,
                "commands.filter.filters.embed.available_value",
                default=("🎵 **Bass Boost** - Aumenta graves\n"
                         "⚡ **Nightcore** - Velocidade + pitch\n"
                         "🎤 **Karaoke** - Remove vocais\n"
                         "🌀 **8D Audio** - Efeito rotacional\n"
                         "🔄 **Reset** - Remove todos"),
            ),
            inline=False
        )

        embed.add_field(
            name=self._translate(
                interaction,
                "commands.filter.filters.embed.howto_label",
                default="💡 Como usar",
            ),
            value=self._translate(
                interaction,
                "commands.filter.filters.embed.howto_value",
                default="Clique nos botões para ativar/desativar filtros. Filtros ativos ficam azuis.",
            ),
            inline=False
        )

        if hasattr(player.current, 'artwork') and player.current.artwork:
            embed.set_thumbnail(url=player.current.artwork)

        embed.set_footer(
            text=self._translate(
                interaction,
                "commands.filter.filters.embed.footer",
                default="⚠️ Filtros podem causar pequeno delay na aplicação",
            )
        )

        view = FilterControlView(self.bot, player)
        await self._send_interaction_message(interaction, embed=embed, view=view)

    @app_commands.command(name="bassboost", description="Apply bass boost with a specific level")
    @app_commands.describe(level="Nível do bassboost: high, medium, low, off")
    @app_commands.choices(level=[
        app_commands.Choice(name="High", value="high"),
        app_commands.Choice(name="Medium", value="medium"),
        app_commands.Choice(name="Low", value="low"),
        app_commands.Choice(name="Off", value="off")
    ])
    async def bassboost(self, interaction: discord.Interaction, level: str):
        """Comando específico para bassboost com níveis"""
        player = await self._player_or_error(interaction)
        if not player:
            return

        try:
            level_lower = level.lower()

            if level_lower == "off":
                filters = wavelink.Filters()
                await player.set_filters(filters)

                embed = discord.Embed(
                    title=self._translate(
                        interaction,
                        "commands.filter.bassboost.off.title",
                        default="🎵 Bass Boost Desativado",
                    ),
                    description=self._translate(
                        interaction,
                        "commands.filter.bassboost.off.description",
                        default="O bass boost foi removido.",
                    ),
                    color=0x00ff00
                )
            elif level_lower in BASSBOOST_LEVELS:
                filters = wavelink.Filters()
                filters.equalizer.set(bands=BASSBOOST_LEVELS[level_lower])
                await player.set_filters(filters)

                embed = discord.Embed(
                    title=self._translate(
                        interaction,
                        "commands.filter.bassboost.on.title",
                        default="🎵 Bass Boost Aplicado",
                    ),
                    description=self._translate(
                        interaction,
                        "commands.filter.bassboost.on.description",
                        default=f"Bass boost configurado para nível **{level_lower.upper()}**",
                        level=level_lower.upper(),
                    ),
                    color=0x00ff00
                )

                level_descriptions = {
                    "high": self._translate(
                        interaction,
                        "commands.filter.bassboost.level_descriptions.high",
                        default="Graves muito intensos - Ideal para música eletrônica",
                    ),
                    "medium": self._translate(
                        interaction,
                        "commands.filter.bassboost.level_descriptions.medium",
                        default="Graves equilibrados - Ideal para hip-hop e pop",
                    ),
                    "low": self._translate(
                        interaction,
                        "commands.filter.bassboost.level_descriptions.low",
                        default="Graves suaves - Ideal para rock e música clássica",
                    ),
                }

                embed.add_field(
                    name=self._translate(
                        interaction,
                        "commands.filter.bassboost.on.level_label",
                        default="📊 Nível Aplicado",
                    ),
                    value=level_descriptions.get(
                        level_lower,
                        self._translate(
                            interaction,
                            "commands.filter.bassboost.level_descriptions.custom",
                            default="Nível personalizado",
                        ),
                    ),
                    inline=False
                )

                bands_info = []
                for band in BASSBOOST_LEVELS[level_lower][:4]:
                    gain_str = f"+{band['gain']}" if band['gain'] > 0 else str(band['gain'])
                    bands_info.append(
                        self._translate(
                            interaction,
                            "commands.filter.bassboost.on.band_line",
                            default=f"Band {band['band']}: {gain_str}",
                            band=band['band'],
                            gain=gain_str,
                        )
                    )

                embed.add_field(
                    name=self._translate(
                        interaction,
                        "commands.filter.bassboost.on.settings_label",
                        default="🔧 Configurações Técnicas",
                    ),
                    value="\n".join(bands_info),
                    inline=True
                )
            else:
                embed = discord.Embed(
                    title=self._translate(
                        interaction,
                        "commands.filter.bassboost.errors.invalid_title",
                        default="❌ Nível Inválido",
                    ),
                    description=self._translate(
                        interaction,
                        "commands.filter.bassboost.errors.invalid_description",
                        default=f"Nível '{level}' não existe. Use: high, medium, low ou off",
                        level=level,
                    ),
                    color=0xff0000
                )
                return await self._send_interaction_message(interaction, embed=embed)

            await self._send_interaction_message(interaction, embed=embed)

        except Exception as e:
            embed = discord.Embed(
                title=self._translate(
                    interaction,
                    "commands.common.embeds.error_title",
                    default="❌ Erro",
                ),
                description=self._translate(
                    interaction,
                    "commands.filter.bassboost.errors.generic",
                    default=f"Erro ao aplicar bass boost: {e}",
                    error=e,
                ),
                color=0xff0000
            )
            await self._send_interaction_message(interaction, embed=embed)

    @app_commands.command(name="resetfilters", description="Remove all audio filters")
    async def resetfilters(self, interaction: discord.Interaction):
        """Comando para resetar todos os filtros"""
        player = await self._player_or_error(interaction)
        if not player:
            return

        try:
            filters = wavelink.Filters()
            await player.set_filters(filters)

            embed = discord.Embed(
                title=self._translate(
                    interaction,
                    "commands.filter.reset.title",
                    default="🔄 Filtros Resetados",
                ),
                description=self._translate(
                    interaction,
                    "commands.filter.reset.description",
                    default="Todos os filtros de áudio foram removidos.",
                ),
                color=0x00ff00
            )

            embed.add_field(
                name=self._translate(
                    interaction,
                    "commands.filter.reset.removed_label",
                    default="✅ Filtros Removidos",
                ),
                value=self._translate(
                    interaction,
                    "commands.filter.reset.removed_value",
                    default=("🎵 Bass Boost\n⚡ Nightcore\n🎤 Karaoke\n🌀 8D Audio\n"
                             "E todos os outros filtros ativos"),
                ),
                inline=True
            )

            if player.current:
                embed.add_field(
                    name=self._translate(
                        interaction,
                        "commands.filter.reset.now_playing_label",
                        default="🎵 Música Atual",
                    ),
                    value=self._translate(
                        interaction,
                        "commands.filter.reset.now_playing_value",
                        default=f"**{player.current.title}**\nVoltou ao áudio normal",
                        title=player.current.title,
                    ),
                    inline=True
                )

            await self._send_interaction_message(interaction, embed=embed)

        except Exception as e:
            embed = discord.Embed(
                title=self._translate(
                    interaction,
                    "commands.common.embeds.error_title",
                    default="❌ Erro",
                ),
                description=self._translate(
                    interaction,
                    "commands.filter.reset.error",
                    default=f"Erro ao resetar filtros: {e}",
                    error=e,
                ),
                color=0xff0000
            )
            await self._send_interaction_message(interaction, embed=embed)


async def setup(bot):
    await bot.add_cog(FilterCommands(bot))
