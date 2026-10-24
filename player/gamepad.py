"""The gamepad: its buttons run the player's actions like the hotkeys do (see
controls/gamepad.py for the bindings and how the controllers are read). Only while the player
(or its popup, or the fullscreen subtitles) is the active window: a button pressed in another
program, or in a dialog over the player, does nothing here."""
import logging
import time

from aqt import mw
from aqt.qt import QApplication, QTimer
from aqt.utils import tooltip

from ..controls import gamepad
from ..core import config

log = logging.getLogger(__name__)

POLL_MS = 16    # reads while a controller is connected: 60 a second
IDLE_MS = 500   # while there is none (gamepad.Gamepads looks for one less often still)


class GamepadMixin:
    def _init_gamepad(self):
        self._pads = None    # gamepad.Gamepads, while the setting is on
        self._pad_mapper = None
        self._pad_names = []
        self._pad_timer = QTimer(self)
        self._pad_timer.timeout.connect(self._poll_gamepad)

    def _start_gamepad(self):
        """Read the controllers if the settings say so, with the bindings they give."""
        cfg = config.get()
        if not cfg["gamepad"]:
            self._stop_gamepad()
            return
        if self._pads is None:
            self._pads = gamepad.Gamepads(run_in_background=mw.taskman.run_in_background)
        self._pad_mapper = gamepad.Mapper(gamepad.get(cfg))
        self._pad_timer.start(POLL_MS if self._pads.pads else IDLE_MS)

    def _stop_gamepad(self):
        self._pad_timer.stop()
        if self._pads is not None:
            self._pads.close()
        self._pads, self._pad_mapper, self._pad_names = None, None, []

    def _gamepad_config_changed(self, old, new):
        if old["gamepad"] != new["gamepad"] or old["gamepad_buttons"] != new["gamepad_buttons"]:
            self._start_gamepad()
            self._refresh_popup()  # its buttons show the gamepad's

    def _poll_gamepad(self):
        now = time.monotonic()
        try:
            held = self._pads.poll(now)
        except Exception:
            log.exception("couldn't read the gamepad")
            self._stop_gamepad()
            return
        names = self._pads.names()
        if names != self._pad_names:
            self._gamepad_connected(names)
        if not self._has_gamepad_focus():
            self._pad_mapper.reset(held)
            return
        for action in self._pad_mapper.update(held, now):
            getattr(self, action)()
            if self._pads is None:  # the action closed the player, or turned this off
                return

    def _gamepad_connected(self, names):
        """A controller was connected or disconnected: read it faster or slower, and say so
        (only while the player is in front: not every time Anki starts with one)."""
        gone = [name for name in self._pad_names if name not in names]
        added = [name for name in names if name not in self._pad_names]
        self._pad_names = names
        self._pad_timer.setInterval(POLL_MS if names else IDLE_MS)
        self._refresh_popup()
        if self.isActiveWindow():
            if added:
                tooltip(f"Gamepad connected: {', '.join(added)}", parent=self)
            elif gone:
                tooltip(f"Gamepad disconnected: {', '.join(gone)}", parent=self)

    def _has_gamepad_focus(self):
        window = QApplication.activeWindow()
        return window is not None and window in (self, self.popup, self.overlay)

    def _gamepad_labels(self):
        """action -> the gamepad binding as the controller connected names it, or {} if
        none is connected."""
        if not self._pads or not self._pads.pads:
            return {}
        kind = self._pads.kind
        return {action: gamepad.combo_label(combo, kind)
                for action, combo in gamepad.get().items() if combo}
