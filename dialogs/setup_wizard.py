"""The setup wizard, on a new install (first_run.offer) or from Tools > Ankime > Setup wizard.
Page by page: the note type and deck cards go to, where known words come from, the
dictionary, then libmpv and ffmpeg, checked and downloaded here as Test setup does. The
settings are saved on Finish, except what a download sets up (saved at once, as from the
settings). What each choice means is worked out in first_run.py."""
import html

from aqt import mw
from aqt.qt import (QButtonGroup, QCheckBox, QComboBox, QFileDialog, QFormLayout, QHBoxLayout,
                    QLabel, QPushButton, QRadioButton, QSpinBox, QVBoxLayout,
                    QWidget, QWizard, QWizardPage)
from aqt.utils import showWarning, tooltip

from ..cards import notes
from ..core import config, downloads
from ..language import yomitan
from . import first_run, setup_check

YOMITAN_API = "https://github.com/yomidevs/yomitan-api"
WAITING = "<i>Checking...</i>"


def _label(text=""):
    label = QLabel(text)
    label.setWordWrap(True)
    label.setOpenExternalLinks(True)
    return label


def _show_status(label, status, message):
    """A check's result in `label`, grown to the lines it wraps to: a wrapped label whose
    text changes isn't always given the height of its new text."""
    label.setText(setup_check.status_html(status, message))
    label.setMinimumHeight(label.heightForWidth(max(label.width(), 200)))


def _note_type_names():
    return sorted(m.name for m in mw.col.models.all_names_and_ids())


def _field_names(note_type):
    model = mw.col.models.by_name(note_type)
    return [f["name"] for f in model["flds"]] if model else []


class _Page(QWizardPage):
    """A page of `wizard`, with its title and what it is about."""

    def __init__(self, wizard, title, subtitle=""):
        super().__init__()
        self.w = wizard  # the wizard's Python side, still there once Qt has deleted it
        self.setTitle(title)
        self.box = QVBoxLayout(self)
        if subtitle:
            self.box.addWidget(_label(subtitle))

    def indented(self):
        """A new layout below the last widget, indented under it."""
        layout = QVBoxLayout()
        layout.setContentsMargins(24, 0, 0, 0)
        self.box.addLayout(layout)
        return layout

    def in_background(self, work, done):
        """work() off the main thread, then done(result) unless the wizard has closed."""
        def finished(future):
            if not self.w.alive:
                return
            try:
                result = future.result()
            except Exception as e:
                result = (setup_check.FAIL, f"The check crashed: {e}")
            done(result)

        mw.taskman.run_in_background(work, finished)


class WelcomePage(_Page):
    def __init__(self, wizard):
        super().__init__(wizard, "Welcome to Ankime")
        self.box.addWidget(_label(
            "Ankime plays your Japanese videos inside Anki: hover a word of the subtitles to "
            "look it up, and one key makes a card of the line, with its audio, a picture "
            "and the word's definition. Every word is shown new, learning or known, from "
            "the cards you already have."))
        self.box.addWidget(_label(
            "This takes a minute or two, and sets up:"
            "<ul><li>the cards: which note type and deck</li>"
            "<li>where known words come from</li>"
            "<li>the dictionary words are looked up in</li>"
            "<li>the video player and ffmpeg, downloaded for you where possible</li></ul>"
            "Everything can be changed later, in Tools > Ankime > Settings."))
        self.box.addStretch(1)


