import discord
from discord.ext import commands
from discord import app_commands

ALLOWED_ADMIN_IDS = {794932318783143948, 703468858341851187}


def is_admin(interaction: discord.Interaction) -> bool:
    return interaction.user and interaction.user.id in ALLOWED_ADMIN_IDS


def serialize_activity(activity: discord.BaseActivity | None) -> dict | None:
    """Serializa apenas atividades 'legado' (Jogando/Ouvindo/Assistindo/Competindo/Streaming)."""
    if activity is None:
        return None

    if isinstance(activity, discord.Streaming):
        return {"type": "streaming", "message": activity.name or "", "url": activity.url or ""}

    if isinstance(activity, discord.Game):
        return {"type": "playing", "message": activity.name or "", "url": None}

    if isinstance(activity, discord.Activity):
        if activity.type == discord.ActivityType.listening:
            return {"type": "listening", "message": activity.name or "", "url": None}
        if activity.type == discord.ActivityType.watching:
            return {"type": "watching", "message": activity.name or "", "url": None}
        if activity.type == discord.ActivityType.competing:
            return {"type": "competing", "message": activity.name or "", "url": None}

    # Tipos não suportados (ex.: custom) não são persistidos
    return None


def build_activity_from_inputs(t: str, message: str | None, url: str | None = None) -> discord.BaseActivity | None:
    """Monta APENAS atividades 'legado'."""
    t = (t or "").lower()
    if t == "playing":
        return discord.Game(name=message or "")
    if t == "listening":
        return discord.Activity(type=discord.ActivityType.listening, name=message or "")
    if t == "watching":
        return discord.Activity(type=discord.ActivityType.watching, name=message or "")
    if t == "competing":
        return discord.Activity(type=discord.ActivityType.competing, name=message or "")
    if t == "streaming":
        return discord.Streaming(name=message or "", url=url or "")
    if t == "clear":
        return None
    return None


class AdminCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # Grupo /admin
    admin = app_commands.Group(name="admin", description="Gerenciamento do bot (somente admin)")

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

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.CheckFailure):
            try:
                message = self._translate(
                    interaction,
                    "commands.admin.errors.no_permission",
                    default="❌ Você não tem permissão para usar este comando.",
                )
                if interaction.response.is_done():
                    await interaction.followup.send(message, ephemeral=True)
                else:
                    await interaction.response.send_message(message, ephemeral=True)
            except Exception:
                pass

    @admin.command(name="setstatus", description="Altera o status do bot (Online, DND, Idle, Invisível)")
    @app_commands.describe(state="Escolha o status desejado")
    @app_commands.choices(state=[
        app_commands.Choice(name="Online", value="online"),
        app_commands.Choice(name="Não perturbe (DND)", value="dnd"),
        app_commands.Choice(name="Ausente (Idle)", value="idle"),
        app_commands.Choice(name="Invisível (Offline)", value="invisible"),
    ])
    @app_commands.check(is_admin)
    async def setstatus(self, interaction: discord.Interaction, state: app_commands.Choice[str]):
        current_activity = interaction.client.activity
        mapping = {
            "online": discord.Status.online,
            "idle": discord.Status.idle,
            "dnd": discord.Status.dnd,
            "invisible": discord.Status.invisible,
        }

        await interaction.client.change_presence(
            status=mapping[state.value],
            activity=current_activity
        )

        # Persiste status; atividade só se for suportada
        config = self.bot._load_presence_config()
        config["status"] = state.value
        serialized = serialize_activity(current_activity)
        if serialized is not None:
            config["activity"] = serialized
        self.bot.save_presence_config(config)

        status_label = self._translate(
            interaction,
            f"commands.admin.status.labels.{state.value}",
            default=state.name,
        )
        embed = discord.Embed(
            title=self._translate(
                interaction,
                "commands.admin.setstatus.success.title",
                default="✅ Status atualizado",
            ),
            description=self._translate(
                interaction,
                "commands.admin.setstatus.success.description",
                default=f"Novo status: **{state.name}**",
                status_name=status_label,
            ),
            color=0x00ff00
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @admin.command(name="setpresence", description="Altera o recado (Jogando/Ouvindo/Assistindo/Competindo/Streaming)")
    @app_commands.describe(
        activity_type="Tipo de atividade (Jogando, Ouvindo, Assistindo, Competindo, Streaming, Limpar)",
        message="Texto do recado (ex.: nome da música, jogo, etc.)",
        url="URL para streaming (obrigatório se tipo = Streaming)"
    )
    @app_commands.choices(activity_type=[
        app_commands.Choice(name="Jogando", value="playing"),
        app_commands.Choice(name="Ouvindo", value="listening"),
        app_commands.Choice(name="Assistindo", value="watching"),
        app_commands.Choice(name="Competindo", value="competing"),
        app_commands.Choice(name="Streaming", value="streaming"),
        app_commands.Choice(name="Limpar", value="clear"),
    ])
    @app_commands.check(is_admin)
    async def setpresence(
        self,
        interaction: discord.Interaction,
        activity_type: app_commands.Choice[str],
        message: str | None = None,
        url: str | None = None
    ):
        t = activity_type.value

        # Validações
        if t != "clear" and t != "streaming":
            if not message:
                error_message = self._translate(
                    interaction,
                    "commands.admin.setpresence.errors.message_required",
                    default="❌ Informe o 'message' para o recado.",
                )
                return await interaction.response.send_message(error_message, ephemeral=True)
        if t == "streaming":
            if not message:
                error_message = self._translate(
                    interaction,
                    "commands.admin.setpresence.errors.streaming_message_required",
                    default="❌ Informe o 'message' para o recado de streaming.",
                )
                return await interaction.response.send_message(error_message, ephemeral=True)
            if not url or not url.startswith(("http://", "https://")):
                error_message = self._translate(
                    interaction,
                    "commands.admin.setpresence.errors.streaming_url",
                    default="❌ Para 'Streaming', forneça uma URL válida (Twitch/YouTube).",
                )
                return await interaction.response.send_message(error_message, ephemeral=True)

        # Mantém status salvo (ou padrão online)
        config = self.bot._load_presence_config()
        saved_status_str = config.get("status", "online")
        status_mapping = {
            "online": discord.Status.online,
            "idle": discord.Status.idle,
            "dnd": discord.Status.dnd,
            "invisible": discord.Status.invisible,
        }
        saved_status = status_mapping.get(saved_status_str, discord.Status.online)

        # Constrói atividade legada
        act = build_activity_from_inputs(t, message, url)

        # Aplica presença
        await interaction.client.change_presence(status=saved_status, activity=act)

        # Persiste no arquivo
        if t == "clear":
            config["activity"] = None
            description = self._translate(
                interaction,
                "commands.admin.setpresence.success.cleared",
                default="Atividade limpada",
            )
        else:
            config["activity"] = {
                "type": t,
                "message": message or "",
                "url": url if t == "streaming" else None
            }
            activity_label = self._translate(
                interaction,
                f"commands.admin.activity_types.{t}",
                default=activity_type.name,
            )
            description = self._translate(
                interaction,
                "commands.admin.setpresence.success.updated",
                default=f"Atividade definida: **{activity_type.name}** — {message or ''}",
                activity_label=activity_label,
                message=message or "",
            )

        # Garante status salvo
        config["status"] = saved_status_str
        self.bot.save_presence_config(config)

        embed = discord.Embed(
            title=self._translate(
                interaction,
                "commands.admin.setpresence.success.title",
                default="✅ Presença atualizada",
            ),
            description=description,
            color=0x00ff00
        )
        if t == "streaming" and url:
            embed.add_field(
                name=self._translate(
                    interaction,
                    "commands.admin.setpresence.success.url_label",
                    default="URL",
                ),
                value=url,
                inline=False
            )

        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(AdminCommands(bot))