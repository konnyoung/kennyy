import os
import asyncio
from flask import Flask, jsonify, request, redirect, url_for, session, render_template_string
from flask_discord import DiscordOAuth2Session, requires_authorization, exceptions as fd_exceptions
import wavelink


def create_dashboard(bot):
    app = Flask(__name__)

    # Lê a redirect URI e habilita HTTP em desenvolvimento automaticamente
    redirect_uri = os.getenv("DISCORD_REDIRECT_URI", "http://localhost:5000/callback")
    if redirect_uri.startswith("http://"):
        # Permite OAuth2 via HTTP (somente DEV)
        os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

    app.config.update(
        SECRET_KEY=os.getenv("FLASK_SECRET_KEY", "change-me"),
        DISCORD_CLIENT_ID=int(os.getenv("DISCORD_CLIENT_ID", "0")),
        DISCORD_CLIENT_SECRET=os.getenv("DISCORD_CLIENT_SECRET", ""),
        DISCORD_REDIRECT_URI=redirect_uri,
    )
    # Scopes mínimos (sem members/presences)
    app.config["DISCORD_BOT_TOKEN"] = os.getenv("DISCORD_BOT_TOKEN", os.getenv("DISCORD_TOKEN"))
    app.config["DISCORD_SCOPE"] = ["identify", "guilds"]

    oauth = DiscordOAuth2Session(app)

    # Redireciona para /login quando a sessão não existir (evita 500)
    @app.errorhandler(fd_exceptions.Unauthorized)
    def _unauth(_):
        return redirect(url_for("login"))

    # Helper para chamar corotinas do bot a partir da thread do Flask
    def call(coro, timeout=15):
        return asyncio.run_coroutine_threadsafe(coro, bot.loop).result(timeout=timeout)

    def _get_guild(guild_id: int):
        return bot.get_guild(int(guild_id))

    def _get_player(guild_id: int):
        g = _get_guild(guild_id)
        return (g, g.voice_client if g else None)

    def _track_to_dict(t):
        if not t:
            return None
        return {
            "title": getattr(t, "title", None),
            "author": getattr(t, "author", None),
            "length": getattr(t, "length", None),  # ms
            "artwork": getattr(t, "artwork", None),
            "uri": getattr(t, "uri", None),
        }

    # ---------- Rotas básicas/UI ----------
    @app.route("/")
    def home():
        # Redireciona para a UI (exige login)
        return redirect(url_for("app_ui"))

    @app.route("/ping")
    def ping():
        return "pong"

    @app.route("/app")
    @requires_authorization
    def app_ui():
        # Painel HTML simples que consome os endpoints abaixo
        html = """
        <!doctype html>
        <meta charset="utf-8">
        <title>Painel do Bot</title>
        <style>
          body { font-family: system-ui, sans-serif; margin: 24px; }
          label { display:block; margin-top:12px; }
          select, input[type=text] { width: 420px; max-width: 100%; padding:8px; }
          button { margin: 6px 6px 0 0; padding:8px 12px; }
          pre { background:#111; color:#0f0; padding:12px; overflow:auto; }
          .row { margin-top: 12px; }
        </style>
        <h1>🎛️ Painel do Bot</h1>

        <div class="row">
          <button id="btnLogout" onclick="window.location='/logout'">Sair</button>
          <button id="btnRefreshMe">Meu perfil</button>
        </div>

        <label>Servidor (guild):</label>
        <select id="guilds"></select>

        <label>Canal de voz:</label>
        <select id="voiceChannels"></select>

        <label>Buscar/Tocar:</label>
        <input id="query" type="text" placeholder="ytsearch: artista música ou URL" />
        <button id="btnPlay">▶️ Play</button>

        <div class="row">
          <button id="btnState">🔎 Estado</button>
          <button id="btnPause">⏸️ Pausar</button>
          <button id="btnResume">▶️ Retomar</button>
          <button id="btnSkip">⏭️ Pular</button>
          <button id="btnStop">⏹️ Parar</button>
          <input id="vol" type="number" min="0" max="150" value="100" style="width:80px;" />
          <button id="btnVol">🔊 Volume</button>
        </div>

        <h3>Resposta</h3>
        <pre id="out"></pre>

        <script>
          const out = document.getElementById('out');
          const guildSel = document.getElementById('guilds');
          const chanSel = document.getElementById('voiceChannels');
          const query = document.getElementById('query');
          const vol = document.getElementById('vol');

          function log(obj){ out.textContent = JSON.stringify(obj, null, 2); }

          async function getJSON(url){
            const r = await fetch(url);
            if(!r.ok){ throw new Error(await r.text()); }
            return r.json();
          }

          async function postJSON(url, body){
            const r = await fetch(url, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body||{})});
            if(!r.ok){ throw new Error(await r.text()); }
            return r.json();
          }

          async function loadGuilds(){
            const data = await getJSON('/guilds');
            const gs = data.mutual_guilds || [];
            guildSel.innerHTML = gs.map(g => `<option value="${g.id}">${g.name} (${g.id})</option>`).join('');
            if(gs.length){ await loadVoiceChannels(); }
            log(data);
          }

          async function loadVoiceChannels(){
            const gid = guildSel.value;
            if(!gid) return;
            const data = await getJSON(`/api/${gid}/voice-channels`);
            const cs = data.channels || [];
            chanSel.innerHTML = cs.map(c => `<option value="${c.id}">${c.name} (${c.type})</option>`).join('');
            log(data);
          }

          document.getElementById('btnRefreshMe').onclick = async ()=>{
            try { log(await getJSON('/me')); } catch(e){ log({error: String(e)}); }
          };

          guildSel.onchange = loadVoiceChannels;

          document.getElementById('btnPlay').onclick = async ()=>{
            try {
              const gid = guildSel.value;
              const cid = chanSel.value;
              const q = query.value.trim();
              log(await postJSON(`/api/${gid}/play`, { query: q, voice_channel_id: cid }));
            } catch(e){ log({error: String(e)}); }
          };

          document.getElementById('btnState').onclick = async ()=>{
            try { log(await getJSON(`/api/${guildSel.value}/state`)); } catch(e){ log({error: String(e)}); }
          };
          document.getElementById('btnPause').onclick = async ()=>{
            try { log(await postJSON(`/api/${guildSel.value}/pause`)); } catch(e){ log({error: String(e)}); }
          };
          document.getElementById('btnResume').onclick = async ()=>{
            try { log(await postJSON(`/api/${guildSel.value}/resume`)); } catch(e){ log({error: String(e)}); }
          };
          document.getElementById('btnSkip').onclick = async ()=>{
            try { log(await postJSON(`/api/${guildSel.value}/skip`)); } catch(e){ log({error: String(e)}); }
          };
          document.getElementById('btnStop').onclick = async ()=>{
            try { log(await postJSON(`/api/${guildSel.value}/stop`)); } catch(e){ log({error: String(e)}); }
          };
          document.getElementById('btnVol').onclick = async ()=>{
            try { log(await postJSON(`/api/${guildSel.value}/volume`, { level: Number(vol.value) })); } catch(e){ log({error: String(e)}); }
          };

          // Inicializa
          loadGuilds().catch(e => log({error: String(e)}));
        </script>
        """
        return render_template_string(html)

    # ---------- Auth ----------
    @app.route("/login")
    def login():
        return oauth.create_session()

    @app.route("/callback")
    def callback():
        # Finaliza o fluxo OAuth e cria a sessão
        oauth.callback()
        return redirect(url_for("app_ui"))

    @app.route("/logout")
    def logout():
        try:
            oauth.revoke()
        except Exception:
            pass
        session.clear()
        return redirect("/")

    @app.route("/me")
    @requires_authorization
    def me():
        u = oauth.fetch_user()
        return jsonify({"id": u.id, "username": str(u)})

    @app.route("/guilds")
    @requires_authorization
    def guilds():
        u = oauth.fetch_user()
        user_guilds = oauth.fetch_guilds()  # guilds do usuário (via OAuth)
        bot_guild_ids = {str(g.id) for g in bot.guilds}
        mutual = [{"id": int(g.id), "name": g.name} for g in user_guilds if str(g.id) in bot_guild_ids]
        return jsonify({"user_id": u.id, "mutual_guilds": mutual})

    # ---------- Listagem de canais de voz (sem members intent) ----------
    @app.route("/api/<int:guild_id>/voice-channels")
    @requires_authorization
    def voice_channels(guild_id: int):
        guild = _get_guild(guild_id)
        if not guild:
            return jsonify({"ok": False, "error": "guild_not_found"}), 404

        chans = []
        for ch in guild.channels:
            # VoiceChannel/StageChannel possuem .connect
            if hasattr(ch, "connect"):
                chans.append({
                    "id": ch.id,
                    "name": ch.name,
                    "type": getattr(getattr(ch, "type", None), "name", "voice"),
                    "bitrate": getattr(ch, "bitrate", None),
                    "user_limit": getattr(ch, "user_limit", None)
                })
        return jsonify({"ok": True, "channels": chans})

    # ---------- Estado ----------
    @app.route("/api/<int:guild_id>/state")
    @requires_authorization
    def state(guild_id: int):
        guild, player = _get_player(guild_id)
        if not guild:
            return jsonify({"ok": False, "error": "guild_not_found"}), 404

        if not player:
            return jsonify({"ok": True, "connected": False, "queue": []})

        queue_list = list(player.queue)
        return jsonify({
            "ok": True,
            "connected": True,
            "playing": bool(player.playing),
            "paused": bool(player.paused),
            "volume": getattr(player, "volume", None),
            "position": getattr(player, "position", None),  # ms
            "channel_id": getattr(player, "channel", None).id if getattr(player, "channel", None) else None,
            "current": _track_to_dict(player.current),
            "queue": [_track_to_dict(t) for t in queue_list],
        })

    # ---------- Ações ----------
    @app.route("/api/<int:guild_id>/play", methods=["POST"])
    @requires_authorization
    def api_play(guild_id: int):
        data = request.get_json(silent=True) or {}
        query = data.get("query")
        voice_channel_id = data.get("voice_channel_id")  # obrigatório
        if not query:
            return jsonify({"ok": False, "error": "missing_query"}), 400
        if not voice_channel_id:
            return jsonify({"ok": False, "error": "voice_channel_required"}), 400

        async def do():
            guild = _get_guild(guild_id)
            if not guild:
                return {"ok": False, "error": "guild_not_found"}

            channel = guild.get_channel(int(voice_channel_id))
            if not channel or not hasattr(channel, "connect"):
                return {"ok": False, "error": "invalid_voice_channel"}

            player = guild.voice_client
            if not player:
                try:
                    player = await channel.connect(cls=wavelink.Player)
                except Exception as e:
                    return {"ok": False, "error": f"connect_failed: {e}"}

            results = await wavelink.Playable.search(
                query if str(query).startswith(("http://", "https://")) else f"ytsearch:{query}"
            )
            if not results:
                return {"ok": False, "error": "no_results"}

            if isinstance(results, wavelink.Playlist):
                for tr in results.tracks:
                    await player.queue.put_wait(tr)
                if not player.playing:
                    nxt = await player.queue.get_wait()
                    await player.play(nxt)
                return {"ok": True, "type": "playlist", "name": results.name, "added": len(results.tracks)}
            else:
                track = results[0]
                if not player.playing:
                    await player.play(track)
                    queued = False
                else:
                    await player.queue.put_wait(track)
                    queued = True
                return {"ok": True, "type": "track", "title": track.title, "queued": queued}

        try:
            return jsonify(call(do()))
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.route("/api/<int:guild_id>/pause", methods=["POST"])
    @requires_authorization
    def api_pause(guild_id: int):
        async def do():
            _, player = _get_player(guild_id)
            if not player or not player.current:
                return {"ok": False, "error": "no_player"}
            await player.pause(True)
            return {"ok": True}
        return jsonify(call(do()))

    @app.route("/api/<int:guild_id>/resume", methods=["POST"])
    @requires_authorization
    def api_resume(guild_id: int):
        async def do():
            _, player = _get_player(guild_id)
            if not player or not player.current:
                return {"ok": False, "error": "no_player"}
            await player.pause(False)
            return {"ok": True}
        return jsonify(call(do()))

    @app.route("/api/<int:guild_id>/skip", methods=["POST"])
    @requires_authorization
    def api_skip(guild_id: int):
        async def do():
            _, player = _get_player(guild_id)
            if not player or not player.playing:
                return {"ok": False, "error": "no_player"}
            try:
                await player.skip(force=True)
            except Exception:
                await player.stop()
            return {"ok": True}
        return jsonify(call(do()))

    @app.route("/api/<int:guild_id>/stop", methods=["POST"])
    @requires_authorization
    def api_stop(guild_id: int):
        async def do():
            guild, player = _get_player(guild_id)
            if not guild or not player:
                return {"ok": False, "error": "no_player"}
            await player.disconnect()
            return {"ok": True}
        return jsonify(call(do()))

    @app.route("/api/<int:guild_id>/volume", methods=["POST"])
    @requires_authorization
    def api_volume(guild_id: int):
        data = request.get_json(silent=True) or {}
        level = data.get("level")
        if level is None:
            return jsonify({"ok": False, "error": "missing_level"}), 400
        try:
            level = int(level)
        except ValueError:
            return jsonify({"ok": False, "error": "invalid_level"}), 400
        if level < 0 or level > 150:
            return jsonify({"ok": False, "error": "range_0_150"}), 400

        async def do():
            _, player = _get_player(guild_id)
            if not player:
                return {"ok": False, "error": "no_player"}
            await player.set_volume(level)
            return {"ok": True, "level": level}
        return jsonify(call(do()))

    return app