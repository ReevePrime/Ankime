"""The subtitle delay, remembered per show (the video's folder, by name) and synced through
the collection: each computer records its own changes, the newest change of a show's delay
winning (store.SyncedMap)."""
from pathlib import Path

from aqt.qt import QLabel
from aqt.utils import tooltip

from ..core import config
from ..core.store import USER_FILES, JsonFile, SyncedMap, SyncedValue
from ..subs import subtitles

OLD_SUB_OFFSETS_FILE = USER_FILES / "sub_offsets.json"
SUB_OFFSETS_KEY = "ankime_sub_offsets"   # one entry for every computer, before SyncedMap
SUB_OFFSETS_PREFIX = "ankime_sub_delay"
SYNC_REACTION = 0.2  # seconds between hearing a line start and pressing the sync key


class SubDelayMixin:
    def _init_sub_delay(self):
        self._offsets_store = SyncedMap(SUB_OFFSETS_PREFIX, legacy=self._legacy_sub_offsets)
        self._sub_offsets = self._load_sub_offsets()  # show folder -> subtitle delay (s)

    @staticmethod
    def _legacy_sub_offsets():
        data = SyncedValue(SUB_OFFSETS_KEY).load()
        if data is None:  # older still: a file on this computer
            data = JsonFile(OLD_SUB_OFFSETS_FILE).load({})
        try:
            # the oldest versions keyed them by the folder's full path
            return {subtitles.delay_key(k): float(v) for k, v in data.items()}
        except (AttributeError, TypeError, ValueError):
            return {}

    def _load_sub_offsets(self):
        offsets = {}
        for key, value in self._offsets_store.load().items():
            try:
                if float(value):
                    offsets[key] = float(value)
            except (TypeError, ValueError):
                pass
        return offsets

    def _reload_sub_offsets(self):
        """Read the delays again (a sync may have brought another computer's), and apply
        the show's if it changed."""
        self._sub_offsets = self._load_sub_offsets()
        if not self._has_video():
            return
        delay = self._sub_offsets.get(self._show_key(self._video), 0.0)
        if abs(delay - self.player.sub_delay) > 1e-6:
            self._apply_saved_sub_delay(self._video)
            self._sub_delay_applied()

    def _sub_delay_applied(self):
        """The subtitles moved in time: re-read the line on screen and the lines' times."""
        self.session.line = None
        self._refresh_transcript_lines()
        self._update_seek_marks()

    def _add_sub_delay_controls(self, bar):
        self.sub_delay_label = QLabel()
        self._update_sub_delay_label(0.0)
        self.sub_delay_label.setToolTip("Subtitle delay, remembered for every video in this "
                                        "folder (by its name, so it carries over through sync)")
        bar.addWidget(self._action_button("Subs −", "sub_earlier"))
        bar.addWidget(self.sub_delay_label)
        bar.addWidget(self._action_button("Subs +", "sub_later"))

    @staticmethod
    def _show_key(path):
        return subtitles.delay_key(Path(path).parent)

    def _apply_saved_sub_delay(self, video):
        """Opening `video`: use its show's delay. The mpv property survives across files, so
        it is always set."""
        delay = self._sub_offsets.get(self._show_key(video), 0.0)
        self.player.sub_delay = delay
        self._update_sub_delay_label(delay)

    def _update_sub_delay_label(self, delay):
        self.sub_delay_label.setText(f"Subs {delay:+.2f}s")

    def _set_sub_delay(self, delay):
        if not self._has_video():
            tooltip("Open a video first")
            return
        delay = round(delay, 3)
        self.player.sub_delay = delay
        key = self._show_key(self._video)
        if delay:
            self._sub_offsets[key] = delay
        else:
            self._sub_offsets.pop(key, None)
        self._offsets_store.set_many({key: delay})  # 0.0 too: the reset syncs as well
        self._update_sub_delay_label(delay)
        self._sub_delay_applied()
        tooltip(f"Subtitle delay: {delay:+.2f}s")

    def _nudge_sub_delay(self, direction):
        if self._has_video():
            step = config.get()["sub_delay_step"]
            self._set_sub_delay(self.player.sub_delay + direction * step)
        else:
            tooltip("Open a video first")

    def sub_earlier(self):
        self._nudge_sub_delay(-1)

    def sub_later(self):
        self._nudge_sub_delay(1)

    def sub_reset(self):
        self._set_sub_delay(0.0)

    # --- syncing to a line you hear --------------------------------------------

    def _sync_now(self):
        """When the line you hear started: now, less the time it took to press the key while
        playing (paused, the playhead is where you put it)."""
        now = self.player.time_pos or 0.0
        return now if self.player.paused else max(0.0, now - SYNC_REACTION)

    def sync_line(self, index=None):
        """Press as a line starts: the subtitle delay is set so that the line nearest to now
        (or the episode's line `index`, from the transcript) starts now. Quicker than
        nudging subtitles that are seconds off; the nudge keys then fine-tune it."""
        if not self._has_video():
            tooltip("Open a video first")
            return
        if not self.session.events:
            tooltip("Still analysing the subtitles...")
            return
        delay = self.session.sync_delay(self._sync_now(), self.player.sub_delay, index)
        self._set_sub_delay(delay)

    def sync_to_event(self, index):
        if 0 <= index < len(self.session.events):
            self.sync_line(index)
