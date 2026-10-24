"""Searching your library ("where else did I hear this word?"): the lines with a word in every
episode analysed so far, the best example sentences first, and playing one of them (see
subs/library.py)."""
import html
import os
from pathlib import Path

from aqt.qt import (QDialog, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                    QPushButton, QVBoxLayout)
from aqt.utils import tooltip

from ..core.timefmt import fmt_time
from ..subs import library, online
from .transcript import I1_MARK, I1_TIP

KIND_TIPS = {"i+1": I1_TIP, "known": "No new word", "harder": "Several new words"}


class LibraryDialog(QDialog):
    """`search(query, done)` runs library.search in the background and calls done(hits,
    more) on the main thread; `rank(hits)` orders them (library.rank); `play(hit)`."""

    def __init__(self, parent, search, rank, play):
        super().__init__(parent)
        self.setWindowTitle("Search your library")
        self.resize(560, 520)
        self._search, self._rank, self._play = search, rank, play
        self._hits = []

        self.query = QLineEdit()
        self.query.setPlaceholderText("A word, as written or its dictionary form")
        self.query.setClearButtonEnabled(True)
        self.query.returnPressed.connect(self.search)
        go = QPushButton("Search")
        go.setAutoDefault(False)
        go.clicked.connect(self.search)
        row = QHBoxLayout()
        row.addWidget(self.query, 1)
        row.addWidget(go)
        self.results = QListWidget()
        self.results.setWordWrap(True)
        self.results.setToolTip("Double-click a line (or press Enter) to play it")
        self.results.itemActivated.connect(self._activated)  # double-click or Enter
        self.status = QLabel("Every episode opened in the player, or scored in the playlist, "
                             "is searched.")
        self.status.setWordWrap(True)
        box = QVBoxLayout(self)
        box.addLayout(row)
        box.addWidget(self.results, 1)
        box.addWidget(self.status)

    def search_for(self, word):
        self.query.setText(word)
        self.search()

    def search(self):
        query = self.query.text().strip()
        self.results.clear()
        if not query:
            return
        self.status.setText("Searching...")
        self._search(query, lambda hits, more: self._show(query, hits, more))

    def _show(self, query, hits, more):
        if query != self.query.text().strip():
            return  # searched for something else since
        self.results.clear()
        self._hits = self._rank(hits)
        for hit, kind in self._hits:
            where = f"{library.show_name(hit.video)} · {Path(hit.video).name}"
            if hit.start is not None:
                where += f" · {fmt_time(hit.start)}"
            item = QListWidgetItem(f"{I1_MARK if kind == 'i+1' else ''}{hit.text}\n{where}")
            item.setToolTip(KIND_TIPS.get(kind, ""))
            self.results.addItem(item)
        episodes = len({hit.video for hit in hits})
        if not hits:
            self.status.setText(f"No line with {html.escape(query)} in the episodes analysed "
                                "so far.")
        else:
            self.status.setText(
                f"{'More than ' if more else ''}{len(hits)} line{'s' if len(hits) != 1 else ''}"
                f" in {episodes} episode{'s' if episodes != 1 else ''}, the i+1 lines (●) "
                "first.")

    def _activated(self, item):
        row = self.results.row(item)
        if 0 <= row < len(self._hits):
            self._play(self._hits[row][0])


class LibrarySearchMixin:
    def _init_library(self):
        self._library_dialog = None
        self._library_hit = None  # (video, line text, start) to go to once it is analysed

    def search_library(self):
        """Open the library search, looking for the selected word if there is one."""
        if self._library_dialog is None:
            self._library_dialog = LibraryDialog(
                self, self._run_library_search,
                lambda hits: library.rank(hits, self.knowledge), self._play_library_hit)
        dialog = self._library_dialog
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        selected = self.session.selected
        if selected:
            dialog.search_for(selected.base or selected.surface)
        else:
            dialog.query.setFocus()

    def _run_library_search(self, query, show):
        self._jobs.invalidate("library")  # only the newest search's results are shown

        def done(future):
            try:
                hits, more = future.result()
            except Exception as e:
                self._library_dialog.status.setText(f"Couldn't search: {html.escape(str(e))}")
                return
            show(hits, more)

        self._jobs.run(lambda: library.search(query), done, scope="library")

    def _play_library_hit(self, hit):
        """Play a line found in the library: in the video playing, at once; in another, once
        it is opened and analysed (it starts near the line meanwhile)."""
        if self.player is None:
            return
        if not online.is_url(hit.video) and not os.path.exists(hit.video):
            tooltip("That video was moved or deleted")
            return
        self._library_hit = (hit.video, hit.text, hit.start)
        if hit.video == self._video and self._has_video():
            self._seek_to_library_hit()
            return
        if hit.video not in self.playlist.paths:
            self.playlist.append([hit.video])
        if hit.start is not None:
            self.playlist.positions[hit.video] = max(hit.start - 1, 0)
        self.play_index(self.playlist.paths.index(hit.video))

    def _seek_to_library_hit(self):
        """Go to the line chosen in the library, once its episode's lines are in."""
        hit = self._library_hit
        if not hit or hit[0] != self._video or not self.session.events:
            return
        self._library_hit = None
        _, text, start = hit
        lines = [i for i, ev in enumerate(self.session.events) if ev.text == text]
        if not lines:
            tooltip("That line isn't in the subtitles any more")
            return
        self._seek_to_event(min(lines, key=lambda i: abs(self.session.events[i].start
                                                         - (start or 0))))
        if self.player.paused:
            self.player.paused = False
