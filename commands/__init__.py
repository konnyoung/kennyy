from __future__ import annotations

from typing import Iterable

import discord
import wavelink

__all__ = ["iter_wavelink_nodes", "resolve_wavelink_player", "player_is_ready"]


def iter_wavelink_nodes() -> Iterable[wavelink.Node]:
	"""Itera sobre todos os nós configurados da pool do Wavelink."""
	seen: set[int] = set()

	pool_nodes = getattr(wavelink.Pool, "nodes", None)
	if isinstance(pool_nodes, dict):
		for node in pool_nodes.values():
			node_id = id(node)
			if node_id in seen:
				continue
			seen.add(node_id)
			yield node

	node_pool = getattr(wavelink, "NodePool", None)
	for attr in ("nodes", "_nodes"):
		nodes_dict = getattr(node_pool, attr, None) if node_pool else None
		if not isinstance(nodes_dict, dict):
			continue
		for node in nodes_dict.values():
			node_id = id(node)
			if node_id in seen:
				continue
			seen.add(node_id)
			yield node


def resolve_wavelink_player(
	bot: discord.Client,
	guild: discord.Guild | None,
) -> wavelink.Player | None:
	"""Tenta localizar o player ativo para o servidor informado."""
	if guild is None:
		return None

	voice_client = guild.voice_client
	if isinstance(voice_client, wavelink.Player):
		return voice_client

	for vc in getattr(bot, "voice_clients", []):
		if isinstance(vc, wavelink.Player) and getattr(vc.guild, "id", None) == guild.id:
			return vc

	for node in iter_wavelink_nodes():
		try:
			candidate = node.get_player(guild.id)
		except Exception:
			continue

		if isinstance(candidate, wavelink.Player):
			return candidate

	return None


def player_is_ready(player: wavelink.Player | None) -> bool:
	"""Verifica se o player está conectado ou tem contexto suficiente para uso."""
	if player is None:
		return False

	connected = getattr(player, "connected", None)
	if connected in (True, None):
		return True

	if connected is False:
		if getattr(player, "channel", None) is not None:
			return True
		if getattr(player, "current", None) is not None:
			return True

	return False