class CardsPage(_Page):
    def __init__(self, wizard):
        super().__init__(wizard, "Your cards", "What kind of note Ankime adds, and to which "
                         "deck.")
        cfg = wizard.cfg
        self.create = QRadioButton(f"Create an '{notes.NOTE_TYPE_NAME}' note type "
                                   "(the simplest if you are new to sentence mining)")
        self.existing = QRadioButton("Use a note type I already have")
        group = QButtonGroup(self)
        for button in (self.create, self.existing):
            group.addButton(button)
            button.toggled.connect(self._toggled)
        self.box.addWidget(self.create)
        about = _label("It has a field for everything Ankime can fill in: the sentence with "
                       "its audio and picture, the word with its reading, definition, pitch "
                       "accent and frequency, the translation, and where the card comes "
                       "from.")
        about.setContentsMargins(24, 0, 0, 6)
        self.box.addWidget(about)
        self.box.addWidget(self.existing)

        self.details = QWidget()
        form = QFormLayout(self.details)
        form.setContentsMargins(24, 0, 0, 0)
        self.note_type = QComboBox()
        self.note_type.setAccessibleName("Note type")
        self.note_type.addItems(_note_type_names())
        form.addRow("Note type", self.note_type)
        self.detected = _label()
        form.addRow(self.detected)
        self.fields = {}
        for key, what in first_run.SUMMARY_FIELDS:
            combo = QComboBox()
            combo.currentTextChanged.connect(lambda _text: self.completeChanged.emit())
            self.fields[key] = combo
            form.addRow(f"{what.capitalize()} field", combo)
        self.box.addWidget(self.details)

        self.deck = QComboBox()
        self.deck.setEditable(True)
        self.deck.addItems(sorted(d.name for d in mw.col.decks.all_names_and_ids()))
        self.deck.setCurrentText(cfg["deck"])
        self.deck.setAccessibleName("Deck for new cards")
        deck_row = QFormLayout()
        deck_row.addRow("Deck for new cards", self.deck)
        self.box.addSpacing(8)
        self.box.addLayout(deck_row)
        self.box.addWidget(_label("<small>A name that isn't a deck yet creates it.</small>"))
        self.box.addStretch(1)

        self._guessed = {}
        self.note_type.currentTextChanged.connect(self._note_type_changed)
        # someone already mining into Lapis, Kiku...: that note type; otherwise a new one
        mined = [name for name, _ in first_run.mined_note_types(mw.col)]
        if first_run.untouched(cfg) and mined:
            self.note_type.setCurrentText(mined[0])
            self.existing.setChecked(True)
        elif first_run.untouched(cfg):
            self.create.setChecked(True)
        else:  # run again: what is set up
            self.note_type.setCurrentText(cfg["note_type"])
            self.existing.setChecked(True)
        self._note_type_changed(self.note_type.currentText(), keep=not first_run.untouched(cfg))

    def _toggled(self, _checked):
        self.details.setEnabled(self.existing.isChecked())
        self.completeChanged.emit()

    def _note_type_changed(self, note_type, keep=False):
        """Fill the field dropdowns from the note type: its preset's fields, or the ones
        named like them; with `keep`, the fields the settings point at."""
        names = _field_names(note_type)
        preset, self._guessed = first_run.guess_fields(names)
        if keep:
            self._guessed = {key: self.w.cfg[key] for key in self._guessed}
        if preset:
            self.detected.setText(f"This looks like a <b>{html.escape(preset)}</b> note type: "
                                  "its fields are filled in for you.")
        else:
            self.detected.setText("Check which field gets what; a blank one is left out.")
        for key, combo in self.fields.items():
            combo.clear()
            if key != "sentence_field":
                combo.addItem("")
            combo.addItems(names)
            combo.setCurrentText(self._guessed.get(key, ""))

    def isComplete(self):
        return self.create.isChecked() or bool(self.note_type.currentText()
                                               and self.fields["sentence_field"].currentText())

    def note_type_name(self):
        """The note type cards will be: the Ankime one when it is to be created."""
        return notes.NOTE_TYPE_NAME if self.create.isChecked() else self.note_type.currentText()

    def values(self):
        values = {"deck": self.deck.currentText().strip() or config.DEFAULTS["deck"]}
        if not self.create.isChecked():
            values.update(self._guessed, note_type=self.note_type.currentText())
            values.update({key: combo.currentText() for key, combo in self.fields.items()})
        return values


