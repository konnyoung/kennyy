import discord
from discord.ext import commands
from discord import app_commands
import math
import os


class _GuildsPagerView(discord.ui.View):
    def __init__(
        self,
        *,
        invoker_id: int,
        total_pages: int,
        make_embed,
        timeout: float = 180,
    ):
        super().__init__(timeout=timeout)
        self._invoker_id = invoker_id
        self._total_pages = max(1, int(total_pages))
        self._make_embed = make_embed
        self._page = 0

        self._prev_button = discord.ui.Button(
            emoji="⬅️",
            style=discord.ButtonStyle.secondary,
        )
        self._next_button = discord.ui.Button(
            emoji="➡️",
            style=discord.ButtonStyle.secondary,
        )

        self._prev_button.callback = self._go_prev
        self._next_button.callback = self._go_next

        self.add_item(self._prev_button)
        self.add_item(self._next_button)
        self._sync_buttons()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return bool(interaction.user and interaction.user.id == self._invoker_id)

    def _sync_buttons(self) -> None:
        if self._total_pages <= 1:
            self._prev_button.disabled = True
            self._next_button.disabled = True
            return

        self._prev_button.disabled = self._page <= 0
        self._next_button.disabled = self._page >= (self._total_pages - 1)

    async def _go_prev(self, interaction: discord.Interaction):
        if self._page <= 0:
            return

        self._page -= 1
        self._sync_buttons()
        await interaction.response.edit_message(embed=self._make_embed(self._page), view=self)

    async def _go_next(self, interaction: discord.Interaction):
        if self._page >= (self._total_pages - 1):
            return

        self._page += 1
        self._sync_buttons()
        await interaction.response.edit_message(embed=self._make_embed(self._page), view=self)


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
            text = translator(key, guild_id=guild_id, default=default)
        else:
            text = default if default is not None else key

        if isinstance(text, str) and kwargs:
            try:
                return text.format(**kwargs)
            except Exception as exc:
                print(f"Erro ao formatar tradução '{key}': {exc}")
                return text

        return text

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

    @admin.command(name="setguildlanguage", description="Set the bot language for a server by its ID (owners only)")
    @app_commands.describe(
        guild_id="Server (Guild) ID",
        language="Choose the language to set for that server",
    )
    @app_commands.choices(
        language=[
            app_commands.Choice(name="🇧🇷 Português (Brasil)", value="pt"),
            app_commands.Choice(name="🇵🇹 Português (Portugal)", value="pt-pt"),
            app_commands.Choice(name="🇺🇸 English", value="en"),
            app_commands.Choice(name="🇪🇸 Español", value="es"),
            app_commands.Choice(name="🇫🇷 Français", value="fr"),
            app_commands.Choice(name="🇮🇹 Italiano", value="it"),
            app_commands.Choice(name="🇯🇵 日本語", value="ja"),
            app_commands.Choice(name="🇹🇷 Türkçe", value="tr"),
            app_commands.Choice(name="🇷🇺 Русский", value="ru"),
        ]
    )
    @app_commands.check(is_admin)
    async def set_guild_language_by_id(
        self,
        interaction: discord.Interaction,
        guild_id: str,
        language: app_commands.Choice[str],
    ):
        bot = getattr(self, "bot", None)
        if bot is None:
            await interaction.response.send_message(
                self._translate(interaction, "commands.admin.setguildlanguage.errors.bot_unavailable", default="❌ Bot not available."),
                ephemeral=True,
            )
            return

        try:
            try:
                guild_id_value = int((guild_id or "").strip())
            except Exception:
                await interaction.response.send_message(
                    self._translate(
                        interaction,
                        "commands.admin.setguildlanguage.errors.invalid_guild_id",
                        default="❌ Please enter a valid server ID.",
                    ),
                    ephemeral=True,
                )
                return

            if guild_id_value <= 0:
                await interaction.response.send_message(
                    self._translate(
                        interaction,
                        "commands.admin.setguildlanguage.errors.invalid_guild_id",
                        default="❌ Please enter a valid server ID.",
                    ),
                    ephemeral=True,
                )
                return

            await interaction.response.defer(ephemeral=True)

            if not hasattr(bot, "set_guild_language"):
                await interaction.followup.send(
                    self._translate(
                        interaction,
                        "commands.admin.setguildlanguage.errors.feature_unavailable",
                        default="❌ This feature is not available on this bot build.",
                    ),
                    ephemeral=True,
                )
                return

            collection = getattr(bot, "language_collection", None)
            if collection is None:
                await interaction.followup.send(
                    self._translate(
                        interaction,
                        "commands.admin.setguildlanguage.errors.db_unavailable",
                        default="❌ Database not configured. Unable to save server language.",
                    ),
                    ephemeral=True,
                )
                return

            new_language = language.value
            if new_language not in getattr(bot, "supported_languages", set()):
                await interaction.followup.send(
                    self._translate(
                        interaction,
                        "commands.admin.setguildlanguage.errors.unsupported",
                        default="❌ Unsupported language.",
                    ),
                    ephemeral=True,
                )
                return

            target_guild = None
            try:
                target_guild = bot.get_guild(guild_id_value)
            except Exception:
                target_guild = None

            unknown_guild_label = self._translate(
                interaction,
                "commands.admin.setguildlanguage.unknown_guild",
                default="Unknown server",
            )
            guild_name = getattr(target_guild, "name", None) or unknown_guild_label

            # Show the language label in its own locale (e.g. "Русский", "Português (Brasil)")
            language_label = bot.translate(
                f"languages.{new_language}.label",
                locale=new_language,
                default=new_language,
            )

            success = bot.set_guild_language(guild_id_value, new_language)
            if not success:
                await interaction.followup.send(
                    self._translate(
                        interaction,
                        "commands.admin.setguildlanguage.errors.save_failed",
                        default="❌ Failed to save language for this server.",
                    ),
                    ephemeral=True,
                )
                return

            embed = discord.Embed(
                title=self._translate(
                    interaction,
                    "commands.admin.setguildlanguage.success.title",
                    default="✅ Server language updated",
                ),
                description=self._translate(
                    interaction,
                    "commands.admin.setguildlanguage.success.description",
                    default=f"Server **{guild_name}** (`{guild_id}`) language set to **{language_label}**.",
                    guild_name=guild_name,
                    guild_id=guild_id_value,
                    language_label=language_label,
                ),
                color=0x00FF00,
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as exc:
            message = self._translate(
                interaction,
                "commands.admin.setguildlanguage.errors.unexpected",
                default="❌ Unexpected error: {error}",
                error=str(exc),
            )
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(message, ephemeral=True)
                else:
                    await interaction.response.send_message(message, ephemeral=True)
            except Exception:
                pass

    @admin.command(name="guilds", description="List all servers where the bot is in (owners only)")
    @app_commands.check(is_admin)
    async def list_guilds(
        self,
        interaction: discord.Interaction,
    ):
        try:
            await interaction.response.defer(ephemeral=True)

            guilds = list(getattr(self.bot, "guilds", []) or [])
            guilds.sort(key=lambda g: (getattr(g, "name", "") or "").casefold())

            per_page = 10
            total = len(guilds)
            total_pages = max(1, math.ceil(total / per_page))

            def make_embed(page_index: int) -> discord.Embed:
                start = page_index * per_page
                end = start + per_page
                page_guilds = guilds[start:end]

                title = self._translate(
                    interaction,
                    "commands.admin.guilds.title",
                    default="📋 Servers",
                )

                if not guilds:
                    description = self._translate(
                        interaction,
                        "commands.admin.guilds.empty",
                        default="No servers found.",
                    )
                else:
                    lines = []
                    for g in page_guilds:
                        name = getattr(g, "name", None) or "Unknown"
                        gid = getattr(g, "id", None)
                        if gid is None:
                            lines.append(f"{name}")
                        else:
                            lines.append(f"{name} (`{gid}`)")
                    description = "\n".join(lines) if lines else "-"

                embed = discord.Embed(
                    title=title,
                    description=description,
                    color=0x5865F2,
                )

                footer = self._translate(
                    interaction,
                    "commands.admin.guilds.footer",
                    default="Page {page}/{total_pages} • Total: {total}",
                    page=page_index + 1,
                    total_pages=total_pages,
                    total=total,
                )
                embed.set_footer(text=footer)
                return embed

            view = _GuildsPagerView(
                invoker_id=interaction.user.id,
                total_pages=total_pages,
                make_embed=make_embed,
            )

            await interaction.followup.send(embed=make_embed(0), view=view, ephemeral=True)
        except Exception as exc:
            message = self._translate(
                interaction,
                "commands.admin.guilds.errors.unexpected",
                default="❌ Unexpected error: {error}",
                error=str(exc),
            )
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(message, ephemeral=True)
                else:
                    await interaction.response.send_message(message, ephemeral=True)
            except Exception:
                pass

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

    @admin.command(name="restart", description="Schedule a bot restart when no one is using it (owners only)")
    @app_commands.describe(
        action="Action to perform",
    )
    @app_commands.choices(
        action=[
            app_commands.Choice(name="Schedule Restart", value="schedule"),
            app_commands.Choice(name="Cancel Scheduled Restart", value="cancel"),
            app_commands.Choice(name="Check Status", value="status"),
        ]
    )
    async def restart(
        self,
        interaction: discord.Interaction,
        action: app_commands.Choice[str],
    ):
        """Agenda reinício do bot quando ninguém estiver usando."""
        
        action_value = action.value if action else "status"
        
        if action_value == "schedule":
            # Agenda o reinício
            if not hasattr(self.bot, "_restart_scheduled"):
                self.bot._restart_scheduled = False
            
            if self.bot._restart_scheduled:
                embed = discord.Embed(
                    title="⚠️ Reinício já agendado",
                    description="O bot já está aguardando para reiniciar assim que ninguém estiver em call.",
                    color=0xffaa00
                )
                return await interaction.response.send_message(embed=embed, ephemeral=True)
            
            self.bot._restart_scheduled = True
            
            # Inicia task de monitoramento
            if not hasattr(self.bot, "_restart_monitor_task") or self.bot._restart_monitor_task is None or self.bot._restart_monitor_task.done():
                import asyncio
                self.bot._restart_monitor_task = asyncio.create_task(self._monitor_restart())
            
            embed = discord.Embed(
                title="✅ Reinício agendado",
                description="O bot será reiniciado automaticamente assim que não houver mais ninguém em nenhuma call.\n\n**Status atual:** Monitorando conexões...",
                color=0x00ff00
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            
        elif action_value == "cancel":
            # Cancela o agendamento
            if not hasattr(self.bot, "_restart_scheduled") or not self.bot._restart_scheduled:
                embed = discord.Embed(
                    title="ℹ️ Nenhum reinício agendado",
                    description="Não há nenhum reinício pendente para cancelar.",
                    color=0x5865f2
                )
                return await interaction.response.send_message(embed=embed, ephemeral=True)
            
            self.bot._restart_scheduled = False
            
            # Cancela a task de monitoramento se existir
            if hasattr(self.bot, "_restart_monitor_task") and self.bot._restart_monitor_task and not self.bot._restart_monitor_task.done():
                self.bot._restart_monitor_task.cancel()
            
            embed = discord.Embed(
                title="🚫 Reinício cancelado",
                description="O agendamento de reinício foi cancelado com sucesso.",
                color=0xff0000
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            
        elif action_value == "status":
            # Verifica status do agendamento
            is_scheduled = getattr(self.bot, "_restart_scheduled", False)
            
            if not is_scheduled:
                embed = discord.Embed(
                    title="ℹ️ Status do Reinício",
                    description="**Status:** Nenhum reinício agendado.",
                    color=0x5865f2
                )
            else:
                # Conta quantos players ativos existem
                active_connections = 0
                try:
                    import wavelink
                    for node in wavelink.Pool.nodes.values():
                        for player in node.players.values():
                            if player.connected:
                                active_connections += 1
                except Exception:
                    pass
                
                embed = discord.Embed(
                    title="⏳ Reinício Agendado",
                    description=f"**Status:** Aguardando para reiniciar\n**Conexões ativas:** {active_connections} call(s)\n\nO bot será reiniciado assim que todas as conexões forem encerradas.",
                    color=0xffaa00
                )
            
            await interaction.response.send_message(embed=embed, ephemeral=True)
    
    async def _monitor_restart(self):
        """Monitora conexões ativas e reinicia quando não houver mais ninguém."""
        import asyncio
        import sys
        
        print("🔄 Monitor de reinício iniciado. Aguardando calls terminarem...")
        
        while getattr(self.bot, "_restart_scheduled", False):
            try:
                # Verifica se há algum player conectado
                has_active_connections = False
                
                try:
                    import wavelink
                    for node in wavelink.Pool.nodes.values():
                        for player in node.players.values():
                            if player.connected:
                                has_active_connections = True
                                break
                        if has_active_connections:
                            break
                except Exception as exc:
                    print(f"Erro ao verificar players ativos: {exc}")
                
                if not has_active_connections:
                    print("✅ Nenhuma conexão ativa detectada. Reiniciando o bot...")
                    
                    # Desconecta de todas as calls (cleanup)
                    try:
                        for guild in self.bot.guilds:
                            if guild.voice_client:
                                try:
                                    await guild.voice_client.disconnect(force=True)
                                except Exception:
                                    pass
                    except Exception:
                        pass
                    
                    # Fecha conexão com Discord
                    await self.bot.close()
                    
                    # Reinicia o processo Python
                    import os
                    print("🔄 Reexecutando o script Python...")
                    os.execv(sys.executable, [sys.executable] + sys.argv)
                
                # Aguarda 10 segundos antes de verificar novamente
                await asyncio.sleep(10)
                
            except asyncio.CancelledError:
                print("🚫 Monitor de reinício cancelado.")
                break
            except Exception as exc:
                print(f"Erro no monitor de reinício: {exc}")
                await asyncio.sleep(10)

    @admin.command(name="nodes", description="Show detailed information about all Lavalink nodes (owners only)")
    async def nodes(self, interaction: discord.Interaction):
        """Mostra informações técnicas detalhadas de todos os nodes Lavalink"""
        try:
            await interaction.response.defer()
        except Exception:
            pass

        import wavelink
        import platform
        import time

        # Verifica se há nodes configurados
        if not hasattr(self.bot, "_lavalink_cfgs") or not self.bot._lavalink_cfgs:
            embed = discord.Embed(
                title="❌ Nenhum Node Configurado",
                description="Nenhum node Lavalink foi configurado no bot.",
                color=0xFF0000,
            )
            return await interaction.followup.send(embed=embed)

        # Coleta informações de todos os nodes
        embeds = []
        
        for cfg in self.bot._lavalink_cfgs:
            node_id = cfg["id"]
            host = cfg["host"]
            port = cfg["port"]
            protocol = cfg["protocol"]
            
            try:
                node = wavelink.Pool.get_node(node_id)
            except wavelink.InvalidNodeException:
                # Node não existe no pool
                embed = discord.Embed(
                    title=f"❌ {node_id.upper()}",
                    description=f"**Host:** `{host}:{port}`\n**Status:** `DISCONNECTED`",
                    color=0xFF0000,
                )
                embed.add_field(name="⚠️ Erro", value="Node não conectado ao pool", inline=False)
                embeds.append(embed)
                continue

            # Pega informações do node
            status = node.status
            status_emoji = {
                wavelink.NodeStatus.CONNECTED: "🟢",
                wavelink.NodeStatus.CONNECTING: "🟡",
                wavelink.NodeStatus.DISCONNECTED: "🔴",
            }.get(status, "⚪")
            
            status_name = getattr(status, "name", str(status))
            
            # Informações básicas
            uri = f"{protocol}://{host}:{port}"
            players_count = len(node.players)
            
            # Busca stats diretamente da API do Lavalink
            stats = None
            try:
                stats = await node.send("GET", path="/v4/stats")
            except Exception:
                pass
            
            # Cor baseada no status
            color = {
                wavelink.NodeStatus.CONNECTED: 0x00FF00,
                wavelink.NodeStatus.CONNECTING: 0xFFFF00,
                wavelink.NodeStatus.DISCONNECTED: 0xFF0000,
            }.get(status, 0x808080)
            
            embed = discord.Embed(
                title=f"{status_emoji} {node_id.upper()}",
                description=f"**URI:** `{uri}`\n**Status:** `{status_name}`",
                color=color,
            )
            
            # Informações de players
            playing_count = sum(1 for p in node.players.values() if getattr(p, "playing", False))
            embed.add_field(
                name="🎵 Players",
                value=f"`{players_count}` total\n`{playing_count}` tocando",
                inline=True,
            )
            
            # Tenta obter latência (igual ao /ping)
            latency_ms = None
            try:
                import aiohttp
                timeout = aiohttp.ClientTimeout(total=4)
                headers = {"Authorization": cfg["password"]}
                url = f"{protocol}://{host}:{port}/v4/info"
                
                start_time = time.perf_counter()
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(url, headers=headers) as resp:
                        if resp.status < 500:
                            end_time = time.perf_counter()
                            latency_ms = int((end_time - start_time) * 1000)
            except Exception:
                pass
            
            if latency_ms is not None:
                # Emoji baseado na latência
                if latency_ms <= 100:
                    ping_emoji = "🟢"
                elif latency_ms <= 200:
                    ping_emoji = "🟡"
                else:
                    ping_emoji = "🔴"
                
                embed.add_field(
                    name="📡 Latência",
                    value=f"{ping_emoji} `{latency_ms}ms`",
                    inline=True,
                )
            else:
                embed.add_field(
                    name="📡 Latência",
                    value="`N/A`",
                    inline=True,
                )
            
            # Pega session_id do node
            session_id = getattr(node, "session_id", None)
            if session_id:
                embed.add_field(
                    name="🔑 Session ID",
                    value=f"`{session_id[:8]}...`",
                    inline=True,
                )
            
            # Stats do node (se disponível)
            if stats and isinstance(stats, dict):
                # Informações de sistema
                memory = stats.get("memory", {})
                if memory:
                    used_mb = memory.get("used", 0) / (1024 * 1024)
                    free_mb = memory.get("free", 0) / (1024 * 1024)
                    allocated_mb = memory.get("allocated", 0) / (1024 * 1024)
                    
                    embed.add_field(
                        name="💾 Memória",
                        value=f"Usado: `{used_mb:.0f}MB`\nLivre: `{free_mb:.0f}MB`\nAlocado: `{allocated_mb:.0f}MB`",
                        inline=True,
                    )
                
                cpu = stats.get("cpu", {})
                if cpu:
                    cores = cpu.get("cores", 0)
                    system_load = cpu.get("systemLoad", 0) * 100
                    lavalink_load = cpu.get("lavalinkLoad", 0) * 100
                    
                    embed.add_field(
                        name="🖥️ CPU",
                        value=f"Cores: `{cores}`\nSistema: `{system_load:.1f}%`\nLavalink: `{lavalink_load:.1f}%`",
                        inline=True,
                    )
                
                # Uptime
                uptime_ms = stats.get("uptime", 0)
                if uptime_ms > 0:
                    uptime_seconds = uptime_ms / 1000
                    days = int(uptime_seconds // 86400)
                    hours = int((uptime_seconds % 86400) // 3600)
                    minutes = int((uptime_seconds % 3600) // 60)
                    
                    uptime_str = []
                    if days > 0:
                        uptime_str.append(f"{days}d")
                    if hours > 0 or days > 0:
                        uptime_str.append(f"{hours}h")
                    uptime_str.append(f"{minutes}m")
                    
                    embed.add_field(
                        name="⏱️ Uptime",
                        value=f"`{' '.join(uptime_str)}`",
                        inline=True,
                    )
                
                # Frame stats (se houver players)
                frame_stats = stats.get("frameStats")
                if frame_stats and players_count > 0:
                    sent = frame_stats.get("sent", 0)
                    nulled = frame_stats.get("nulled", 0)
                    deficit = frame_stats.get("deficit", 0)
                    
                    embed.add_field(
                        name="📊 Frame Stats",
                        value=f"Enviados: `{sent}`\nNulos: `{nulled}`\nDeficit: `{deficit}`",
                        inline=True,
                    )
                
                # Players globais do servidor Lavalink (todos os bots)
                global_players = stats.get("players", 0)
                global_playing = stats.get("playingPlayers", 0)
                embed.add_field(
                    name="🌐 Servidor (Global)",
                    value=f"Players: `{global_players}`\nTocando: `{global_playing}`",
                    inline=True,
                )
            else:
                embed.add_field(
                    name="⚠️ Stats",
                    value="Stats não disponíveis\n(node pode estar iniciando ou não suporta)",
                    inline=False,
                )
            
            # Informações do bot conectado
            embed.set_footer(text=f"🤖 Bot: {self.bot.user.name} | Python {platform.python_version()}")
            
            embeds.append(embed)
        
        # Envia todos os embeds
        if len(embeds) == 1:
            await interaction.followup.send(embed=embeds[0])
        else:
            await interaction.followup.send(embeds=embeds)


async def setup(bot):
    cog = AdminCommands(bot)
    # Define permissões padrão - comando visível apenas para administradores
    # Os owners do bot sempre podem usar mesmo sem a permissão
    cog.admin.default_permissions = discord.Permissions(administrator=True)
    await bot.add_cog(cog)