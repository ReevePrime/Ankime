"""Getting subtitles for the video playing: searching Jimaku and downloading a file next to
the video, aligning a subtitle file to the audio with alass or ffsubsync (after a download,
or the file in use), and making subtitles from the speech with faster-whisper or
whisper.cpp for shows Jimaku doesn't have."""
import logging
from pathlib import Path

from aqt import mw
from aqt.qt import (QCheckBox, QDialog, QDialogButtonBox, QHBoxLayout, QInputDialog, QLabel,
                    QLineEdit, QListWidget, QPushButton, QSpinBox, Qt, QVBoxLayout, QWidget)
from aqt.utils import showWarning, tooltip

from ..core import config, state
from ..subs import jimaku, online, retime, subtitles, transcribe

log = logging.getLogger(__name__)


def _size(n):
    return f"{n / 2**20:.1f} MB" if n >= 2**20 else f"{max(1, n // 1024)} KB"


class JimakuDialog(QDialog):
    """Search Jimaku for a video's subtitles and download a file: `chosen` is then (file
    name, bytes), and `align` whether to align it to the audio. With a transcriber set,
    `generate` says the subtitles are to be made from the speech instead."""

    def __init__(self, parent, video, key, aligner, transcriber=""):
        super().__init__(parent)
        self.setWindowTitle("Search Jimaku")
        self._key, self._aligner = key, bool(aligner)
        self._transcriber = bool(transcriber)
        self._entries, self._files = [], []
        self._generation = 0  # a newer request drops the answer to an older one
        self._open = True
        self.chosen, self.align, self.generate = None, False, False

        self.query = QLineEdit(jimaku.search_query(video))
        self.query.returnPressed.connect(self.search)
        search = QPushButton("Search")
        search.setAutoDefault(False)
        search.clicked.connect(self.search)
        self.episode = QSpinBox()
        self.episode.setRange(0, 9999)
        self.episode.setSpecialValueText("All")
        self.episode.setPrefix("Episode ")
        self.episode.setValue(jimaku.episode_number(video) or 0)
        self.episode.setToolTip("Only the files Jimaku thinks are of this episode (All: every "
                                "file of the show)")
        self.episode.valueChanged.connect(self._load_files)
        row = QHBoxLayout()
        row.addWidget(self.query, 1)
        row.addWidget(self.episode)
        row.addWidget(search)

        self.entries = QListWidget()
        self.entries.setAccessibleName("Shows")
        self.entries.currentRowChanged.connect(self._load_files)
        self.files = QListWidget()
        self.files.setAccessibleName("Files")
        self.files.itemActivated.connect(self.download)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)  # it shows Jimaku's messages
        self.align_box = QCheckBox(f"Align to the audio with {retime.tool_kind(aligner)}"
                                   if aligner else "")
        self.align_box.setToolTip("Downloaded subtitles are often a few seconds off: this "
                                  "shifts them to match the speech (takes a moment)")
        self.align_box.setChecked(state.get("align_downloads"))
        self.align_box.setVisible(bool(aligner))

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.download_btn = buttons.addButton("Download",
                                              QDialogButtonBox.ButtonRole.AcceptRole)
        self.download_btn.setAutoDefault(False)  # Enter in the search box searches
        self.download_btn.clicked.connect(self.download)
        buttons.rejected.connect(self.reject)
        if transcriber:
            generate = buttons.addButton("Generate from speech",
                                         QDialogButtonBox.ButtonRole.ActionRole)
            generate.setAutoDefault(False)
            generate.setToolTip(f"No subtitles on Jimaku? Make them from the speech with "
                                f"{transcribe.tool_kind(transcriber)} instead (minutes with "
                                "a GPU, much longer without)")
            generate.clicked.connect(self._generate)

        layout = QVBoxLayout(self)
        layout.addLayout(row)
        layout.addWidget(QLabel("Show"))
        layout.addWidget(self.entries, 1)
        layout.addWidget(QLabel("Subtitle file"))
        layout.addWidget(self.files, 2)
        layout.addWidget(self.status)
        layout.addWidget(self.align_box)
        layout.addWidget(buttons)
        self.resize(560, 520)
        self.search()

    # --- background requests ---------------------------------------------------

    def _run(self, message, work, done):
        """work() in the background, then done(result) unless a newer request was made or
        the dialog closed meanwhile; a failure is shown in the status line."""
        self._generation += 1
        generation = self._generation
        self.status.setText(message)

        def finished(future):
            if not self._open or generation != self._generation:
                return
            try:
                result = future.result()
            except jimaku.JimakuError as e:
                self.status.setText(str(e))
                return
            except Exception as e:
                log.warning("Jimaku request failed: %s", e)
                self.status.setText(f"Something went wrong: {e}")
                return
            self.status.setText("")
            done(result)

        mw.taskman.run_in_background(work, finished)

    def search(self):
        query, key = self.query.text().strip(), self._key
        if not query:
            return
        self.entries.clear()
        self.files.clear()
        self._entries, self._files = [], []

        def done(entries):
            self._entries = entries
            for e in entries:
                names = [e.get("name") or ""] + [n for n in (e.get("japanese_name"),
                                                            e.get("english_name")) if n]
                self.entries.addItem(" · ".join(dict.fromkeys(names)))
            if entries:
                self.entries.setCurrentRow(0)
            else:
                self.status.setText(f"Nothing on Jimaku for “{query}”: try another name "
                                    "(the romaji or Japanese title)" + self._or_generate())

        self._run("Searching Jimaku...", lambda: jimaku.search(query, key), done)

    def _load_files(self, *_):
        row = self.entries.currentRow()
        self.files.clear()
        self._files = []
        if not 0 <= row < len(self._entries):
            return
        entry_id, episode, key = self._entries[row]["id"], self.episode.value() or None, self._key

        def done(found):
            self._files = found
            for f in found:
                self.files.addItem(f"{f.get('name', '')}   ({_size(f.get('size') or 0)})")
            if found:
                self.files.setCurrentRow(0)
            elif episode:
                self.status.setText(f"No file for episode {episode}: set the episode to All "
                                    "to see every file (a season's archive, say)"
                                    + self._or_generate())
            else:
                self.status.setText("This entry has no subtitle files or zip archives"
                                    + self._or_generate())

        self._run("Getting the files...", lambda: jimaku.files(entry_id, key, episode), done)

    def download(self, *_):
        row = self.files.currentRow()
        if not 0 <= row < len(self._files):
            self.status.setText("Choose a file first.")
            return
        name, url, key = self._files[row]["name"], self._files[row]["url"], self._key
        episode = self.episode.value() or None

        def done(found):
            if not found:
                self.status.setText(f"No subtitle files in {name}.")
                return
            names = [n for n, _ in found]
            index = jimaku.best_match(names, episode)
            if index is None:
                choice, ok = QInputDialog.getItem(self, "Choose the subtitles",
                                                  f"Subtitle files in {name}:", names, 0, False)
                if not ok:
                    return
                index = names.index(choice)
            self.chosen = found[index]
            self.align = self._aligner and self.align_box.isChecked()
            if self._aligner:
                state.save(align_downloads=self.align_box.isChecked())
            self.accept()

        self._run(f"Downloading {name}...",
                  lambda: jimaku.subtitle_files(name, jimaku.download(url, key)), done)

    def _or_generate(self):
        return ", or generate them from the speech." if self._transcriber else "."

    def _generate(self):
        self.generate = True
        self.accept()

    def done(self, result):
        self._open = False
        super().done(result)


