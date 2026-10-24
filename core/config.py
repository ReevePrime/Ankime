"""The add-on's settings. One cached copy that everything reads, background threads included,
without going to disk; saving the settings refreshes it and tells whoever listens, so changes
apply at once. The defaults are config.json (what Anki's "Restore Defaults" goes back to).

Only the settings that differ from the defaults are written: the others follow config.json,
so a default changed in a later version reaches everyone who hadn't changed that setting.
What the player remembers between sessions (volume, panel widths...) isn't a setting: see
state.py."""
import json
import logging
from pathlib import Path
from types import MappingProxyType

ADDON = __name__.split(".")[0]   # the add-on's package: Anki keeps its config under that name
DEFAULTS = json.loads((Path(__file__).parent.parent / "config.json").read_text(encoding="utf-8"))

log = logging.getLogger(__name__)
_current = None   # the merged settings, read-only
_listeners = []   # fn(old, new), called on the main thread after a change


def merge(user):
    """`user` over DEFAULTS. Tables (hotkeys) are merged key by key, so an action added in a
    later version gets its default key."""
    cfg = dict(DEFAULTS)
    for key, value in (user or {}).items():
        default = DEFAULTS.get(key)
        if isinstance(default, dict) and isinstance(value, dict):
            value = {**default, **value}
        cfg[key] = value
    return cfg


def strip(cfg):
    """What of `cfg` differs from DEFAULTS, key by key in tables: what is written. Keys that
    aren't settings (any more) are dropped."""
    out = {}
    for key, value in cfg.items():
        if key not in DEFAULTS:
            continue
        default = DEFAULTS[key]
        if isinstance(default, dict) and isinstance(value, dict):
            value = {k: v for k, v in value.items() if k in default and default[k] != v}
            if not value:
                continue
        elif value == default:
            continue
        out[key] = value
    return out


def migrate(user):
    """`user` (the user's raw config) with the settings of older versions in their new form:
    learning_style ("color", "underline" or "dotted": how learning words stood out besides
    their colour) is learning_word_style's underline now."""
    user = dict(user or {})
    old = user.pop("learning_style", None)
    if old in ("underline", "dotted"):
        style = user.get("learning_word_style")
        style = dict(style) if isinstance(style, dict) else {}
        style.setdefault("underline", "solid" if old == "underline" else "dotted")
        user["learning_word_style"] = style
    return user


def _manager():
    from aqt import mw
    return mw.addonManager


def _stored():
    """The user's config as written in meta.json, or None if Anki can't say."""
    mgr = _manager()
    try:
        stored = mgr.addonMeta(mgr.addonFromModule(ADDON)).get("config")
    except Exception:
        return None
    return stored if isinstance(stored, dict) else None


def _load():
    """The settings from Anki, once: older versions wrote every setting (and the player's
    state) into them, which is tidied up here."""
    from . import state
    user, moved = state.migrate(dict(_manager().getConfig(ADDON) or {}))
    tidy = strip(migrate(user))
    if moved or tidy != _stored():
        try:
            _manager().writeConfig(ADDON, tidy)
        except Exception as e:
            log.warning("couldn't tidy the config: %s", e)
    return tidy


def get():
    """The settings, read-only. Safe from any thread."""
    global _current
    if _current is None:
        _current = MappingProxyType(merge(_load()))
    return _current


def _changed(user):
    global _current
    old, _current = get(), MappingProxyType(merge(user))
    for fn in list(_listeners):
        fn(old, _current)


def write(cfg):
    """Save a whole config (main thread) and apply it."""
    cfg = strip(cfg)
    _manager().writeConfig(ADDON, cfg)
    _changed(cfg)


def save(**values):
    """Change some settings, keeping the rest."""
    write({**get(), **values})


def _edited(user):
    """Saved in Anki's JSON config editor, which writes every setting: write only what
    differs from the defaults, and apply it."""
    write(migrate(user))


def on_change(fn):
    _listeners.append(fn)


def remove_listener(fn):
    if fn in _listeners:
        _listeners.remove(fn)


def install():
    """At startup: load the settings, and follow edits made in Anki's JSON config editor."""
    _manager().setConfigUpdatedAction(ADDON, _edited)
    get()
