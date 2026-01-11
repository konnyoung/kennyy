"""
Sistema de logs para o bot de música.
Envia embeds via webhooks configurados na variável de ambiente LOG_WEBHOOKS.
Suporta múltiplos webhooks separados por vírgula.
"""
import discord
import aiohttp
import os
import re
from datetime import datetime
from typing import Optional, List


class BotLogger:
    """Classe para gerenciar logs do bot via webhooks"""
    
    def __init__(self, bot):
        self.bot = bot
        self._webhook_urls: List[str] = []
        self._load_webhooks()
    
    def _load_webhooks(self) -> None:
        """Carrega as URLs dos webhooks da variável de ambiente"""
        webhooks_env = os.getenv("LOG_WEBHOOKS", "").strip()
        if webhooks_env:
            # Suporta múltiplos webhooks separados por vírgula
            self._webhook_urls = [
                url.strip() for url in webhooks_env.split(",") 
                if url.strip() and self._is_valid_webhook_url(url.strip())
            ]
        else:
            self._webhook_urls = []
    
    def _is_valid_webhook_url(self, url: str) -> bool:
        """Verifica se a URL é um webhook válido do Discord"""
        pattern = r'^https://discord\.com/api/webhooks/\d+/[\w-]+$'
        return bool(re.match(pattern, url))
    
    def _get_node_display_name(self, node=None) -> str | None:
        """Obtém o nome de exibição do node (mesmo método do bot principal)"""
        if node is None:
            # Tenta pegar qualquer node conectado
            try:
                nodes = getattr(self.bot, '_wavelink_nodes', None)
                if nodes:
                    for n in nodes.values():
                        if getattr(n, 'connected', False):
                            node = n
                            break
            except Exception:
                pass
        
        if node is None:
            return None
            
        identifier = getattr(node, "identifier", None)
        if not identifier:
            return None

        identifier_str = str(identifier)

        for cfg in getattr(self.bot, "_lavalink_cfgs", []) or []:
            try:
                if str(cfg.get("id")) != identifier_str:
                    continue
                name = str(cfg.get("name") or "").strip()
                if name:
                    return name
            except Exception:
                continue

        match = re.match(r"^node(\d+)$", identifier_str)
        if match:
            env_name = (os.getenv(f"LAVALINK_NODE{match.group(1)}_NAME", "") or "").strip()
            if env_name:
                return env_name

        return identifier_str
    
    async def _is_logging_enabled(self) -> bool:
        """Verifica se os logs estão habilitados no MongoDB"""
        if not hasattr(self.bot, 'logs_collection') or self.bot.logs_collection is None:
            return True  # Se não há MongoDB, assume que está habilitado
        
        try:
            config = self.bot.logs_collection.find_one({"_id": "global"})
            if config:
                return config.get("enabled", True)
            return True  # Padrão: habilitado
        except Exception as exc:
            print(f"Erro ao verificar status de logs no MongoDB: {exc}")
            return True

    def _translate(
        self,
        key: str,
        *,
        guild_id: Optional[int] = None,
        default: Optional[str] = None,
        **kwargs,
    ) -> str:
        """Atalho para traduzir textos respeitando o idioma configurado."""

        translator = getattr(self.bot, "translate", None)
        if callable(translator):
            return translator(
                key,
                guild_id=guild_id,
                default=default,
                **kwargs,
            )

        if default is None:
            return key

        if kwargs:
            try:
                return default.format(**kwargs)
            except Exception:
                return default

        return default
    
    def _add_node_footer(self, embed: discord.Embed, node=None) -> None:
        """Adiciona o footer com o nome do node"""
        node_name = self._get_node_display_name(node)
        if node_name:
            embed.set_footer(text=f"Node: {node_name}")
    
    async def _send_log(self, embed: discord.Embed, node=None) -> bool:
        """Envia uma embed para todos os webhooks configurados"""
        if not await self._is_logging_enabled():
            return False
        
        if not self._webhook_urls:
            return False
        
        # Adiciona o footer com o node
        self._add_node_footer(embed, node)
        
        success = False
        
        async with aiohttp.ClientSession() as session:
            for webhook_url in self._webhook_urls:
                try:
                    webhook = discord.Webhook.from_url(webhook_url, session=session)
                    await webhook.send(embed=embed)
                    success = True
                except Exception as exc:
                    print(f"Erro ao enviar log via webhook: {exc}")
        
        return success
    
    async def log_guild_join(self, guild: discord.Guild) -> None:
        """Registra quando o bot entra em um servidor, respeitando o idioma local."""

        guild_id = getattr(guild, "id", None)
        title = self._translate(
            "logs.guild_join.title",
            guild_id=guild_id,
            default="📥 Entrei em um novo servidor!",
        )

        embed = discord.Embed(title=title, color=0x00FF00, timestamp=datetime.utcnow())

        unknown_text = self._translate(
            "logs.common.unknown",
            guild_id=guild_id,
            default="Desconhecido",
        )

        name_label = self._translate(
            "logs.guild_join.fields.name",
            guild_id=guild_id,
            default="Nome do Servidor",
        )
        embed.add_field(name=name_label, value=guild.name, inline=False)

        id_label = self._translate(
            "logs.guild_join.fields.id",
            guild_id=guild_id,
            default="ID do Servidor",
        )
        embed.add_field(name=id_label, value=f"`{guild.id}`", inline=True)

        members_label = self._translate(
            "logs.guild_join.fields.members",
            guild_id=guild_id,
            default="Membros",
        )

        member_count = getattr(guild, "member_count", None)
        if member_count:
            members_value = self._translate(
                "logs.common.member_count",
                guild_id=guild_id,
                default="{count} membros",
                count=f"{member_count:,}",
            )
        else:
            members_value = unknown_text

        embed.add_field(name=members_label, value=members_value, inline=True)

        if guild.owner_id:
            owner_label = self._translate(
                "logs.guild_join.fields.owner",
                guild_id=guild_id,
                default="Dono",
            )
            owner_value = f"<@{guild.owner_id}> (`{guild.owner_id}`)"
            embed.add_field(name=owner_label, value=owner_value, inline=True)

        if guild.created_at:
            created_label = self._translate(
                "logs.guild_join.fields.created_at",
                guild_id=guild_id,
                default="Criado em",
            )
            creation_date = guild.created_at.strftime("%d/%m/%Y")
            embed.add_field(name=created_label, value=creation_date, inline=True)

        verification_key = {
            discord.VerificationLevel.none: "logs.common.verification.none",
            discord.VerificationLevel.low: "logs.common.verification.low",
            discord.VerificationLevel.medium: "logs.common.verification.medium",
            discord.VerificationLevel.high: "logs.common.verification.high",
            discord.VerificationLevel.highest: "logs.common.verification.highest",
        }.get(guild.verification_level, "logs.common.unknown")

        verification_label = self._translate(
            "logs.guild_join.fields.verification",
            guild_id=guild_id,
            default="Verificação",
        )
        verification_value = self._translate(
            verification_key,
            guild_id=guild_id,
            default="Desconhecido",
        )

        boost_label = self._translate(
            "logs.guild_join.fields.boost",
            guild_id=guild_id,
            default="Boost",
        )
        boost_count = getattr(guild, "premium_subscription_count", 0) or 0
        boost_key = "logs.common.boost.with_count" if boost_count else "logs.common.boost.basic"
        boost_value = self._translate(
            boost_key,
            guild_id=guild_id,
            default="Nível {tier}" if not boost_count else "Nível {tier} ({count} boosts)",
            tier=guild.premium_tier,
            count=boost_count,
        )

        embed.add_field(name=boost_label, value=boost_value, inline=True)
        embed.add_field(name=verification_label, value=verification_value, inline=True)

        total_label = self._translate(
            "logs.guild_join.fields.total",
            guild_id=guild_id,
            default="Total de Servidores",
        )
        total_value = self._translate(
            "logs.guild_join.summary",
            guild_id=guild_id,
            default="🌐 Agora estou em **{total}** servidores!",
            total=len(self.bot.guilds),
        )
        embed.add_field(name=total_label, value=total_value, inline=False)

        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        if guild.banner:
            embed.set_image(url=guild.banner.url)

        await self._send_log(embed)
    
    async def log_guild_remove(self, guild: discord.Guild) -> None:
        """Registra quando o bot sai de um servidor, respeitando o idioma local."""

        guild_id = getattr(guild, "id", None)
        title = self._translate(
            "logs.guild_remove.title",
            guild_id=guild_id,
            default="📤 Saí de um servidor",
        )

        embed = discord.Embed(title=title, color=0xFF0000, timestamp=datetime.utcnow())

        unknown_text = self._translate(
            "logs.common.unknown",
            guild_id=guild_id,
            default="Desconhecido",
        )

        name_label = self._translate(
            "logs.guild_remove.fields.name",
            guild_id=guild_id,
            default="Nome do Servidor",
        )
        embed.add_field(name=name_label, value=guild.name, inline=False)

        id_label = self._translate(
            "logs.guild_remove.fields.id",
            guild_id=guild_id,
            default="ID do Servidor",
        )
        embed.add_field(name=id_label, value=f"`{guild.id}`", inline=True)

        members_label = self._translate(
            "logs.guild_remove.fields.members",
            guild_id=guild_id,
            default="Membros",
        )
        member_count = getattr(guild, "member_count", None)
        if member_count:
            members_value = self._translate(
                "logs.common.member_count",
                guild_id=guild_id,
                default="{count} membros",
                count=f"{member_count:,}",
            )
        else:
            members_value = unknown_text
        embed.add_field(name=members_label, value=members_value, inline=True)

        if guild.owner_id:
            owner_label = self._translate(
                "logs.guild_remove.fields.owner",
                guild_id=guild_id,
                default="Dono",
            )
            owner_value = f"<@{guild.owner_id}> (`{guild.owner_id}`)"
            embed.add_field(name=owner_label, value=owner_value, inline=True)

        if guild.created_at:
            created_label = self._translate(
                "logs.guild_remove.fields.created_at",
                guild_id=guild_id,
                default="Criado em",
            )
            creation_date = guild.created_at.strftime("%d/%m/%Y")
            embed.add_field(name=created_label, value=creation_date, inline=True)

        verification_key = {
            discord.VerificationLevel.none: "logs.common.verification.none",
            discord.VerificationLevel.low: "logs.common.verification.low",
            discord.VerificationLevel.medium: "logs.common.verification.medium",
            discord.VerificationLevel.high: "logs.common.verification.high",
            discord.VerificationLevel.highest: "logs.common.verification.highest",
        }.get(guild.verification_level, "logs.common.unknown")

        verification_label = self._translate(
            "logs.guild_remove.fields.verification",
            guild_id=guild_id,
            default="Verificação",
        )
        verification_value = self._translate(
            verification_key,
            guild_id=guild_id,
            default="Desconhecido",
        )

        boost_label = self._translate(
            "logs.guild_remove.fields.boost",
            guild_id=guild_id,
            default="Boost",
        )
        boost_count = getattr(guild, "premium_subscription_count", 0) or 0
        boost_key = "logs.common.boost.with_count" if boost_count else "logs.common.boost.basic"
        boost_value = self._translate(
            boost_key,
            guild_id=guild_id,
            default="Nível {tier}" if not boost_count else "Nível {tier} ({count} boosts)",
            tier=guild.premium_tier,
            count=boost_count,
        )

        embed.add_field(name=boost_label, value=boost_value, inline=True)
        embed.add_field(name=verification_label, value=verification_value, inline=True)

        total_label = self._translate(
            "logs.guild_remove.fields.total",
            guild_id=guild_id,
            default="Total de Servidores",
        )
        total_value = self._translate(
            "logs.guild_remove.summary",
            guild_id=guild_id,
            default="🌐 Agora estou em **{total}** servidores",
            total=len(self.bot.guilds),
        )
        embed.add_field(name=total_label, value=total_value, inline=False)

        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        if guild.banner:
            embed.set_image(url=guild.banner.url)

        await self._send_log(embed)
    
    async def log_music_start(
        self,
        track_name: str,
        track_url: Optional[str],
        artwork_url: Optional[str],
        channel_name: str,
        requester_name: str,
        guild_name: str,
        *,
        guild_id: Optional[int] = None,
        node=None,
    ) -> None:
        """Registra quando um usuário inicia uma nova música, respeitando o idioma do servidor."""

        title = self._translate(
            "logs.music_start.title",
            guild_id=guild_id,
            default="🎵 Nova música iniciada",
        )

        embed = discord.Embed(title=title, color=0x3498DB, timestamp=datetime.utcnow())

        music_label = self._translate(
            "logs.music_start.fields.music",
            guild_id=guild_id,
            default="Música",
        )

        if track_url:
            music_value = f"[{track_name}]({track_url})"
        else:
            music_value = track_name

        embed.add_field(name=music_label, value=music_value, inline=False)

        unknown_text = self._translate(
            "logs.common.unknown",
            guild_id=guild_id,
            default="Desconhecido",
        )

        server_label = self._translate(
            "logs.music_start.fields.server",
            guild_id=guild_id,
            default="Servidor",
        )
        if isinstance(guild_name, str) and guild_name.lower() == "unknown":
            server_value = unknown_text
        else:
            server_value = guild_name or unknown_text
            # Adiciona o ID do servidor entre parênteses
            if guild_id:
                server_value = f"{server_value} ({guild_id})"
        embed.add_field(name=server_label, value=server_value, inline=True)

        channel_label = self._translate(
            "logs.music_start.fields.channel",
            guild_id=guild_id,
            default="Canal de Voz",
        )

        if isinstance(channel_name, str) and channel_name.lower() == "unknown":
            channel_value = unknown_text
        else:
            channel_value = channel_name or unknown_text
        embed.add_field(name=channel_label, value=channel_value, inline=True)

        requester_label = self._translate(
            "logs.music_start.fields.requester",
            guild_id=guild_id,
            default="Solicitado por",
        )

        requester_value = requester_name or unknown_text
        if isinstance(requester_value, str) and requester_value.lower() == "unknown":
            requester_value = self._translate(
                "logs.music_start.unknown_requester",
                guild_id=guild_id,
                default="Desconhecido",
            )

        embed.add_field(name=requester_label, value=requester_value, inline=True)

        if artwork_url:
            embed.set_thumbnail(url=artwork_url)

        await self._send_log(embed, node=node)
    
    async def log_error(
        self,
        error_type: str,
        error_message: str,
        *,
        guild_name: Optional[str] = None,
        guild_id: Optional[int] = None,
        additional_info: Optional[str] = None,
        node=None,
    ) -> None:
        """Registra erros do bot respeitando o idioma do servidor."""

        title = self._translate(
            "logs.errors.title",
            guild_id=guild_id,
            default="❌ Erro: {type}",
            type=error_type,
        )

        embed = discord.Embed(title=title, color=0xFF0000, timestamp=datetime.utcnow())

        message_label = self._translate(
            "logs.errors.fields.message",
            guild_id=guild_id,
            default="Mensagem",
        )
        embed.add_field(
            name=message_label,
            value=f"```{error_message[:1000]}```",
            inline=False,
        )

        if guild_name:
            guild_label = self._translate(
                "logs.errors.fields.guild",
                guild_id=guild_id,
                default="Servidor",
            )
            embed.add_field(name=guild_label, value=guild_name, inline=True)

        if additional_info:
            details_label = self._translate(
                "logs.errors.fields.details",
                guild_id=guild_id,
                default="Detalhes",
            )
            embed.add_field(
                name=details_label,
                value=f"```{additional_info[:500]}```",
                inline=False,
            )

        await self._send_log(embed, node=node)

    async def log_lavalink_error(
        self,
        node_identifier: str,
        error_message: str,
        guild_name: Optional[str] = None,
        guild_id: Optional[int] = None,
        node=None,
    ) -> None:
        """Registra erros específicos do Lavalink."""

        additional_info = self._translate(
            "logs.lavalink.details",
            guild_id=guild_id,
            default="Nó: {node}",
            node=node_identifier,
        )

        await self.log_error(
            error_type="Lavalink",
            error_message=error_message,
            guild_name=guild_name,
            guild_id=guild_id,
            additional_info=additional_info,
            node=node,
        )
