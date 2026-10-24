"""Hotkeys from the config. Stored in Qt notation ("Ctrl+N"); mpv needs its own spelling."""
from ..core import config

# action -> label, in the order the settings list them. The action name is also the
# PlayerWindow method it triggers. The default keys are in config.json.
ACTIONS = {
    "mine_line": "Add sentence card",
    "add_word_card": "Add card for selected word",
    "undo_last_card": "Undo (last card or mark known)",
    "edit_last_card": "Edit the last card added",
    "copy_line": "Copy the line (and the lines the card takes)",
    "include_previous": "Card: include previous line",
    "include_next": "Card: include next line",
    "reset_range": "Card: only the line on screen",
    "mark_known": "Mark selected word known",
    "mark_line_known": "Mark every new word on this line known",
    "play_word_audio": "Play the selected word's audio",
    "word_previous": "Select previous word",
    "word_next": "Select next word",
    "show_definition": "Show / hide the selected word's definition",
    "popup_scroll_up": "Scroll the definition up",
    "popup_scroll_down": "Scroll the definition down",
    "replay_line": "Replay line",
    "loop_line": "Loop line on / off",
    "previous_line": "Previous line",
    "next_line": "Next line",
    "toggle_transcript": "Show / hide transcript",
    "cycle_translation": "Translation: hidden / blurred / shown",
    "next_i1": "Skip to next i+1 line",
    "cycle_line_filter": "Cycle line filter (all / skip known / i+1 only / i+1 jump)",
    "search_library": "Search your library (for the selected word)",
    "toggle_pause": "Play / pause",
    "play": "Play",
    "pause": "Pause",
    "stop": "Stop",
    "rewind": "Rewind",
    "forward": "Forward",
    "speed_down": "Slower",
    "speed_up": "Faster",
    "speed_reset": "Normal speed",
    "volume_down": "Volume down",
    "volume_up": "Volume up",
    "mute": "Mute on / off",
    "toggle_fullscreen": "Fullscreen on / off",
    "sub_earlier": "Subtitles earlier",
    "sub_later": "Subtitles later",
    "sub_reset": "Reset subtitle delay",
    "sync_line": "Subtitles: the nearest line starts now",
    "previous_video": "Previous video",
    "next_video": "Next video",
}

# Qt key names that mpv spells differently.
_MPV_KEYS = {"return": "ENTER", "enter": "KP_ENTER", "esc": "ESC", "del": "DEL",
             "ins": "INS", "pgup": "PGUP", "pgdown": "PGDWN", "backspace": "BS",
             "space": "SPACE", "tab": "TAB"}
# Characters mpv can't take literally ("#" starts a comment in its key bindings).
_MPV_CHARS = {"#": "SHARP"}
_MODIFIERS = {"ctrl", "alt", "shift", "meta"}


def get(cfg=None):
    """action -> Qt key string ("" = unbound)."""
    keys = (cfg or config.get())["hotkeys"]
    return {action: keys.get(action, "") for action in ACTIONS}


def to_mpv(qt_key):
    """"Ctrl+Shift+N" -> "ctrl+N"; "Ctrl+Return" -> "ctrl+ENTER"; "Ctrl++" -> "ctrl++"."""
    head, _, key = qt_key.rpartition("+")
    if not key:  # the key is "+" itself: "+" or "Ctrl++"
        head, key = head.removesuffix("+"), "+"
    mods = [m.lower() for m in head.split("+")] if head else []
    if len(key) == 1:
        # mpv has no "shift+n": a shifted letter is just the capital
        key = key.upper() if "shift" in mods else key.lower()
        key = _MPV_CHARS.get(key, key)
        mods = [m for m in mods if m != "shift"]
    else:
        key = _MPV_KEYS.get(key.lower(), key.upper() if key[0] != "F" else key)
    return "+".join([m for m in mods if m in _MODIFIERS] + [key])