class KnownWordsPage(_Page):
    def __init__(self, wizard):
        super().__init__(wizard, "Known words", "Ankime shows each word of the subtitles "
                         "new, learning or known, from your cards, so you can see which lines "
                         "have just one new word.")
        cfg = wizard.cfg
        self.interval = QSpinBox()
        self.interval.setRange(1, 365)
        self.interval.setSuffix(" days")
        self.interval.setValue(cfg["known_interval"])
        self.interval.setAccessibleName("Interval at which a word is known")
        row = QHBoxLayout()
        row.addWidget(QLabel("A word is known once its card's interval reaches"))
        row.addWidget(self.interval)
        row.addStretch(1)
        self.box.addLayout(row)
        self.no_word = _label()
        self.box.addWidget(self.no_word)
        self.others_label = _label("Also count the words of these cards:")
        self.box.addSpacing(8)
        self.box.addWidget(self.others_label)
        self.others = QVBoxLayout()
        self.box.addLayout(self.others)
        self._checks = []  # (checkbox, source)

        self.box.addSpacing(8)
        self.box.addWidget(_label("Already know words from JPDB, Migaku or a word list? "
                                  "Mark them known now, so they don't show as new:"))
        word_list = QPushButton("Mark known from a word list...")
        word_list.clicked.connect(self._word_list)
        row = QHBoxLayout()
        row.addWidget(word_list)
        row.addStretch(1)
        self.box.addLayout(row)
        self.box.addStretch(1)

    def initializePage(self):
        cards = self.w.cards
        note_type = cards.note_type_name()
        if not cards.create.isChecked() and not cards.fields["word_field"].currentText():
            self.no_word.setText(f"No word field is set for '{html.escape(note_type)}' "
                                 "(go Back to set one), so its cards can't tell which words you know: "
                                 "only the words you mark known count, and those of the "
                                 "note types below.")
        else:
            self.no_word.setText("")
        self.no_word.setVisible(bool(self.no_word.text()))
        checked = {s["note_type"] for s in self.w.cfg["vocab_sources"]}
        for box, _source in self._checks:
            box.deleteLater()
        self._checks = []
        for source in first_run.known_word_sources(mw.col, note_type):
            box = QCheckBox(f"{source['note_type']} (its {source['word_field']} field)")
            box.setChecked(not checked or source["note_type"] in checked)
            self.others.addWidget(box)
            self._checks.append((box, source))
        self.others_label.setVisible(bool(self._checks))

    def _word_list(self):
        from ..language import known_words
        known_words.import_word_list(self)

    def values(self):
        chosen = [source for box, source in self._checks if box.isChecked()]
        offered = {source["note_type"] for _box, source in self._checks}
        kept = [s for s in self.w.cfg["vocab_sources"]
                if not isinstance(s, dict) or s.get("note_type") not in offered]
        return {"known_interval": self.interval.value(), "vocab_sources": kept + chosen}


