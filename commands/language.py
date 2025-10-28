import discord
from discord.ext import commands
from discord import app_commands

try:
    from commands.admin import ALLOWED_ADMIN_IDS as _BOT_ADMIN_IDS
except Exception:
    _BOT_ADMIN_IDS: set[int] = set()
else:
    _BOT_ADMIN_IDS = set(_BOT_ADMIN_IDS)

LANGUAGE_OPTIONS = {
    "pt": {
        "emoji": "🇧🇷",
    },
    "pt-pt": {
        "emoji": "🇵🇹",
    },
    "en": {
        "emoji": "🇺🇸",
    },
    "fr": {
        "emoji": "🇫🇷",
    },
}


class LanguageCommands(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot: commands.Bot = bot

    @app_commands.command(
        name="language",
        description="Alterna o idioma padrão do bot neste servidor",
    )
    @app_commands.describe(language="Escolha o idioma. Deixe em branco para alternar automaticamente.")
    @app_commands.choices(
        language=[
            app_commands.Choice(name="🇧🇷 Português (Brasil)", value="pt"),
            app_commands.Choice(name="🇵🇹 Português (Portugal)", value="pt-pt"),
            app_commands.Choice(name="🇺🇸 English", value="en"),
            app_commands.Choice(name="🇫🇷 Français", value="fr"),
        ]
    )
    async def language(
        self,
        interaction: discord.Interaction,
        language: app_commands.Choice[str] | None = None,
    ) -> None:
        bot = getattr(self, "bot", None)
        if bot is None:
            await interaction.response.send_message("Bot não disponível.", ephemeral=True)
            return

        if interaction.guild is None:
            message = bot.translate("commands.language.errors.guild_only", locale=bot.default_language)
            await interaction.response.send_message(message, ephemeral=True)
            return

        member = interaction.user
        guild_admin = False
        if isinstance(member, discord.Member):
            perms = getattr(member, "guild_permissions", None)
            guild_admin = bool(perms and perms.administrator)

        bot_admin = getattr(interaction.user, "id", None) in _BOT_ADMIN_IDS

        if not (guild_admin or bot_admin):
            message = bot.translate("commands.language.errors.no_permission", guild_id=interaction.guild.id)
            await interaction.response.send_message(message, ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        if not hasattr(bot, "set_guild_language"):
            message = bot.translate("commands.language.errors.feature_unavailable", guild_id=interaction.guild.id)
            await interaction.followup.send(message, ephemeral=True)
            return

        collection = getattr(bot, "language_collection", None)
        if collection is None:
            message = bot.translate("commands.language.errors.db_unavailable", guild_id=interaction.guild.id)
            await interaction.followup.send(message, ephemeral=True)
            return

        guild_id = interaction.guild.id
        current_language = bot.get_guild_language(guild_id)

        if language is None:
            new_language = "en" if current_language == "pt" else bot.default_language
        else:
            new_language = language.value

        if new_language not in bot.supported_languages:
            message = bot.translate("commands.language.errors.unsupported", guild_id=guild_id)
            await interaction.followup.send(message, ephemeral=True)
            return

        option = LANGUAGE_OPTIONS.get(new_language, LANGUAGE_OPTIONS.get(bot.default_language, {"emoji": "🔤"}))

        if new_language == current_language:
            language_label = bot.translate(f"languages.{new_language}.label", guild_id=guild_id, locale=current_language)
            title = bot.translate("commands.language.success.already_current.title", guild_id=guild_id, locale=current_language)
            description = bot.translate(
                "commands.language.success.already_current.description",
                guild_id=guild_id,
                locale=current_language,
                language_label=language_label,
            )
            embed = discord.Embed(
                title=f"{option['emoji']} {title}",
                description=description,
                color=0x2ecc71,
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        success = bot.set_guild_language(guild_id, new_language)
        if not success:
            message = bot.translate("commands.language.success.save_failed", guild_id=guild_id, locale=current_language)
            await interaction.followup.send(message, ephemeral=True)
            return

        language_label = bot.translate(f"languages.{new_language}.label", guild_id=guild_id, locale=new_language)
        title = bot.translate("commands.language.success.updated.title", guild_id=guild_id, locale=new_language)
        description = bot.translate(
            "commands.language.success.updated.description",
            guild_id=guild_id,
            locale=new_language,
            language_label=language_label,
        )
        current_language_label = bot.translate(
            "commands.language.success.updated.current_language_label",
            guild_id=guild_id,
            locale=new_language,
        )
        footer = bot.translate(
            "commands.language.success.updated.footer",
            guild_id=guild_id,
            locale=new_language,
        )

        embed = discord.Embed(
            title=f"{option['emoji']} {title}",
            description=description,
            color=0x2ecc71,
        )
        embed.add_field(
            name=current_language_label,
            value=f"{option['emoji']} {language_label}",
            inline=False,
        )
        embed.set_footer(text=footer)

        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(LanguageCommands(bot))
