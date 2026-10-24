"""Condensed audio: the episode's audio with only its subtitled lines, one after the other, to
listen to without the video. The audio is cut in a background thread (cards/media.py)."""
import html
import logging
from pathlib import Path

from aqt.qt import QFileDialog
from aqt.utils import showWarning, tooltip

from ..cards import media
from ..core import config, state
from ..core.timefmt import fmt_time
from ..subs import online

log = logging.getLogger(__name__)


class CondensedAudioMixin:
    def _init_condensed(self):
        self._condensing = False  # one at a time: each one reads the whole video

    def export_condensed_audio(self):
        """Save the condensed audio of the episode playing, next to the video by default."""
        if not self._has_video():
            tooltip("Open a video first")
            return
        if not self.session.events:
            tooltip("No subtitles yet: wait for them to be analysed, or load some")
            return
        if self._condensing:
            tooltip("Condensed audio is being made already")
            return
        fmt = media.audio_format(config.get())
        video, title = self._video, self.playlist.titles.get(self._video)
        if online.is_url(video):  # into the folder videos were last opened from
            folder = Path(state.get("last_video_dir") or Path.home())
            suggested = folder / online.file_stem(title or "video")
        else:
            suggested = Path(video).with_suffix("")
        path, _ = QFileDialog.getSaveFileName(
            self, "Save condensed audio", f"{suggested}.condensed.{fmt}", f"Audio (*.{fmt})")
        if not path:
            return
        out = Path(path)
        if out.suffix[1:].lower() not in media.AUDIO_CODECS:
            out = out.with_name(f"{out.name}.{fmt}")
        delay = self.player.sub_delay
        times = [(start + delay, end + delay) for start, end, _ in self.session.events]
        audio_stream = None if online.is_url(video) else self._audio_stream()

        def work():
            return media.condensed_audio(online.media_sources(video, title)[1], times, out,
                                         audio_stream)

        def done(future):
            self._condensing = False
            try:
                seconds = future.result()
            except Exception as e:
                log.warning("couldn't make the condensed audio of %s: %s", video, e)
                showWarning(f"Couldn't make the condensed audio:\n\n{e}", parent=self)
                return
            tooltip(f"Saved {html.escape(out.name)} ({fmt_time(seconds)} of audio)",
                    period=5000)

        self._condensing = True
        tooltip("Making the condensed audio...")
        # not scoped to the episode: the file is worth having even if another video opens
        self._jobs.run(work, done)