class DictionaryPage(_Page):
    def __init__(self, wizard):
        super().__init__(wizard, "Looking words up", "Where the definitions, readings and "
                         "pitch accents come from.")
        cfg = wizard.cfg
        self.local = QRadioButton("Ankime's own dictionaries (the simplest: nothing else to "
                                  "install)")
        self.yomitan = QRadioButton("Yomitan in your browser, through yomitan-api")
        group = QButtonGroup(self)
        for button in (self.local, self.yomitan):
            group.addButton(button)
        (self.local if yomitan.uses_local(cfg) else self.yomitan).setChecked(True)
        self._chosen = False  # picked here: the checks don't change it any more
        group.buttonClicked.connect(lambda _button: setattr(self, "_chosen", True))

        self.box.addWidget(self.local)
        inner = self.indented()
        inner.addWidget(_label("Yomitan dictionaries imported into Ankime. JMdict, an "
                               "English dictionary, is a 16 MB download."))
        self.local_status = _label(WAITING)
        inner.addWidget(self.local_status)
        row = QHBoxLayout()
        jmdict = QPushButton("Download JMdict")
        jmdict.clicked.connect(self._download_jmdict)
        manage = QPushButton("Import or manage dictionaries...")
        manage.clicked.connect(self._manage)
        row.addWidget(jmdict)
        row.addWidget(manage)
        row.addStretch(1)
        inner.addLayout(row)
        self.box.addSpacing(8)

        self.box.addWidget(self.yomitan)
        inner = self.indented()
        inner.addWidget(_label(
            "Uses the dictionaries you already have in Yomitan, and can add the word's "
            "audio to cards. The browser has to be open, with "
            f'<a href="{YOMITAN_API}">yomitan-api</a> set up.'))
        self.yomitan_status = _label(WAITING)
        inner.addWidget(self.yomitan_status)
        self.box.addStretch(1)
        self._results = {}

    def initializePage(self):
        self.check()

    def check(self):
        """Check both, then pick the one that works, unless one was picked here."""
        self._results = {}
        self.local_status.setText(WAITING)
        self.yomitan_status.setText(WAITING)
        cfg = self.w.cfg
        self.in_background(setup_check.check_local_dictionaries,
                           lambda result: self._checked("local", result))
        self.in_background(lambda: setup_check.check_yomitan(cfg["yomitan_url"],
                                                             cfg["scan_length"]),
                           lambda result: self._checked("yomitan", result))

    def _checked(self, which, result):
        self._results[which] = result
        label = self.local_status if which == "local" else self.yomitan_status
        _show_status(label, *result)
        if len(self._results) == 2 and not self._chosen:
            # the one that works; neither: Ankime's own, JMdict is a click away; both: as set
            yomitan_ok = self._results["yomitan"][0] == setup_check.OK
            local_ok = self._results["local"][0] != setup_check.FAIL
            if yomitan_ok != local_ok:
                (self.yomitan if yomitan_ok else self.local).setChecked(True)
            elif not yomitan_ok:
                self.local.setChecked(True)

    def _use_local(self, *_):
        self.local.setChecked(True)
        self._chosen = True
        yomitan.BREAKER.retry_now()
        self.check()

    def _download_jmdict(self):
        from .dictionaries import download_jmdict

        def done(future):
            try:
                titles = future.result()
            except Exception as e:
                if not isinstance(e, downloads.Cancelled):
                    showWarning(f"Couldn't download JMdict:\n\n{e}", parent=self)
                return
            tooltip(f"Imported {html.escape(', '.join(titles))}", parent=self)
            self._use_local()

        download_jmdict(self, done)

    def _manage(self):
        from .dictionaries import open_dictionaries
        open_dictionaries(self, self._use_local)
        self.check()

    def values(self):
        return {"dictionary": "local" if self.local.isChecked() else "yomitan"}


class ToolsPage(_Page):
    # (setting, name, what it does, browse for a folder?)
    TOOLS = [("mpv_dll_dir", "libmpv", "plays the videos", True),
             ("ffmpeg_path", "ffmpeg", "cuts each card's audio and picture", False)]

    def __init__(self, wizard):
        super().__init__(wizard, "Playing the video", "Two free programs do the work. On "
                         "Windows, Ankime can download them for you.")
        self.paths = {key: wizard.cfg[key] for key, *_ in self.TOOLS}
        self.status = {}
        self._run = {}  # setting -> number of its latest check: older results are dropped
        for key, name, what, folder in self.TOOLS:
            self.box.addWidget(_label(f"<b>{name}</b> {what}."))
            inner = self.indented()
            self.status[key] = _label(WAITING)
            inner.addWidget(self.status[key])
            buttons = QHBoxLayout()
            if downloads.can_install():
                download = QPushButton("Download")
                download.setAccessibleName(f"Download {name}")
                download.setToolTip(f"{name} {downloads.TOOLS[name].version}, "
                                    f"from {downloads.TOOLS[name].source}")
                download.clicked.connect(lambda _c=False, n=name: self._download(n))
                buttons.addWidget(download)
            browse = QPushButton("Browse...")
            browse.setAccessibleName(f"Choose {name}")
            browse.clicked.connect(lambda _c=False, k=key, f=folder: self._browse(k, f))
            buttons.addWidget(browse)
            buttons.addStretch(1)
            inner.addLayout(buttons)
            self.box.addSpacing(12)
        self.box.addWidget(_label("You can go on without them and set them up later: "
                                  "Tools > Ankime > Settings > Test setup."))
        self.box.addStretch(1)

    def initializePage(self):
        for key, *_ in self.TOOLS:
            self.check(key)

    def check(self, key):
        self._run[key] = run = self._run.get(key, 0) + 1
        self.status[key].setText(WAITING)
        cfg, path = self.w.cfg, self.paths[key]
        if key == "mpv_dll_dir":
            work = lambda: setup_check.check_libmpv(path)  # noqa: E731
        else:
            work = lambda: setup_check.check_ffmpeg(  # noqa: E731
                path, cfg["image_format"], cfg["animated_image"], cfg["audio_format"])

        def done(result):
            if self._run[key] != run:
                return
            status, message = result
            if status == setup_check.FAIL and "not found" in message and downloads.can_install():
                message = message.split(". ")[0] + ". Download it, or choose where it is."
            _show_status(self.status[key], status, message)

        self.in_background(work, done)

    def _set(self, key, value):
        self.paths[key] = value
        self.check(key)

    def _download(self, name):
        from .downloads import install_tool
        install_tool(self, name, self._set)

    def _browse(self, key, folder):
        if folder:
            path = QFileDialog.getExistingDirectory(self, "Folder containing libmpv",
                                                    self.paths[key])
        else:
            path, _ = QFileDialog.getOpenFileName(self, "Select ffmpeg", self.paths[key])
        if path:
            self._set(key, path)

    def values(self):
        return dict(self.paths)


