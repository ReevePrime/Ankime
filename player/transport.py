"""Playback: the seek bar, the transport buttons, line navigation and the line loop, speed
and volume, the audio track, and the playback clock (watch time, resume positions)."""
import logging
import time

from aqt.operations import QueryOp
from aqt.qt import (QColor, QComboBox, QHBoxLayout, QLabel, QPainter, QSlider, QStyle,
                    QStyleOptionSlider, Qt, QTimer)
from aqt.utils import tooltip

from ..core import config, immersion, state
from ..core.timefmt import fmt_time
from ..dialogs.immersion import LogDialog
from ..language import progress
from . import transcript
from .mpv_controller import MAX_VOLUME

log = logging.getLogger(__name__)

LOG_FLUSH_MS = 5 * 60 * 1000
LOOP_PAD = 0.1  # seconds of slack around a looped line
SPEEDS = (0.5, 0.75, 0.9, 1.0, 1.1, 1.25, 1.5, 2.0)
VOLUME_STEP = 5


def step_speed(current, direction):
    """The speed in SPEEDS after (direction +1) or before (-1) `current`, which may be
    between two of them; `current` itself at either end."""
    if direction > 0:
        return next((s for s in SPEEDS if s > current + 1e-6), current)
    return next((s for s in reversed(SPEEDS) if s < current - 1e-6), current)


def fmt_speed(speed):
    return f"{speed:g}×"


# Ticks under the seek bar: i+1 lines in the transcript's green, the cards added this session
# in the purple of the card-range badge.
MARK_COLORS = {"i+1": transcript.I1_COLORS, "mined": {False: "#7048e8", True: "#b197fc"}}
MARK_HEIGHT = 4  # px


class SeekSlider(QSlider):
    """A slider that jumps to where it is clicked instead of paging, with ticks under it
    for the lines worth finding (see set_marks)."""

    _marks = ()  # (slider value, kind in MARK_COLORS)

    def set_marks(self, marks):
        self._marks = list(marks)
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        if not self._marks or self.maximum() <= 0:
            return
        opt = QStyleOptionSlider()
        self.initStyleOption(opt)
        handle = self.style().pixelMetric(QStyle.PixelMetric.PM_SliderLength, opt, self)
        night = transcript._night_mode()
        p = QPainter(self)
        for value, kind in self._marks:
            x = handle // 2 + QStyle.sliderPositionFromValue(
                self.minimum(), self.maximum(), int(value), self.width() - handle)
            p.fillRect(x, self.height() - MARK_HEIGHT, 2, MARK_HEIGHT,
                       QColor(MARK_COLORS[kind][night]))
        p.end()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.maximum() > 0:
            # position() is Qt6; the Qt5 builds of Anki only have pos()
            x = event.position().x() if hasattr(event, "position") else event.pos().x()
            self.setValue(QStyle.sliderValueFromPosition(
                self.minimum(), self.maximum(), int(x), self.width()))
        super().mousePressEvent(event)


