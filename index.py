import discord
import re
from discord.ext import commands
import wavelink
import os
import asyncio
import logging
import sys
import json
import argparse
import aiohttp
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, parse_qs

from pymongo import MongoClient
from pymongo import errors as pymongo_errors

from threading import Thread
from dotenv import load_dotenv
from rich.live import Live
from rich.console import Console
from rich.panel import Panel

from commands.play import MusicControlView
from commands.logger import BotLogger

# Carrega variáveis de ambiente
load_dotenv()


# ============================================================================
# QueueCache - Sistema para salvar filas quando um node cai
# ============================================================================
QUEUE_CACHE_TTL_MS = 60 * 60 * 1000  # 1 hora em ms


class QueueCache:
    """Cache de filas para recuperação após queda de node."""

    def __init__(self):
        self._cache: dict[int, dict] = {}  # guild_id -> {tracks, savedAt, expiresAt}

    def save_queue(
        self,
        guild_id: int,
        current_track: wavelink.Playable | None,
        queue_tracks: list[wavelink.Playable],
    ) -> None:
        """Salva a fila atual de um servidor."""
        if not guild_id:
            return

        tracks = []

        # Adiciona track atual primeiro
        if current_track:
            tracks.append(self._serialize_track(current_track))

        # Adiciona tracks da fila
        for track in queue_tracks:
            tracks.append(self._serialize_track(track))

        if not tracks:
            return

        import time
        now = int(time.time() * 1000)
        self._cache[guild_id] = {
            "tracks": tracks,
            "savedAt": now,
            "expiresAt": now + QUEUE_CACHE_TTL_MS,
        }
        print(f"[QueueCache] Salvou {len(tracks)} track(s) para guild {guild_id}")

    def get_queue(self, guild_id: int) -> list[dict] | None:
        """Obtém a fila salva de um servidor."""
        if not guild_id:
            return None

        entry = self._cache.get(guild_id)
        if not entry:
            return None

        # Verifica expiração
        import time
        now = int(time.time() * 1000)
        if now > entry["expiresAt"]:
            del self._cache[guild_id]
            print(f"[QueueCache] Cache expirado para guild {guild_id}")
            return None

        return entry["tracks"]

    def clear_queue(self, guild_id: int) -> None:
        """Limpa o cache de um servidor."""
        if guild_id in self._cache:
            del self._cache[guild_id]
            print(f"[QueueCache] Limpou cache para guild {guild_id}")

    def has_cache(self, guild_id: int) -> bool:
        """Verifica se existe cache válido para um servidor."""
        if not guild_id:
            return False

        entry = self._cache.get(guild_id)
        if not entry:
            return False

        import time
        now = int(time.time() * 1000)
        if now > entry["expiresAt"]:
            del self._cache[guild_id]
            return False

        return True

    def get_cache_age(self, guild_id: int) -> int | None:
        """Retorna a idade do cache em ms."""
        entry = self._cache.get(guild_id)
        if not entry:
            return None
        import time
        return int(time.time() * 1000) - entry["savedAt"]

    def _serialize_track(self, track: wavelink.Playable) -> dict:
        """Serializa um track para armazenamento."""
        requester = getattr(track, "requester", None)
        return {
            "encoded": getattr(track, "encoded", None),
            "title": getattr(track, "title", None),
            "author": getattr(track, "author", None),
            "uri": getattr(track, "uri", None),
            "identifier": getattr(track, "identifier", None),
            "length": getattr(track, "length", None),
            "artworkUrl": getattr(track, "artwork", None) or getattr(track, "artworkUrl", None),
            "sourceName": getattr(track, "source", None),
            "requester": {
                "id": requester.id,
                "username": requester.name,
            } if requester else None,
        }

# Parse argumentos de linha de comando
parser = argparse.ArgumentParser(description='Music Bot com suporte a proxy')
parser.add_argument('--proxy', type=str, help='Proxy SOCKS5/HTTP (ex: socks5://127.0.0.1:40000)', default=None)
args = parser.parse_args()

# Logs
logging.basicConfig(level=logging.INFO)
# Silencia aviso sobre message_content ausente (slash-only não precisa)
logging.getLogger("discord.ext.commands.bot").setLevel(logging.ERROR)


