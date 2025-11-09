import discord
from discord.ext import commands
import wavelink
import os
import asyncio
import logging
import sys
import json
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

# Logs
logging.basicConfig(level=logging.INFO)
# Silencia aviso sobre message_content ausente (slash-only não precisa)
logging.getLogger("discord.ext.commands.bot").setLevel(logging.ERROR)


class MusicBot(commands.Bot):
    def __init__(self):
        # Intents mínimos (SEM privilegiadas)
        intents = discord.Intents.none()  # começa com tudo False
        intents.guilds = True             # necessário para slash
        intents.voice_states = True       # necessário para tocar entrar/sair de voz

        super().__init__(command_prefix="!", intents=intents, help_command=None)
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
        self._mongo_connected = False
        self._alone_tasks: dict[int, asyncio.Task] = {}
        self.owner_ids: set[int] = self._load_owner_ids()
        self.logger = None

        if not self.owner_ids:
            print("Aviso: BOT_OWNER_IDS não definidos. Comandos de administrador do bot ficarão indisponíveis.")

        self._init_mongo()
        self._load_locales()
        self._init_logger()

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
                pause_message = self.translate(
                    "player.lonely.pause",
                    guild_id=guild.id,
                    call=channel.mention,
                    default=f"Fiquei sozinho em {channel.mention}. Irei pausar a fila por 2 minutos até alguém retornar. Caso contrário, irei me desconectar!",
                )
                await message_channel.send(pause_message)
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

        if getattr(player, "paused", False) and getattr(player, "current", None):
            try:
                await player.pause(False)
            except Exception as exc:
                print(f"Falha ao retomar player após retorno de ouvintes: {exc}")

        message_channel = self._preferred_text_channel(player, guild)
        channel = getattr(player, "channel", None)
        if message_channel is not None and channel is not None:
            try:
                resume_message = self.translate(
                    "player.lonely.resume",
                    guild_id=guild.id,
                    call=channel.mention,
                    default=f"Alguém entrou em {channel.mention}! Retomando a fila.",
                )
                await message_channel.send(resume_message)
                if hasattr(player, "text_channel"):
                    player.text_channel = message_channel
            except Exception as exc:
                print(f"Falha ao enviar aviso de retomada: {exc}")

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
                    disconnect_message = self.translate(
                        "player.lonely.disconnect",
                        guild_id=guild_id,
                        call=channel.mention,
                        default=f"Ninguém voltou para {channel.mention}. Irei me desconectar agora.",
                    )
                    await message_channel.send(disconnect_message)
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
            "commands.lyrics"
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

        joined_channel = getattr(after, "channel", None)
        left_channel = getattr(before, "channel", None)

        if left_channel is not None or joined_channel is None:
            await self._evaluate_voice_channel(member.guild)
            return

        guild = joined_channel.guild

        await asyncio.sleep(1)

        voice_client = guild.voice_client
        if voice_client is None or getattr(voice_client, "channel", None) != joined_channel:
            return

        try:
            await guild.change_voice_state(channel=joined_channel, self_deaf=True)
            print(f"🔇 Bot ensurdecido no servidor: {guild.name}")
        except Exception as e:
            print(f"Erro ao ensurdecer bot: {e}")

        await self._evaluate_voice_channel(member.guild)

    async def on_wavelink_node_ready(self, payload: wavelink.NodeReadyEventPayload):
        print(f"Nó Lavalink '{payload.node.identifier}' está pronto!")

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
        configs: list[dict[str, str | bool]] = []
        self._lavalink_cfgs = []

        for idx in range(1, 4):
            host = (os.getenv(f"LAVALINK_NODE{idx}_HOST", "") or "").strip()
            if not host:
                continue

            port = (os.getenv(f"LAVALINK_NODE{idx}_PORT", "2333") or "2333").strip() or "2333"
            password = os.getenv(f"LAVALINK_NODE{idx}_PASSWORD", "youshallnotpass")
            secure = (os.getenv(f"LAVALINK_NODE{idx}_SECURE", "false") or "false").lower() == "true"
            protocol = "wss" if secure else "ws"
            configs.append({
                "id": f"node{idx}",
                "protocol": protocol,
                "host": host,
                "port": port,
                "password": password,
                "secure": secure,
            })

        # Compatibilidade com configuração antiga (apenas um nó)
        if not configs:
            host = (os.getenv("LAVALINK_HOST", "") or "").strip()
            if host:
                port = (os.getenv("LAVALINK_PORT", "2333") or "2333").strip() or "2333"
                password = os.getenv("LAVALINK_PASSWORD", "youshallnotpass")
                secure = (os.getenv("LAVALINK_SECURE", "false") or "false").lower() == "true"
                protocol = "wss" if secure else "ws"
                configs.append({
                    "id": "node1",
                    "protocol": protocol,
                    "host": host,
                    "port": port,
                    "password": password,
                    "secure": secure,
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

    async def ensure_lavalink_connected(self) -> bool:
        """Valida a conexão com os nós Lavalink e tenta reconectar se necessário."""
        await self.connect_lavalink()

        connected_nodes: list[wavelink.Node] = []
        pending_identifiers: list[str] = []

        for cfg in self._lavalink_cfgs:
            identifier = cfg["id"]
            try:
                node = wavelink.Pool.get_node(identifier)
            except wavelink.InvalidNodeException:
                pending_identifiers.append(identifier)
                continue

            if node.status == wavelink.NodeStatus.CONNECTED:
                connected_nodes.append(node)
            else:
                pending_identifiers.append(identifier)

        if pending_identifiers:
            print(f"⏳ Aguardando reconexão dos nós: {', '.join(pending_identifiers)}")

        if not connected_nodes:
            print("❌ Nenhum nó Lavalink conectado no momento.")
            return False

        return True

    async def search_with_failover(self, query: str):
        """Realiza buscas no Lavalink com failover entre os nós configurados."""

        attempt_nodes: list[wavelink.Node] = []
        seen: set[str] = set()

        for cfg in self._lavalink_cfgs:
            identifier = cfg["id"]
            try:
                node = wavelink.Pool.get_node(identifier)
            except wavelink.InvalidNodeException:
                continue

            if node.status != wavelink.NodeStatus.CONNECTED:
                continue

            if identifier not in seen:
                attempt_nodes.append(node)
                seen.add(identifier)

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

    async def _lavalink_watchdog(self):
        """Tarefa em background que mantém a conexão ativa e tenta reconectar quando necessário."""
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                ok = await self.ensure_lavalink_connected()
                if not ok:
                    # Aguarda um pouco antes de tentar novamente para evitar loop agressivo
                    await asyncio.sleep(15)
                # Intervalo padrão entre checagens
                await asyncio.sleep(60)
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

    def _generate_panel_content(self) -> str:
        total_calls = len(self.voice_clients)
        total_playing = sum(1 for vc in self.voice_clients if getattr(vc, "playing", False))

        node_lines: list[str] = []

        def build_line(node_id: str, node: wavelink.Node | None):
            status_icon = "🔴"
            call_count = 0
            playing_count = 0

            if node:
                status_icon = "🟢" if node.status == wavelink.NodeStatus.CONNECTED else "🔴"
                players = getattr(node, "players", {}) or {}
                if isinstance(players, dict):
                    call_count = len(players)
                    playing_count = sum(1 for p in players.values() if getattr(p, "playing", False))
                elif isinstance(players, (list, tuple, set)):
                    call_count = len(players)
                    playing_count = sum(1 for p in players if getattr(p, "playing", False))

            node_lines.append(f"{node_id}: {status_icon} calls={call_count} tocando={playing_count}")

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
            guild = player.guild
            if guild and guild.voice_client:
                await guild.change_voice_state(channel=guild.voice_client.channel, self_deaf=True)
        except Exception as e:
            print(f"Erro ao garantir ensurdecimento do bot: {e}")
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

        print(f"Track finalizado. Razão: {reason_upper}. Guild: {getattr(player.guild, 'name', 'Desconhecido')}")

        if reason_upper == "LOAD_FAILED":
            exception_info = getattr(player, "_last_error", None)
            fallback_started = await self._try_play_fallback(player, payload.track, exception_info)
            player._last_error = None
            if fallback_started:
                return

            await self._notify_track_failure(player, payload.track, exception_info)

            if not player.queue.is_empty:
                try:
                    next_track = await player.queue.get_wait()
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

        if reason and reason.upper() == "LOAD_FAILED":
            suppress_finished_embed = True
            if failed_track:
                await self._notify_track_failure(
                    player,
                    failed_track,
                    getattr(player, "_last_error", None),
                )

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
                    description = self.translate("player.queue_finished.description", guild_id=guild_id)
                    embed = discord.Embed(
                        title=title,
                        description=description,
                        color=0x0099ff,
                    )
                    await channel.send(embed=embed)
            except Exception as e:
                print(f"Erro ao enviar embed de fila finalizada: {e}")

        # Limpa referências para evitar updates de progresso pendentes
        if hasattr(player, "current_embed_message"):
            player.current_embed_message = None

        # Desconecta do canal de voz
        try:
            if getattr(player, "connected", False) or getattr(player, "channel", None):
                await player.disconnect()
                guild_name = getattr(player.guild, "name", "Desconhecido")
                print(f"Desconectado do canal de voz após finalizar fila no servidor: {guild_name}")
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

        embed = discord.Embed(title=title, description=description, color=0xff0033)
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

        title = self.translate(
            "commands.play.now_playing.title",
            guild_id=guild_id,
            default="🎵 Tocando Agora",
        )
        description = self.translate(
            "commands.play.now_playing.description",
            guild_id=guild_id,
            default="**{title}**",
            title=getattr(track, "title", "-"),
        )

        embed = discord.Embed(title=title, description=description, color=0x00ff00)

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


bot = MusicBot()

if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        print("ERRO: Token do Discord não encontrado no arquivo .env!")
        exit(1)

    try:
        bot.run(token)
    except Exception as e:
        print(f"Erro ao iniciar o bot: {e}")