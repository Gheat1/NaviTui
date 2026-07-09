"""Player settings — `~/.config/navitui/player.toml`, read once at startup.

Everything has a sane default; the file is optional. A commented template is
written on first run so discovering the knobs never requires the README.
Restart to apply changes (keybinds are baked into the app's bindings table).
"""

from __future__ import annotations

import tomllib
from pathlib import Path

KEYBINDS = {
    # action id            default key(s), comma-separated for aliases
    "play_pause":          "space",
    "next_track":          "n",
    "prev_track":          "b",
    "search":              "slash",
    "shuffle":             "s",
    "repeat":              "r",
    "seek_back":           "left",
    "seek_forward":        "right",
    "seek_back_big":       "shift+left",
    "seek_forward_big":    "shift+right",
    "volume_down":         "minus",
    "volume_up":           "plus,equals_sign",
    "mute":                "m",
    "enqueue":             "a",
    "play_next":           "A",
    "queue_remove":        "x",
    "queue_clear":         "X",
    "queue_move_up":       "ctrl+up",
    "queue_move_down":     "ctrl+down",
    "star":                "f",
    "playlist_add":        "p",
    "lyrics":              "L",
    "share":               "S",
    "go_album":            "e",
    "go_artist":           "E",
    "notifications":       "N",
    "panel_prev":          "h",
    "panel_next":          "l",
    "refresh":             "R",
    "theme_cycle":         "t",
    "theme_pick":          "T",
    "zen":                 "z",
    "help":                "question_mark",
    "quit":                "q",
}

DEFAULTS = {
    "replaygain": "album",        # album | track | no
    "gapless": "weak",            # yes | weak | no
    "notifications": True,        # desktop notification on track change
    "discord_rich_presence": False,
    "discord_app_id": "",         # discord.com/developers/applications
}

_TEMPLATE = """\
# NaviTui player settings — restart the app after editing.

# ReplayGain mode: "album", "track", or "no"
#replaygain = "album"

# Gapless playback: "yes", "weak" (default; gapless when formats match), "no"
#gapless = "weak"

# Desktop notification on track change (toggle at runtime with N)
#notifications = true

# Discord rich presence (needs `pip install pypresence` and an application id
# from discord.com/developers/applications)
#discord_rich_presence = false
#discord_app_id = ""

# Remap any key. Action ids and defaults:
#[keybinds]
{keybinds}
"""


def load(config_dir: Path) -> dict:
    """Defaults overlaid with whatever player.toml sets. Unknown keys are
    ignored so typos can't crash startup."""
    cfg = dict(DEFAULTS)
    cfg["keybinds"] = dict(KEYBINDS)
    try:
        overrides = tomllib.loads((config_dir / "player.toml").read_text())
    except FileNotFoundError:
        return cfg
    except Exception:
        return cfg  # malformed file: run on defaults rather than crash
    for key, value in overrides.items():
        if key == "keybinds" and isinstance(value, dict):
            for action, keys in value.items():
                if action in KEYBINDS and isinstance(keys, str) and keys:
                    cfg["keybinds"][action] = keys
        elif key in DEFAULTS and isinstance(value, type(DEFAULTS[key])):
            cfg[key] = value
    return cfg


def write_template(config_dir: Path) -> None:
    """Drop the commented template next to the credentials, once."""
    path = config_dir / "player.toml"
    if path.exists():
        return
    try:
        config_dir.mkdir(parents=True, exist_ok=True)
        keybind_lines = "\n".join(f'#{a} = "{k}"' for a, k in KEYBINDS.items())
        path.write_text(_TEMPLATE.format(keybinds=keybind_lines))
    except OSError:
        pass