class DonePage(_Page):
    def __init__(self, wizard, open_player):
        super().__init__(wizard, "All set", "Click Finish to save these settings:")
        self.summary = _label()
        self.box.addWidget(self.summary)
        self.open_player = QCheckBox("Open the player now")
        self.open_player.setChecked(True)
        self.open_player.setVisible(open_player is not None)
        self.box.addWidget(self.open_player)
        self.box.addSpacing(8)
        self.box.addWidget(_label(
            "In the player, open a video with its Japanese subtitles beside it "
            "(ep01.ja.srt next to ep01.mkv), hover a word to look it up, and press Ctrl+N to "
            "make a card of the line.<br><br>Every setting, hotkeys and gamepad included, is in Tools > "
            "Ankime > Settings, and this wizard is in Tools > Ankime > Setup wizard."))
        self.box.addStretch(1)

    def initializePage(self):
        values = {**self.w.cfg, **self.w.values()}
        create = self.w.cards.create.isChecked()
        self.summary.setText(f"<ul>{first_run.summary(values, create)}</ul>")


class SetupWizard(QWizard):
    """`open_player`: called after Finish if the last page's box is ticked (shown only
    then)."""

    def __init__(self, parent, open_player=None):
        super().__init__(parent)
        self.alive = True
        self._open_player = open_player
        self.cfg = dict(config.get())
        self.setWindowTitle("Ankime: Setup")
        self.setWizardStyle(QWizard.WizardStyle.ClassicStyle)  # follows Anki's theme
        self.setOption(QWizard.WizardOption.NoBackButtonOnStartPage, True)
        self.cards = CardsPage(self)
        self.known = KnownWordsPage(self)
        self.dictionary = DictionaryPage(self)
        self.tools = ToolsPage(self)
        self.done_page = DonePage(self, open_player)
        for page in (WelcomePage(self), self.cards, self.known, self.dictionary, self.tools,
                     self.done_page):
            self.addPage(page)
        self.setMinimumSize(640, 560)

    def values(self):
        """The settings chosen, those the wizard sets only."""
        values = {}
        for page in (self.cards, self.known, self.dictionary, self.tools):
            values.update(page.values())
        return values

    def accept(self):
        values = self.values()
        if self.cards.create.isChecked():
            try:
                values.update(notes.create_note_type(mw.col))
            except Exception as e:
                showWarning(f"Couldn't create the note type:\n\n{e}", parent=self)
                return
        values["vocab_sources"] = [s for s in values["vocab_sources"]
                                   if not isinstance(s, dict)
                                   or s.get("note_type") != values.get("note_type")]
        config.save(**values)  # an open player picks the changes up at once
        yomitan.BREAKER.retry_now()
        if mw.col.decks.id_for_name(values["deck"]) is None:
            from aqt.operations.deck import add_deck
            add_deck(parent=mw, name=values["deck"]).run_in_background()
        open_player = self._open_player and self.done_page.open_player.isChecked()
        super().accept()
        tooltip("Ankime is set up", parent=mw)
        if open_player:
            self._open_player()

    def reject(self):
        super().reject()
        tooltip("Tools > Ankime > Setup wizard runs it again any time", parent=mw)

    def done(self, result):
        self.alive = False
        super().done(result)
