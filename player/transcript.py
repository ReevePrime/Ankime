"""Transcript side panel: every line of the episode (click to seek, right-click to add a
card, search), and the episode's new words by frequency (batch-add them as cards)."""
from aqt.qt import (QAbstractItemView, QBrush, QColor, QComboBox, QHBoxLayout, QLabel,
                    QLineEdit, QListWidget, QListWidgetItem, QMenu, QPushButton, Qt,
                    QTabWidget, QVBoxLayout, QWidget)

from ..core.timefmt import fmt_time

# i+1 lines are green, bold and marked, so colour isn't the only thing telling them apart.
# 4.4:1 against a white list; a lighter green on Anki's dark theme.
I1_COLORS = {False: "#2b8a3e", True: "#69db7c"}
I1_MARK = "● "
I1_TIP = "i+1: exactly one new word"
# (value, label): the order of the new words
WORD_SORTS = [("count", "Most frequent in this episode"),
              ("frequency", "Most common in Japanese")]


def _night_mode():
    try:
        from aqt.theme import theme_manager
        return bool(theme_manager.night_mode)
    except Exception:
        return False


class TranscriptPanel(QWidget):
    """Callbacks: on_seek(line index), on_mine(line index), on_add_words([lemma]),
    on_mark_known([lemma]), on_cancel_batch(), search(query) -> matching line indices,
    on_word_sort(one of WORD_SORTS' values), on_copy(line index), on_sync(line index): the
    subtitles are shifted so that line starts at the playhead, on_mark_line_known(line
    index): its new words are marked known."""

    def __init__(self, on_seek, on_mine, on_add_words, on_mark_known, on_cancel_batch,
                 search, word_sort="count", on_word_sort=None, on_copy=None, on_sync=None,
                 on_mark_line_known=None):
        super().__init__()
        self._on_seek, self._on_mine, self._search = on_seek, on_mine, search
        self._on_copy, self._on_sync = on_copy, on_sync
        self._on_mark_line_known = on_mark_line_known
        self._on_add_words, self._on_mark_known = on_add_words, on_mark_known
        self._texts = []  # each line's text without the i+1 mark

        # Lines
        self.query = QLineEdit()
        self.query.setPlaceholderText("Search the episode (word or dictionary form)")
        self.query.setClearButtonEnabled(True)
        self.query.textChanged.connect(self._filter)
        self.lines = QListWidget()
        self.lines.setWordWrap(True)
        self.lines.itemClicked.connect(lambda item: self._on_seek(item.data(Qt.ItemDataRole.UserRole)))
        self.lines.itemActivated.connect(  # Enter, for the keyboard
            lambda item: self._on_seek(item.data(Qt.ItemDataRole.UserRole)))
        self.lines.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.lines.customContextMenuRequested.connect(self._line_menu)
        self.lines.setToolTip("Click a line to play it; right-click to add a card for it")
        self.hits = QLabel()
        lines_tab = QWidget()
        box = QVBoxLayout(lines_tab)
        box.setContentsMargins(0, 4, 0, 0)
        box.addWidget(self.query)
        box.addWidget(self.lines, 1)
        box.addWidget(self.hits)

        # New words
        self.words = QListWidget()
        self.words.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.words.setToolTip("New words in this episode, most frequent first. "
                              "Double-click one to find its lines.")
        self.words.itemDoubleClicked.connect(self._find_word)
        self.sort = QComboBox()
        self.sort.setAccessibleName("Order of the new words")
        self.sort.setToolTip("Most common in Japanese: by the rank in Yomitan's frequency "
                             "dictionaries (#1 is the most common word)")
        for value, label in WORD_SORTS:
            self.sort.addItem(label, value)
        self.sort.setCurrentIndex(max(self.sort.findData(word_sort), 0))
        if on_word_sort:
            self.sort.activated.connect(lambda i: on_word_sort(self.sort.itemData(i)))
        self.words_note = QLabel()
        self.words_note.setTextFormat(Qt.TextFormat.PlainText)
        self.words_note.setWordWrap(True)
        self.words_note.hide()
        add = QPushButton("Add cards")
        add.setToolTip("One card per selected word, from a line where it is the only new word "
                       "if there is one, otherwise the shortest line it appears in")
        add.clicked.connect(lambda: self._selected_words(self._on_add_words))
        known = QPushButton("Mark known")
        known.clicked.connect(lambda: self._selected_words(self._on_mark_known))
        # the batch of words being added: progress and a way to stop it
        self.batch_label = QLabel()
        self.batch_label.setTextFormat(Qt.TextFormat.PlainText)  # it shows words from files
        self.batch_label.setWordWrap(True)
        self.batch_cancel = QPushButton("Cancel")
        self.batch_cancel.setToolTip("Stop adding the words still queued (the card being "
                                     "made now is still added)")
        self.batch_cancel.clicked.connect(on_cancel_batch)
        self.batch_row = QWidget()
        batch = QHBoxLayout(self.batch_row)
        batch.setContentsMargins(0, 0, 0, 0)
        batch.addWidget(self.batch_label, 1)
        batch.addWidget(self.batch_cancel)
        self.batch_row.hide()
        words_tab = QWidget()
        box = QVBoxLayout(words_tab)
        box.setContentsMargins(0, 4, 0, 0)
        box.addWidget(self.sort)
        box.addWidget(self.words, 1)
        box.addWidget(self.words_note)
        box.addWidget(self.batch_row)
        row = QHBoxLayout()
        row.addWidget(add)
        row.addWidget(known)
        box.addLayout(row)

        self.tabs = QTabWidget()
        self.tabs.addTab(lines_tab, "Lines")
        self.tabs.addTab(words_tab, "New words")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.tabs)

    # --- lines -------------------------------------------------------------

    def set_lines(self, events, delay, kinds):
        """`events` are (start, end, text) in file time; `kinds[i]` is the line's
        known / i+1 / harder kind (i+1 lines are marked, bold and green)."""
        self.lines.clear()
        self._texts = [f"{fmt_time(start + delay)}  {text.replace(chr(10), ' ')}"
                       for start, _end, text in events]
        for i, text in enumerate(self._texts):
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, i)
            self.lines.addItem(item)
        self.set_kinds(kinds)
        self._filter()

    def set_kinds(self, kinds):
        color = QBrush(QColor(I1_COLORS[_night_mode()]))
        for i in range(self.lines.count()):
            item = self.lines.item(i)
            i1 = kinds[i] == "i+1"
            item.setText(f"{I1_MARK}{self._texts[i]}" if i1 else self._texts[i])
            item.setForeground(color if i1 else QBrush())
            font = item.font()
            font.setBold(i1)
            item.setFont(font)
            item.setToolTip(I1_TIP if i1 else "")

    def _line_menu(self, pos):
        item = self.lines.itemAt(pos)
        if item is None:
            return
        index = item.data(Qt.ItemDataRole.UserRole)
        menu = QMenu(self)
        menu.addAction("Add card for this line", lambda: self._on_mine(index))
        menu.addAction("Play from here", lambda: self._on_seek(index))
        if self._on_copy:
            menu.addAction("Copy line", lambda: self._on_copy(index))
        if self._on_mark_line_known:
            menu.addAction("Mark every new word on this line known",
                           lambda: self._on_mark_line_known(index))
        if self._on_sync:
            sync = menu.addAction("Sync subtitles: this line starts now",
                                  lambda: self._on_sync(index))
            sync.setToolTip("Pause where you hear this line start, then pick it: the "
                            "subtitles move to match")
            menu.setToolTipsVisible(True)
        menu.exec(self.lines.viewport().mapToGlobal(pos))

    def show_line(self, index):
        """Highlight the line playing (None: between lines)."""
        if index is None or not 0 <= index < self.lines.count():
            return
        item = self.lines.item(index)
        if not item.isHidden():
            self.lines.setCurrentItem(item)
            self.lines.scrollToItem(item, QAbstractItemView.ScrollHint.PositionAtCenter)

    def _filter(self):
        query = self.query.text().strip()
        matching = set(self._search(query)) if query else None
        for i in range(self.lines.count()):
            self.lines.item(i).setHidden(matching is not None and i not in matching)
        self.hits.setText(f"{len(matching)} lines" if matching is not None else "")

    # --- new words ---------------------------------------------------------

    def word_sort(self):
        return self.sort.currentData()

    def set_words_note(self, text):
        """A line under the new words ("getting their frequency..."); "" hides it."""
        self.words_note.setText(text)
        self.words_note.setVisible(bool(text))

    def set_words(self, counts, ranks=None):
        """[(lemma, number of lines)], in the order shown; `ranks` ({lemma: frequency rank})
        adds each word's rank to it. Keeps the selection."""
        chosen = {item.data(Qt.ItemDataRole.UserRole) for item in self.words.selectedItems()}
        self.words.clear()
        for lemma, n in counts:
            rank = (ranks or {}).get(lemma)
            item = QListWidgetItem(f"{lemma}  ×{n}" + (f"  · #{rank}" if rank else ""))
            item.setData(Qt.ItemDataRole.UserRole, lemma)
            self.words.addItem(item)
            item.setSelected(lemma in chosen)
        self.tabs.setTabText(1, f"New words ({len(counts)})")

    def show_batch(self, text):
        """Progress of the batch of words being added; "" once it is over."""
        self.batch_label.setText(text)
        self.batch_row.setVisible(bool(text))

    def _selected_words(self, callback):
        lemmas = [item.data(Qt.ItemDataRole.UserRole) for item in self.words.selectedItems()]
        if lemmas:
            callback(lemmas)

    def _find_word(self, item):
        self.query.setText(item.data(Qt.ItemDataRole.UserRole))
        self.tabs.setCurrentIndex(0)
