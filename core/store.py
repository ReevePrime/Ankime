"""Persistence. JsonFile keeps machine-specific state under user_files/ (playlist, resume
positions, tokenization cache); SyncedValue keeps state in the collection's config, so it
syncs through AnkiWeb, and SyncedMap / device_entries build on it so that two computers
editing the same state between syncs don't overwrite each other (known words, subtitle
delays, immersion log)."""
import json
import logging
import os
import tempfile
import time
import uuid
from pathlib import Path

USER_FILES = Path(__file__).parent.parent / "user_files"   # Anki keeps it on updates
DEVICE_FILE = USER_FILES / "device.json"

log = logging.getLogger(__name__)
_device = None


def _dump(data):
    return json.dumps(data, ensure_ascii=False, sort_keys=True, indent=0)


def main_col():
    from aqt import mw
    return mw.col


class JsonFile:
    """A JSON document on disk. Writes are atomic (temp file, then replace) and skipped when
    the data is what was last loaded or saved."""

    def __init__(self, path):
        self.path = Path(path)
        self._last = None  # serialized form of what is on disk

    def load(self, default=None):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default
        self._last = _dump(data)
        return data

    def save(self, data):
        """True if the file was written. Errors are swallowed: losing a playlist or a cache
        is not worth an error dialog."""
        text = _dump(data)
        if text == self._last:
            return False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=self.path.name, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(text)
                os.replace(tmp, self.path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except OSError as e:
            log.warning("couldn't write %s: %s", self.path, e)
            return False
        self._last = text
        return True


class SyncedValue:
    """A JSON value in the collection's config (main thread only). Saving an unchanged value
    does not touch the collection, so it does not create a sync change either."""

    def __init__(self, key, col=None):
        self.key = key
        self._col = (lambda: col) if col is not None else main_col
        self._last = None

    def load(self, default=None):
        data = self._col().get_config(self.key, None)
        if data is None:
            return default
        self._last = _dump(data)
        return data

    def save(self, data):
        text = _dump(data)
        col = self._col()
        if text == self._last or col is None:  # None: the profile is closing
            return False
        col.set_config(self.key, data)
        self._last = text
        return True


# --- state that several computers edit -----------------------------------------
#
# Anki syncs the collection's config one entry at a time, and the entry changed last wins.
# A single entry edited on two computers between syncs would lose one computer's changes,
# so each computer writes only its own entry, "<prefix>::<device id>", and readers merge
# every computer's entry.

def device_id():
    """This computer's id: random, made once and kept in user_files/ (which doesn't sync)."""
    global _device
    if _device is None:
        f = JsonFile(DEVICE_FILE)
        _device = f.load()
        if not isinstance(_device, str) or not _device:
            _device = uuid.uuid4().hex[:12]
            f.save(_device)
    return _device


def device_entries(prefix, col=None):
    """{device id: value} of every computer's entry under `prefix`, this one's included.
    `col` defaults to Anki's."""
    marker = prefix + "::"
    try:
        entries = (col if col is not None else main_col()).all_config()
    except Exception as e:  # no collection, or an Anki without all_config
        log.warning("couldn't list the collection's config: %s", e)
        return {}
    return {key[len(marker):]: value for key, value in entries.items()
            if key.startswith(marker)}


def _decode_changes(entry):
    """{key: (value, time)} from a computer's entry. Entries group their changes by when
    they were made, {"<time>": {key: value}}, so that the thousands of words of an imported
    word list cost one time stamp, not one each; older versions wrote {key: [value, time]}.
    Both are read, even mixed; anything else is skipped."""
    changes = {}
    if not isinstance(entry, dict):
        return changes
    for name, item in entry.items():
        if isinstance(item, dict):  # a group: name is its time
            try:
                stamp = float(name)
            except ValueError:
                continue
            group = item.items()
        else:  # the older format: name is the key
            try:
                value, stamp = item
                stamp = float(stamp)
            except (TypeError, ValueError):
                continue
            group = [(name, value)]
        for key, value in group:
            if key not in changes or stamp >= changes[key][1]:
                changes[key] = (value, stamp)
    return changes


def _encode_changes(changes):
    """{key: (value, time)} -> {"<time>": {key: value}}, times in whole seconds."""
    groups = {}
    for key, (value, stamp) in changes.items():
        groups.setdefault(str(int(stamp)), {})[key] = value
    return groups


class SyncedMap:
    """{key: value} edited from several computers. Every change is stamped with its time in
    this computer's entry; reading merges the entries, the newest change of each key
    winning. A change of this computer's that another computer has since overridden can
    never win again: it is dropped when the entry is saved, so the entry doesn't keep what
    no longer counts. `legacy` is the value of the single entry older versions kept
    ({key: value}), read as changes made before any other."""

    def __init__(self, prefix, col=None, legacy=None, device=None, clock=time.time):
        self.prefix = prefix
        self._col = (lambda: col) if col is not None else main_col
        self._device = device or device_id()
        self._own = SyncedValue(f"{prefix}::{self._device}", col)
        self._legacy = legacy or (lambda: {})
        self._clock = clock
        self._changes = {}  # this computer's changes: key -> (value, time)
        self._others = {}   # key -> time of the newest change another computer made to it

    def load(self):
        """The merged {key: value}."""
        return {key: value for key, (value, _) in self.load_stamped().items()}

    def load_stamped(self):
        """The merged {key: (value, time of the change that set it)}: 0 for the legacy
        entry's, whose time isn't known."""
        merged = {key: (value, 0.0) for key, value in (self._legacy() or {}).items()}
        self._changes = _decode_changes(self._own.load({}))
        self._others = {}
        entries = device_entries(self.prefix, self._col())
        entries.pop(self._device, None)
        for entry in entries.values():
            for key, (value, stamp) in _decode_changes(entry).items():
                self._others[key] = max(self._others.get(key, stamp), stamp)
                if key not in merged or stamp >= merged[key][1]:
                    merged[key] = (value, stamp)
        for key, (value, stamp) in self._changes.items():
            if key not in merged or stamp >= merged[key][1]:
                merged[key] = (value, stamp)
        return merged

    def set_many(self, values):
        """Record changes ({key: value}), saving this computer's entry once."""
        now = int(self._clock())
        for key, value in values.items():
            self._changes[key] = (value, now)
        self._changes = {key: change for key, change in self._changes.items()
                         if change[1] >= self._others.get(key, change[1])}
        self._own.save(_encode_changes(self._changes))
