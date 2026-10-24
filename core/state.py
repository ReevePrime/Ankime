"""What the player remembers between sessions on this computer (the volume, the speed, the
panel widths, the last folder a video was opened from...): state, not settings, so it lives in
user_files/state.json rather than in the add-on's config. Main thread only."""
from .store import USER_FILES, JsonFile

STATE_FILE = USER_FILES / "state.json"
DEFAULTS = {
    "volume": 100,
    "speed": 1.0,
    "panel_widths": {"playlist": 260, "transcript": 320},
    "last_video_dir": "",
    "note_type_offered": False,   # the setup wizard was offered (a new install)
    "new_words_sort": "count",    # the transcript's new words: "count" | "frequency"
    "align_downloads": True,      # align Jimaku downloads to the audio (if an aligner is set)
}

_file = JsonFile(STATE_FILE)
_state = None


def _load():
    global _state
    if _state is None:
        data = _file.load({})
        _state = data if isinstance(data, dict) else {}
    return _state


def get(key):
    value = _load().get(key)
    default = DEFAULTS[key]
    if value is None or type(value) is not type(default) and not (
            isinstance(default, float) and isinstance(value, int)):
        return default  # missing, or of the wrong type (a hand-edited file)
    return {**default, **value} if isinstance(default, dict) else value


def save(**values):
    for key in values:
        if key not in DEFAULTS:
            raise KeyError(key)
    _load().update(values)
    _file.save(_state)


def migrate(cfg):
    """Older versions kept this state in the add-on's config: move what `cfg` (the user's
    raw config) has of it here, unless this file already has it. Returns `cfg` without it,
    and whether anything was in it."""
    found = {key: cfg[key] for key in DEFAULTS if key in cfg}
    if not found:
        return cfg, False
    state = _load()
    for key, value in found.items():
        state.setdefault(key, value)
    _file.save(state)
    return {k: v for k, v in cfg.items() if k not in DEFAULTS}, True
