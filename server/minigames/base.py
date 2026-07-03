"""
base.py — the pluggable mini-game contract.

A mini-game is one self-contained module. The shell (lobby, networking,
scoring, settings UI) never changes when you add one. Implement this class,
register it in minigames/__init__.py, and it shows up in the lobby dropdown
with its own settings panel.

Coordinate space: a fixed 960x540 world. The client letterboxes it to fit.
"""

WORLD_W = 960
WORLD_H = 540

# Settings every game gets for free (bots are driven by the game's bot_input).
BOT_SETTINGS = [
    {"k": "bots", "label": "Bots", "type": "int", "def": 0, "min": 0, "max": 6},
    {"k": "bot_skill", "label": "Bot skill", "type": "choice", "def": "normal",
     "choices": ["easy", "normal", "mean"]},
]


class MiniGame:
    ID = "base"
    NAME = "?"
    TAG = ""                 # one-line pitch shown in the lobby
    CONTROLS = "WASD / arrows to move"
    MIN_PLAYERS = 1

    @classmethod
    def settings_schema(cls):
        """List of setting descriptors, rendered generically by the client:
        {"k","label","type":"bool"|"int"|"choice", "def", ["min","max"], ["choices"]}
        """
        return list(BOT_SETTINGS)

    def __init__(self, roster, settings, rng):
        """roster: list of {"pid","name","bot"} participating this round.
        settings: validated dict (schema keys -> values). rng: random.Random."""
        self.roster = roster
        self.settings = settings
        self.rng = rng

    def setup(self):
        """Return the static arena payload sent once at round start."""
        return {"g": self.ID, "w": WORLD_W, "h": WORLD_H}

    def on_input(self, pid, keys):
        """keys: {"u","d","l","r","a"} booleans. Latest key-state wins."""

    def bot_input(self, pid, skill):
        """Return a keys dict for a bot this tick (called by the shell)."""
        return {"u": False, "d": False, "l": False, "r": False, "a": False}

    def tick(self, dt, events):
        """Advance one fixed step. Append wire-ready fx events to `events`."""

    def snapshot(self, full=False):
        """Dynamic state broadcast at snapshot rate. `full` for late joiners."""
        return {}

    def drop_player(self, pid):
        """Player kicked/left for good mid-round: give them last place."""

    def status(self):
        """One-line live status shown in the host terminal dashboard."""
        return ""

    def is_over(self):
        return True

    def placements(self):
        """Return [(pid, place), ...] — competition ranking, ties share a place."""
        return [(p["pid"], 1) for p in self.roster]
