import discord
from discord.ext import commands
from discord import app_commands


class Help(commands.Cog):
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

    @app_commands.command(name="help", description="Mostra a lista de comandos")
    async def help(self, interaction: discord.Interaction):
        cmds = list(self.bot.tree.get_commands())
        cmds = [c for c in cmds if getattr(c, "parent", None) is None]
        cmds.sort(key=lambda c: c.name)

        embed = discord.Embed(
            title=self._translate(
                interaction,
                "commands.help.embed.title",
                default="📖 Comandos Disponíveis",
            ),
            color=0x5865F2
        )
        for cmd in cmds:
            qualified = cmd.qualified_name.replace(" ", ".") if cmd.qualified_name else cmd.name
            key = f"commands.{qualified}.description"
            description = self._translate(
                interaction,
                key,
                default=cmd.description,
            )
            if not description:
                description = self._translate(
                    interaction,
                    "commands.help.embed.no_description",
                    default="Sem descrição",
                )
            embed.add_field(name=f"/{cmd.name}", value=description, inline=False)

        embed.set_footer(
            text=self._translate(
                interaction,
                "commands.help.embed.footer",
                default=f"Total: {len(cmds)} comando(s)",
                count=len(cmds),
            )
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Help(bot))