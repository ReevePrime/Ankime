"""i+1 lines in the player: the badge, the line filter that skips lines (or jumps from i+1
line to i+1 line, episode after episode), the automatic pause and the jump to the next i+1
line. The rules themselves are in language/line_filter.py."""
from aqt.utils import tooltip

from ..core import config
from ..language import line_filter


class LineFilterMixin:
    def _init_line_filter(self):
        self._pause_at = None  # video time at which to pause (end of an i+1 line)
        self._jump_span = None  # (start, end) of the i+1 line "i1_jump" is playing
        # (start, end) of a line you chose to play (in the transcript, in the library): the
        # line filter lets it play, whatever its kind
        self._chosen_span = None

    def _choose_line(self, start, end):
        self._chosen_span = (start, end)

    @staticmethod
    def _around(span, t):
        """Is video time `t` in `span` (or just around it, see line_filter.jump_target)?"""
        return bool(span) and t is not None and (
            span[0] - line_filter.JUMP_LEAD <= t <= span[1] + line_filter.JUMP_TAIL)

    def _line_kind(self, text):
        return self.session.line_kind(self.knowledge, text)

    def _update_badge(self):
        line = self.session.line
        kind = self._line_kind(line.text) if line else None
        self.panel.set_badge("i+1" if kind == "i+1" else "")

    def _next_line_time(self, accept):
        """Start, in video time, of the first line after the playhead whose kind `accept`s."""
        return self.session.next_line_time(self.player.time_pos or 0, self.player.sub_delay,
                                           lambda text: accept(self._line_kind(text)))

    def _on_line_shown(self):
        """A line with its words is on the panel: skip it if the line filter says so,
        otherwise pause on it if it is i+1 and the player is set to."""
        p, line = self.player, self.session.line
        if p is None or not line or not line.text or self._loop is not None:
            return
        kind = self._line_kind(line.text)
        mode = config.get()["line_filter"]
        action = line_filter.on_line_shown(kind, mode, p.paused)
        if action == "skip" and self._around(self._chosen_span, line.start):
            action = "play"
        if action == "play":
            self._arm_line_pause(kind)
        elif action == "skip":
            target = self._next_line_time(lambda k: line_filter.wanted(k, mode))
            if target is not None:
                self._seek_to_line(target)

    def _arm_line_pause(self, kind, replay=False):
        """Set up the automatic pause for an i+1 line (see line_filter.pause_plan)."""
        self._pause_at = None
        line, p = self.session.line, self.player
        plan = line_filter.pause_plan(kind, config.get()["i1_pause"], line.start, line.end,
                                      p.time_pos, p.paused, replay)
        if plan is None:
            return
        how, t = plan
        if how == "at":
            self._pause_at = t
        else:
            p.paused = True
            p.seek(t)

    def _check_line_pause(self, t):
        """On every tick: pause once the armed i+1 line has been heard."""
        if line_filter.reached(self._pause_at, t):
            self._pause_at = None
            self.player.paused = True

    def _i1_spans(self):
        """(start, end) of the episode's i+1 lines, in video time."""
        delay = self.player.sub_delay
        return [(ev.start + delay, ev.end + delay) for ev in self.session.events
                if self._line_kind(ev.text) == "i+1"]

    def _check_line_jump(self, t):
        """On every tick, with the "i1_jump" filter: once playback leaves an i+1 line (or is
        anywhere else), go to the next one; after the last, to the next episode. Not before
        the episode is analysed, nor while a line loops."""
        if (t is None or config.get()["line_filter"] != "i1_jump" or self._loop is not None
                or not self.session.events or self.player.paused):
            return
        if self._around(self._jump_span, t) or self._around(self._chosen_span, t):
            return  # still in it: the lines' kinds needn't be worked out on every tick
        self._chosen_span = None
        spans = self._i1_spans()
        target = line_filter.jump_target(spans, t)
        if target is None:
            self._jump_span = next(s for s in spans if self._around(s, t))
        elif target != "end":
            self._jump_span = next(s for s in spans if s[0] == target)
            self._seek_to_line(target)
        else:
            self._jump_span = None
            if self.playlist.index + 1 < len(self.playlist.paths):
                tooltip("No more i+1 lines: next episode")
                self.next_video()
            else:
                self.player.paused = True
                tooltip("No more i+1 lines in the playlist")

    def next_i1(self):
        if not self._has_video():
            tooltip("Open a video first")
        elif not self.session.events:
            tooltip("Still analysing the subtitles...")
        else:
            target = self._next_line_time(lambda kind: kind == "i+1")
            if target is None:
                tooltip("No more i+1 lines in this episode")
            else:
                self._seek_to_line(target)

    def cycle_line_filter(self):
        nxt = line_filter.next_filter(config.get()["line_filter"])
        config.save(line_filter=nxt)
        tooltip(dict(line_filter.LINE_FILTERS)[nxt])