class MusicBot(commands.Bot):
    def __init__(self, proxy: str | None = None):
        # Intents mínimos (SEM privilegiadas)
        intents = discord.Intents.none()  # começa com tudo False
        intents.guilds = True             # necessário para slash
        intents.voice_states = True       # necessário para tocar entrar/sair de voz

        # Configurar proxy (discord.py cria o connector automaticamente)
        if proxy:
            print(f"🌐 Usando proxy: {proxy}")
        
        super().__init__(
            command_prefix="!", 
            intents=intents, 
            help_command=None,
            proxy=proxy
        )
        self.synced = False
        # Guarda configs do Lavalink para possíveis reconexões
        self._lavalink_cfgs = []
        self._watchdog_task = None
        self._panel_task = None
        self.show_logs = False
        self.console = Console()
        self._live = None
        self._key_listener_started = False
        self._presence_applied = False
        self.default_language = "en"
        self.supported_languages: set[str] = set()
        self.locales: dict[str, dict[str, Any]] = {}
        self.locale_dir = Path(os.getenv("LOCALES_DIR", Path(__file__).resolve().parent / "locales"))
        self.mongo_client: MongoClient | None = None
        self.mongo_db = None
        self.language_collection = None
        self.presence_collection = None
        self.logs_collection = None
        self.warp_collection = None
        self._mongo_connected = False
        self._alone_tasks: dict[int, asyncio.Task] = {}
        self.owner_ids: set[int] = self._load_owner_ids()
        self.logger = None
        self.enable_warp_reconnect: bool = True
        # Afinidade de node por sessão (por guild): usada para manter o mesmo node após failover
        # enquanto o bot permanecer conectado na call. Não é persistido.
        self._session_node_affinity: dict[int, str] = {}
        # Lista negra temporária: nodes que falharam recentemente e não devem ser reconectados pelo watchdog por um tempo
        self._node_blacklist: dict[str, float] = {}  # node_id -> timestamp quando expira
        # Rastreamento de uptime dos nodes (timestamps de quando conectaram)
        self._node_connected_at: dict[str, float] = {}  # node_id -> timestamp quando conectou
        # Rastreamento de downtime dos nodes (timestamps de quando desconectaram)
        self._node_disconnected_at: dict[str, float] = {}  # node_id -> timestamp quando desconectou
        # Cache de filas para recuperação após queda de node
        self.queue_cache = QueueCache()
        # Cache de notificações pendentes de node down (para não notificar se reconectar rápido)
        self._pending_node_notifications: dict[str, asyncio.Task] = {}
        # TTL para notificações de node down (não notifica a mesma guild duas vezes em 2 min)
        self._node_notify_cache: dict[str, float] = {}  # "guild_id:node_id" -> timestamp
        # Set de nodes que estão em processo de reconexão (evita múltiplas tasks simultâneas)
        self._reconnecting_nodes: set[str] = set()
        # Debounce para on_wavelink_node_ready (evita spam de mensagens)
        self._node_ready_debounce: dict[str, float] = {}  # node_id -> last timestamp

        if not self.owner_ids:
            print("Aviso: BOT_OWNER_IDS não definidos. Comandos de administrador do bot ficarão indisponíveis.")

        self._init_mongo()
        self.enable_warp_reconnect = self._load_warp_setting()
        self._load_locales()
        self._init_logger()

    def _get_node_display_name(self, node: wavelink.Node | None) -> str | None:
        identifier = getattr(node, "identifier", None)
        if not identifier:
            return None

        identifier_str = str(identifier)

        for cfg in getattr(self, "_lavalink_cfgs", []) or []:
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

    def _set_session_node_affinity(self, guild_id: int | None, node_identifier: str | None) -> None:
        if not guild_id or not node_identifier:
            return
        self._session_node_affinity[int(guild_id)] = str(node_identifier)

    def _clear_session_node_affinity(self, guild_id: int | None) -> None:
        if not guild_id:
            return
        try:
            self._session_node_affinity.pop(int(guild_id), None)
        except Exception:
            pass

    def _load_owner_ids(self) -> set[int]:
        raw = os.getenv("BOT_OWNER_IDS", "")
        owner_ids: set[int] = set()

        if raw:
            separators = raw.replace(";", ",").split(",")
            for chunk in separators:
                value = chunk.strip()
                if not value:
                    continue
                try:
                    owner_ids.add(int(value))
                except ValueError:
                    print(f"Aviso: BOT_OWNER_IDS contém valor inválido '{value}'. Ignorando...")

        return owner_ids

    def _init_mongo(self) -> None:
        uri = os.getenv("MONGODB_URI", "").strip()
        if not uri:
            print("MONGODB_URI não definido. Os recursos de idioma permanecerão no padrão em inglês.")
            return

        try:
            self.mongo_client = MongoClient(uri, serverSelectionTimeoutMS=5000)
            # O ping força autenticação imediata e valida a senha
            self.mongo_client.admin.command("ping")
            db_name = os.getenv("MONGODB_DATABASE") or self._mongo_db_name_from_uri(uri, "kenny")
            self.mongo_db = self.mongo_client[db_name]
            self.language_collection = self.mongo_db["guild_languages"]
            self.language_collection.create_index("guild_id", unique=True)
            self.presence_collection = self.mongo_db["bot_presence"]
            self.logs_collection = self.mongo_db["logs_settings"]
            self.warp_collection = self.mongo_db["warp_settings"]
            self._mongo_connected = True
            print("MongoDB conectado com sucesso. Preferências de idioma e presença ativadas!")
        except pymongo_errors.OperationFailure as exc:
            print(f"Falha de autenticação no MongoDB (senha incorreta?): {exc}")
        except pymongo_errors.ServerSelectionTimeoutError as exc:
            print(f"Não foi possível se conectar ao MongoDB: {exc}")
        except Exception as exc:
            print(f"Erro inesperado ao inicializar MongoDB: {exc}")

    def _init_logger(self) -> None:
        """Inicializa o sistema de logs do bot"""
        from commands.logger import BotLogger
        self.logger = BotLogger(self)

    @staticmethod
    def _mongo_db_name_from_uri(uri: str, default: str) -> str:
        try:
            parsed = urlparse(uri)
        except Exception:
            return default

        path = (parsed.path or "").strip("/")
        if path:
            return path

        query_params = parse_qs(parsed.query)
        auth_source = query_params.get("authSource")
        if auth_source and auth_source[0]:
            return auth_source[0]

        return default

    def _load_locales(self) -> None:
        self.locales.clear()
        self.supported_languages = {self.default_language}

        try:
            if not self.locale_dir.exists():
                print(f"Diretório de locales não encontrado em {self.locale_dir}. Usando apenas mensagens padrão em inglês.")
                return

            for locale_file in self.locale_dir.glob("*.json"):
                try:
                    with locale_file.open("r", encoding="utf-8") as fp:
                        data = json.load(fp)
                        if isinstance(data, dict):
                            locale_code = locale_file.stem.lower()
                            self.locales[locale_code] = data
                            self.supported_languages.add(locale_code)
                except Exception as exc:
                    print(f"Erro ao carregar locale '{locale_file.name}': {exc}")

            if self.default_language not in self.locales:
                self.locales[self.default_language] = {}
        except Exception as exc:
            print(f"Falha ao carregar arquivos de locale: {exc}")
            self.locales = {self.default_language: {}}

    def _resolve_locale_value(self, locale: str, key: str) -> Any:
        data = self.locales.get(locale)
        if not data:
            return None

        current: Any = data
        for part in key.split('.'):
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                return None
        return current

    def translate(
        self,
        key: str,
        *,
        guild_id: int | None = None,
        locale: str | None = None,
        default: str | None = None,
        **kwargs,
    ) -> str:
        target_locale = locale
        if not target_locale and guild_id is not None:
            target_locale = self.get_guild_language(guild_id)
        if not target_locale:
            target_locale = self.default_language

        text = self._resolve_locale_value(target_locale, key)
        if text is None and target_locale != self.default_language:
            text = self._resolve_locale_value(self.default_language, key)

        if text is None:
            if default is not None:
                text = default
            else:
                return key

        if isinstance(text, str):
            if kwargs:
                try:
                    return text.format(**kwargs)
                except Exception as exc:
                    print(f"Erro ao formatar tradução '{key}' ({target_locale}): {exc}")
                    return text
            return text

        return text

    def _get_loop_mode(self, player: wavelink.Player | None) -> wavelink.QueueMode:
        if not isinstance(player, wavelink.Player):
            return wavelink.QueueMode.normal

        try:
            queue_mode = getattr(player.queue, "mode", wavelink.QueueMode.normal)
        except Exception:
            queue_mode = wavelink.QueueMode.normal

        return getattr(player, "loop_mode_override", queue_mode)

    def _apply_loop_mode(self, player: wavelink.Player | None, mode: wavelink.QueueMode) -> None:
        if not isinstance(player, wavelink.Player):
            return

        player.loop_mode_override = mode
        try:
            player.queue.mode = mode
        except Exception:
            pass

    def _count_non_bot_listeners(self, channel: discord.abc.Connectable | None) -> int:
        if channel is None or not hasattr(channel, "members"):
            return 0

        count = 0
        for member in channel.members:
            if member.id == getattr(self.user, "id", None):
                continue
            if getattr(member, "bot", False):
                continue
            count += 1
        return count

    def _preferred_text_channel(
        self,
        player: wavelink.Player | None,
        guild: discord.Guild | None,
    ) -> discord.abc.Messageable | None:
        if player and hasattr(player, "text_channel"):
            text_channel = getattr(player, "text_channel", None)
            if text_channel is not None and guild is not None:
                me = guild.me
                if me and text_channel.permissions_for(me).send_messages:
                    return text_channel
            elif text_channel is not None:
                return text_channel

        if guild is None:
            return None

        me = guild.me
        if guild.system_channel and me and guild.system_channel.permissions_for(me).send_messages:
            return guild.system_channel

        for channel in getattr(guild, "text_channels", []):
            if me and channel.permissions_for(me).send_messages:
                return channel

        return None

    async def _activate_lonely_pause(self, guild: discord.Guild, player: wavelink.Player) -> None:
        channel = getattr(player, "channel", None)
        if channel is None:
            return

        if getattr(player, "afk_pause_active", False):
            return

        player.afk_pause_active = True

        if getattr(player, "playing", False) and not player.paused:
            try:
                await player.pause(True)
            except Exception as exc:
                print(f"Falha ao pausar player por ausência de ouvintes: {exc}")

        message_channel = self._preferred_text_channel(player, guild)
        if message_channel is not None:
            try:
                pause_title = self.translate(
                    "player.lonely.pause_title",
                    guild_id=guild.id,
                    default="Eita! Me deixaram só :(",
                )
                pause_description = self.translate(
                    "player.lonely.pause",
                    guild_id=guild.id,
                    call=channel.mention,
                    default="Irei pausar a fila por 2 minutos até alguém retornar. Caso contrário, irei me desconectar!",
                )
                embed = discord.Embed(
                    title=f"<:catchill:1451442818408124557> {pause_title}",
                    description=pause_description,
                    color=0xffa500,  # Orange
                )
                pause_msg = await message_channel.send(embed=embed)
                player.lonely_pause_message = pause_msg
                if hasattr(player, "text_channel"):
                    player.text_channel = message_channel
            except Exception as exc:
                print(f"Falha ao enviar aviso de pausa por ausência: {exc}")

        existing_task = self._alone_tasks.get(guild.id)
        if existing_task:
            if existing_task.done():
                self._alone_tasks.pop(guild.id, None)
            else:
                return

        task = asyncio.create_task(self._lonely_disconnect_countdown(guild.id, channel.id))
        self._alone_tasks[guild.id] = task

    async def _cancel_lonely_pause(self, guild: discord.Guild, player: wavelink.Player) -> None:
        task = self._alone_tasks.pop(guild.id, None)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                print(f"Falha ao aguardar cancelamento de tarefa AFK: {exc}")

        if not getattr(player, "afk_pause_active", False):
            return

        player.afk_pause_active = False

        # Delete the pause message if it exists
        pause_msg = getattr(player, "lonely_pause_message", None)
        if pause_msg:
            try:
                await pause_msg.delete()
            except Exception:
                pass
            player.lonely_pause_message = None

        if getattr(player, "paused", False) and getattr(player, "current", None):
            try:
                await player.pause(False)
            except Exception as exc:
                print(f"Falha ao retomar player após retorno de ouvintes: {exc}")

        message_channel = self._preferred_text_channel(player, guild)
        channel = getattr(player, "channel", None)
        if message_channel is not None and channel is not None:
            try:
                resume_title = self.translate(
                    "player.lonely.resume_title",
                    guild_id=guild.id,
                    default="Yay! Não estou mais sozinho :D",
                )
                resume_description = self.translate(
                    "player.lonely.resume",
                    guild_id=guild.id,
                    call=channel.mention,
                    default=f"Alguém entrou em {channel.mention}! Retomando a fila.",
                )
                embed = discord.Embed(
                    title=f"<:7156remwink:1451443034838405330> {resume_title}",
                    description=resume_description,
                    color=0x87ceeb,  # Light blue
                )
                resume_msg = await message_channel.send(embed=embed)
                # Auto-delete after 10 seconds
                asyncio.create_task(self._delete_message_after(resume_msg, 10))
                if hasattr(player, "text_channel"):
                    player.text_channel = message_channel
            except Exception as exc:
                print(f"Falha ao enviar aviso de retomada: {exc}")

    async def _delete_message_after(self, message: discord.Message, delay: float) -> None:
        """Delete a message after a specified delay in seconds."""
        try:
            await asyncio.sleep(delay)
            await message.delete()
        except Exception:
            pass

    async def _lonely_disconnect_countdown(self, guild_id: int, channel_id: int) -> None:
        player: wavelink.Player | None = None
        try:
            try:
                await asyncio.sleep(120)
            except asyncio.CancelledError:
                return

            guild = self.get_guild(guild_id)
            if guild is None:
                return

            voice_client = guild.voice_client
            if not isinstance(voice_client, wavelink.Player):
                return

            player = voice_client
            channel = getattr(player, "channel", None)
            if channel is None or channel.id != channel_id:
                return

            if self._count_non_bot_listeners(channel) > 0:
                return

            message_channel = self._preferred_text_channel(player, guild)
            if message_channel is not None:
                try:
                    disconnect_title = self.translate(
                        "player.lonely.disconnect_title",
                        guild_id=guild_id,
                        default="Bem, estou indo embora, ninguém voltou mesmo...",
                    )
                    disconnect_description = self.translate(
                        "player.lonely.disconnect",
                        guild_id=guild_id,
                        call=channel.mention,
                        default=f"Ninguém voltou para {channel.mention}. Irei me desconectar agora.",
                    )
                    embed = discord.Embed(
                        title=f"👋 {disconnect_title}",
                        description=disconnect_description,
                        color=0xff0000,  # Red
                    )
                    await message_channel.send(embed=embed)
                    if hasattr(player, "text_channel"):
                        player.text_channel = message_channel
                except Exception as exc:
                    print(f"Falha ao enviar aviso de desconexão por ausência: {exc}")

            await self._clear_now_playing_message(player)

            try:
                await player.stop()
            except Exception:
                pass

            try:
                player.queue.clear()
            except Exception:
                pass

            try:
                await player.disconnect()
            except Exception as exc:
                print(f"Falha ao desconectar após ausência prolongada: {exc}")
        finally:
            try:
                self._clear_session_node_affinity(guild_id)
            except Exception:
                pass
            
            # Limpa letras ativas quando desconecta
            try:
                lyrics_cog = self.get_cog("LyricsCommands")
                if lyrics_cog:
                    lyrics_cog.cleanup_guild_lyrics(guild_id)
            except Exception:
                pass
            if player is not None:
                player.afk_pause_active = False

            current_task = asyncio.current_task()
            stored_task = self._alone_tasks.get(guild_id)
            if stored_task is current_task:
                self._alone_tasks.pop(guild_id, None)

    async def _evaluate_voice_channel(self, guild: discord.Guild | None) -> None:
        if guild is None:
            return

        player = guild.voice_client
        if not isinstance(player, wavelink.Player):
            task = self._alone_tasks.pop(guild.id, None)
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            return

        channel = getattr(player, "channel", None)
        if channel is None:
            await self._cancel_lonely_pause(guild, player)
            return

        listener_count = self._count_non_bot_listeners(channel)

        if listener_count == 0:
            await self._activate_lonely_pause(guild, player)
        else:
            await self._cancel_lonely_pause(guild, player)

    def get_guild_language(self, guild_id: int) -> str:
        if guild_id is None:
            return self.default_language

        if self.language_collection is None:
            return self.default_language

        try:
            document = self.language_collection.find_one({"guild_id": guild_id}, {"_id": 0, "language": 1})
            if document and document.get("language") in self.supported_languages:
                return document["language"]
        except Exception as exc:
            print(f"Erro ao obter idioma para o servidor {guild_id}: {exc}")

        return self.default_language

    def set_guild_language(self, guild_id: int, language: str) -> bool:
        if language not in self.supported_languages:
            print(f"Idioma '{language}' não suportado. Idiomas disponíveis: {sorted(self.supported_languages)}")
            return False

        if self.language_collection is None:
            return False

        try:
            self.language_collection.update_one(
                {"guild_id": guild_id},
                {"$set": {"language": language}},
                upsert=True,
            )
            return True
        except Exception as exc:
            print(f"Erro ao salvar idioma para o servidor {guild_id}: {exc}")
            return False

    async def setup_hook(self):
        # Conecta ao Lavalink (usa helper para permitir reconectar depois)
        await self.connect_lavalink()

        # Inicia watchdog que mantém a conexão viva e tenta reconectar se cair
        if not self._watchdog_task:
            self._watchdog_task = asyncio.create_task(self._lavalink_watchdog())

        # Inicia painel em tempo real
        if not self._panel_task:
            self._panel_task = asyncio.create_task(self._start_panel())

        if not self._presence_applied:
            print("Agendando restauração da presença salva...")
            asyncio.create_task(self._apply_presence_when_ready())

        # Inicia atalho de teclado para alternar logs/painel
        if not self._key_listener_started:
            Thread(target=self._keyboard_listener, daemon=True).start()
            self._key_listener_started = True
            print("Pressione 'l' para alternar entre painel e logs em tempo real.")

        # Carrega cogs
        extensions = [
            "commands.play",
            "commands.queue", 
            "commands.clearqueue",
            "commands.search",
            "commands.filter",
            "commands.help",
            "commands.admin",
            "commands.ping",
            "commands.language",
            "commands.lyrics",
            "commands.resumequeue",
        ]
        
        for ext in extensions:
            try:
                await self.load_extension(ext)
                print(f"✅ {ext} carregado")
            except Exception as e:
                print(f"❌ Erro ao carregar {ext}: {e}")
        
        print("Carregamento de extensões finalizado!")
        print(f"Cogs carregados: {list(self.cogs.keys())}")

        # Log dos intents ativos (debug)
        print(f"Intents: guilds={self.intents.guilds}, voice_states={self.intents.voice_states}, "
              f"members={self.intents.members}, presences={self.intents.presences}, "
              f"message_content={self.intents.message_content}")

    async def close(self):
        if self.mongo_client:
            try:
                self.mongo_client.close()
                print("Conexão com MongoDB encerrada.")
            except Exception as exc:
                print(f"Erro ao encerrar MongoDB: {exc}")
            finally:
                self.mongo_client = None
        await super().close()

    async def on_ready(self):
        if not self.synced:
            try:
                synced = await self.tree.sync()
                self.synced = True
                print(f"Sincronizados {len(synced)} comandos")
            except Exception as e:
                print(f"Erro ao sincronizar comandos: {e}")

        if not self._presence_applied:
            print("Presença ainda não aplicada; aguardando tarefa de restauração.")

        print(f"{self.user} está online!")
        print(f"ID do Bot: {self.user.id}")

    async def on_voice_state_update(self, member, before, after):
        if member.id != self.user.id:
            await self._evaluate_voice_channel(member.guild)
            return

        await self._evaluate_voice_channel(member.guild)

    @commands.Cog.listener()
    async def on_wavelink_websocket_closed(self, payload: wavelink.WebsocketClosedEventPayload):
        """Detecta quando a conexão WebSocket com um node é perdida"""
        try:
            player = getattr(payload, "player", None)
            if player is None:
                print(f"⚠️ WebSocket fechado (player=None, código: {payload.code})")
                return
            
            guild = getattr(player, "guild", None)
            # Durante failover de node não queremos nenhum cleanup agressivo que derrube a call.
            try:
                if guild is not None and getattr(guild, "_node_failover_inflight", False):
                    return
            except Exception:
                pass
            guild_id = guild.id if guild else "unknown"
            node = getattr(player, "node", None)
            node_id = getattr(node, "identifier", "unknown") if node else "unknown"
            
            print(f"⚠️ WebSocket fechado para player na guild {guild_id} (node: {node_id})")
            print(f"   Código: {payload.code}, Razão: {payload.reason}, By remote: {payload.by_remote}")
            
            # Se o node caiu (não foi fechamento normal), tenta destruir o player
            if payload.code in [1006, 4014, 4015]:  # Códigos de erro de conexão
                print(f"🔴 Node {node_id} parece ter caído. Limpando player...")
                try:
                    await player.disconnect(force=True)
                    print(f"✅ Player da guild {guild_id} desconectado com sucesso")
                    
                    # Limpa letras ativas quando node cai
                    lyrics_cog = self.get_cog("LyricsCommands")
                    if lyrics_cog and guild:
                        lyrics_cog.cleanup_guild_lyrics(guild.id)
                except Exception as exc:
                    print(f"⚠️ Erro ao desconectar player: {exc}")
        except Exception as e:
            print(f"⚠️ Erro no handler de WebSocket fechado: {e}")

    async def on_wavelink_node_ready(self, payload: wavelink.NodeReadyEventPayload):
        node = payload.node
        identifier = node.identifier
        
        # Debounce: ignora se já recebemos evento deste node nos últimos 5 segundos
        import time
        current_time = time.time()
        last_ready = self._node_ready_debounce.get(identifier, 0)
        if current_time - last_ready < 5.0:
            return  # Ignora eventos duplicados
        
        self._node_ready_debounce[identifier] = current_time
        
        print(f"Nó Lavalink '{identifier}' está pronto!")
        
        # Remove do set de "reconectando" se estava lá
        self._reconnecting_nodes.discard(identifier)
        
        # Registra timestamp de conexão para tracking de uptime
        self._node_connected_at[identifier] = current_time
        # Remove timestamp de desconexão se existir
        self._node_disconnected_at.pop(identifier, None)
        
        # Quando um nó reconecta, limpa sessões antigas dos players
        # Isso força o rebuild na próxima interação, evitando o bug de "entrar e sair da call"
        try:
            new_session_id = getattr(node, "session_id", None)
            if new_session_id:
                # Atualiza a session_id de todos os players deste nó
                for player in list(node.players.values()):
                    old_session = getattr(player, "_session_id", None)
                    
                    if old_session and old_session != new_session_id:
                        print(f"🔄 Player guild {player.guild.id} com sessão antiga ({old_session}). Nova sessão: {new_session_id}")
                        # Remove a session antiga para forçar rebuild
                        player._session_id = None
                    elif not old_session:
                        # Player novo ou sem rastreamento - atribui a sessão atual
                        player._session_id = new_session_id
        except Exception as exc:
            print(f"Aviso: erro ao atualizar session_id dos players após reconnect do nó: {exc}")

    async def on_guild_join(self, guild: discord.Guild):
        """Evento chamado quando o bot entra em um servidor"""
        print(f"📥 Bot entrou no servidor: {guild.name} (ID: {guild.id})")
        
        # Envia log se o logger estiver configurado
        if self.logger:
            try:
                await self.logger.log_guild_join(guild)
            except Exception as exc:
                print(f"Erro ao enviar log de entrada em servidor: {exc}")

    async def on_guild_remove(self, guild: discord.Guild):
        """Evento chamado quando o bot sai de um servidor"""
        print(f"📤 Bot saiu do servidor: {guild.name} (ID: {guild.id})")
        
        # Envia log se o logger estiver configurado
        if self.logger:
            try:
                await self.logger.log_guild_remove(guild)
            except Exception as exc:
                print(f"Erro ao enviar log de saída de servidor: {exc}")

    async def on_error(self, event_method: str, *args, **kwargs):
        """Manipulador de erros gerais do bot"""
        import traceback
        
        error_msg = traceback.format_exc()
        print(f"Erro no evento '{event_method}':\n{error_msg}")
        
        # Envia log do erro
        if self.logger:
            try:
                await self.logger.log_error(
                    error_type="Bot Event",
                    error_message=f"Evento: {event_method}\n{error_msg}",
                    additional_info=f"Args: {args}, Kwargs: {kwargs}"
                )
            except Exception as exc:
                print(f"Erro ao enviar log de erro geral: {exc}")

    async def connect_lavalink(self):
        """Estabelece conexão com o(s) nós Lavalink usando variáveis de ambiente."""
        configs: list[dict[str, str | bool | list]] = []
        self._lavalink_cfgs = []
        self._node_sources: dict[str, list[str]] = {}  # Mapeia node_id -> sources

        for idx in range(1, 11):  # Suporta até 10 nodes (NODE1 até NODE10)
            host = (os.getenv(f"LAVALINK_NODE{idx}_HOST", "") or "").strip()
            if not host:
                continue

            name = (os.getenv(f"LAVALINK_NODE{idx}_NAME", "") or "").strip()

            port = (os.getenv(f"LAVALINK_NODE{idx}_PORT", "2333") or "2333").strip() or "2333"
            password = os.getenv(f"LAVALINK_NODE{idx}_PASSWORD", "youshallnotpass")
            secure = (os.getenv(f"LAVALINK_NODE{idx}_SECURE", "false") or "false").lower() == "true"
            sources_str = (os.getenv(f"LAVALINK_NODE{idx}_SOURCES", "youtube") or "youtube").strip()
            sources = [s.strip().lower() for s in sources_str.split(",") if s.strip()]
            protocol = "wss" if secure else "ws"
            
            node_id = f"node{idx}"
            self._node_sources[node_id] = sources
            
            configs.append({
                "id": node_id,
                "name": name,
                "protocol": protocol,
                "host": host,
                "port": port,
                "password": password,
                "secure": secure,
                "sources": sources,
            })

        # Compatibilidade com configuração antiga (apenas um nó)
        if not configs:
            host = (os.getenv("LAVALINK_HOST", "") or "").strip()
            if host:
                name = (os.getenv("LAVALINK_NODE1_NAME", "") or os.getenv("LAVALINK_NAME", "") or "").strip()
                port = (os.getenv("LAVALINK_PORT", "2333") or "2333").strip() or "2333"
                password = os.getenv("LAVALINK_PASSWORD", "youshallnotpass")
                secure = (os.getenv("LAVALINK_SECURE", "false") or "false").lower() == "true"
                protocol = "wss" if secure else "ws"
                self._node_sources["node1"] = ["youtube"]  # Default para config antiga
                configs.append({
                    "id": "node1",
                    "name": name,
                    "protocol": protocol,
                    "host": host,
                    "port": port,
                    "password": password,
                    "secure": secure,
                    "sources": ["youtube"],
                })

        self._lavalink_cfgs = configs

        if not self._lavalink_cfgs:
            print("Nenhum nó Lavalink configurado!")
            return

        nodes_to_connect: list[wavelink.Node] = []

        for cfg in self._lavalink_cfgs:
            identifier = cfg["id"]
            uri = f"{cfg['protocol']}://{cfg['host']}:{cfg['port']}"

            try:
                existing = wavelink.Pool.get_node(identifier)
            except wavelink.InvalidNodeException:
                existing = None

            if existing and existing.status == wavelink.NodeStatus.CONNECTED:
                continue

            if existing:
                status_name = getattr(existing.status, "name", str(existing.status))
                print(f"Reiniciando conexão com o nó {identifier} (status atual: {status_name}).")
                try:
                    await existing.close(eject=True)
                except Exception as exc:
                    print(f"Erro ao fechar nó {identifier} antes de reconectar: {exc}")

            nodes_to_connect.append(wavelink.Node(uri=uri, password=cfg["password"], identifier=identifier))

        if nodes_to_connect:
            try:
                await wavelink.Pool.connect(client=self, nodes=nodes_to_connect)
                # Log das sources configuradas para cada node
                print(f"📊 Configuração de sources por node:")
                for node_id, sources in self._node_sources.items():
                    print(f"   {node_id}: {', '.join(sources)}")
            except Exception as e:
                print(f"Erro ao conectar aos nós Lavalink: {e}")
                print("Certifique-se de que os servidores Lavalink estão rodando!")
            else:
                for cfg in self._lavalink_cfgs:
                    identifier = cfg["id"]
                    uri = f"{cfg['protocol']}://{cfg['host']}:{cfg['port']}"
                    try:
                        node = wavelink.Pool.get_node(identifier)
                        status_name = getattr(node.status, "name", str(node.status))
                    except wavelink.InvalidNodeException:
                        status_name = "DESCONHECIDO"
                    print(f"Nó {identifier}: {uri} • status={status_name}")

    async def mark_node_as_failed(self, node_identifier: str) -> None:
        """Marca um nó como falho e o remove do pool (não tenta reconectar por 2 minutos)."""
        # Adiciona à lista negra temporária (120 segundos = 2 minutos)
        import time
        blacklist_duration = 120.0
        self._node_blacklist[node_identifier] = asyncio.get_event_loop().time() + blacklist_duration
        print(f"🚫 Node {node_identifier} na lista negra por {int(blacklist_duration)}s (watchdog não tentará reconectar)")
        
        # Registra timestamp de desconexão para tracking de downtime
        self._node_disconnected_at[node_identifier] = time.time()
        # Remove timestamp de conexão se existir
        self._node_connected_at.pop(node_identifier, None)
        
        # Salva filas de players afetados e agenda notificação
        await self._save_queue_and_notify_node_down(node_identifier)
        
        # Destrói todos os players do node imediatamente para evitar ghost state
        try:
            node = wavelink.Pool.get_node(node_identifier)
            if hasattr(node, 'players') and node.players:
                players_to_destroy = list(node.players.values())
                print(f"💀 Destruindo {len(players_to_destroy)} player(s) do node {node_identifier}...")
                for player in players_to_destroy:
                    try:
                        guild_name = getattr(player.guild, "name", "Unknown") if player.guild else "Unknown"
                        await player.disconnect()
                        print(f"   ✓ Player destruído (guild: {guild_name})")
                    except Exception as e:
                        print(f"   ⚠️ Erro ao destruir player: {e}")
        except wavelink.InvalidNodeException:
            pass  # Node já foi removido
        except Exception as exc:
            print(f"⚠️ Erro ao destruir players do nó {node_identifier}: {exc}")
        
        # Fecha o node
        try:
            node = wavelink.Pool.get_node(node_identifier)
            print(f"🔌 Desconectando nó {node_identifier}...")
            await node.close(eject=True)
            print(f"✅ Nó {node_identifier} removido do pool.")
        except wavelink.InvalidNodeException:
            pass  # Node já foi removido
        except Exception as exc:
            print(f"⚠️ Erro ao fechar nó {node_identifier}: {exc}")

    async def reconnect_specific_node(self, node_identifier: str) -> bool:
        """Reconecta um nó específico sem afetar os outros (usado pelo watchdog)."""
        # Evita múltiplas tentativas de reconexão simultâneas para o mesmo node
        if node_identifier in self._reconnecting_nodes:
            return False  # Já está reconectando
        
        # Verifica se o node está na lista negra
        current_time = asyncio.get_event_loop().time()
        blacklist_expiry = self._node_blacklist.get(node_identifier, 0)
        
        if current_time < blacklist_expiry:
            # Node ainda está na lista negra, não tenta reconectar
            return False
        
        # Marca como "reconectando" para evitar duplicatas
        self._reconnecting_nodes.add(node_identifier)
        
        try:
            # Remove da lista negra se expirou
            self._node_blacklist.pop(node_identifier, None)
            
            # Fecha apenas o nó específico se ainda existir
            try:
                node = wavelink.Pool.get_node(node_identifier)
                await node.close(eject=True)
            except wavelink.InvalidNodeException:
                pass  # Node já foi removido
            except Exception:
                pass  # Silencioso durante watchdog
            
            await asyncio.sleep(0.3)
            
            # Encontra a config do nó
            cfg = None
            for c in self._lavalink_cfgs:
                if c["id"] == node_identifier:
                    cfg = c
                    break
            
            if not cfg:
                return False
            
            # Reconecta apenas este nó (suprime logging temporariamente)
            uri = f"{cfg['protocol']}://{cfg['host']}:{cfg['port']}"
            new_node = wavelink.Node(uri=uri, password=cfg["password"], identifier=node_identifier)
            
            # Suprime temporariamente o logging do Wavelink
            wavelink_logger = logging.getLogger("wavelink")
            original_level = wavelink_logger.level
            wavelink_logger.setLevel(logging.CRITICAL)
            
            try:
                await wavelink.Pool.connect(client=self, nodes=[new_node])
            except Exception:
                return False
            finally:
                wavelink_logger.setLevel(original_level)
            
            # Aguarda o nó ficar pronto (máximo 2 segundos para não bloquear)
            max_wait = 2.0
            waited = 0.0
            poll_interval = 0.2
            
            while waited < max_wait:
                try:
                    node = wavelink.Pool.get_node(node_identifier)
                    if node.status == wavelink.NodeStatus.CONNECTED:
                        # Nota: a mensagem de "reconectado" será exibida via on_wavelink_node_ready
                        return True
                except wavelink.InvalidNodeException:
                    pass
                
                await asyncio.sleep(poll_interval)
                waited += poll_interval
            
            print(f"⚠️ Nó {node_identifier} não conectou após {max_wait}s.")
            return False
        finally:
            # Remove do set ao terminar (sucesso ou falha)
            self._reconnecting_nodes.discard(node_identifier)

    def is_node_blacklisted(self, node_identifier: str) -> bool:
        """Verifica se um node está na blacklist."""
        current_time = asyncio.get_event_loop().time()
        blacklist_expiry = self._node_blacklist.get(node_identifier, 0)
        return current_time < blacklist_expiry

    def get_least_used_node(self) -> wavelink.Node | None:
        """Retorna o node com menos players ativos (e que não está na blacklist)."""
        candidates: list[tuple[wavelink.Node, int]] = []

        for node in wavelink.Pool.nodes.values():
            identifier = getattr(node, "identifier", None)
            if not identifier:
                continue

            # Ignora nodes na blacklist
            if self.is_node_blacklisted(identifier):
                continue

            # Ignora nodes não conectados
            if node.status != wavelink.NodeStatus.CONNECTED:
                continue

            player_count = len(node.players) if hasattr(node, "players") else 0
            candidates.append((node, player_count))

        if not candidates:
            return None

        # Ordena por número de players (menor primeiro)
        candidates.sort(key=lambda x: x[1])
        return candidates[0][0]

    def has_healthy_node(self) -> bool:
        """Verifica se existe pelo menos um node saudável (conectado e não na blacklist)."""
        for node in wavelink.Pool.nodes.values():
            identifier = getattr(node, "identifier", None)
            if not identifier:
                continue

            if self.is_node_blacklisted(identifier):
                continue

            if node.status == wavelink.NodeStatus.CONNECTED:
                return True

        return False

    def detect_source_from_track(self, track: wavelink.Playable) -> str | None:
        """Detecta a source de uma track baseado no URI ou source_name."""
        # Primeiro tenta pelo source_name se disponível
        source_name = getattr(track, "source", None)
        if source_name:
            source_lower = str(source_name).lower()
            if "youtube" in source_lower:
                return "youtube"
            if "spotify" in source_lower:
                return "spotify"
            if "deezer" in source_lower:
                return "deezer"
            if "soundcloud" in source_lower:
                return "soundcloud"
            if "apple" in source_lower:
                return "applemusic"
            if "tidal" in source_lower:
                return "tidal"
            if "twitch" in source_lower:
                return "twitch"
        
        # Fallback para URI
        uri = getattr(track, "uri", None)
        if uri:
            return self.detect_source_from_query(uri)
        
        return None

    def detect_source_from_query(self, query: str) -> str | None:
        """Detecta a source de uma query baseado no URL ou prefixo."""
        query_lower = query.lower().strip()
        
        # URLs do YouTube
        if any(x in query_lower for x in ["youtube.com", "youtu.be", "music.youtube.com"]):
            return "youtube"
        
        # URLs do Spotify
        if "spotify.com" in query_lower or "open.spotify.com" in query_lower:
            return "spotify"
        
        # URLs do Deezer
        if "deezer.com" in query_lower or "deezer.page.link" in query_lower:
            return "deezer"
        
        # URLs do SoundCloud
        if "soundcloud.com" in query_lower:
            return "soundcloud"
        
        # URLs do Apple Music
        if "music.apple.com" in query_lower:
            return "applemusic"
        
        # URLs do Tidal
        if "tidal.com" in query_lower:
            return "tidal"
        
        # URLs do Bandcamp
        if "bandcamp.com" in query_lower:
            return "bandcamp"
        
        # URLs do Twitch
        if "twitch.tv" in query_lower:
            return "twitch"
        
        # URLs do Vimeo
        if "vimeo.com" in query_lower:
            return "vimeo"
        
        # Prefixos de busca do Lavalink
        if query_lower.startswith("ytsearch:") or query_lower.startswith("ytmsearch:"):
            return "youtube"
        if query_lower.startswith("spsearch:"):
            return "spotify"
        if query_lower.startswith("dzsearch:"):
            return "deezer"
        if query_lower.startswith("scsearch:"):
            return "soundcloud"
        if query_lower.startswith("amsearch:"):
            return "applemusic"
        
        # Se não detectou nenhuma source específica, retorna None (usa qualquer node)
        return None

    def get_node_for_source(self, source: str | None) -> wavelink.Node | None:
        """Retorna o melhor node para uma source específica."""
        if source is None:
            # Se não tem source específica, usa o menos ocupado
            return self.get_least_used_node()
        
        candidates: list[tuple[wavelink.Node, int]] = []

        for node in wavelink.Pool.nodes.values():
            identifier = getattr(node, "identifier", None)
            if not identifier:
                continue

            # Ignora nodes na blacklist
            if self.is_node_blacklisted(identifier):
                continue

            # Ignora nodes não conectados
            if node.status != wavelink.NodeStatus.CONNECTED:
                continue

            # Verifica se o node suporta a source
            node_sources = self._node_sources.get(identifier, ["youtube"])
            if source not in node_sources:
                continue

            player_count = len(node.players) if hasattr(node, "players") else 0
            candidates.append((node, player_count))

        if not candidates:
            # Se nenhum node suporta a source, usa qualquer node saudável
            return self.get_least_used_node()

        # Ordena por número de players (menor primeiro)
        candidates.sort(key=lambda x: x[1])
        return candidates[0][0]

    def get_nodes_for_source(self, source: str | None) -> list[wavelink.Node]:
        """Retorna lista de nodes que suportam uma source, ordenados por uso."""
        nodes: list[tuple[wavelink.Node, int]] = []

        for node in wavelink.Pool.nodes.values():
            identifier = getattr(node, "identifier", None)
            if not identifier:
                continue

            # Ignora nodes na blacklist
            if self.is_node_blacklisted(identifier):
                continue

            # Ignora nodes não conectados
            if node.status != wavelink.NodeStatus.CONNECTED:
                continue

            # Se source é None ou node suporta a source
            if source is None:
                node_sources = self._node_sources.get(identifier, ["youtube"])
                player_count = len(node.players) if hasattr(node, "players") else 0
                nodes.append((node, player_count))
            else:
                node_sources = self._node_sources.get(identifier, ["youtube"])
                if source in node_sources:
                    player_count = len(node.players) if hasattr(node, "players") else 0
                    nodes.append((node, player_count))

        # Ordena por número de players (menor primeiro)
        nodes.sort(key=lambda x: x[1])
        return [n[0] for n in nodes]

    def node_supports_track(self, node: wavelink.Node, track: wavelink.Playable) -> bool:
        """Verifica se um node suporta a source de uma track."""
        track_source = self.detect_source_from_track(track)
        if track_source is None:
            return True  # Se não detectou source, assume que qualquer node suporta
        
        node_id = getattr(node, "identifier", None)
        if not node_id:
            return True
        
        node_sources = self._node_sources.get(node_id, ["youtube"])
        return track_source in node_sources

    async def ensure_player_can_play_track(
        self,
        player: wavelink.Player,
        track: wavelink.Playable,
    ) -> wavelink.Player:
        """
        Verifica se o player atual pode tocar a track.
        Se o node não suporta a source da track, faz rebuild do player em um node compatível.
        Retorna o player (pode ser um novo player após rebuild).
        """
        node = getattr(player, "node", None)
        if not node:
            return player
        
        if self.node_supports_track(node, track):
            return player  # Node atual suporta, não precisa fazer nada
        
        track_source = self.detect_source_from_track(track)
        node_id = getattr(node, "identifier", None)
        print(f"⚠️ Node {node_id} não suporta {track_source}. Buscando node compatível...")
        
        # Encontra um node que suporte a source
        compatible_node = self.get_node_for_source(track_source)
        if not compatible_node:
            print(f"❌ Nenhum node disponível suporta {track_source}!")
            return player  # Retorna o player atual e deixa falhar naturalmente
        
        print(f"🔄 Fazendo rebuild do player para node {compatible_node.identifier}...")
        
        guild = getattr(player, "guild", None)
        if not guild:
            return player
        
        channel = player.channel
        if not channel:
            return player
        
        # Salva estado atual
        queue_backup = list(player.queue)
        loop_mode = getattr(player, "_loop_mode", wavelink.QueueMode.normal)
        text_channel = getattr(player, "text_channel", None)
        
        # Desconecta player atual
        try:
            await player.disconnect()
        except Exception as e:
            print(f"Erro ao desconectar player durante rebuild por source: {e}")
        
        # Aguarda um momento
        await asyncio.sleep(0.5)
        
        # Reconecta com o node compatível
        try:
            def _player_factory(client, ch):
                return wavelink.Player(client, ch, nodes=[compatible_node])
            
            new_player = await channel.connect(cls=_player_factory, self_deaf=True, reconnect=True, timeout=6.0)
            
            # Restaura estado
            new_player.text_channel = text_channel
            new_player._loop_mode = loop_mode
            
            # Restaura fila
            for queued_track in queue_backup:
                await new_player.queue.put_wait(queued_track)
            
            print(f"✅ Rebuild concluído. Player agora no node {compatible_node.identifier}")
            return new_player
            
        except Exception as e:
            print(f"❌ Erro durante rebuild por source: {e}")
            return player

    async def _save_queue_and_notify_node_down(self, node_identifier: str) -> None:
        """Salva filas de players afetados e agenda notificação de node down."""
        import time

        try:
            node = wavelink.Pool.get_node(node_identifier)
            players = list(node.players.values()) if hasattr(node, "players") else []
        except wavelink.InvalidNodeException:
            players = []

        if not players:
            # Tenta encontrar players pelo voice_client
            players = [
                vc for vc in self.voice_clients
                if isinstance(vc, wavelink.Player)
                and getattr(getattr(vc, "node", None), "identifier", None) == node_identifier
            ]

        if not players:
            print(f"[NodeDown] Nenhum player afetado pelo node {node_identifier}")
            return

        affected_guilds: list[tuple[int, discord.TextChannel | None]] = []

        for player in players:
            guild = getattr(player, "guild", None)
            if not guild:
                continue

            guild_id = guild.id
            current_track = getattr(player, "current", None)
            queue_tracks = list(player.queue) if hasattr(player, "queue") else []

            # Salva a fila no cache
            if current_track or queue_tracks:
                self.queue_cache.save_queue(guild_id, current_track, queue_tracks)

            # Obtém text_channel para notificação
            text_channel = getattr(player, "text_channel", None)
            if text_channel is None and current_track:
                requester = getattr(current_track, "requester", None)
                if requester and hasattr(requester, "channel"):
                    text_channel = requester.channel

            affected_guilds.append((guild_id, text_channel))

        # Agenda notificação com delay (15s) - pode ser cancelada se reconectar rápido
        await self._schedule_node_down_notification(node_identifier, affected_guilds)

    async def _schedule_node_down_notification(
        self,
        node_identifier: str,
        affected_guilds: list[tuple[int, discord.TextChannel | None]],
    ) -> None:
        """Agenda notificação de node down após grace period."""
        QUICK_RECONNECT_GRACE_MS = 15_000  # 15 segundos
        NODE_NOTIFY_TTL_MS = 120_000  # 2 minutos

        # Cancela notificação anterior se existir
        pending = self._pending_node_notifications.get(node_identifier)
        if pending and not pending.done():
            pending.cancel()
            print(f"[NodeDown] Cancelou notificação pendente para {node_identifier}")

        async def delayed_notify():
            await asyncio.sleep(QUICK_RECONNECT_GRACE_MS / 1000)

            # Verifica se o node reconectou
            try:
                node = wavelink.Pool.get_node(node_identifier)
                if node.status == wavelink.NodeStatus.CONNECTED:
                    print(f"[NodeDown] Node {node_identifier} reconectou - cancelando notificação")
                    return
            except wavelink.InvalidNodeException:
                pass  # Node ainda offline

            # Envia notificações
            import time
            now = time.time() * 1000

            for guild_id, text_channel in affected_guilds:
                if not text_channel:
                    continue

                # Verifica TTL para não spammar
                cache_key = f"{guild_id}:{node_identifier}"
                last_notified = self._node_notify_cache.get(cache_key, 0)
                if now - last_notified < NODE_NOTIFY_TTL_MS:
                    continue

                try:
                    await self._send_node_down_embed(guild_id, text_channel, node_identifier)
                    self._node_notify_cache[cache_key] = now
                except Exception as e:
                    print(f"[NodeDown] Erro ao notificar guild {guild_id}: {e}")

        task = asyncio.create_task(delayed_notify())
        self._pending_node_notifications[node_identifier] = task

    async def _send_node_down_embed(
        self,
        guild_id: int,
        channel: discord.TextChannel,
        node_identifier: str,
    ) -> None:
        """Envia embed de node down com botão de recuperar fila."""
        title = self.translate(
            "player.node_down.title",
            guild_id=guild_id,
            default="Connection Lost",
        )
        description = self.translate(
            "player.node_down.description",
            guild_id=guild_id,
            default="The server that was playing your music went offline. To reduce costs and keep the bot alive, we use third-party servers that may have brief instability (and we don't control them, unfortunately).\n\nBut since I'm a nice bot, I saved your queue :3",
        )

        embed = discord.Embed(
            title=f"<:crymeru:1453534083983474867> {title}",
            description=description,
            color=0xFF6B6B,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_footer(text=f"Node: {node_identifier}")

        # Adiciona botão de recuperar fila se houver cache
        components = []
        if self.queue_cache.has_cache(guild_id):
            button_label = self.translate(
                "player.node_down.recover_button",
                guild_id=guild_id,
                default="Recover Queue",
            )
            view = discord.ui.View(timeout=3600)  # 1 hora
            button = discord.ui.Button(
                style=discord.ButtonStyle.primary,
                label=button_label,
                emoji="🔄",
                custom_id=f"resumequeue:{guild_id}",
            )

            async def button_callback(interaction: discord.Interaction):
                # Redireciona para o comando /resumequeue
                cog = self.get_cog("ResumeQueueCog")
                if cog:
                    # Chama o callback interno do comando (não o objeto Command)
                    await cog.resumequeue.callback(cog, interaction)
                else:
                    await interaction.response.send_message(
                        "Command not available. Please use `/resumequeue`.",
                        ephemeral=True,
                    )

            button.callback = button_callback
            view.add_item(button)
            await channel.send(embed=embed, view=view)
        else:
            await channel.send(embed=embed)

        print(f"[NodeDown] Notificou guild {guild_id} sobre queda do node {node_identifier}")

    async def force_reconnect_lavalink(self) -> bool:
        """Força uma reconexão completa com todos os nós Lavalink, fechando conexões antigas."""
        print("🔄 Forçando reconexão completa com todos os nós Lavalink...")
        
        # Fecha todos os nós existentes
        for node in list(wavelink.Pool.nodes.values()):
            identifier = getattr(node, "identifier", "unknown")
            try:
                print(f"🔌 Desconectando nó {identifier}...")
                await node.close(eject=True)
            except Exception as exc:
                print(f"Aviso: erro ao fechar nó {identifier}: {exc}")
        
        # Aguarda um pouco para garantir que as conexões foram fechadas
        await asyncio.sleep(0.5)
        
        # Reconecta todos os nós
        await self.connect_lavalink()
        
        # Aguarda os nós ficarem prontos (máximo 5 segundos)
        print("⏳ Aguardando nós ficarem prontos...")
        max_wait = 5.0
        waited = 0.0
        poll_interval = 0.2
        
        while waited < max_wait:
            connected_count = 0
            for cfg in self._lavalink_cfgs:
                identifier = cfg["id"]
                try:
                    node = wavelink.Pool.get_node(identifier)
                    if node.status == wavelink.NodeStatus.CONNECTED:
                        connected_count += 1
                except wavelink.InvalidNodeException:
                    pass
            
            if connected_count > 0:
                break
            
            await asyncio.sleep(poll_interval)
            waited += poll_interval
        
        # Verifica status final de cada nó
        connected_count = 0
        for cfg in self._lavalink_cfgs:
            identifier = cfg["id"]
            try:
                node = wavelink.Pool.get_node(identifier)
                if node.status == wavelink.NodeStatus.CONNECTED:
                    connected_count += 1
                    # Nota: on_wavelink_node_ready já exibe a mensagem de conexão
                else:
                    status_name = getattr(node.status, "name", str(node.status))
                    print(f"⚠️ Nó {identifier} ainda não conectou (status: {status_name})")
            except wavelink.InvalidNodeException:
                print(f"❌ Nó {identifier} não foi reconectado.")
        
        if connected_count > 0:
            print(f"✅ Reconexão concluída: {connected_count}/{len(self._lavalink_cfgs)} nós ativos.")
            return True
        else:
            print("❌ Nenhum nó foi reconectado após aguardar.")
            return False

    async def _health_check_node(self, node: wavelink.Node, timeout: float = 10.0) -> bool:
        """Faz um health check leve em um node para verificar se está realmente respondendo."""
        try:
            # Tenta buscar stats do node com timeout curto
            await asyncio.wait_for(node.fetch_stats(), timeout=timeout)
            return True
        except (asyncio.TimeoutError, wavelink.LavalinkException, Exception):
            return False

    async def ensure_lavalink_connected(self) -> bool:
        """Valida a conexão com os nós Lavalink e tenta reconectar se necessário."""
        # Primeiro verifica se já existe algum nó conectado
        connected_nodes: list[wavelink.Node] = []
        pending_identifiers: list[str] = []

        for cfg in self._lavalink_cfgs:
            identifier = cfg["id"]
            try:
                node = wavelink.Pool.get_node(identifier)
            except wavelink.InvalidNodeException:
                pending_identifiers.append(identifier)
                # Registra timestamp de desconexão se ainda não foi registrado
                if identifier not in self._node_disconnected_at:
                    import time
                    self._node_disconnected_at[identifier] = time.time()
                    self._node_connected_at.pop(identifier, None)
                continue

            if node.status == wavelink.NodeStatus.CONNECTED:
                connected_nodes.append(node)
            else:
                pending_identifiers.append(identifier)
                # Registra timestamp de desconexão se ainda não foi registrado
                if identifier not in self._node_disconnected_at:
                    import time
                    self._node_disconnected_at[identifier] = time.time()
                    self._node_connected_at.pop(identifier, None)

        # Se já existe pelo menos um nó conectado, tenta reconectar os pendentes em background
        if connected_nodes:
            if pending_identifiers:
                # Filtra nodes que não estão na blacklist
                current_time = asyncio.get_event_loop().time()
                nodes_to_reconnect = [
                    pid for pid in pending_identifiers
                    if current_time >= self._node_blacklist.get(pid, 0)
                ]
                
                if nodes_to_reconnect:
                    # Filtra nodes que já estão em processo de reconexão
                    nodes_to_reconnect = [
                        pid for pid in nodes_to_reconnect
                        if pid not in self._reconnecting_nodes
                    ]
                    
                    if nodes_to_reconnect:
                        print(f"🔄 Tentando reconectar nós pendentes em background: {', '.join(nodes_to_reconnect)}")
                        # Tenta reconectar cada node pendente sem bloquear
                        for pending_id in nodes_to_reconnect:
                            asyncio.create_task(self.reconnect_specific_node(pending_id))
            return True

        # Se não há nenhum nó conectado, tenta conectar
        print("⚠️ Nenhum nó Lavalink conectado. Tentando conectar...")
        await self.connect_lavalink()

        # Verifica novamente após a tentativa de conexão
        connected_nodes.clear()
        for cfg in self._lavalink_cfgs:
            identifier = cfg["id"]
            try:
                node = wavelink.Pool.get_node(identifier)
                if node.status == wavelink.NodeStatus.CONNECTED:
                    connected_nodes.append(node)
            except wavelink.InvalidNodeException:
                continue

        if not connected_nodes:
            print("❌ Nenhum nó Lavalink conectado no momento.")
            return False

        return True

    async def search_with_failover(self, query: str):
        """Realiza buscas no Lavalink com failover entre os nós configurados."""

        # Detecta a source da query para priorizar nodes adequados
        detected_source = self.detect_source_from_query(query)
        
        attempt_nodes: list[wavelink.Node] = []
        seen: set[str] = set()
        
        # Primeiro, tenta nodes que suportam a source detectada
        if detected_source:
            preferred_nodes = self.get_nodes_for_source(detected_source)
            for node in preferred_nodes:
                if node.identifier not in seen:
                    attempt_nodes.append(node)
                    seen.add(node.identifier)

        # Adiciona nodes da config que ainda não foram incluídos
        for cfg in self._lavalink_cfgs:
            identifier = cfg["id"]
            if identifier in seen:
                continue
                
            try:
                node = wavelink.Pool.get_node(identifier)
            except wavelink.InvalidNodeException:
                continue

            if node.status != wavelink.NodeStatus.CONNECTED:
                continue

            attempt_nodes.append(node)
            seen.add(identifier)

        # Adiciona qualquer outro node conectado que não foi incluído
        for node in wavelink.Pool.nodes.values():
            if node.identifier in seen or node.status != wavelink.NodeStatus.CONNECTED:
                continue
            attempt_nodes.append(node)
            seen.add(node.identifier)

        if not attempt_nodes:
            raise RuntimeError("Nenhum nó Lavalink disponível para busca.")

        errors: list[str] = []
        for node in attempt_nodes:
            try:
                return await wavelink.Playable.search(query, node=node)
            except Exception as exc:
                error_msg = f"{node.identifier}: {exc}"
                errors.append(error_msg)
                print(f"Erro ao buscar em {node.identifier}: {exc}. Tentando próximo nó...")

        raise RuntimeError("Falha ao buscar em todos os nós disponíveis. " + "; ".join(errors))

        raise RuntimeError("Falha ao buscar em todos os nós disponíveis. " + "; ".join(errors))

    async def _lavalink_watchdog(self):
        """Tarefa em background que mantém a conexão ativa e tenta reconectar quando necessário."""
        await self.wait_until_ready()
        last_health_check = 0
        while not self.is_closed():
            try:
                ok = await self.ensure_lavalink_connected()
                if not ok:
                    # Aguarda um pouco antes de tentar novamente para evitar loop agressivo
                    await asyncio.sleep(15)
                else:
                    # Health check periódico (a cada 30 segundos quando não há nodes pendentes)
                    import time
                    current_time = time.time()
                    # Ping a cada 30s usando /stats (universal, todos Lavalink suportam)
                    if current_time - last_health_check >= 30:
                        for cfg in self._lavalink_cfgs:
                            identifier = cfg["id"]
                            
                            # Pula nodes que estão na blacklist
                            blacklist_expiry = self._node_blacklist.get(identifier, 0)
                            if current_time < blacklist_expiry:
                                continue
                            
                            try:
                                node = wavelink.Pool.get_node(identifier)
                                if node.status == wavelink.NodeStatus.CONNECTED:
                                    # Ping com /stats (universal, funciona em todos Lavalink)
                                    try:
                                        await asyncio.wait_for(node.fetch_stats(), timeout=8.0)
                                    except (asyncio.TimeoutError, Exception):
                                        print(f"❌ Node {identifier} não respondeu ao ping - marcando como failed")
                                        await self.mark_node_as_failed(identifier)
                            except wavelink.InvalidNodeException:
                                pass  # Node não existe no pool
                            except Exception as exc:
                                print(f"⚠️ Erro ao verificar node {identifier}: {exc}")
                        
                        last_health_check = current_time
                    
                    # Verifica se há nodes pendentes para ajustar intervalo
                    pending_count = 0
                    for cfg in self._lavalink_cfgs:
                        try:
                            node = wavelink.Pool.get_node(cfg["id"])
                            if node.status != wavelink.NodeStatus.CONNECTED:
                                pending_count += 1
                        except wavelink.InvalidNodeException:
                            pending_count += 1
                    
                    # Se há nodes pendentes, checa mais frequentemente (15s), senão usa intervalo normal (30s para health check)
                    check_interval = 15 if pending_count > 0 else 30
                    await asyncio.sleep(check_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"Erro no watchdog do Lavalink: {e}")
                await asyncio.sleep(30)

    async def _start_panel(self):
        """Mostra o painel ao vivo no console."""
        await self.wait_until_ready()
        panel_paused = False
        with Live(console=self.console, refresh_per_second=1, transient=False) as live:
            self._live = live
            while not self.is_closed():
                try:
                    if self.show_logs:
                        if not panel_paused and live.is_started:
                            await asyncio.to_thread(live.stop)
                            panel_paused = True
                        await asyncio.sleep(0.5)
                        continue

                    if panel_paused:
                        await asyncio.to_thread(live.start)
                        panel_paused = False

                    content = self._generate_panel_content()
                    await asyncio.to_thread(
                        live.update,
                        Panel(content, title="Painel de Monitoramento", border_style="blue"),
                    )
                    await asyncio.sleep(1)
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logging.exception("Erro no painel do console", exc_info=e)
                    await asyncio.sleep(5)

    def _format_duration(self, seconds: float) -> str:
        """Formata duração em segundos para formato legível (ex: 2h 30m, 45s)"""
        if seconds < 60:
            return f"{int(seconds)}s"
        elif seconds < 3600:
            minutes = int(seconds / 60)
            secs = int(seconds % 60)
            return f"{minutes}m {secs}s"
        else:
            hours = int(seconds / 3600)
            minutes = int((seconds % 3600) / 60)
            return f"{hours}h {minutes}m"

    def _generate_panel_content(self) -> str:
        import time
        current_time = time.time()
        
        total_calls = len(self.voice_clients)
        total_playing = sum(1 for vc in self.voice_clients if getattr(vc, "playing", False))

        node_lines: list[str] = []

        def build_line(node_id: str, node: wavelink.Node | None):
            status_icon = "🔴"
            call_count = 0
            playing_count = 0
            time_info = ""

            if node and node.status == wavelink.NodeStatus.CONNECTED:
                status_icon = "🟢"
                players = getattr(node, "players", {}) or {}
                if isinstance(players, dict):
                    call_count = len(players)
                    playing_count = sum(1 for p in players.values() if getattr(p, "playing", False))
                elif isinstance(players, (list, tuple, set)):
                    call_count = len(players)
                    playing_count = sum(1 for p in players if getattr(p, "playing", False))
                
                # Uptime do node
                connected_at = self._node_connected_at.get(node_id)
                if connected_at:
                    uptime = current_time - connected_at
                    time_info = f" | uptime: {self._format_duration(uptime)}"
            else:
                # Node offline - mostrar downtime e tempo restante na blacklist
                disconnected_at = self._node_disconnected_at.get(node_id)
                blacklist_expiry = self._node_blacklist.get(node_id, 0)
                
                time_parts = []
                if disconnected_at:
                    downtime = current_time - disconnected_at
                    time_parts.append(f"offline: {self._format_duration(downtime)}")
                
                if blacklist_expiry > current_time:
                    remaining = blacklist_expiry - current_time
                    time_parts.append(f"blacklist: {self._format_duration(remaining)}")
                
                if time_parts:
                    time_info = f" | {' | '.join(time_parts)}"

            node_lines.append(f"{node_id}: {status_icon} calls={call_count} tocando={playing_count}{time_info}")

        if self._lavalink_cfgs:
            seen_ids: set[str] = set()
            for cfg in self._lavalink_cfgs:
                identifier = cfg["id"]
                try:
                    node = wavelink.Pool.get_node(identifier)
                except wavelink.InvalidNodeException:
                    node = None
                build_line(identifier, node)
                seen_ids.add(identifier)

            for identifier, node in wavelink.Pool.nodes.items():
                if identifier in seen_ids:
                    continue
                build_line(identifier, node)
        else:
            for identifier, node in wavelink.Pool.nodes.items():
                build_line(identifier, node)

        if not node_lines:
            node_lines.append("Nenhum nó conectado")

        nodes_status = "\n".join(node_lines)

        return (
            f"Calls totais: {total_calls}\n"
            f"Tocando (total): {total_playing}\n"
            f"Por nó:\n{nodes_status}"
        )

    def _keyboard_listener(self):
        """Escuta a tecla de atalho para alternar logs/painel."""
        try:
            import msvcrt  # Disponível no Windows
        except ImportError:
            msvcrt = None

        while True:
            try:
                if msvcrt:
                    key = msvcrt.getch()
                    if not key:
                        continue
                    if key == b"\x03":  # Ctrl+C
                        print("Encerrando bot (Ctrl+C pressionado)...")
                        asyncio.run_coroutine_threadsafe(self.close(), self.loop)
                        break
                    try:
                        char = key.decode("utf-8").lower()
                    except Exception:
                        continue
                else:
                    char = sys.stdin.read(1)
                    if not char:
                        continue
                    char = char.lower()

                if char == 'l':
                    self.show_logs = not self.show_logs
                    modo = "logs" if self.show_logs else "painel"
                    print(f"Modo {modo} ativado. Pressione 'l' para alternar novamente.")
            except Exception as e:
                print(f"Erro no listener de teclado: {e}")
                break

    async def on_wavelink_track_start(self, payload: wavelink.TrackStartEventPayload):
        player = payload.player
        if not player:
            return
        await self._apply_track_start_effects(player, payload.track)
        
        # Envia log de início de música
        if self.logger and payload.track:
            try:
                guild = player.guild
                channel = getattr(player, "channel", None)
                
                # Obtém informações da track
                track_name = getattr(payload.track, "title", "Unknown")
                track_url = getattr(payload.track, "uri", None)
                
                # Tenta obter a artwork de várias formas
                artwork_url = None
                if hasattr(payload.track, "artwork"):
                    artwork = getattr(payload.track, "artwork", None)
                    if artwork:
                        artwork_url = getattr(artwork, "url", None) or str(artwork) if artwork else None
                
                # Se não encontrou, tenta pegar do artworkUrl direto (algumas versões do wavelink)
                if not artwork_url and hasattr(payload.track, "artworkUrl"):
                    artwork_url = getattr(payload.track, "artworkUrl", None)
                
                # Fallback: tenta pegar thumbnail do YouTube se for URL do YouTube
                if not artwork_url and track_url and "youtube.com" in track_url or "youtu.be" in track_url:
                    # Extrai o ID do vídeo do YouTube
                    import re
                    youtube_patterns = [
                        r'(?:youtube\.com\/watch\?v=|youtu\.be\/)([^&\n?#]+)',
                        r'youtube\.com\/embed\/([^&\n?#]+)',
                    ]
                    for pattern in youtube_patterns:
                        match = re.search(pattern, track_url)
                        if match:
                            video_id = match.group(1)
                            artwork_url = f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg"
                            break
                
                # Obtém informações do canal e usuário
                channel_name = channel.name if channel else "Unknown"
                
                # Tenta obter o requester de várias formas
                requester = getattr(payload.track, "requester", None)
                requester_name = "Unknown"
                
                # Tenta obter de um dicionário personalizado no player
                if not requester and hasattr(player, "_track_requesters"):
                    track_id = getattr(payload.track, "identifier", None) or getattr(payload.track, "encoded", None)
                    if track_id and track_id in player._track_requesters:
                        requester = player._track_requesters.get(track_id)
                
                if requester:
                    # Se for um objeto discord.Member ou discord.User
                    if hasattr(requester, "display_name"):
                        requester_name = f"{requester.display_name} (@{requester.name})"
                    elif hasattr(requester, "name"):
                        requester_name = f"@{requester.name}"
                    elif hasattr(requester, "id"):
                        requester_name = f"<@{requester.id}>"
                    else:
                        requester_name = str(requester)
                
                guild_name = guild.name if guild else "Unknown"
                guild_id = guild.id if guild else None
                
                await self.logger.log_music_start(
                    track_name=track_name,
                    track_url=track_url,
                    artwork_url=artwork_url,
                    channel_name=channel_name,
                    requester_name=requester_name,
                    guild_name=guild_name,
                    guild_id=guild_id,
                    node=getattr(player, "node", None),
                )
            except Exception as exc:
                print(f"Erro ao enviar log de início de música: {exc}")

    async def _apply_track_start_effects(self, player: wavelink.Player, track: wavelink.Playable | None) -> None:
        if not player:
            return
        if track is None:
            track = getattr(player, "current", None)
            if track is None:
                return
        try:
            channel = getattr(player, "channel", None)
            if channel and isinstance(channel, discord.VoiceChannel):
                if not hasattr(player, "_original_channel_status"):
                    player._original_channel_status = getattr(channel, "status", None)
                if not hasattr(player, "_channel_status_overridden"):
                    player._channel_status_overridden = False
                if not player._channel_status_overridden:
                    current_status = getattr(channel, "status", None)
                    player._original_channel_status = current_status
                track_title = getattr(track, "title", None)
                if track_title:
                    new_status = f"🎵 {track_title}".strip()
                    if len(new_status) > 100:
                        new_status = new_status[:97] + "..."
                    if getattr(channel, "status", None) != new_status:
                        await channel.edit(status=new_status)
                        player._channel_status_overridden = True
        except discord.Forbidden:
            pass
        except Exception as exc:
            print(f"Falha ao atualizar status do canal de voz: {exc}")
        if not hasattr(player, "_fallback_attempts"):
            player._fallback_attempts = set()
        player._fallback_in_progress = False
        # Reset de tentativas de failover por node para a faixa atual
        try:
            player._unavailable_failover_attempts = set()
        except Exception:
            pass
        loop_mode = self._get_loop_mode(player)
        self._apply_loop_mode(player, loop_mode)
        await self._cancel_progress_task(player)
        await self._send_now_playing_embed(player, track)
        self._start_progress_task(player, track)

    async def on_wavelink_track_end(self, payload: wavelink.TrackEndEventPayload):
        player = payload.player
        if not player:
            return
        await self._restore_voice_channel_status(player)

        reason = (getattr(payload, "reason", "Unknown") or "Unknown")
        reason_upper = reason.upper() if isinstance(reason, str) else "UNKNOWN"
        if reason_upper == "LOADFAILED":
            reason_upper = "LOAD_FAILED"

        # Evita que eventos disparados por disconnect/reconnect durante failover
        # executem o fluxo normal (que pode desconectar a call).
        try:
            guild = getattr(player, "guild", None)
            if guild is not None and getattr(guild, "_node_failover_inflight", False) and reason_upper != "LOAD_FAILED":
                return
        except Exception:
            pass

        # Ignora evento se estamos fazendo failover de "Something broke"
        if getattr(player, "_something_broke_failover_in_progress", False):
            print(f"⏳ Ignorando track_end durante failover de 'Something broke'")
            return

        print(f"Track finalizado. Razão: {reason_upper}. Guild: {getattr(player.guild, 'name', 'Desconhecido')}")

        if reason_upper == "LOAD_FAILED":
            exception_info = getattr(player, "_last_error", None)

            pending = getattr(player, "_warp_retry_future", None)
            if pending and not pending.done():
                try:
                    success = await pending
                except Exception as exc:
                    print(f"Warp retry future falhou: {exc}")
                    success = False
                player._last_error = None
                if success:
                    return

            if pending and pending.done():
                try:
                    success = pending.result()
                except Exception as exc:
                    print(f"Warp retry future result erro: {exc}")
                    success = False
                player._last_error = None
                if success:
                    return

            if getattr(player, "_warp_retry_pending", False) and not getattr(player, "_warp_retry_attempted", False):
                player._warp_retry_attempted = True
                retry_track = getattr(player, "_warp_retry_track", None) or payload.track
                scheduled = await self._schedule_warp_retry(player, retry_track)
                player._warp_retry_pending = False
                player._warp_retry_track = None
                player._last_error = None
                if scheduled:
                    return

            # Failover automático entre nodes quando o vídeo estiver indisponível.
            # Não notifica o usuário enquanto ainda houver alternativas.
            node_failover_started = await self._try_play_node_failover_for_unavailable(
                player,
                payload.track,
                exception_info,
            )
            player._last_error = None
            if node_failover_started:
                return

            # Se a tentativa de failover desconectou/reconectou o voice_client,
            # garante que o restante do fluxo use o player atual.
            try:
                refreshed = getattr(getattr(player, "guild", None), "voice_client", None)
                if isinstance(refreshed, wavelink.Player) and refreshed is not player:
                    player = refreshed
            except Exception:
                pass

            fallback_started = await self._try_play_fallback(player, payload.track, exception_info)
            player._last_error = None
            if fallback_started:
                return

            await self._notify_track_failure(player, payload.track, exception_info)

            if not player.queue.is_empty:
                try:
                    next_track = await player.queue.get_wait()
                    # Verifica se precisa trocar de node para tocar essa track
                    player = await self.ensure_player_can_play_track(player, next_track)
                    await player.play(next_track)
                except Exception as e:
                    print(f"Erro ao tentar tocar próxima faixa após falha de carregamento: {e}")
            else:
                await self._handle_queue_finished(
                    player,
                    reason_upper,
                    failed_track=payload.track,
                    suppress_finished_embed=True,
                )
            return

        if reason_upper == "REPLACED":
            return

        if reason_upper == "STOPPED":
            if not player.queue.is_empty:
                await self._clear_now_playing_message(player)
                try:
                    next_track = await player.queue.get_wait()
                    # Verifica se precisa trocar de node para tocar essa track
                    player = await self.ensure_player_can_play_track(player, next_track)
                    await player.play(next_track)
                except Exception as exc:
                    print(f"Erro ao iniciar próxima faixa após stop: {exc}")
                    await self._handle_queue_finished(player, reason_upper, failed_track=payload.track)
            else:
                await self._handle_queue_finished(player, reason_upper, failed_track=payload.track)
            return

        loop_mode = self._get_loop_mode(player)

        if reason_upper == "FINISHED" and payload.track:
            if loop_mode is wavelink.QueueMode.loop:
                try:
                    await player.play(payload.track)
                except Exception as exc:
                    print(f"Falha ao reiniciar faixa em loop: {exc}")
                else:
                    self._apply_loop_mode(player, loop_mode)
                return
            if loop_mode is wavelink.QueueMode.loop_all:
                try:
                    await player.queue.put_wait(payload.track)
                except Exception as exc:
                    print(f"Não foi possível refileirar faixa em loop_all: {exc}")
                self._apply_loop_mode(player, loop_mode)

        if not player.queue.is_empty:
            next_track = await player.queue.get_wait()
            # Verifica se precisa trocar de node para tocar essa track
            player = await self.ensure_player_can_play_track(player, next_track)
            await player.play(next_track)
            self._apply_loop_mode(player, loop_mode)
        else:
            await self._handle_queue_finished(player, reason_upper, failed_track=payload.track)

    async def on_wavelink_track_exception(self, payload: wavelink.TrackExceptionEventPayload):
        player = payload.player
        if not player:
            return

        if isinstance(payload.exception, dict):
            player._last_error = payload.exception
            track_title = getattr(payload.track, "title", "Unknown")
            severity = payload.exception.get("severity")
            message = payload.exception.get("message")
            cause = payload.exception.get("cause")
            print(
                "Falha ao carregar faixa '%s'. Severidade: %s. Motivo: %s. Causa: %s" % (
                    track_title,
                    severity or "?",
                    message or "?",
                    cause or "?",
                )
            )
            
            # Tenta failover em outro node se for "Something broke when playing the track."
            # SÓ faz failover se o WARP estiver DESATIVADO (para evitar conflitos)
            warp_enabled = getattr(self, "enable_warp_reconnect", False)
            if self._is_something_broke_error(payload.exception) and not warp_enabled:
                # Marca que estamos fazendo failover para evitar que track_end desconecte
                player._something_broke_failover_in_progress = True
                try:
                    failover_success = await self._try_play_node_failover_for_something_broke(
                        player, payload.track, payload.exception
                    )
                    if failover_success:
                        print(f"✅ Failover para outro node bem-sucedido para '{track_title}'")
                        player._last_error = None
                        return  # Não continua, já tratamos o erro
                finally:
                    player._something_broke_failover_in_progress = False
            
            if self._should_reconnect_warp(track_title, severity, message):
                player._warp_retry_pending = True
                player._warp_retry_track = payload.track
                player._warp_retry_attempted = False
                existing = getattr(player, "_warp_retry_future", None)
                if not existing or existing.done():
                    player._warp_retry_future = asyncio.create_task(
                        self._warp_reconnect_flow(player, payload.track)
                    )
            
            # Envia log do erro do Lavalink
            if self.logger:
                try:
                    guild = player.guild if hasattr(player, "guild") else None
                    guild_name = guild.name if guild else None
                    guild_id = guild.id if guild else None
                    node_id = player.node.identifier if hasattr(player, 'node') and player.node else "unknown"
                    error_msg = f"Track: {track_title}\nSeveridade: {severity or '?'}\nMotivo: {message or '?'}"
                    
                    await self.logger.log_lavalink_error(
                        node_identifier=node_id,
                        error_message=error_msg,
                        guild_name=guild_name,
                        guild_id=guild_id,
                    )
                except Exception as exc:
                    print(f"Erro ao enviar log de erro do Lavalink: {exc}")
        else:
            player._last_error = None

    def _is_video_unavailable_error(self, exception: dict | None) -> bool:
        if not isinstance(exception, dict):
            return False
        message = str(exception.get("message") or "").lower()
        cause = str(exception.get("cause") or "").lower()
        combined = f"{message} {cause}".strip()
        return "this video is unavailable" in combined

    def _is_something_broke_error(self, exception: dict | None) -> bool:
        """Detecta o erro 'Something broke when playing the track.' do Lavalink."""
        if not isinstance(exception, dict):
            return False
        severity = str(exception.get("severity") or "").lower()
        message = str(exception.get("message") or "").strip()
        return severity == "fault" and message == "Something broke when playing the track."

    def _connected_nodes_in_priority_order(self) -> list[wavelink.Node]:
        nodes: list[wavelink.Node] = []
        seen: set[str] = set()

        for cfg in getattr(self, "_lavalink_cfgs", []) or []:
            identifier = cfg.get("id")
            if not identifier or identifier in seen:
                continue
            try:
                node = wavelink.Pool.get_node(identifier)
            except wavelink.InvalidNodeException:
                continue
            if node.status != wavelink.NodeStatus.CONNECTED:
                continue
            nodes.append(node)
            seen.add(identifier)

        for node in wavelink.Pool.nodes.values():
            if node.identifier in seen or node.status != wavelink.NodeStatus.CONNECTED:
                continue
            nodes.append(node)
            seen.add(node.identifier)

        return nodes

    async def _try_play_node_failover_for_unavailable(
        self,
        player: wavelink.Player,
        track: wavelink.Playable | None,
        exception: dict | None,
    ) -> bool:
        if not player or track is None:
            return False

        # Só tenta se realmente for o erro esperado.
        if not self._is_video_unavailable_error(exception):
            return False

        # Se o usuário usa apenas 1 node (ou só 1 conectado), mantém comportamento atual.
        candidates = self._connected_nodes_in_priority_order()
        if len(candidates) <= 1:
            return False

        current_node = getattr(player, "node", None)
        current_id = getattr(current_node, "identifier", None)
        original_node = current_node

        tried = getattr(player, "_unavailable_failover_attempts", None)
        if not isinstance(tried, set):
            tried = set()
        if current_id:
            tried.add(str(current_id))

        guild = getattr(player, "guild", None)
        voice_channel = getattr(player, "channel", None)
        if guild is None or voice_channel is None:
            return False

        # Marca que estamos em failover para evitar fluxos paralelos de track_end.
        try:
            setattr(guild, "_node_failover_inflight", True)
        except Exception:
            pass

        loop_mode = self._get_loop_mode(player)

        async def _migrate_player_to_node(target_node: wavelink.Node) -> bool:
            """Migra o MESMO player para outro node Lavalink sem reconectar voz no Discord."""
            if target_node is None:
                return False

            try:
                if target_node.status != wavelink.NodeStatus.CONNECTED:
                    return False
            except Exception:
                pass

            guild_id = getattr(guild, "id", None)
            if not guild_id:
                return False

            # Precisa de voice state completo para mandar o VOICE_UPDATE para o novo node.
            try:
                voice_data = getattr(player, "_voice_state", {}).get("voice", {})
            except Exception:
                voice_data = {}

            session_id = voice_data.get("session_id")
            token = voice_data.get("token")
            endpoint = voice_data.get("endpoint")
            if not session_id or not token or not endpoint:
                return False

            old_node = getattr(player, "node", None)
            if old_node is target_node:
                return True

            # "Mata" o player no node antigo (best-effort) pra não ficar player fantasma.
            try:
                if old_node is not None and getattr(old_node, "session_id", None):
                    await old_node._destroy_player(int(guild_id))
            except Exception:
                pass

            # Atualiza mapeamentos internos antes de mandar eventos pro novo node.
            try:
                if old_node is not None:
                    old_node._players.pop(int(guild_id), None)
            except Exception:
                pass

            try:
                player._node = target_node
            except Exception:
                return False

            try:
                target_node._players[int(guild_id)] = player
            except Exception:
                pass

            request = {"voice": {"sessionId": session_id, "token": token, "endpoint": endpoint}}
            try:
                await target_node._update_player(int(guild_id), data=request)
            except Exception:
                # Reverte se falhar, sem derrubar a call.
                try:
                    target_node._players.pop(int(guild_id), None)
                except Exception:
                    pass
                try:
                    if old_node is not None:
                        player._node = old_node
                        try:
                            old_node._players[int(guild_id)] = player
                        except Exception:
                            pass
                except Exception:
                    pass
                return False

            # Atualiza o rastreamento de sessão do player para evitar rebuild desnecessário
            # quando o usuário adiciona mais músicas logo após o failover.
            try:
                player._session_id = getattr(target_node, "session_id", None)
            except Exception:
                pass

            # Mantém afinidade com o node atual enquanto durar a sessão na call.
            try:
                self._set_session_node_affinity(int(guild_id), getattr(target_node, "identifier", None))
            except Exception:
                pass

            return True

        try:
            # Ordem: tenta todos os nodes conectados exceto o atual e os já tentados.
            for node in candidates:
                node_id = getattr(node, "identifier", None)
                if not node_id:
                    continue
                if current_id and node_id == current_id:
                    continue
                if str(node_id) in tried:
                    continue

                print(f"🔁 Video indisponível. Tentando failover do node '{current_id}' para '{node_id}'...")
                tried.add(str(node_id))

                try:
                    migrated = await _migrate_player_to_node(node)
                    if not migrated:
                        continue

                    await player.play(track)
                    self._apply_loop_mode(player, loop_mode)
                    try:
                        player._unavailable_failover_attempts = tried
                    except Exception:
                        pass
                    return True
                except Exception as exc:
                    print(f"Falha ao fazer failover para node '{node_id}': {exc}")
                    continue

            # Esgotou alternativas: volta para o node original (best-effort), sem derrubar a call.
            try:
                if original_node is not None:
                    await _migrate_player_to_node(original_node)
            except Exception:
                pass

            try:
                player._unavailable_failover_attempts = tried
            except Exception:
                pass

            return False
        finally:
            try:
                setattr(guild, "_node_failover_inflight", False)
            except Exception:
                pass

    async def _try_play_node_failover_for_something_broke(
        self,
        player: wavelink.Player,
        track: wavelink.Playable | None,
        exception: dict | None,
    ) -> bool:
        """
        Tenta fazer failover para outro node quando recebe 'Something broke when playing the track.'
        Migra o player para outro node SEM desconectar da call de voz.
        """
        if not player or track is None:
            return False

        # Só tenta se for o erro esperado
        if not self._is_something_broke_error(exception):
            return False

        # Se só tem 1 node, não tem pra onde ir
        candidates = self._connected_nodes_in_priority_order()
        if len(candidates) <= 1:
            print("⚠️ Apenas 1 node disponível, não é possível fazer failover")
            return False

        current_node = getattr(player, "node", None)
        current_id = getattr(current_node, "identifier", None)

        # Rastreia nodes já tentados para este erro
        tried = getattr(player, "_something_broke_failover_attempts", None)
        if not isinstance(tried, set):
            tried = set()
        if current_id:
            tried.add(str(current_id))

        guild = getattr(player, "guild", None)
        voice_channel = getattr(player, "channel", None)
        if guild is None or voice_channel is None:
            return False

        guild_id = getattr(guild, "id", None)
        if not guild_id:
            return False

        # Salva estado atual
        loop_mode = self._get_loop_mode(player)
        original_node = current_node

        # Marca que estamos em failover
        try:
            setattr(guild, "_node_failover_inflight", True)
        except Exception:
            pass

        print(f"🔄 Erro 'Something broke' no node '{current_id}'. Tentando failover...")

        async def _migrate_player_to_node(target_node: wavelink.Node) -> bool:
            """Migra o MESMO player para outro node Lavalink sem reconectar voz no Discord."""
            if target_node is None:
                return False

            try:
                if target_node.status != wavelink.NodeStatus.CONNECTED:
                    return False
            except Exception:
                pass

            if not guild_id:
                return False

            # Precisa de voice state completo para mandar o VOICE_UPDATE para o novo node.
            try:
                voice_data = getattr(player, "_voice_state", {}).get("voice", {})
            except Exception:
                voice_data = {}

            session_id = voice_data.get("session_id")
            token = voice_data.get("token")
            endpoint = voice_data.get("endpoint")
            if not session_id or not token or not endpoint:
                print(f"⚠️ Dados de voz incompletos para migração")
                return False

            old_node = getattr(player, "node", None)
            if old_node is target_node:
                return True

            # "Mata" o player no node antigo (best-effort) pra não ficar player fantasma.
            try:
                if old_node is not None and getattr(old_node, "session_id", None):
                    await old_node._destroy_player(int(guild_id))
            except Exception:
                pass

            # Atualiza mapeamentos internos antes de mandar eventos pro novo node.
            try:
                if old_node is not None:
                    old_node._players.pop(int(guild_id), None)
            except Exception:
                pass

            try:
                player._node = target_node
            except Exception:
                return False

            try:
                target_node._players[int(guild_id)] = player
            except Exception:
                pass

            request = {"voice": {"sessionId": session_id, "token": token, "endpoint": endpoint}}
            try:
                await target_node._update_player(int(guild_id), data=request)
            except Exception:
                # Reverte se falhar, sem derrubar a call.
                try:
                    target_node._players.pop(int(guild_id), None)
                except Exception:
                    pass
                try:
                    if old_node is not None:
                        player._node = old_node
                        try:
                            old_node._players[int(guild_id)] = player
                        except Exception:
                            pass
                except Exception:
                    pass
                return False

            # Atualiza o rastreamento de sessão do player
            try:
                player._session_id = getattr(target_node, "session_id", None)
            except Exception:
                pass

            return True

        try:
            for node in candidates:
                node_id = getattr(node, "identifier", None)
                if not node_id:
                    continue
                if current_id and node_id == current_id:
                    continue
                if str(node_id) in tried:
                    continue

                print(f"🔁 Tentando failover para node '{node_id}'...")
                tried.add(str(node_id))

                try:
                    migrated = await _migrate_player_to_node(node)
                    if not migrated:
                        print(f"⚠️ Migração para '{node_id}' falhou, tentando próximo...")
                        continue

                    # Toca a música no novo node
                    await player.play(track)
                    self._apply_loop_mode(player, loop_mode)
                    
                    # Limpa tentativas
                    try:
                        player._something_broke_failover_attempts = set()
                    except Exception:
                        pass

                    # Atualiza afinidade de sessão
                    try:
                        self._set_session_node_affinity(int(guild_id), node_id)
                    except Exception:
                        pass

                    print(f"✅ Failover para node '{node_id}' bem-sucedido!")
                    return True
                except Exception as exc:
                    print(f"Falha ao fazer failover para node '{node_id}': {exc}")
                    continue

            # Esgotou alternativas: tenta voltar para o node original
            try:
                if original_node is not None and original_node not in [n for n in candidates if str(getattr(n, "identifier", "")) in tried]:
                    await _migrate_player_to_node(original_node)
            except Exception:
                pass

            try:
                player._something_broke_failover_attempts = tried
            except Exception:
                pass

            print("❌ Todas as tentativas de failover falharam")
            return False
        finally:
            try:
                setattr(guild, "_node_failover_inflight", False)
            except Exception:
                pass

    def _should_reconnect_warp(self, track_title: str | None, severity: Any, message: Any) -> bool:
        """Confere se o erro atual deve disparar o script de reconexao do WARP."""
        if not getattr(self, "enable_warp_reconnect", True):
            return False
        if not sys.platform.startswith("linux"):
            return False

        if not severity or str(severity).strip().lower() != "fault":
            return False

        if not message or str(message).strip() != "Something broke when playing the track.":
            return False

        return True

    async def _warp_reconnect_flow(self, player: wavelink.Player, track: wavelink.Playable | None) -> bool:
        """Roda o script WARP, avisa o usuario e tenta re-tocar a mesma faixa apos 5s."""
        if track is None:
            track = getattr(player, "current", None)
        if track is None:
            return False

        player._warp_retry_pending = True
        player._warp_retry_track = track
        player._warp_retry_attempted = False

        guild = getattr(player, "guild", None)
        guild_id = getattr(guild, "id", None)

        channel = getattr(player, "text_channel", None)
        if channel is None:
            requester = getattr(track, "requester", None)
            if requester and hasattr(requester, "channel"):
                channel = requester.channel
        if channel is None:
            channel = self._preferred_text_channel(player, guild)

        switching_ip_title = self.translate(
            "player.warp.switching_ip",
            guild_id=guild_id,
            default="Trocando de IP...",
        )
        switching_ip_desc = self.translate(
            "player.warp.switching_ip_description",
            guild_id=guild_id,
            default="O YouTube acaba bloqueando meu acesso para tocar as músicas, então eu irei tentar tocar a sua música até dar certo!",
        )
        switching_ip_footer = self.translate(
            "player.warp.switching_ip_footer",
            guild_id=guild_id,
            default="Isso pode demorar alguns segundos, não se assuste!",
        )

        warp_msg = None
        if channel is not None:
            try:
                embed = discord.Embed(
                    title=f"<a:unadance:1450689460307230760> {switching_ip_title}",
                    description=switching_ip_desc,
                    color=0x5284FF,
                )
                embed.set_footer(text=switching_ip_footer)
                warp_msg = await channel.send(embed=embed)
            except Exception as exc:
                print(f"Falha ao enviar aviso de retry WARP: {exc}")

        try:
            player._warp_retry_inflight = True
            await self._run_warp_reconnect_script()
            connected = await self.ensure_lavalink_connected()
            if not connected:
                return False
            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                return False

            await player.play(track)
            loop_mode = self._get_loop_mode(player)
            self._apply_loop_mode(player, loop_mode)
            player._warp_retry_pending = False
            player._warp_retry_attempted = False
            player._warp_retry_track = None
            
            # Delete the warp message on success
            if warp_msg is not None:
                try:
                    await warp_msg.delete()
                except Exception:
                    pass
            
            return True
        except Exception as exc:
            print(f"Falha no fluxo de retry WARP: {exc}")
            return False
        finally:
            player._warp_retry_inflight = False
            try:
                player._warp_retry_future = None
            except Exception:
                pass

    async def _schedule_warp_retry(self, player: wavelink.Player, track: wavelink.Playable | None, delay_seconds: float = 5.0) -> bool:
        """Avisa o usuario e tenta reproduzir novamente apos a reconexao WARP."""
        if track is None:
            return False

        guild = getattr(player, "guild", None)
        guild_id = getattr(guild, "id", None)

        channel = getattr(player, "text_channel", None)
        if channel is None:
            requester = getattr(track, "requester", None)
            if requester and hasattr(requester, "channel"):
                channel = requester.channel
        if channel is None:
            channel = self._preferred_text_channel(player, guild)

        message = self.translate(
            "player.track_retry.pending",
            guild_id=guild_id,
            default="Estou fazendo alguns ajustezinhos para tocar sua musica... Tentando novamente em 5s.",
        )

        if channel is not None:
            try:
                await channel.send(message)
            except Exception as exc:
                print(f"Falha ao avisar sobre nova tentativa de reproducao: {exc}")

        reconnect_task = getattr(player, "_warp_reconnect_task", None)
        if reconnect_task and not reconnect_task.done():
            try:
                await reconnect_task
            except Exception as exc:
                print(f"Erro aguardando script de reconexao WARP: {exc}")

        try:
            await asyncio.sleep(delay_seconds)
        except asyncio.CancelledError:
            return False

        connected = await self.ensure_lavalink_connected()
        if not connected:
            print("Lavalink ainda desconectado apos tentativa de WARP.")
            return False

        try:
            await player.play(track)
            loop_mode = self._get_loop_mode(player)
            self._apply_loop_mode(player, loop_mode)
            player._warp_retry_pending = False
            player._warp_retry_attempted = False
            player._warp_retry_track = None
            return True
        except Exception as exc:
            print(f"Nao foi possivel re-tentar a faixa apos reconexao WARP: {exc}")
            return False

    async def _run_warp_reconnect_script(self) -> None:
        """Executa o script fornecido para reconectar o WARP e loga o resultado."""
        script = (
            'LOG="/var/log/warp-reconnect.log"\n'
            'if ! touch "$LOG" 2>/dev/null; then\n'
            '  LOG="/tmp/warp-reconnect.log"\n'
            '  touch "$LOG" 2>/dev/null\n'
            'fi\n'
            'echo "==== Execucao em $(date) ====" >> "$LOG"\n'
            'warp-cli --accept-tos disconnect >> "$LOG" 2>&1\n'
            'sleep 1\n'
            'warp-cli --accept-tos connect >> "$LOG" 2>&1\n'
            'echo "" >> "$LOG"\n'
        )

        try:
            proc = await asyncio.create_subprocess_exec(
                "/bin/bash",
                "-s",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate(script.encode())

            if proc.returncode != 0:
                print(
                    "Script de reconexao WARP retornou codigo %s. stdout: %s stderr: %s"
                    % (proc.returncode, stdout.decode().strip(), stderr.decode().strip())
                )
        except FileNotFoundError:
            print("bash not found; nao foi possivel executar o script de reconexao WARP.")
        except Exception as exc:
            print(f"Erro ao executar script de reconexao WARP: {exc}")

    async def _handle_queue_finished(
        self,
        player: wavelink.Player,
        reason: str | None,
        *,
        failed_track: wavelink.Playable | None = None,
        suppress_finished_embed: bool = False,
    ) -> None:
        # Evita desconectar em eventos de substituição, quando outra faixa já assumiu
        if reason and reason.upper() == "REPLACED":
            return

        # Se foi LOAD_FAILED, o caller (on_wavelink_track_end) já notificou o usuário
        # via _notify_track_failure, então apenas suprimimos o embed de "fila acabou"
        if reason and reason.upper() == "LOAD_FAILED":
            suppress_finished_embed = True

        if suppress_finished_embed:
            send_finished_embed = False
        else:
            send_finished_embed = True

        await self._clear_now_playing_message(player)

        # Envia embed avisando que a fila acabou
        if send_finished_embed:
            try:
                channel = getattr(player, "text_channel", None)
                if channel is None and getattr(player, "current", None):
                    requester = getattr(player.current, "requester", None)
                    if requester and hasattr(requester, "channel"):
                        channel = requester.channel

                guild_id = getattr(player.guild, "id", None)

                if channel:
                    title = self.translate("player.queue_finished.title", guild_id=guild_id)
                    embed = discord.Embed(
                        title=title,
                        color=0xcac3a5,
                    )
                    
                    # Cria view com botões de link
                    view = discord.ui.View(timeout=None)
                    
                    website_label = self.translate("player.queue_finished.website_button", guild_id=guild_id, default="Website")
                    support_label = self.translate("player.queue_finished.support_button", guild_id=guild_id, default="Support Server")
                    vote_label = self.translate("player.queue_finished.vote_button", guild_id=guild_id, default="Vote on top.gg")
                    
                    website_url = os.getenv("BOT_WEBSITE_URL", "").strip()
                    support_url = os.getenv("BOT_SUPPORT_URL", "").strip()
                    topgg_url = os.getenv("BOT_TOPGG_URL", "").strip()
                    
                    if website_url:
                        view.add_item(discord.ui.Button(
                            style=discord.ButtonStyle.secondary,
                            label=website_label,
                            url=website_url,
                        ))
                    if support_url:
                        view.add_item(discord.ui.Button(
                            style=discord.ButtonStyle.secondary,
                            label=support_label,
                            url=support_url,
                        ))
                    if topgg_url:
                        view.add_item(discord.ui.Button(
                            style=discord.ButtonStyle.secondary,
                            label=vote_label,
                            url=topgg_url,
                        ))
                    
                    await channel.send(embed=embed, view=view)
            except Exception as e:
                print(f"Erro ao enviar embed de fila finalizada: {e}")

        # Limpa referências para evitar updates de progresso pendentes
        if hasattr(player, "current_embed_message"):
            player.current_embed_message = None

        # Desconecta do canal de voz
        try:
            if getattr(player, "connected", False) or getattr(player, "channel", None):
                guild_id = getattr(player.guild, "id", None)
                try:
                    self._clear_session_node_affinity(guild_id)
                except Exception:
                    pass
                await player.disconnect()
                guild_name = getattr(player.guild, "name", "Desconhecido")
                print(f"Desconectado do canal de voz após finalizar fila no servidor: {guild_name}")
                
                # Limpa letras ativas quando fila acaba
                try:
                    lyrics_cog = self.get_cog("LyricsCommands")
                    if lyrics_cog and guild_id:
                        lyrics_cog.cleanup_guild_lyrics(guild_id)
                except Exception:
                    pass
        except Exception as e:
            print(f"Erro ao desconectar após finalizar fila: {e}")

        if hasattr(player, "_last_error"):
            player._last_error = None

    async def _notify_track_failure(
        self,
        player: wavelink.Player,
        track: wavelink.Playable | None,
        exception: dict | None,
    ) -> None:
        channel = getattr(player, "text_channel", None)

        if channel is None and track:
            requester = getattr(track, "requester", None)
            if requester and hasattr(requester, "channel"):
                channel = requester.channel

        if channel is None:
            return

        guild_id = getattr(player.guild, "id", None)
        track_title = getattr(track, "title", None) or self.translate(
            "player.track_failed.unknown_track",
            guild_id=guild_id,
            default="Unknown track",
        )

        raw_reason = None
        if isinstance(exception, dict):
            raw_reason = exception.get("message") or exception.get("cause")
            severity = exception.get("severity")
            if severity and raw_reason:
                raw_reason = f"{raw_reason} (severity: {severity})"

        # Detecta erro "No mirror found" - música não encontrada no YouTube
        is_no_mirror = False
        if raw_reason and "no mirror found" in raw_reason.lower():
            is_no_mirror = True

        if is_no_mirror:
            # Mensagem amigável para "No mirror found"
            title = self.translate(
                "player.track_failed.no_mirror_title",
                guild_id=guild_id,
                default="🎵 Can't play this track",
            )
            description = self.translate(
                "player.track_failed.no_mirror_description",
                guild_id=guild_id,
                track=track_title,
                default=f"I couldn't find **{track_title}** on YouTube Music (where I search for songs). The artist might not have uploaded it there.\n\nTry sending a direct YouTube link, that makes my job easier! :3",
            )
            footer_text = self.translate(
                "player.track_failed.no_mirror_footer",
                guild_id=guild_id,
                default="Don't worry, this isn't your fault!",
            )
            embed_color = 0x5865F2  # Discord blurple - mais amigável
        else:
            if not raw_reason:
                raw_reason = self.translate(
                    "player.track_failed.fallback_reason",
                    guild_id=guild_id,
                    default="Unknown reason (possibly restricted or unavailable).",
                )

            description = self.translate(
                "player.track_failed.description",
                guild_id=guild_id,
                track=track_title,
                reason=raw_reason,
                default=f"Couldn't play {track_title}. {raw_reason}",
            )

            title = self.translate(
                "player.track_failed.title",
                guild_id=guild_id,
                default="❌ Failed to play",
            )

            footer_text = self.translate(
                "player.track_failed.footer",
                guild_id=guild_id,
                default="I'll skip this track and continue.",
            )
            embed_color = 0xff0033

        embed = discord.Embed(title=title, description=description, color=embed_color)
        embed.set_footer(text=footer_text)

        try:
            await channel.send(embed=embed)
        except Exception as e:
            print(f"Erro ao enviar notificação de falha de faixa: {e}")

    def _should_try_fallback(
        self,
        exception: dict | None,
    ) -> bool:
        if not isinstance(exception, dict):
            return False

        message = str(exception.get("message") or "").lower()
        cause = str(exception.get("cause") or "").lower()
        combined = f"{message} {cause}".strip()
        if "requires login" in combined:
            return True
        if "sign in" in combined:
            return True
        if "video requires login" in combined:
            return True
        severity = str(exception.get("severity") or "").lower()
        if "login" in combined and severity in {"suspicious", "fault"}:
            return True
        return False

    def _track_fallback_key(self, track: wavelink.Playable | None) -> str | None:
        if track is None:
            return None

        identifier = getattr(track, "identifier", None)
        if isinstance(identifier, str) and identifier:
            return identifier

        title = getattr(track, "title", None)
        author = getattr(track, "author", None)
        if isinstance(title, str) and isinstance(author, str) and title and author:
            return f"{title.lower()}::{author.lower()}"
        if isinstance(title, str) and title:
            return title.lower()
        return None

    def _build_fallback_queries(self, track: wavelink.Playable | None) -> list[str]:
        if track is None:
            return []

        queries: list[str] = []
        title = getattr(track, "title", None)
        author = getattr(track, "author", None)

        if isinstance(title, str) and isinstance(author, str) and title and author:
            queries.append(f"ytmsearch:{title} {author}")
        if isinstance(title, str) and title:
            queries.append(f"ytsearch:{title}")

        uri = getattr(track, "uri", None) or getattr(track, "url", None)
        if isinstance(uri, str) and uri and uri not in queries:
            queries.append(uri)

        # Remove duplicatas preservando ordem
        seen: set[str] = set()
        unique_queries: list[str] = []
        for item in queries:
            key = item.strip()
            if not key or key in seen:
                continue
            seen.add(key)
            unique_queries.append(key)
        return unique_queries

    def _extract_first_playable(self, result: Any) -> wavelink.Playable | None:
        if result is None:
            return None

        playlist_cls = getattr(wavelink, "Playlist", None)
        playable_cls = getattr(wavelink, "Playable", None)

        items: list[Any]

        if playlist_cls and isinstance(result, playlist_cls):
            items = list(getattr(result, "tracks", []) or [])
        elif isinstance(result, list):
            items = result
        elif playable_cls and isinstance(result, playable_cls):
            return result
        else:
            try:
                items = list(result)
            except TypeError:
                items = []

        if not items:
            return None

        for candidate in items:
            if playable_cls and not isinstance(candidate, playable_cls):
                continue
            return candidate
        return None

    async def _send_fallback_notice(
        self,
        player: wavelink.Player,
        original_track: wavelink.Playable | None,
        fallback_track: wavelink.Playable,
    ) -> None:
        channel = getattr(player, "text_channel", None)
        if channel is None and original_track is not None:
            requester = getattr(original_track, "requester", None)
            if requester and hasattr(requester, "channel"):
                channel = requester.channel
        if channel is None:
            return

        guild_id = getattr(player.guild, "id", None)
        unknown_label = self.translate(
            "player.track_failed.unknown_track",
            guild_id=guild_id,
            default="Unknown track",
        )

        original_title = getattr(original_track, "title", None) if original_track else None
        fallback_title = getattr(fallback_track, "title", None)

        display_original = original_title or unknown_label
        display_fallback = fallback_title or unknown_label

        message = self.translate(
            "player.track_failed.fallback_playing",
            guild_id=guild_id,
            default="⚠️ Could not play **{original}** (login required). Playing an alternative: **{fallback}**",
            original=display_original,
            fallback=display_fallback,
        )

        try:
            await channel.send(message)
        except Exception as exc:
            print(f"Erro ao enviar mensagem de fallback: {exc}")

    async def _try_play_fallback(
        self,
        player: wavelink.Player,
        track: wavelink.Playable | None,
        exception: dict | None,
    ) -> bool:
        if not player or track is None:
            return False

        if getattr(player, "_fallback_in_progress", False):
            return False

        if not self._should_try_fallback(exception):
            return False

        fallback_key = self._track_fallback_key(track)
        attempts = getattr(player, "_fallback_attempts", None)
        if not isinstance(attempts, set):
            attempts = set()

        if fallback_key and fallback_key in attempts:
            return False

        if fallback_key:
            attempts.add(fallback_key)
        player._fallback_attempts = attempts

        queries = self._build_fallback_queries(track)
        if not queries:
            return False

        fallback_success = False

        for query in queries:
            try:
                result = await self.search_with_failover(query)
            except Exception as exc:
                print(f"Erro ao buscar fallback '{query}': {exc}")
                continue

            candidate = self._extract_first_playable(result)
            if candidate is None:
                continue

            candidate_key = self._track_fallback_key(candidate)
            if fallback_key and candidate_key == fallback_key:
                # Evita repetir exatamente a mesma faixa
                continue

            requester = getattr(track, "requester", None)
            if requester is not None:
                try:
                    candidate.requester = requester
                except Exception:
                    setattr(candidate, "requester", requester)

            try:
                player._fallback_in_progress = True
                await self._send_fallback_notice(player, track, candidate)
                await player.play(candidate)
                fallback_success = True
                break
            except Exception as exc:
                print(f"Erro ao tocar fallback '{query}': {exc}")
            finally:
                player._fallback_in_progress = False

        return fallback_success

    def _start_progress_task(self, player: wavelink.Player, track: wavelink.Playable | None) -> None:
        if player is None or track is None:
            return

        task = asyncio.create_task(self.update_progress_bar(player, track))
        player._kenny_progress_task = task

    async def _cancel_progress_task(self, player: wavelink.Player) -> None:
        task = getattr(player, "_kenny_progress_task", None)
        if not isinstance(task, asyncio.Task):
            return

        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                print(f"Erro ao cancelar tarefa de progresso: {exc}")

        player._kenny_progress_task = None

    async def _send_now_playing_embed(
        self,
        player: wavelink.Player,
        track: wavelink.Playable | None,
    ) -> None:
        if not track:
            return

        channel = getattr(player, "text_channel", None)
        if channel is None:
            requester = getattr(track, "requester", None)
            if requester and hasattr(requester, "channel"):
                channel = requester.channel
        if channel is None:
            return

        player.text_channel = channel

        embed = self._build_now_playing_embed(player, track)
        if embed is None:
            return

        view = MusicControlView(
            self,
            player=player,
            guild_id=getattr(getattr(player, "guild", None), "id", None),
        )
        previous_message = getattr(player, "current_embed_message", None)

        if previous_message:
            try:
                await previous_message.delete()
            except discord.NotFound:
                pass
            except Exception as exc:
                print(f"Falha ao remover embed anterior de reprodução: {exc}")
            finally:
                player.current_embed_message = None

        try:
            message = await channel.send(embed=embed, view=view)
        except Exception as exc:
            print(f"Erro ao enviar embed de reprodução: {exc}")
            return

        player.current_embed_message = message
        return

    def _detect_track_source(self, track: wavelink.Playable | None) -> str | None:
        """Resolve a fonte principal da faixa para reuso (ícone/cor)."""
        if track is None:
            return None

        info = getattr(track, "info", {}) or {}
        source = str(info.get("sourceName") or info.get("source") or "").lower()
        uri = (
            getattr(track, "uri", None)
            or getattr(track, "url", None)
            or info.get("uri")
            or ""
        ).lower()

        def match(substr: str) -> bool:
            return substr in source or substr in uri

        if match("music.youtube") or match("ytm"):
            return "ytmusic"
        if match("youtube") or match("youtu.be"):
            return "youtube"
        if match("deezer"):
            return "deezer"
        if match("spotify"):
            return "spotify"
        if match("apple") or match("applemusic"):
            return "applemusic"
        if match("soundcloud"):
            return "soundcloud"
        if match("twitch"):
            return "twitch"

        return None

    def _track_source_icon(self, track: wavelink.Playable | None) -> str:
        """Retorna o ícone do serviço da faixa (custom emoji ou 🎵)."""
        icons = {
            "ytmusic": "<:ytmusic:1446620983267037395>",
            "youtube": "<:youtube:1446621413179002991>",
            "deezer": "<:deezer1:1454874279886983168>",
            "spotify": "<:spotify:1446621523631931423>",
            "applemusic": "<:apple_music:1448505821142061210>",
            "soundcloud": "<:soundcloud:1446621634294452275>",
            "twitch": "<:twitch:1446621864787968163>",
        }

        source = self._detect_track_source(track)
        if source and source in icons:
            return icons[source]
        return "🎵"

    def _track_source_color(self, track: wavelink.Playable | None) -> int:
        """Seleciona uma cor de destaque alinhada ao serviço atual."""
        colors = {
            "ytmusic": 0xFF0050,
            "youtube": 0xFF0000,
            "deezer": 0xAD47FF,
            "spotify": 0x1DB954,
            "applemusic": 0xFA2D48,
            "soundcloud": 0xFF5500,
            "twitch": 0x9146FF,
        }

        source = self._detect_track_source(track)
        return colors.get(source, 0x00FF00)

    def _strip_leading_icons(self, text: str) -> str:
        """Remove emojis/custom emojis no início da string."""
        if not text:
            return text
        # Remove custom emoji no início
        stripped = re.sub(r"^(<:[^:]+:\d+>\s*)+", "", text)
        # Remove emojis/pictogramas iniciais
        stripped = re.sub(r"^[\u2600-\u27BF\U0001F300-\U0001FAFF]+\s*", "", stripped)
        return stripped.strip()

    async def _restore_voice_channel_status(self, player: wavelink.Player) -> None:
        channel = getattr(player, "channel", None)
        if channel is None or not isinstance(channel, discord.VoiceChannel):
            return

        original_status = getattr(player, "_original_channel_status", None)

        if getattr(player, "_channel_status_overridden", False):
            try:
                await channel.edit(status=original_status)
            except discord.Forbidden:
                pass
            except Exception as exc:
                print(f"Falha ao restaurar status do canal de voz: {exc}")
            finally:
                player._channel_status_overridden = False

        if not hasattr(player, "_original_channel_status"):
            player._original_channel_status = getattr(channel, "status", None)

    async def _clear_now_playing_message(self, player: wavelink.Player) -> None:
        await self._cancel_progress_task(player)
        await self._restore_voice_channel_status(player)

        message = getattr(player, "current_embed_message", None)
        if not message:
            return

        try:
            await message.delete()
        except discord.NotFound:
            pass
        except Exception as exc:
            print(f"Erro ao remover embed de reprodução: {exc}")
        finally:
            player.current_embed_message = None

    def _build_now_playing_embed(
        self,
        player: wavelink.Player,
        track: wavelink.Playable,
    ) -> discord.Embed | None:
        if track is None:
            return None

        guild_id = getattr(player.guild, "id", None)
        base_title = self.translate(
            "commands.play.now_playing.title",
            guild_id=guild_id,
            default="Tocando Agora",
        )
        base_title = self._strip_leading_icons(base_title)

        icon = self._track_source_icon(track)
        title = f"{icon} {base_title}" if base_title else icon
        description = self.translate(
            "commands.play.now_playing.description",
            guild_id=guild_id,
            default="**{title}**",
            title=getattr(track, "title", "-"),
        )

        embed_color = self._track_source_color(track)
        embed = discord.Embed(title=title, description=description, color=embed_color)

        artist_label = self.translate(
            "commands.common.labels.artist",
            guild_id=guild_id,
            default="👤 Artista",
        )
        duration_label = self.translate(
            "commands.common.labels.duration",
            guild_id=guild_id,
            default="⏱️ Duração",
        )
        volume_label = self.translate(
            "commands.play.now_playing.volume_label",
            guild_id=guild_id,
            default="🔊 Volume",
        )
        queue_label = self.translate(
            "commands.play.now_playing.queue_label",
            guild_id=guild_id,
            default="🎶 Fila",
        )
        status_label = self.translate(
            "commands.play.now_playing.status_label",
            guild_id=guild_id,
            default="🔄 Status",
        )

        unknown_author = self.translate(
            "commands.common.labels.unknown_author",
            guild_id=guild_id,
            default="Desconhecido",
        )
        if getattr(player, "paused", False):
            status_value = self.translate(
                "commands.play.now_playing.status.paused",
                guild_id=guild_id,
                default="⏸️ Pausado",
            )
        else:
            status_value = self.translate(
                "commands.play.now_playing.status.playing",
                guild_id=guild_id,
                default="🔄 Reproduzindo",
            )
        queue_value = self.translate(
            "commands.play.now_playing.queue_value",
            guild_id=guild_id,
            default="{count} música(s)",
            count=player.queue.count,
        )

        embed.add_field(
            name=artist_label,
            value=getattr(track, "author", None) or unknown_author,
            inline=True,
        )

        total_duration = getattr(track, "length", 0)
        embed.add_field(
            name=duration_label,
            value=self.format_time(total_duration),
            inline=True,
        )

        embed.add_field(
            name=volume_label,
            value=f"{getattr(player, 'volume', 100)}%",
            inline=True,
        )

        embed.add_field(name=queue_label, value=queue_value, inline=True)
        embed.add_field(name=status_label, value=status_value, inline=True)

        requester = getattr(track, "requester", None)
        if requester:
            requester_label = self.translate(
                "commands.play.now_playing.requested_by",
                guild_id=guild_id,
                default="👤 Solicitado por",
            )
            embed.add_field(
                name=requester_label,
                value=getattr(requester, "mention", str(requester)),
                inline=True,
            )

        progress_field_name = self.translate(
            "commands.queue.embed.progress_label",
            guild_id=guild_id,
            default="Progresso",
        )

        current_position = getattr(player, "position", 0)
        if total_duration:
            progress_percent = min(current_position / total_duration, 1.0)
        else:
            progress_percent = 0.0
        bar_length = 25
        filled_length = int(bar_length * progress_percent)
        bar = "█" * filled_length + "░" * (bar_length - filled_length)
        current_time = self.format_time(current_position)
        total_time = self.format_time(total_duration)
        embed.add_field(
            name=progress_field_name,
            value=f"`{current_time}` {bar} `{total_time}`",
            inline=False,
        )

        if hasattr(track, "artwork") and getattr(track, "artwork", None):
            embed.set_thumbnail(url=track.artwork)

        footer_text = self.translate(
            "commands.play.now_playing.footer",
            guild_id=guild_id,
            default="Use os botões abaixo para controlar a reprodução",
        )

        node_name = self._get_node_display_name(getattr(player, "node", None))
        if node_name:
            footer_text = f"{node_name} • {footer_text}"
        embed.set_footer(text=footer_text)

        return embed

    async def update_progress_bar(self, player: wavelink.Player, track: wavelink.Playable | None):
        expected_track_id = None
        if track is not None:
            expected_track_id = (
                getattr(track, "track", None)
                or getattr(track, "identifier", None)
                or getattr(track, "id", None)
            )

        current_task = asyncio.current_task()

        try:
            while player and getattr(player, "current", None):
                current_track = getattr(player, "current", None)

                if expected_track_id:
                    current_track_id = (
                        getattr(current_track, "track", None)
                        or getattr(current_track, "identifier", None)
                        or getattr(current_track, "id", None)
                    )
                    if current_track_id and current_track_id != expected_track_id:
                        break

                embed_message = getattr(player, "current_embed_message", None)
                if not embed_message:
                    break

                try:
                    embed_source = current_track or track
                    if embed_source is None:
                        break

                    embed = self._build_now_playing_embed(player, embed_source)
                    if embed is None:
                        break
                    await embed_message.edit(embed=embed)
                except discord.NotFound:
                    player.current_embed_message = None
                    break
                except Exception as e:
                    print(f"Erro ao atualizar embed de reprodução: {e}")
                    break

                await asyncio.sleep(5)
                if not player.playing:
                    # Se a música pausou, esperamos até retomar ou terminar
                    await asyncio.sleep(2)
        finally:
            stored_task = getattr(player, "_kenny_progress_task", None)
            if isinstance(stored_task, asyncio.Task) and stored_task is current_task:
                player._kenny_progress_task = None

    def format_time(self, milliseconds):
        if milliseconds is None:
            return "00:00"
        seconds = int(milliseconds / 1000)
        minutes = seconds // 60
        seconds = seconds % 60
        return f"{minutes:02d}:{seconds:02d}"

    def _load_warp_setting(self) -> bool:
        """Carrega flag de auto-reconnect do WARP do MongoDB (padrão: True)."""
        if not self._mongo_connected or self.warp_collection is None:
            return True

        try:
            doc = self.warp_collection.find_one({"_id": "warp_reconnect"})
            if not doc:
                return True
            value = doc.get("enabled")
            return bool(value) if value is not None else True
        except Exception as exc:
            print(f"Falha ao carregar configuração de WARP do MongoDB: {exc}")
            return True

    def save_warp_setting(self, enabled: bool) -> bool:
        """Salva flag de auto-reconnect do WARP no MongoDB."""
        if not self._mongo_connected or self.warp_collection is None:
            print("MongoDB não conectado. Não foi possível salvar configuração de WARP.")
            return False

        try:
            self.warp_collection.update_one(
                {"_id": "warp_reconnect"},
                {"$set": {"enabled": bool(enabled)}},
                upsert=True,
            )
            return True
        except Exception as exc:
            print(f"Falha ao salvar configuração de WARP no MongoDB: {exc}")
            return False

    def _load_presence_config(self) -> dict:
        """Carrega configuração de presença do MongoDB."""
        if not self._mongo_connected or self.presence_collection is None:
            print("MongoDB não conectado. Usando presença padrão.")
            return {}
        
        try:
            # Busca documento com _id="bot_presence" (único documento)
            doc = self.presence_collection.find_one({"_id": "bot_presence"})
            if doc:
                # Remove _id antes de retornar
                doc.pop("_id", None)
                print(f"Configuração de presença carregada do MongoDB: {doc}")
                return doc
            else:
                print("Nenhuma configuração de presença encontrada no MongoDB. Usando defaults.")
                return {}
        except Exception as exc:
            print(f"Falha ao carregar presença do MongoDB: {exc}")
            return {}
    
    def save_presence_config(self, config: dict) -> bool:
        """Salva configuração de presença no MongoDB."""
        if not self._mongo_connected or self.presence_collection is None:
            print("MongoDB não conectado. Não foi possível salvar presença.")
            return False
        
        try:
            # Upsert: atualiza se existe, insere se não existe
            self.presence_collection.update_one(
                {"_id": "bot_presence"},
                {"$set": config},
                upsert=True
            )
            print(f"Configuração de presença salva no MongoDB: {config}")
            return True
        except Exception as exc:
            print(f"Falha ao salvar presença no MongoDB: {exc}")
            return False

    def _build_activity_from_cfg(self, cfg: dict | None) -> discord.BaseActivity | None:
        if not isinstance(cfg, dict):
            return None

        activity_type = (cfg.get("type") or "").lower()
        message = cfg.get("message") or ""
        url = cfg.get("url") or None

        if activity_type == "playing":
            return discord.Game(name=message)
        if activity_type == "listening":
            return discord.Activity(type=discord.ActivityType.listening, name=message)
        if activity_type == "watching":
            return discord.Activity(type=discord.ActivityType.watching, name=message)
        if activity_type == "competing":
            return discord.Activity(type=discord.ActivityType.competing, name=message)
        if activity_type == "streaming" and url:
            return discord.Streaming(name=message, url=url)
        return None

    async def _apply_presence_when_ready(self) -> None:
        print("Tarefa de presença aguardando bot ficar pronto...")
        await self.wait_until_ready()
        print("Bot sinalizado como pronto; aguardando 1s antes de aplicar presença.")
        await asyncio.sleep(1)
        try:
            print("Executando apply_saved_presence()...")
            await self.apply_saved_presence()
            self._presence_applied = True
            print("Presença salva aplicada com sucesso.")
        except Exception as e:
            import traceback

            print(f"Não foi possível aplicar presença salva: {e}")
            traceback.print_exc()

    async def apply_saved_presence(self) -> None:
        config = self._load_presence_config()
        status_str = (config.get("status") or "online").lower()
        status_map = {
            "online": discord.Status.online,
            "idle": discord.Status.idle,
            "dnd": discord.Status.dnd,
            "invisible": discord.Status.invisible,
        }
        status = status_map.get(status_str, discord.Status.online)
        activity_cfg = config.get("activity")
        activity = self._build_activity_from_cfg(activity_cfg)

        if not config:
            print("Nenhuma configuração de presença encontrada; pulando aplicação.")
            return

        print(f"Aplicando presença salva: status={status_str}, atividade={activity_cfg}")
        await self.change_presence(status=status, activity=activity)


bot = MusicBot(proxy=args.proxy)

if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        print("ERRO: Token do Discord não encontrado no arquivo .env!")
        exit(1)

    try:
        bot.run(token)
    except Exception as e:
        print(f"Erro ao iniciar o bot: {e}")