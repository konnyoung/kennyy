import discord
from discord.ext import commands
from discord import app_commands
import os


def _resolve_bot_owner_ids(interaction: discord.Interaction) -> set[int]:
    client = getattr(interaction, "client", None)
    if client is None:
        return set()

    owner_ids = getattr(client, "owner_ids", None)
    if owner_ids:
        try:
            return set(int(owner_id) for owner_id in owner_ids)
        except Exception:
            return set(owner_ids)

    fallback_owner = getattr(client, "owner_id", None)
    if fallback_owner is not None:
        try:
            return {int(fallback_owner)}
        except Exception:
            return set()

    return set()


def is_admin(interaction: discord.Interaction) -> bool:
    if interaction.user is None:
        return False

    return interaction.user.id in _resolve_bot_owner_ids(interaction)


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
    admin = app_commands.Group(name="admin", description="Bot management (owner only)")
    
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Check global do Cog - só permite owners"""
        if interaction.user is None:
            return False
        
        owner_ids = getattr(self.bot, "owner_ids", set())
        if not owner_ids:
            # Fallback para owner_id único
            owner_id = getattr(self.bot, "owner_id", None)
            if owner_id:
                owner_ids = {owner_id}
        
        is_owner = interaction.user.id in owner_ids
        
        if not is_owner:
            # Responde com mensagem invisível
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message(
                        "❌ Você não tem permissão para usar este comando.",
                        ephemeral=True
                    )
            except:
                pass
            return False
        
        return True

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
                    default="❌ You don't have permission to use this command.",
                )
                if interaction.response.is_done():
                    await interaction.followup.send(message, ephemeral=True)
                else:
                    await interaction.response.send_message(message, ephemeral=True)
            except Exception:
                pass

    @admin.command(name="setstatus", description="Change the bot's status (Online, DND, Idle, Invisible)")
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

    @admin.command(name="setpresence", description="Update the bot's activity (Playing/Listening/Watching/Competing/Streaming)")
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

    @admin.command(name="logs", description="Enable or disable bot logs")
    @app_commands.describe(
        action="Enable or disable logs"
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="Enable", value="enable"),
        app_commands.Choice(name="Disable", value="disable"),
        app_commands.Choice(name="Status", value="status"),
    ])
    @app_commands.check(is_admin)
    async def logs_toggle(
        self,
        interaction: discord.Interaction,
        action: app_commands.Choice[str]
    ):
        # Verifica se o MongoDB está conectado
        if not hasattr(self.bot, 'logs_collection') or self.bot.logs_collection is None:
            error_message = self._translate(
                interaction,
                "commands.admin.logs.errors.no_mongodb",
                default="❌ MongoDB não está configurado. Não é possível salvar preferências de logs.",
            )
            return await interaction.response.send_message(error_message, ephemeral=True)
        
        action_value = action.value
        
        if action_value == "status":
            # Mostra o status atual
            try:
                config = self.bot.logs_collection.find_one({"_id": "global"})
                enabled = config.get("enabled", True) if config else True

                status_text = self._translate(
                    interaction,
                    "commands.admin.logs.status.enabled" if enabled else "commands.admin.logs.status.disabled",
                    default="habilitados" if enabled else "desabilitados",
                )

                embed = discord.Embed(
                    title=self._translate(
                        interaction,
                        "commands.admin.logs.status.title",
                        default="📊 Status dos Logs",
                    ),
                    description=self._translate(
                        interaction,
                        "commands.admin.logs.status.description",
                        default=f"Os logs estão atualmente **{status_text}**.",
                        status=status_text,
                    ),
                    color=0x3498db
                )

                # Verifica se o canal de logs está configurado
                log_channel_id = os.getenv("LOG_CHANNEL_ID", "")
                if log_channel_id and log_channel_id.isdigit():
                    embed.add_field(
                        name=self._translate(
                            interaction,
                            "commands.admin.logs.status.channel_label",
                            default="Canal de Logs",
                        ),
                        value=f"<#{log_channel_id}>",
                        inline=False
                    )
                else:
                    embed.add_field(
                        name=self._translate(
                            interaction,
                            "commands.admin.logs.status.channel_label",
                            default="Canal de Logs",
                        ),
                        value=self._translate(
                            interaction,
                            "commands.admin.logs.status.no_channel",
                            default="⚠️ Não configurado (defina LOG_CHANNEL_ID no .env)",
                        ),
                        inline=False
                    )

                await interaction.response.send_message(embed=embed, ephemeral=True)
            except Exception as exc:
                error_message = self._translate(
                    interaction,
                    "commands.admin.logs.errors.status_failed",
                    default=f"❌ Erro ao verificar status dos logs: {exc}",
                    error=str(exc),
                )
                await interaction.response.send_message(error_message, ephemeral=True)
        
        elif action_value == "enable":
            # Habilita os logs
            try:
                self.bot.logs_collection.update_one(
                    {"_id": "global"},
                    {"$set": {"enabled": True}},
                    upsert=True
                )
                
                embed = discord.Embed(
                    title=self._translate(
                        interaction,
                        "commands.admin.logs.enable.title",
                        default="✅ Logs Habilitados",
                    ),
                    description=self._translate(
                        interaction,
                        "commands.admin.logs.enable.description",
                        default="Os logs do bot foram habilitados com sucesso!",
                    ),
                    color=0x00ff00
                )
                
                await interaction.response.send_message(embed=embed, ephemeral=True)
            except Exception as exc:
                error_message = self._translate(
                    interaction,
                    "commands.admin.logs.errors.enable_failed",
                    default=f"❌ Erro ao habilitar logs: {exc}",
                    error=str(exc),
                )
                await interaction.response.send_message(error_message, ephemeral=True)
        
        elif action_value == "disable":
            # Desabilita os logs
            try:
                self.bot.logs_collection.update_one(
                    {"_id": "global"},
                    {"$set": {"enabled": False}},
                    upsert=True
                )
                
                embed = discord.Embed(
                    title=self._translate(
                        interaction,
                        "commands.admin.logs.disable.title",
                        default="🔕 Logs Desabilitados",
                    ),
                    description=self._translate(
                        interaction,
                        "commands.admin.logs.disable.description",
                        default="Os logs do bot foram desabilitados com sucesso!",
                    ),
                    color=0xff9900
                )
                
                await interaction.response.send_message(embed=embed, ephemeral=True)
            except Exception as exc:
                error_message = self._translate(
                    interaction,
                    "commands.admin.logs.errors.disable_failed",
                    default=f"❌ Erro ao desabilitar logs: {exc}",
                    error=str(exc),
                )
                await interaction.response.send_message(error_message, ephemeral=True)

    @admin.command(name="warp", description="Enable or disable WARP auto-reconnect (owners only)")
    @app_commands.describe(action="Enable, disable or show status of WARP auto-reconnect")
    @app_commands.choices(action=[
        app_commands.Choice(name="Enable", value="enable"),
        app_commands.Choice(name="Disable", value="disable"),
        app_commands.Choice(name="Status", value="status"),
    ])
    @app_commands.check(is_admin)
    async def warp_toggle(
        self,
        interaction: discord.Interaction,
        action: app_commands.Choice[str],
    ):
        action_value = action.value
        current = getattr(self.bot, "enable_warp_reconnect", True)

        if action_value == "status":
            status_label = self._translate(
                interaction,
                "commands.admin.warp.status.enabled" if current else "commands.admin.warp.status.disabled",
                default="enabled" if current else "disabled",
            )
            embed = discord.Embed(
                title=self._translate(
                    interaction,
                    "commands.admin.warp.status.title",
                    default="WARP auto-reconnect",
                ),
                description=self._translate(
                    interaction,
                    "commands.admin.warp.status.description",
                    default=f"WARP auto-reconnect is currently **{status_label}**.",
                    status=status_label,
                ),
                color=0x3498db,
            )
            return await interaction.response.send_message(embed=embed, ephemeral=True)

        if action_value == "enable":
            self.bot.enable_warp_reconnect = True
            title_key = "commands.admin.warp.enable.title"
            desc_key = "commands.admin.warp.enable.description"
            default_title = "✅ WARP auto-reconnect enabled"
            default_desc = "The bot will run warp-cli on specific faults and retry after 5s."
            color = 0x57F287
        else:
            self.bot.enable_warp_reconnect = False
            title_key = "commands.admin.warp.disable.title"
            desc_key = "commands.admin.warp.disable.description"
            default_title = "🚫 WARP auto-reconnect disabled"
            default_desc = "The bot will no longer run warp-cli or retry on that fault."
            color = 0xED4245

        persisted = False
        saver = getattr(self.bot, "save_warp_setting", None)
        if callable(saver):
            persisted = saver(self.bot.enable_warp_reconnect)

        embed = discord.Embed(
            title=self._translate(
                interaction,
                title_key,
                default=default_title,
            ),
            description=self._translate(
                interaction,
                desc_key,
                default=default_desc,
            ),
            color=color,
        )
        if not persisted:
            embed.set_footer(text="Preferência não persistida no MongoDB (modo apenas em memória).")
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot):
    cog = AdminCommands(bot)
    # Define permissões padrão - comando visível apenas para administradores
    # Os owners do bot sempre podem usar mesmo sem a permissão
    cog.admin.default_permissions = discord.Permissions(administrator=True)
    await bot.add_cog(cog)