import os
import asyncio
import time

import aiohttp
import discord
from discord.ext import commands
from discord import app_commands


class Ping(commands.Cog):
    def __init__(self, bot: commands.Bot):
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

    def _status_emoji(self, ms: int | None) -> str:
        if ms is None:
            return "⚪"
        if ms <= 100:
            return "🟢"
        if ms <= 200:
            return "🟡"
        return "🔴"

    def _env_lavalink_configs(self) -> list[dict]:
        """Lê a configuração dos nós Lavalink direto do .env."""
        configs: list[dict] = []

        for idx in range(1, 4):
            host = (os.getenv(f"LAVALINK_NODE{idx}_HOST", "") or "").strip()
            if not host:
                continue

            name = (os.getenv(f"LAVALINK_NODE{idx}_NAME", "") or "").strip()
            if not name:
                name = f"node{idx}"
            port = (os.getenv(f"LAVALINK_NODE{idx}_PORT", "2333") or "2333").strip()
            password = os.getenv(f"LAVALINK_NODE{idx}_PASSWORD", "youshallnotpass")
            secure = (os.getenv(f"LAVALINK_NODE{idx}_SECURE", "false") or "false").lower() == "true"
            scheme = "https" if secure else "http"
            configs.append({
                "identifier": f"node{idx}",
                "name": name,
                "secure": secure,
                "host": host,
                "port": port,
                "password": password,
                "scheme": scheme,
                "base": f"{scheme}://{host}:{port}",
            })

        if not configs:
            host = (os.getenv("LAVALINK_HOST", "") or "").strip()
            if host:
                name = (os.getenv("LAVALINK_NODE_NAME", "") or "").strip()
                if not name:
                    name = "node1"
                port = (os.getenv("LAVALINK_PORT", "2333") or "2333").strip()
                password = os.getenv("LAVALINK_PASSWORD", "youshallnotpass")
                secure = (os.getenv("LAVALINK_SECURE", "false") or "false").lower() == "true"
                scheme = "https" if secure else "http"
                configs.append({
                    "identifier": "node1",
                    "name": name,
                    "secure": secure,
                    "host": host,
                    "port": port,
                    "password": password,
                    "scheme": scheme,
                    "base": f"{scheme}://{host}:{port}",
                })

        return configs

    async def _tcp_ping(self, host: str, port: int, timeout: float = 3.0) -> int | None:
        """Realiza um ping TCP simples (conectar/fechar) e retorna a latência em ms."""
        start = time.perf_counter()
        try:
            conn = asyncio.open_connection(host, port)
            reader, writer = await asyncio.wait_for(conn, timeout=timeout)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            end = time.perf_counter()
            return int((end - start) * 1000)
        except Exception:
            return None

    async def _lavalink_http_ping(self) -> list[tuple[dict, int | None, str]]:
        """
        Mede a latência HTTP dos nós Lavalink. Retorna lista de tuplas (cfg, ms|None, endpoint).
        """
        configs = self._env_lavalink_configs()
        if not configs:
            return []

        results: list[tuple[dict, int | None, str]] = []
        endpoints = ["/v4/info", "/version"]
        timeout = aiohttp.ClientTimeout(total=4)

        async with aiohttp.ClientSession(timeout=timeout) as session:
            for cfg in configs:
                latency_ms: int | None = None
                endpoint_used = "N/A"
                headers = {"Authorization": cfg["password"]}
                for ep in endpoints:
                    url = cfg["base"] + ep
                    start = time.perf_counter()
                    try:
                        async with session.get(url, headers=headers) as resp:
                            if resp.status < 500:
                                end = time.perf_counter()
                                latency_ms = int((end - start) * 1000)
                                endpoint_used = ep
                                break
                    except Exception:
                        continue
                results.append((cfg, latency_ms, endpoint_used))

        return results

    @app_commands.command(name="ping", description="Measure latency for Discord, Lavalink, and Cloudflare")
    async def ping(self, interaction: discord.Interaction):
        await interaction.response.defer()

        # Discord (gateway heartbeat)
        discord_ms = int(self.bot.latency * 1000)

        # Executa em paralelo
        lavalink_task = asyncio.create_task(self._lavalink_http_ping())
        cloudflare_task = asyncio.create_task(self._tcp_ping("1.1.1.1", 443))

        lavalink_results = await lavalink_task
        cloudflare_ms = await cloudflare_task

        # Monta embed
        embed = discord.Embed(
            title=self._translate(
                interaction,
                "commands.ping.embed.title",
                default="🏓 Ping",
            ),
            color=0x5865F2
        )

        embed.add_field(
            name=self._translate(
                interaction,
                "commands.ping.fields.discord.name",
                default="Discord (Gateway)",
            ),
            value=self._translate(
                interaction,
                "commands.ping.fields.discord.value",
                default=f"{self._status_emoji(discord_ms)} `{discord_ms} ms`",
                emoji=self._status_emoji(discord_ms),
                latency=discord_ms,
            ),
            inline=False
        )

        if lavalink_results:
            for cfg, lavalink_ms, lavalink_ep in lavalink_results:
                display_name = cfg.get("name") or cfg.get("identifier") or "node"
                label = self._translate(
                    interaction,
                    "commands.ping.fields.lavalink.display_name",
                    default=f"Lavalink node: {display_name}",
                    name=display_name,
                )
                lavalink_value = (
                    self._translate(
                        interaction,
                        "commands.ping.fields.lavalink.value",
                        default=f"{self._status_emoji(lavalink_ms)} `{lavalink_ms} ms` • endpoint: `{lavalink_ep}`" if lavalink_ms is not None else f"{self._status_emoji(None)} indisponível",
                        emoji=self._status_emoji(lavalink_ms),
                        latency=lavalink_ms,
                        endpoint=lavalink_ep,
                    )
                )
                embed.add_field(name=label, value=lavalink_value, inline=False)
        else:
            embed.add_field(
                name=self._translate(
                    interaction,
                    "commands.ping.fields.lavalink_empty.name",
                    default="Lavalink",
                ),
                value=self._translate(
                    interaction,
                    "commands.ping.fields.lavalink_empty.value",
                    default=f"{self._status_emoji(None)} nenhuma configuração encontrada",
                    emoji=self._status_emoji(None),
                ),
                inline=False,
            )

        cf_value = (
            self._translate(
                interaction,
                "commands.ping.fields.cloudflare.value",
                default=f"{self._status_emoji(cloudflare_ms)} `{cloudflare_ms} ms` • TCP 443" if cloudflare_ms is not None else f"{self._status_emoji(None)} indisponível",
                emoji=self._status_emoji(cloudflare_ms),
                latency=cloudflare_ms,
            )
        )
        embed.add_field(
            name=self._translate(
                interaction,
                "commands.ping.fields.cloudflare.name",
                default="1.1.1.1",
            ),
            value=cf_value,
            inline=False
        )

        embed.set_footer(
            text=self._translate(
                interaction,
                "commands.ping.embed.footer",
                default="Medições realizadas agora",
            )
        )
        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Ping(bot))