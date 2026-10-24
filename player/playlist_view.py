"""The playlist panel: adding and ordering videos (by hand, or the episodes you know most of
first: what to watch next), moving through them, and each episode's coverage score (how many
of its words you know), worked out in the background."""
from pathlib import Path

from aqt.qt import (QAbstractItemView, QApplication, QFileDialog, QHBoxLayout, QInputDialog,
                    QListWidget, QMenu, QPushButton, QToolButton, QVBoxLayout, QWidget)
from aqt.utils import showWarning, tooltip

from ..core import config, state
from ..core.store import USER_FILES, JsonFile
from ..language import knowledge, tokenizer, yomitan
from ..subs import online, session, subtitles
from ..subs.playlist import Playlist, natural_key, videos_in

VIDEO_EXTS = "Video (*.mkv *.mp4 *.avi *.webm *.mov);;All files (*)"
PLAYLIST_FILE = USER_FILES / "playlist.json"


class PlaylistMixin:
    def _init_playlist(self):
        self.playlist = Playlist()
        self._playlist_file = JsonFile(PLAYLIST_FILE)
        self._episode_lemmas = {}  # video path -> content_lemmas of its subtitles
        self._unscored = set()     # playlist videos with no readable subtitles
        self._scoring = False      # a playlist episode is being tokenized in the background

    def _build_playlist_box(self):
        self.playlist_box = QWidget()
        self.playlist_box.setMinimumWidth(160)
        self.playlist_box.hide()
        self.playlist_list = QListWidget()
        self.playlist_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.playlist_list.itemDoubleClicked.connect(
            lambda _item: self.play_index(self.playlist_list.currentRow()))
        add_files = QPushButton("Add files...")
        add_files.clicked.connect(self.add_files)
        add_folder = QPushButton("Add folder...")
        add_folder.clicked.connect(self.add_folder)
        up = QPushButton("▲")
        up.setToolTip("Move selected up")
        up.clicked.connect(lambda: self.move_selected(-1))
        down = QPushButton("▼")
        down.setToolTip("Move selected down")
        down.clicked.connect(lambda: self.move_selected(1))
        remove = QPushButton("Remove")
        remove.clicked.connect(self.remove_selected)
        clear = QPushButton("Clear")
        clear.clicked.connect(self.clear_playlist)
        top, bottom = QHBoxLayout(), QHBoxLayout()
        top.addWidget(add_files)
        top.addWidget(add_folder)
        top.addWidget(self._sort_button())
        bottom.addWidget(up)
        bottom.addWidget(down)
        bottom.addWidget(remove)
        bottom.addWidget(clear)
        box = QVBoxLayout(self.playlist_box)
        box.setContentsMargins(0, 0, 0, 0)
        box.addWidget(self.playlist_list, 1)
        box.addLayout(top)
        box.addLayout(bottom)
        return self.playlist_box

    def _refresh_playlist_view(self):
        self.playlist_list.clear()
        for _ in self.playlist.paths:
            self.playlist_list.addItem("")
        self._relabel_playlist()
        self._save_playlist()
        self._score_next_episode()

    def _sort_button(self):
        sort = QToolButton()
        sort.setText("Sort")
        sort.setToolTip("What to watch next: the episodes you know most of first")
        sort.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(sort)
        menu.addAction("Best coverage first", self.sort_by_coverage).setToolTip(
            "The episodes whose words you know (or are learning) the most of first. "
            "Episodes not scored yet go last")
        menu.addAction("By name", self.sort_by_name).setToolTip("Episode 2 before episode 10")
        menu.setToolTipsVisible(True)
        sort.setMenu(menu)
        return sort

    def _coverage(self, path):
        """The share of an episode's distinct words that are known or learning, or None if
        it isn't scored (yet)."""
        lemmas = self._episode_lemmas.get(path)
        counts = self.knowledge.coverage(lemmas) if lemmas else None
        total = sum(counts.values()) if counts else 0
        return (counts["known"] + counts["learning"]) / total if total else None

    def _relabel_playlist(self):
        """Name of each video with its coverage, to pick what to watch next. Done in place so
        the selection survives."""
        for i, path in enumerate(self.playlist.paths):
            item = self.playlist_list.item(i)
            if item is None:
                return
            name = self.playlist.name(path)
            lemmas = self._episode_lemmas.get(path)
            counts = self.knowledge.coverage(lemmas) if lemmas else None
            if counts:
                total = sum(counts.values())
                covered = (counts["known"] + counts["learning"]) / total
                name += (f"\n{covered:.0%} · {knowledge.words_to_reach(counts, 90)} to 90% "
                         f"· {knowledge.words_to_reach(counts, 95)} to 95%")
                item.setToolTip(f"{total} distinct words: {counts['known']} known, "
                                f"{counts['learning']} learning, {counts['new']} new")
            else:
                item.setToolTip("")
            item.setText(f"▶ {name}" if i == self.playlist.index else name)

    # --- scores --------------------------------------------------------------------

    def _score_next_episode(self):
        """Tokenize the playlist's episodes one after the other (Yomitan is not hammered,
        and each score shows up as soon as it is ready). Episodes whose subtitles are only
        embedded are scored when they are played."""
        if self._scoring:
            return
        path = next((p for p in self.playlist.paths if p not in self._episode_lemmas
                     and p not in self._unscored and p != self._video), None)
        if path is None or self._yomitan_waiting():
            return  # offline: scoring goes on once Yomitan is back
        self._scoring = True
        cfg = config.get()
        key = tokenizer.analysis_key(cfg)
        chosen = self.playlist.subtitles.get(path)  # read here: the playlist is the main thread's

        def work():
            if online.is_url(path):  # its subtitles are only fetched when it is played
                return {}
            source, _ = subtitles.find_sidecars(path, cfg["source_lang"], cfg["target_lang"],
                                                chosen)
            if not source:
                return {}
            return session.tokenize_file(source, key, tokenizer.tokenize_many, path)[1]

        def done(future):
            self._scoring = False
            try:
                tokens = future.result()
            except yomitan.YomitanOffline as e:
                self._yomitan_failed(e)
                return  # not the episode's fault: tried again once Yomitan is back
            except Exception:
                tokens = None  # unreadable file: don't retry every refresh
            if tokens:  # only the words are kept: the episode's lines load when it is played
                self._episode_lemmas[path] = knowledge.content_lemmas(
                    t for ts in tokens.values() for t in ts)
                self._relabel_playlist()
            else:
                self._unscored.add(path)
            self._score_next_episode()

        self._jobs.run(work, done, scope="scores")

    def _update_score(self):
        counts = self.knowledge.coverage(self._episode_lemmas.get(self._video, {}))
        total = sum(counts.values())
        if total:
            self.score_label.setText(
                f"Known {counts['known'] / total:.0%} · "
                f"learning {counts['learning'] / total:.0%} · new {counts['new'] / total:.0%}"
                f" · {knowledge.words_to_reach(counts, 90)} words to 90% "
                f"· {knowledge.words_to_reach(counts, 95)} to 95%")
        self._relabel_playlist()

    # --- saving --------------------------------------------------------------------

    def _save_playlist(self):
        self._playlist_file.save(self.playlist.to_json())

    def _restore_playlist(self):
        """Reload last session's playlist. Nothing starts playing: Play / next resumes it."""
        data = self._playlist_file.load()
        if not data or not self.playlist.restore(data):
            return
        self._refresh_playlist_view()
        if self.playlist.paths:
            self.playlist_list.setCurrentRow(max(self.playlist.index, 0))

    # --- editing and moving through it ---------------------------------------------

    def move_selected(self, delta):
        rows = [self.playlist_list.row(i) for i in self.playlist_list.selectedItems()]
        selected = self.playlist.move(rows, delta)
        self._refresh_playlist_view()
        for row in selected:
            self.playlist_list.item(row).setSelected(True)

    def _sort_playlist(self, key):
        self.playlist.sort(key)
        self._refresh_playlist_view()
        if self.playlist.paths:
            self.playlist_list.setCurrentRow(max(self.playlist.index, 0))

    def sort_by_coverage(self):
        """The episodes you know most of first; those not scored yet last, in their order."""
        def key(path):
            covered = self._coverage(path)
            return (covered is None, -(covered or 0))

        self._sort_playlist(key)
        waiting = sum(1 for p in self.playlist.paths if self._coverage(p) is None)
        if waiting:
            tooltip(f"{waiting} episode{'s' if waiting != 1 else ''} not scored yet "
                    f"{'are' if waiting != 1 else 'is'} at the end")

    def sort_by_name(self):
        self._sort_playlist(natural_key)

    def set_playlist(self, paths, start=0):
        """Replace the playlist and start playing at `start`."""
        self.playlist.replace(paths)
        self._refresh_playlist_view()
        self.play_index(start)

    def play_index(self, i):
        if not 0 <= i < len(self.playlist.paths):
            return
        self.playlist.index = i
        self._refresh_playlist_view()
        self.open_video(self.playlist.paths[i])

    def next_video(self):
        if self.player is not None and not self.player.path:
            self.play_index(max(self.playlist.index, 0))  # nothing loaded yet: resume the saved spot
        elif self.player is not None and self.playlist.index + 1 < len(self.playlist.paths):
            self.play_index(self.playlist.index + 1)

    def previous_video(self):
        if self.player is not None and self.playlist.index > 0:
            self.play_index(self.playlist.index - 1)

    def _append(self, paths):
        """Add videos to the end; starts playing if the playlist was empty."""
        if not paths:
            return
        was_empty = self.playlist.append(paths)
        self._refresh_playlist_view()
        if was_empty:
            self.play_index(0)

    def choose_video(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open video", state.get("last_video_dir"), VIDEO_EXTS)
        if not path:
            return
        self._remember_dir(Path(path).parent)
        self.set_playlist([path])

    def open_url(self):
        """Play a video from YouTube or another site yt-dlp supports: mpv streams it
        through yt-dlp, which also gets its subtitles (see subs/online.py)."""
        copied = QApplication.clipboard().text().strip()
        url, ok = QInputDialog.getText(
            self, "Open URL", "Address of the video (YouTube, or another site yt-dlp "
            "supports):", text=copied if online.is_url(copied) else "")
        url = url.strip()
        if not ok or not url:
            return
        if not online.is_url(url):
            showWarning("That isn't a web address: it should start with https://", parent=self)
            return
        if not online.ytdlp_path():
            showWarning("Playing videos from the web needs yt-dlp: install it (from "
                        "github.com/yt-dlp/yt-dlp, or with pip, winget or Homebrew) so that "
                        "it is on the PATH, then restart Anki.", parent=self)
            return
        self.set_playlist([url])

    def add_files(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Add videos", state.get("last_video_dir"), VIDEO_EXTS)
        if paths:
            self._remember_dir(Path(paths[0]).parent)
            self._append(paths)

    def add_folder(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Add every video in a folder", state.get("last_video_dir"))
        if not folder:
            return
        self._remember_dir(folder)
        files = videos_in(folder)
        if not files:
            tooltip("No videos in that folder.")
        self._append(files)

    @staticmethod
    def _remember_dir(folder):
        state.save(last_video_dir=str(folder))

    def remove_selected(self):
        self.playlist.remove(self.playlist_list.row(i) for i in self.playlist_list.selectedItems())
        self._refresh_playlist_view()

    def clear_playlist(self):
        self.playlist.clear()
        self._refresh_playlist_view()
