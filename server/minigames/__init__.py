"""Mini-game registry. Adding a game = write a module, add one line here."""

from .sumo import SumoRing
from .cycles import LightCycles

GAMES = {SumoRing.ID: SumoRing, LightCycles.ID: LightCycles}

GAME_LIST = [{"id": g.ID, "name": g.NAME, "tag": g.TAG} for g in GAMES.values()]


def default_settings(game_id):
    return {s["k"]: s["def"] for s in GAMES[game_id].settings_schema()}