class TransportMixin:
    def _init_transport(self):
        self._loop = None          # (start, end) of the looped line
        self._loop_follow = False  # move the loop to the next line that shows
        self._tick_clock = None    # monotonic time of the last tick, for the immersion log
        self.log = immersion.ImmersionLog()
        # Started once the player exists (_start_timers). The tick runs only while
        # playing: seek bar, the i+1 pause and jump, watch time.
        self.tick_timer = QTimer(self)
        self.tick_timer.timeout.connect(self._tick)
        self.tick_timer.setInterval(100)
        self.position_timer = QTimer(self)
        self.position_timer.timeout.connect(self._remember_position)
        self.log_timer = QTimer(self)
        self.log_timer.timeout.connect(self.log.flush)

    def _start_timers(self):
        self.position_timer.start(5000)
        self.log_timer.start(LOG_FLUSH_MS)

    def _stop_timers(self):
        self.tick_timer.stop()
        self.position_timer.stop()
        self.log_timer.stop()

    # --- seek bar --------------------------------------------------------------

    def _seek_bar(self):
        self.seek_slider = SeekSlider(Qt.Orientation.Horizontal)
        self.seek_slider.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # keep the arrows for hotkeys
        self.seek_slider.sliderMoved.connect(lambda v: self._show_time(v / 10))
        self.seek_slider.sliderReleased.connect(self._slider_seek)
        self.seek_slider.setMinimumHeight(24)  # room for the ticks
        self.seek_slider.setToolTip("Ticks: green for i+1 lines, purple for the cards you "
                                    "added this session")
        self.time_label = QLabel("--:-- / --:--")
        bar = QHBoxLayout()
        bar.addWidget(self.seek_slider, 1)
        bar.addWidget(self.time_label)
        return bar

    def _slider_seek(self):
        if self._has_video():
            self.player.seek(self.seek_slider.value() / 10)

    def _show_time(self, t):
        duration = self.player.duration if self.player else None
        total = fmt_time(duration) if duration else "--:--"
        self.time_label.setText(f"{fmt_time(t or 0)} / {total}")

    def _update_seek_marks(self):
        """Tick the i+1 lines and the cards added from this video on the seek bar."""
        duration = self.player.duration if self._has_video() else None
        if not duration:
            self.seek_slider.set_marks([])
            return
        delay = self.player.sub_delay
        marks = [((ev.start + delay) * 10, "i+1") for ev in self.session.events
                 if self._line_kind(ev.text) == "i+1"]
        marks += [(start * 10, "mined") for video, start in self._mined.values()
                  if video == self._video]
        self.seek_slider.set_marks(marks)

    def _update_seek_bar(self, t):
        if self.player is None or self.seek_slider.isSliderDown():
            return
        duration = self.player.duration or 0
        self.seek_slider.setMaximum(int(duration * 10))
        self.seek_slider.setValue(int((t or 0) * 10))
        self._show_time(t)

    # --- transport bar -----------------------------------------------------------

    def _transport_bar(self):
        """Mini player controls above the subtitle panel."""
        bar = QHBoxLayout()
        bar.addStretch(1)
        icons = QStyle.StandardPixmap
        # standard media icons keep the row short; each button's name and tooltip say what
        # it does
        for text, action, icon in (
                ("", "previous_video", icons.SP_MediaSkipBackward),
                (lambda: f"{self._seek_seconds()}s", "rewind", icons.SP_MediaSeekBackward),
                ("◀ Line", "previous_line", None), ("", "play", icons.SP_MediaPlay),
                ("", "pause", icons.SP_MediaPause), ("", "stop", icons.SP_MediaStop),
                ("Line ▶", "next_line", None),
                (lambda: f"{self._seek_seconds()}s", "forward", icons.SP_MediaSeekForward),
                ("", "next_video", icons.SP_MediaSkipForward)):
            bar.addWidget(self._action_button(text, action, icon=icon))
        self.loop_btn = self._action_button("🔁 Loop", "loop_line")
        self.loop_btn.setCheckable(True)
        bar.addWidget(self.loop_btn)
        bar.addSpacing(12)
        self._add_sub_delay_controls(bar)
        bar.addSpacing(12)
        self.audio_box = QComboBox()
        self.audio_box.setToolTip("Audio track (the cards use it too)")
        self.audio_box.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.audio_box.activated.connect(self._choose_audio)
        self.audio_box.hide()  # only shown when the video has several audio tracks
        bar.addWidget(self.audio_box)
        bar.addSpacing(12)
        self._add_speed_and_volume(bar)
        bar.addStretch(1)
        return bar

    # --- speed and volume ----------------------------------------------------------

    def _add_speed_and_volume(self, bar):
        self.speed_box = QComboBox()
        self.speed_box.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.speed_box.setToolTip("Playback speed (the pitch stays the same)")
        for speed in SPEEDS:
            self.speed_box.addItem(fmt_speed(speed), speed)
        self.speed_box.setCurrentIndex(SPEEDS.index(1.0))
        self.speed_box.activated.connect(
            lambda i: self._set_speed(self.speed_box.itemData(i)))
        bar.addWidget(self.speed_box)
        self.mute_btn = self._action_button(
            lambda: "🔇" if self.player is not None and self.player.muted else "🔊", "mute")
        self.mute_btn.setCheckable(True)
        bar.addWidget(self.mute_btn)
        self.volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.volume_slider.setRange(0, int(MAX_VOLUME))
        self.volume_slider.setFixedWidth(100)
        self.volume_slider.valueChanged.connect(self._volume_moved)
        bar.addWidget(self.volume_slider)

    def _restore_volume_and_speed(self):
        """The player was created: start at the volume and speed of last session."""
        self.player.volume = state.get("volume")
        self.player.speed = state.get("speed")

    def _save_player_settings(self):
        """On close: remember the volume, speed and panel widths for next time."""
        self._remember_panel_widths()
        state.save(volume=round(self.player.volume), speed=self.player.speed,
                   panel_widths=dict(self._panel_widths))

    def _set_speed(self, speed):
        if self.player is not None:
            self.player.speed = speed

    def _step_speed(self, direction):
        if self.player is not None:
            speed = step_speed(self.player.speed, direction)
            self.player.speed = speed
            tooltip(f"Speed {fmt_speed(speed)}")

    def speed_down(self):
        self._step_speed(-1)

    def speed_up(self):
        self._step_speed(1)

    def speed_reset(self):
        if self.player is not None:
            self.player.speed = 1.0
            tooltip("Speed 1×")

    def _on_speed_changed(self, speed):
        i = self.speed_box.findData(speed)
        if i < 0:  # set elsewhere (the saved setting): show it anyway
            self.speed_box.addItem(fmt_speed(speed), speed)
            i = self.speed_box.count() - 1
        self.speed_box.setCurrentIndex(i)

    def _volume_moved(self, value):
        if self.player is not None and abs(self.player.volume - value) >= 1:
            self.player.volume = value

    def _step_volume(self, direction):
        if self.player is not None:
            self.player.volume = self.player.volume + direction * VOLUME_STEP
            self.player.muted = False
            tooltip(f"Volume {round(self.player.volume)}%")

    def volume_down(self):
        self._step_volume(-1)

    def volume_up(self):
        self._step_volume(1)

    def mute(self):
        if self.player is not None:
            self.player.muted = not self.player.muted

    def _on_volume_changed(self, volume, muted):
        self.volume_slider.blockSignals(True)
        self.volume_slider.setValue(round(volume))
        self.volume_slider.blockSignals(False)
        self.volume_slider.setToolTip(f"Volume {round(volume)}%")
        self.mute_btn.setChecked(muted)
        self.mute_btn.setText("🔇" if muted else "🔊")  # the label's function agrees

    # --- audio track -------------------------------------------------------------

    def _populate_audio_tracks(self, tracks):
        self.audio_box.clear()
        audio = [t for t in tracks if t.get("type") == "audio"]
        for t in audio:
            label = " · ".join(str(x) for x in (t.get("lang"), t.get("title")) if x)
            self.audio_box.addItem(label or f"Track {t['id']}", t["id"])
            if t.get("selected"):
                self.audio_box.setCurrentIndex(self.audio_box.count() - 1)
        self.audio_box.setVisible(len(audio) > 1)

    def _choose_audio(self, index):
        if self.player is not None:
            self.player.select("aid", self.audio_box.itemData(index))

    def _audio_stream(self):
        """ffmpeg stream index of the audio track playing now, for cutting card audio."""
        for t in self.player.track_list:
            if t.get("type") == "audio" and t.get("selected"):
                return None if t.get("external") else t.get("ff-index")
        return None

    # --- playback clock ----------------------------------------------------------

    def _remember_position(self):
        """Note how far into the current video we are and save the playlist (only written
        when something changed)."""
        if self._has_video():
            t, duration = self.player.time_pos, self.player.duration
            if t is not None and self.player.path == self._video:  # else still loading
                self.playlist.remember(self._video, t, duration)
        self._save_playlist()

    def _on_pause_changed(self, paused):
        if not paused:
            self._paused_for_popup = False  # playing again: the popup no longer resumes it
        if paused or self.player is None or not self.player.path:
            self.tick_timer.stop()
            self._tick_clock = None
        else:
            self.tick_timer.start()
        if self.player is not None:
            self._update_seek_bar(self.player.time_pos)

    def _tick(self):
        p = self.player
        if p is None:
            return
        t = p.time_pos
        now = time.monotonic()
        if self._tick_clock is not None and t is not None:
            self.log.add_watch(min(now - self._tick_clock, 1.0))  # a stall doesn't count
        self._tick_clock = now
        self._check_line_pause(t)
        self._check_line_jump(t)
        self._update_seek_bar(t)

    def show_log(self):
        """The immersion log, once the known words' history is read from the review log
        (in the background: a big collection has a long one)."""
        self.log.flush()
        goal = config.get()["daily_goal_minutes"]
        inputs = self.knowledge.history_inputs()
        today, rollover = self.log.day(), self.log.rollover()

        def failed(error):
            log.warning("couldn't work out the known words' history: %s", error)
            LogDialog(self, self.log, goal).exec()

        op = QueryOp(parent=self,
                     op=lambda col: progress.known_history(col, inputs, today, rollover),
                     success=lambda known: LogDialog(self, self.log, goal, known).exec())
        op.failure(failed).with_progress("Reading your review history...").run_in_background()

    # --- lines: navigation and loop ---------------------------------------------

    def _seek_to_line(self, start):
        self.player.seek(start + 0.02)

    def _go_line(self, step):
        if not self._has_video():
            return
        target = self.session.neighbour_time(self.player.time_pos or 0, self.player.sub_delay,
                                             step)
        if self._loop is not None:  # the loop moves along to the new line
            self._clear_loop(keep_button=True)
            self._loop_follow = True
        if target is not None:
            self._seek_to_line(target)
        elif not self.session.events:
            self.player.mpv.command("sub-seek", step)  # episode not analysed yet: ask mpv

    def previous_line(self):
        self._go_line(-1)

    def next_line(self):
        self._go_line(1)

    def replay_line(self):
        span = self.session.span(self.player.sub_delay) if self._has_video() else None
        if span is None:
            return
        self.player.seek(span.start)
        self._arm_line_pause(self._line_kind(self.session.line.text), replay=True)

    def loop_line(self):
        if self._loop is not None or self._loop_follow:
            self._clear_loop()
            tooltip("Loop off")
        elif self._start_loop():
            tooltip("Looping this line")
        else:
            tooltip("No subtitle on screen")
        self.loop_btn.setChecked(self._loop is not None)

    def _start_loop(self):
        span = self.session.span(self.player.sub_delay)
        if span is None:
            return False
        _text, start, end = span
        self._loop = (max(0, start - LOOP_PAD), end + LOOP_PAD)
        self.player.loop(*self._loop)
        self.loop_btn.setChecked(True)
        return True

    def _clear_loop(self, keep_button=False):
        self._loop, self._loop_follow = None, False
        if self.player is not None:
            self.player.clear_loop()
        if not keep_button:
            self.loop_btn.setChecked(False)

    # --- play, pause, seek -------------------------------------------------------

    @staticmethod
    def _seek_seconds():
        return config.get()["seek_seconds"]

    def toggle_pause(self):
        if self.player is not None:
            self.player.paused = not self.player.paused

    def play(self):
        if self.player is not None and not self.player.path:
            self.play_index(max(self.playlist.index, 0))  # nothing loaded yet: resume the saved spot
        elif self.player is not None:
            self.player.paused = False

    def pause(self):
        if self.player is not None:
            self.player.paused = True

    def stop(self):
        """Pause and go back to the start (a real mpv stop would unload the video)."""
        if self._has_video():
            self.player.paused = True
            self.player.seek(0)

    def rewind(self):
        if self._has_video():
            self.player.seek_by(-self._seek_seconds())

    def forward(self):
        if self._has_video():
            self.player.seek_by(self._seek_seconds())