class SubtitleSearchMixin:
    def _init_subtitle_search(self):
        self._transcribing = None  # the video being transcribed: one at a time, it's slow
        self._transcribe_stopped = False  # by its Stop button: no error to show

    def _transcribe_row(self):
        """How far making subtitles from the speech has got, with a way to stop it."""
        box = QWidget()
        row = QHBoxLayout(box)
        row.setContentsMargins(0, 0, 0, 0)
        self.transcribe_label = QLabel()
        self.transcribe_label.setTextFormat(Qt.TextFormat.PlainText)  # it shows file names
        stop = QPushButton("Stop")
        stop.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        stop.setToolTip("Stop making subtitles from the speech (nothing is saved)")
        stop.clicked.connect(self._stop_transcribing)
        row.addWidget(self.transcribe_label)
        row.addWidget(stop)
        box.hide()
        self.transcribe_box = box
        return box

    def _stop_transcribing(self):
        self._transcribe_stopped = True
        transcribe.stop_all()

    def _show_transcribing(self, text):
        """"" hides it."""
        self.transcribe_label.setText(text)
        self.transcribe_box.setVisible(bool(text))

    def search_jimaku(self):
        """Search Jimaku for the video's subtitles; the file is saved next to the video
        (found by name from then on) and used at once, aligned to the audio first if an
        aligner is set and wanted. With a transcriber set, the dialog can make them from
        the speech instead."""
        if not self._has_video():
            tooltip("Open a video first")
            return
        if online.is_url(self._video):
            tooltip("Jimaku's subtitles are saved next to the video: only for videos on your "
                    "computer")
            return
        cfg = config.get()
        key = cfg["jimaku_api_key"].strip()
        if not key:
            showWarning("Searching Jimaku needs an API key: make a free account on "
                        "jimaku.cc, generate a key on its account page, and enter it in "
                        "Tools > Ankime > Settings > Player.", parent=self)
            return
        video = self._video
        dialog = JimakuDialog(self, video, key, cfg["sub_sync_tool"], cfg["transcriber"])
        if not dialog.exec():
            return
        if dialog.generate:
            self._transcribe(video)
            return
        if not dialog.chosen:
            return
        name, data = dialog.chosen
        path = jimaku.save_path(video, name, subtitles.lang_code(cfg["source_lang"]))
        try:
            path.write_bytes(data)
        except OSError as e:
            showWarning(f"Couldn't save the subtitles next to the video:\n\n{e}", parent=self)
            return
        if dialog.align:
            self._align(video, path, downloaded=True)
        else:
            self._use_subtitle_file(video, path)

    def align_subtitles(self):
        """Align the subtitle file in use (found next to the video, chosen, or extracted
        from it) to the audio; the aligned copy is saved next to the video and used."""
        if not self._has_video():
            tooltip("Open a video first")
            return
        if online.is_url(self._video):
            tooltip("Aligning reads the whole video: only for videos on your computer")
            return
        files = self._episode_files
        if not files or files[0] != self._video or not files[1]:
            tooltip("No subtitle file to align (the video's subtitles are still being read, "
                    "or it has none)")
            return
        self._align(self._video, files[1])

    def _align(self, video, sub, downloaded=False):
        cfg = config.get()
        if not cfg["sub_sync_tool"]:
            showWarning("Aligning subtitles needs alass or ffsubsync: install one and set it "
                        "as the subtitle aligner in Tools > Ankime > Settings > Player.",
                        parent=self)
            return
        out = retime.aligned_path(sub, video, subtitles.lang_code(cfg["source_lang"]))
        tooltip("Aligning the subtitles to the audio...")

        def done(future):
            try:
                path = future.result()
            except Exception as e:
                log.warning("couldn't align %s: %s", sub, e)
                kept = "\n\nThe subtitles are used as downloaded." if downloaded else ""
                showWarning(f"Couldn't align the subtitles:\n\n{e}{kept}", parent=self)
                if downloaded:
                    self._use_subtitle_file(video, sub)
                return
            note = ""
            if self.player is not None and abs(self.player.sub_delay) > 1e-6:
                note = (f"<br>The show's delay ({self.player.sub_delay:+.2f}s) still "
                        "applies: reset it if they are off now")
            self._use_subtitle_file(video, path, "Aligned subtitles", note)

        self._jobs.run(lambda: retime.align(video, sub, out), done)

    def generate_subtitles(self):
        """Make the video's subtitles from its speech (the audio track playing) with
        faster-whisper or whisper.cpp; they are saved next to the video and used."""
        if not self._has_video():
            tooltip("Open a video first")
            return
        if online.is_url(self._video):
            tooltip("Transcribing reads the whole video: only for videos on your computer")
            return
        if not config.get()["transcriber"]:
            showWarning("Making subtitles from the speech needs faster-whisper or "
                        "whisper.cpp: install one and set it as the transcriber in Tools > "
                        "Ankime > Settings > Player.", parent=self)
            return
        self._transcribe(self._video)

    def _transcribe(self, video):
        if self._transcribing:
            tooltip("Subtitles are being made from the speech already")
            return
        out = transcribe.subtitle_path(video, subtitles.lang_code(config.get()["source_lang"]))
        playing = self.player is not None and self.player.path == video
        audio_stream = self._audio_stream() if playing else None
        duration = self.player.duration if playing else None
        name = Path(video).name
        shown = [-1]  # the percentage on screen: the label changes once per percent

        def show(percent):
            if self._transcribing == video:
                self._show_transcribing(f"Transcribing {name}... {percent}%")

        def progress(fraction):  # from the background thread
            percent = int(fraction * 100)
            if percent != shown[0]:
                shown[0] = percent
                mw.taskman.run_on_main(lambda: show(percent))

        def done(future):
            self._transcribing = None
            self._show_transcribing("")
            try:
                path = future.result()
            except Exception as e:
                if self._transcribe_stopped:
                    tooltip("Stopped making subtitles from the speech")
                    return
                log.warning("couldn't transcribe %s: %s", video, e)
                showWarning(f"Couldn't make subtitles from the speech:\n\n{e}", parent=self)
                return
            self._use_subtitle_file(video, path, "Subtitles from the speech",
                                    "<br>Made by speech recognition: expect mistakes")

        self._transcribing, self._transcribe_stopped = video, False
        self._show_transcribing(f"Transcribing {name}...")
        tooltip("Making subtitles from the speech: minutes with a GPU, much longer without. "
                "You can keep watching meanwhile", period=6000)
        # not scoped to the episode: the subtitles are worth having once another video plays
        self._jobs.run(lambda: transcribe.transcribe(video, out, audio_stream, duration,
                                                     progress), done)
