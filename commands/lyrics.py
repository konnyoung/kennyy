import discord
from discord.ext import commands
from discord import app_commands
import aiohttp
import re
from urllib.parse import quote
import wavelink


class LyricsCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def _translate(self, interaction, key, default="Translation missing", **kwargs):
        return self.bot.translate(key, guild_id=interaction.guild_id, default=default, **kwargs)

    async def fetch_lyrics(self, title: str, artist: str) -> dict | None:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        timeout = aiohttp.ClientTimeout(total=15)
        safe_title = title.strip()
        safe_artist = artist.strip()

        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            result = await self._fetch_from_some_random_api(session, safe_title, safe_artist)
            if result:
                return result

            fallback = await self._fetch_from_lyrics_ovh(session, safe_title, safe_artist)
            if fallback:
                return fallback

        return None

    async def _fetch_from_some_random_api(self, session: aiohttp.ClientSession, title: str, artist: str) -> dict | None:
        query = f"{artist} {title}".strip()
        url = f"https://some-random-api.com/lyrics?title={quote(query)}"

        try:
            async with session.get(url) as response:
                if response.status != 200:
                    return None

                data = await response.json()
        except Exception as e:
            print(f"Erro ao buscar letras via SomeRandomAPI: {e}")
            return None

        if not isinstance(data, dict) or data.get("error"):
            return None

        lyrics = data.get("lyrics")
        if not lyrics:
            return None

        cleaned = self._clean_lyrics_text(lyrics)

        thumb = None
        thumbnails = data.get("thumbnail")
        if isinstance(thumbnails, dict):
            thumb = thumbnails.get("genius") or thumbnails.get("primary")
        elif isinstance(thumbnails, str):
            thumb = thumbnails

        links = data.get("links")
        url_source = None
        if isinstance(links, dict):
            url_source = links.get("genius") or links.get("apple") or links.get("spotify")

        return {
            "title": data.get("title") or title,
            "artist": data.get("author") or artist,
            "lyrics": cleaned,
            "thumbnail": thumb,
            "url": url_source,
            "source": "Some Random API"
        }

    async def _fetch_from_lyrics_ovh(self, session: aiohttp.ClientSession, title: str, artist: str) -> dict | None:
        url = f"https://api.lyrics.ovh/v1/{quote(artist)}/{quote(title)}"

        try:
            async with session.get(url) as response:
                if response.status != 200:
                    return None

                data = await response.json()
        except Exception as e:
            print(f"Erro ao buscar letras via Lyrics.ovh: {e}")
            return None

        if not isinstance(data, dict) or data.get("error"):
            return None

        lyrics = data.get("lyrics")
        if not lyrics:
            return None

        cleaned = self._clean_lyrics_text(lyrics)

        return {
            "title": title,
            "artist": artist,
            "lyrics": cleaned,
            "thumbnail": None,
            "url": None,
            "source": "Lyrics.ovh"
        }

    def _clean_lyrics_text(self, text: str) -> str:
        normalized = text.replace('\r\n', '\n').replace('\r', '\n')
        normalized = re.sub(r'\n{3,}', '\n\n', normalized)
        lines = [line.rstrip() for line in normalized.split('\n')]
        cleaned_lines = [line for line in lines if line.strip()]
        return '\n'.join(cleaned_lines).strip()

    @app_commands.command(name="lyrics", description="Exibe a letra da música que está tocando")
    async def lyrics(self, interaction: discord.Interaction):
        await interaction.response.defer()
        
        # Verifica se há música tocando
        if not interaction.guild:
            return await interaction.followup.send("❌ Este comando só funciona em servidores.", ephemeral=True)
        
        player: wavelink.Player = interaction.guild.voice_client
        
        if not player or not player.current:
            embed = discord.Embed(
                title=self._translate(
                    interaction,
                    "commands.lyrics.errors.no_track.title",
                    default="❌ Nenhuma música tocando"
                ),
                description=self._translate(
                    interaction,
                    "commands.lyrics.errors.no_track.description",
                    default="Não há música tocando no momento. Use /play para tocar algo primeiro!"
                ),
                color=0xff0000
            )
            return await interaction.followup.send(embed=embed)
        
        current_track = player.current

        lyrics_data = await self.fetch_lyrics(current_track.title, current_track.author)

        if not lyrics_data:
            embed = discord.Embed(
                title=self._translate(
                    interaction,
                    "commands.lyrics.errors.not_found.title",
                    default="❌ Não encontrado"
                ),
                description=self._translate(
                    interaction,
                    "commands.lyrics.errors.not_found.description",
                    default="Não consegui encontrar a letra para: **{query}**",
                    query=f"{current_track.title} - {current_track.author}"
                ),
                color=0xff0000
            )
            return await interaction.followup.send(embed=embed)
        
        # Divide letra se for muito grande (limite Discord: 4096 chars no description)
        lyrics_text = lyrics_data["lyrics"]
        if len(lyrics_text) > 4000:
            lyrics_text = lyrics_text[:3997] + "..."
        
        embed_title = self._translate(
            interaction,
            "commands.lyrics.embed.title",
            default="🎵 {title}",
            title=lyrics_data.get("title") or current_track.title
        )

        embed = discord.Embed(
            title=embed_title,
            description=lyrics_text,
            color=0xffff64,
            url=lyrics_data.get("url") or None
        )
        embed.set_author(
            name=lyrics_data.get("artist") or current_track.author,
            icon_url=lyrics_data.get("thumbnail") or None
        )
        embed.set_footer(
            text=self._translate(
                interaction,
                "commands.lyrics.embed.footer",
                default="Fonte: {source} • Música atual: {track}",
                track=current_track.title,
                source=lyrics_data.get("source", "?")
            )
        )
        
        await interaction.followup.send(embed=embed)


async def setup(bot):
    await bot.add_cog(LyricsCommands(bot))
