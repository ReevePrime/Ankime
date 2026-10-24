"""Settings dialog: edits the add-on's config (core/config.py), which applies it at once."""
import html
import logging
import time

from aqt import mw

from aqt.qt import (QCheckBox, QColor, QColorDialog, QComboBox, QDialog, QDialogButtonBox,
                    QDoubleSpinBox, QFileDialog, QFormLayout, QGridLayout, QHBoxLayout,
                    QKeySequence,
                    QKeySequenceEdit, QLabel, QLineEdit, QMenu, QPushButton, QScrollArea,
                    QSpinBox, Qt, QTabWidget, QTimer, QToolButton, QVBoxLayout,
                    QWidget)
from aqt.utils import askUser, showWarning, tooltip

from ..cards import media, notes, presets
from ..controls import gamepad, hotkeys
from ..core import config, downloads
from ..language import line_filter, pitch, tokenizer, yomitan
from ..player import mpv_controller, popup, sub_panel
from ..subs import subtitles
from . import first_run, setup_check

# (config key, label) for the note-type fields; `optional` ones may be left blank.
REQUIRED_FIELDS = [("sentence_field", "Sentence field")]
OPTIONAL_FIELDS = [("sentence_furigana_field", "Sentence furigana field"),
                   ("context_field", "Context field"),
                   ("audio_field", "Audio field"), ("image_field", "Image field"),
                   ("word_field", "Word field"), ("key_field", "Key field"),
                   ("reading_field", "Reading field"),
                   ("definition_field", "Definition field"),
                   ("pitch_field", "Pitch accent field"), ("frequency_field", "Frequency field"),
                   ("word_audio_field", "Word audio field"),
                   ("translation_field", "Translation field"),
                   ("source_field", "Source field")]
REOPEN = 2  # SettingsDialog result: show the dialog again, with the settings as now saved

log = logging.getLogger(__name__)


class PathRow(QWidget):
    """Line edit plus a Browse button (file or folder)."""

    def __init__(self, text, pick_folder, caption):
        super().__init__()
        self._pick_folder, self._caption = pick_folder, caption
        self.edit = QLineEdit(text)
        browse = QPushButton("Browse...")
        browse.clicked.connect(self._browse)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.edit, 1)
        row.addWidget(browse)

    def _browse(self):
        if self._pick_folder:
            path = QFileDialog.getExistingDirectory(self, self._caption, self.edit.text())
        else:
            path, _ = QFileDialog.getOpenFileName(self, self._caption, self.edit.text())
        if path:
            self.edit.setText(path)

    def text(self):
        return self.edit.text().strip()


class GamepadButtons:
    """The Gamepad tab's bindings: a button per action showing its binding; click it, then
    press a button on the controller, or two (hold the first, press the second). The
    controllers are read while the dialog is open, to show which one is connected and in
    its own names."""
    POLL_MS = 16

    def __init__(self, dialog, form, bindings):
        self._dialog = dialog
        self._bindings = dict(bindings)
        self._buttons = {}         # action -> QPushButton
        self._capturing = None     # (action, gamepad.Capture)
        self._held = frozenset()
        self._pads = gamepad.Gamepads(run_in_background=mw.taskman.run_in_background)
        self._names = None
        self.status = QLabel()
        self.status.setWordWrap(True)
        form.addRow(self.status)
        for action in gamepad.ACTIONS:
            button = QPushButton()
            button.setAccessibleName(gamepad.ACTIONS[action])
            button.clicked.connect(lambda _checked=False, a=action: self._capture(a))
            clear = QPushButton("Clear")
            clear.setAccessibleName(f"Clear: {gamepad.ACTIONS[action]}")
            clear.clicked.connect(lambda _checked=False, a=action: self._set(a, ""))
            row = QHBoxLayout()
            row.addWidget(button, 1)
            row.addWidget(clear)
            self._buttons[action] = button
            form.addRow(gamepad.ACTIONS[action], row)
        self._timer = QTimer(dialog)
        self._timer.timeout.connect(self._poll)
        self._timer.start(self.POLL_MS)
        dialog.finished.connect(self.close)
        self._relabel()

    def value(self):
        return dict(self._bindings)

    def close(self, *_):
        self._timer.stop()
        self._pads.close()

    def _capture(self, action):
        if self._capturing and self._capturing[0] == action:
            self._capturing = None  # clicked again: never mind
        else:
            self._capturing = (action, gamepad.Capture(self._held))
        self._relabel()

    def _set(self, action, combo):
        """Bind `action` to `combo`, unbinding the action it was bound to before."""
        taken = [a for a, c in self._bindings.items() if combo and c == combo and a != action]
        for other in taken:
            self._bindings[other] = ""
        self._bindings[action] = combo
        self._capturing = None
        self._relabel()
        if taken:
            label = gamepad.combo_label(combo, self._pads.kind)
            tooltip(f"{html.escape(label)} no longer does: "
                    f"{html.escape(gamepad.ACTIONS[taken[0]])}", parent=self._dialog)

    def _poll(self):
        try:
            self._held = self._pads.poll(time.monotonic())
        except Exception as e:
            log.warning("couldn't read the gamepad: %s", e)
            self._timer.stop()
            self.status.setText(f"Couldn't read the gamepad: {e}")
            return
        if self._pads.names() != self._names:
            self._relabel()
        if self._capturing:
            combo = self._capturing[1].feed(self._held)
            if combo:
                self._set(self._capturing[0], combo)

    def _relabel(self):
        self._names = self._pads.names()
        kind = self._pads.kind
        if self._names:
            self.status.setText(f"Connected: {html.escape(', '.join(self._names))}. Click an "
                                "action's button, then press the gamepad's button for it, or "
                                "hold one and press another for a combination.")
        else:
            self.status.setText("No gamepad found: connect one (it's found within a few "
                                "seconds). Xbox controllers work as they are, and so do "
                                "PlayStation ones on Windows and Linux; others get numbered "
                                "buttons. On macOS, install SDL2 first (brew install sdl2).")
        capturing = self._capturing[0] if self._capturing else None
        for action, button in self._buttons.items():
            combo = self._bindings.get(action, "")
            button.setText("Press a button on the gamepad... (click to cancel)"
                           if action == capturing else
                           gamepad.combo_label(combo, kind) or "(none)")


def _field_names(note_type):
    model = mw.col.models.by_name(note_type)
    return [f["name"] for f in model["flds"]] if model else []


def _note_type_names():
    """Every note type's name, sorted (models.all_names is deprecated)."""
    return sorted(m.name for m in mw.col.models.all_names_and_ids())


def _sentence_field(field_names):
    """The sentence field of the preset the note type looks like, or ""."""
    preset = presets.detect(field_names)
    return presets.fields_for(preset, field_names)["sentence_field"] if preset else ""


class VocabSources(QWidget):
    """The vocab_sources setting: other note types whose cards count towards known words,
    one row (note type, word field) each. With `field_key` "sentence_field", the
    sentence_sources setting: other note types whose sentences count (sentence_words)."""

    def __init__(self, sources, field_key="word_field"):
        super().__init__()
        self._key = field_key
        self._guess = presets.word_field if field_key == "word_field" else _sentence_field
        self._rows = []  # (row widget, note type combo, field combo)
        self._box = QVBoxLayout(self)
        self._box.setContentsMargins(0, 0, 0, 0)
        add = QPushButton("Add a note type...")
        add.setToolTip(
            "Another note type whose cards count towards known words: a core deck, "
            "AnkiMorphs' am-study-morphs field..." if field_key == "word_field" else
            "Another note type of sentence cards: every word of a known card's sentence "
            "counts as known")
        add.clicked.connect(lambda: self._add_row("", ""))
        self._box.addWidget(add)
        for source in sources:
            if isinstance(source, dict):
                self._add_row(source.get("note_type", ""), source.get(field_key, ""))

    def _add_row(self, note_type, field):
        row = QWidget()
        line = QHBoxLayout(row)
        line.setContentsMargins(0, 0, 0, 0)
        types = QComboBox()
        types.addItems(_note_type_names())
        if note_type and types.findText(note_type) < 0:
            types.addItem(note_type)  # missing: shown rather than dropped
        types.setCurrentText(note_type)
        types.setAccessibleName("Note type")
        fields = QComboBox()
        fields.setAccessibleName("Word field" if self._key == "word_field"
                                 else "Sentence field")
        remove = QPushButton("Remove")
        remove.setAccessibleName("Remove this note type")
        entry = (row, types, fields)

        def refill(keep=None):
            current = keep if keep is not None else fields.currentText()
            names = _field_names(types.currentText())
            fields.clear()
            fields.addItems(names)
            if current and current not in names:
                fields.addItem(current)
            # a note type with a preset (Kaishi 1.5k...): its field, else the first one
            fields.setCurrentText(current or self._guess(names)
                                  or (names[0] if names else ""))

        types.currentTextChanged.connect(lambda _text: refill(""))
        refill(field)
        remove.clicked.connect(lambda: self._remove_row(entry))
        line.addWidget(types, 1)
        line.addWidget(QLabel("field"))
        line.addWidget(fields, 1)
        line.addWidget(remove)
        self._rows.append(entry)
        self._box.insertWidget(len(self._rows) - 1, row)

    def add(self, note_type, field):
        """Count `note_type` too, unless it is counted already."""
        if all(types.currentText() != note_type for _row, types, _fields in self._rows):
            self._add_row(note_type, field)
            return True
        return False

    def _remove_row(self, entry):
        self._rows.remove(entry)
        entry[0].deleteLater()

    def value(self):
        return [{"note_type": types.currentText(), self._key: fields.currentText()}
                for _row, types, fields in self._rows
                if types.currentText() and fields.currentText()]


class ColorButton(QToolButton):
    """A colour setting: "#rrggbb", or "" for the default. A click picks a colour; the
    arrow's menu goes back to the default."""

    def __init__(self, color, what):
        super().__init__()
        self._what = what
        self.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        menu = QMenu(self)
        menu.addAction("Default", lambda: self.set_value(""))
        self.setMenu(menu)
        self.clicked.connect(self._pick)
        self.setAccessibleName(what)
        self.set_value(color)

    def _pick(self):
        picked = QColorDialog.getColor(QColor(self._color or "#ffffff"), self, self._what)
        if picked.isValid():
            self.set_value(picked.name())

    def set_value(self, color):
        self._color = color if sub_panel.HEX_COLOR.fullmatch(color or "") else ""
        self.setText(self._color or "Default")
        if self._color:
            text = "#000000" if QColor(self._color).lightness() > 140 else "#ffffff"
            self.setStyleSheet(f"QToolButton {{ background: {self._color}; color: {text}; }}")
        else:
            self.setStyleSheet("")

    def value(self):
        return self._color


class PitchColors(QWidget):
    """The colour of each pitch-accent category (sub_panel.PITCH_COLORS), "" for Ankime's."""

    def __init__(self, colors):
        super().__init__()
        grid = QGridLayout(self)
        grid.setContentsMargins(0, 0, 0, 0)
        colors = colors if isinstance(colors, dict) else {}
        self.buttons = {}
        for n, (name, label) in enumerate(pitch.CATEGORIES):
            short = name.capitalize()
            button = self.buttons[name] = ColorButton(colors.get(name, ""),
                                                      f"{short} colour")
            button.setToolTip(f"{label}. Default: {sub_panel.PITCH_COLORS[name][0]}, "
                              "lighter in night mode and fullscreen")
            text = QLabel(short)
            text.setBuddy(button)
            grid.addWidget(text, n // 3, (n % 3) * 2)
            grid.addWidget(button, n // 3, (n % 3) * 2 + 1)
        grid.setColumnStretch(6, 1)

    def value(self):
        return {name: button.value() for name, button in self.buttons.items()}


class WordStyle(QWidget):
    """The style of the words of one status (see sub_panel.WORD_STYLES), on one row."""

    def __init__(self, style, label):
        super().__init__()
        line = QHBoxLayout(self)
        line.setContentsMargins(0, 0, 0, 0)
        self.color = ColorButton("", f"{label}: colour")
        self.color.setToolTip("Text colour. Default: Ankime's, lighter in night mode and "
                              "fullscreen")
        self.background = ColorButton("", f"{label}: background")
        self.background.setToolTip("Highlight behind the word. Default: none")
        self.bold = QCheckBox("Bold")
        self.italic = QCheckBox("Italic")
        self.underline = SettingsDialog._data_combo(sub_panel.UNDERLINES, "none")
        self.underline.setAccessibleName(f"{label}: underline")
        self.size = QSpinBox()
        self.size.setRange(*sub_panel.WORD_SIZES)
        self.size.setSingleStep(10)
        self.size.setSuffix(" %")
        self.size.setAccessibleName(f"{label}: size")
        self.size.setToolTip("Size, relative to the subtitle size")
        for widget, text in ((self.color, "colour"), (self.background, "background")):
            line.addWidget(QLabel(text))
            line.addWidget(widget)
        for widget in (self.bold, self.italic, self.underline, self.size):
            line.addWidget(widget)
        line.addStretch(1)
        self.set_value(style)

    def set_value(self, style):
        style = style if isinstance(style, dict) else {}
        self.color.set_value(style.get("color", ""))
        self.background.set_value(style.get("background", ""))
        self.bold.setChecked(bool(style.get("bold")))
        self.italic.setChecked(bool(style.get("italic")))
        self.underline.setCurrentIndex(max(self.underline.findData(style.get("underline")), 0))
        self.size.setValue(sub_panel.word_size(style))

    def value(self):
        return {"color": self.color.value(), "background": self.background.value(),
                "bold": self.bold.isChecked(), "italic": self.italic.isChecked(),
                "underline": self.underline.currentData(), "size": self.size.value()}


class SettingsDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Ankime Settings")
        self.cfg = config.get()
        cfg = self.cfg

        self.tabs = QTabWidget()

        # Cards: where they go and what they hold
        form = self._tab("Cards")
        self.deck = self._combo(cfg["deck"], [d.name for d in mw.col.decks.all_names_and_ids()])
        self.note_type = self._combo(cfg["note_type"], _note_type_names())
        self.note_type.currentTextChanged.connect(self._reload_fields)
        create = QPushButton("Create Ankime note type")
        create.setToolTip("Add a note type with a field for everything Ankime can fill in, "
                          "and select it with its fields below")
        create.clicked.connect(self._create_note_type)
        note_type_row = QHBoxLayout()
        note_type_row.addWidget(self.note_type, 1)
        note_type_row.addWidget(create)
        form.addRow("Deck", self.deck)
        form.addRow("Note type", note_type_row)
        self.preset = QComboBox()
        self.preset.addItem("Fill in the fields for...", "")
        for name in presets.PRESETS:
            self.preset.addItem(name, name)
        self.preset.setToolTip("Point the fields below at those of a popular note type. "
                               "Fields it doesn't have are left blank")
        self.preset.activated.connect(self._apply_preset)
        form.addRow("Preset", self.preset)

        self.fields = {}
        for key, label in REQUIRED_FIELDS + OPTIONAL_FIELDS:
            self.fields[key] = QComboBox()
            form.addRow(label, self.fields[key])
        self._reload_fields()
        self.fields["source_field"].setToolTip(
            "Receives where the card comes from: show · episode · time")
        self.fields["sentence_furigana_field"].setToolTip(
            "Receives the sentence with readings, 日本語[にほんご]を 勉強[べんきょう]する, for "
            "the {{furigana:}} filter")
        self.fields["context_field"].setToolTip("Receives the line before the card's")
        self.fields["key_field"].setToolTip(
            "Receives the word a second time, for note types that keep it twice "
            "(JP Mining Note's Key)")

        self.definition = self._data_combo(yomitan.DEFINITION_MARKERS,
                                           cfg["definition_marker"])
        self.definition.setToolTip("What goes in the definition field")
        form.addRow("Definition", self.definition)
        self.bold = QCheckBox("Bold the word in the sentence")
        self.bold.setChecked(cfg["bold_word"])
        form.addRow("", self.bold)
        self.review = QCheckBox("Review each card in Anki's Add window before adding it")
        self.review.setToolTip(
            "The card opens in Anki's Add window, filled in, to trim the sentence or keep "
            "one definition before you add it. Cards added in a batch, or for the episode's "
            "lines, are added straight away")
        self.review.setChecked(cfg["review_before_adding"])
        form.addRow("", self.review)
        self.tags = QLineEdit(" ".join(cfg["tags"]))
        self.tags.setToolTip("Added to every card, separated by spaces")
        form.addRow("Tags", self.tags)
        self.show_tag = QCheckBox("Tag cards with their show (ankime::Show)")
        self.show_tag.setToolTip("The show is the name of the video's folder")
        self.show_tag.setChecked(cfg["show_tag"])
        form.addRow("", self.show_tag)

        # Media
        form = self._tab("Media")
        self.padding = QDoubleSpinBox()
        self.padding.setRange(0, 5)
        self.padding.setSingleStep(0.1)
        self.padding.setSuffix(" s")
        self.padding.setValue(cfg["audio_padding"])
        form.addRow("Audio padding", self.padding)
        self.normalize = QCheckBox("Normalize the volume of card audio")
        self.normalize.setChecked(cfg["normalize_audio"])
        form.addRow("", self.normalize)
        self.audio_format = self._data_combo(media.AUDIO_FORMATS, cfg["audio_format"])
        self.audio_format.setToolTip("Opus is the smallest, but AnkiMobile before iOS 17 "
                                     "can't play it")
        form.addRow("Audio format", self.audio_format)
        self.image_format = self._data_combo(media.IMAGE_FORMATS, cfg["image_format"])
        form.addRow("Picture format", self.image_format)
        self.animated_image = self._data_combo(media.ANIMATED_FORMATS, cfg["animated_image"])
        self.animated_image.setToolTip(
            f"A short silent clip of the line (up to {media.ANIMATED_MAX_SECONDS} s) "
            "instead of a still picture. Larger files; a still picture is taken if ffmpeg "
            "can't make it")
        form.addRow("Animated picture", self.animated_image)
        self.image_height = QSpinBox()
        self.image_height.setRange(0, 4320)
        self.image_height.setSingleStep(60)
        self.image_height.setSuffix(" px")
        self.image_height.setSpecialValueText("Original size")
        self.image_height.setValue(cfg["image_height"])
        self.image_height.setToolTip("Pictures taller than this are scaled down")
        form.addRow("Picture max height", self.image_height)
        self.ffmpeg = PathRow(cfg["ffmpeg_path"], False, "Select ffmpeg")
        form.addRow("ffmpeg", self._with_download(self.ffmpeg, "ffmpeg"))
        self.mpv_dir = PathRow(cfg["mpv_dll_dir"], True, "Folder containing libmpv-2.dll")
        self.mpv_dir.setToolTip("Takes effect the next time the player is opened")
        form.addRow("libmpv folder", self._with_download(self.mpv_dir, "libmpv"))
        self.video_output = self._data_combo(mpv_controller.VIDEO_OUTPUTS, cfg["video_output"])
        self.video_output.setToolTip(
            "How mpv shows the video. Automatic draws it with OpenGL, which works everywhere "
            "Anki does; the native window is the older way (Windows and X11 only), to try if "
            "the video stays black. Takes effect the next time the player is opened")
        form.addRow("Video output", self.video_output)

        # Player
        form = self._tab("Player")
        self.seek = QSpinBox()
        self.seek.setRange(1, 600)
        self.seek.setSuffix(" s")
        self.seek.setValue(cfg["seek_seconds"])
        self.seek.setToolTip("How far the rewind / forward buttons and hotkeys jump")
        form.addRow("Rewind / forward", self.seek)
        self.source_lang = self._language_combo(cfg["source_lang"])
        self.source_lang.setToolTip("Language of the subtitles you learn from, found by the "
                                    "tags in file names (ep01.ja.srt) and of embedded tracks "
                                    "(Ankime is for Japanese only)")
        form.addRow("Source language", self.source_lang)
        self.target_lang = self._language_combo(cfg["target_lang"])
        self.target_lang.setToolTip("Language of the translation. Used when a translation "
                                    "field is set")
        form.addRow("Target language", self.target_lang)
        self.sub_step = QDoubleSpinBox()
        self.sub_step.setRange(0.01, 5)
        self.sub_step.setSingleStep(0.05)
        self.sub_step.setDecimals(2)
        self.sub_step.setSuffix(" s")
        self.sub_step.setValue(cfg["sub_delay_step"])
        self.sub_step.setToolTip("How much each subtitle-delay key press shifts the subtitles")
        form.addRow("Subtitle delay step", self.sub_step)
        self.merge_lines = QCheckBox("Cards take the whole sentence when it is split over lines")
        self.merge_lines.setToolTip("A line ending in 、 or in a particle like を or に goes "
                                    "on into the next one: the card takes both (up to 3 "
                                    "lines). The range shows under the line; reset it with "
                                    "its hotkey")
        self.merge_lines.setChecked(cfg["merge_split_lines"])
        form.addRow("", self.merge_lines)
        self.jimaku_key = QLineEdit(cfg["jimaku_api_key"])
        self.jimaku_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.jimaku_key.setPlaceholderText("From your account page on jimaku.cc")
        self.jimaku_key.setToolTip("For More > Search Jimaku... in the player: make a free "
                                   "account on jimaku.cc and generate an API key there")
        form.addRow("Jimaku API key", self.jimaku_key)
        self.sub_sync_tool = PathRow(cfg["sub_sync_tool"], False, "Select alass or ffsubsync")
        self.sub_sync_tool.setToolTip("alass (alass-cli) or ffsubsync, installed separately: "
                                      "aligns subtitles to the audio, after a Jimaku download "
                                      "or with More > Align subtitles to the audio")
        form.addRow("Subtitle aligner", self.sub_sync_tool)
        self.transcriber = PathRow(cfg["transcriber"], False,
                                   "Select faster-whisper or whisper.cpp (whisper-cli)")
        self.transcriber.setToolTip("faster-whisper (Faster-Whisper-XXL, whisper-ctranslate2) "
                                    "or whisper.cpp (whisper-cli), installed separately: "
                                    "makes subtitles from the speech, for shows with none on "
                                    "Jimaku (More > Generate subtitles from speech)")
        form.addRow("Transcriber", self.transcriber)
        self.transcriber_model = QLineEdit(cfg["transcriber_model"])
        self.transcriber_model.setPlaceholderText("Default: the transcriber's own, or the "
                                                  "largest ggml model next to whisper-cli")
        self.transcriber_model.setToolTip("faster-whisper: a model name (large-v3, "
                                          "large-v3-turbo...). whisper.cpp: the path to a "
                                          "multilingual ggml-*.bin file (not a .en one)")
        form.addRow("Transcriber model", self.transcriber_model)
        self.line_filter = self._data_combo(line_filter.LINE_FILTERS, cfg["line_filter"])
        self.line_filter.setToolTip("i+1 = a line with exactly one new word. A line you "
                                    "'fully know' has no new words (learning words don't count)")
        form.addRow("Line filter", self.line_filter)
        self.i1_pause = self._data_combo(line_filter.I1_PAUSE, cfg["i1_pause"])
        self.i1_pause.setToolTip("Pause automatically on i+1 lines, to read the line first "
                                 "or once you have heard it; or after every line")
        form.addRow("Auto pause", self.i1_pause)
        self.font_size = QSpinBox()
        self.font_size.setRange(12, 72)
        self.font_size.setSuffix(" px")
        self.font_size.setValue(cfg["subtitle_font_size"])
        self.font_size.setToolTip("Size of the subtitles under the video (a fifth larger in "
                                  "fullscreen)")
        form.addRow("Subtitle size", self.font_size)
        follow = cfg["popup_font_size"] == popup.FOLLOW_SUBTITLES
        self.popup_font_size = QSpinBox()
        self.popup_font_size.setRange(*popup.FONT_SIZES)
        self.popup_font_size.setSuffix(" px")
        self.popup_font_size.setValue(config.DEFAULTS["popup_font_size"] if follow
                                      else cfg["popup_font_size"])
        self.popup_font_size.setToolTip("Size of the text in a word's definition popup; the "
                                        "popup grows with it, and with the subtitles' zoom "
                                        "and fullscreen")
        self.popup_follow = QCheckBox("Follow the subtitle size")
        self.popup_follow.setToolTip("Keep the definitions in proportion to the subtitles: "
                                     "15 px at the default 28 px subtitles")
        self.popup_follow.toggled.connect(lambda on: self.popup_font_size.setEnabled(not on))
        self.popup_follow.setChecked(follow)
        self.popup_font_size.setEnabled(not follow)
        popup_row = QHBoxLayout()
        popup_row.addWidget(self.popup_font_size)
        popup_row.addWidget(self.popup_follow)
        popup_row.addStretch(1)
        form.addRow("Definition size", popup_row)
        # how new (orange), learning (blue) and known words look: for colour-blind users,
        # an underline, bold or a size tells them apart without colour
        self.word_styles = {}
        for _, key, label in sub_panel.WORD_STYLES:
            self.word_styles[key] = WordStyle(cfg[key], label)
            form.addRow(label, self.word_styles[key])
        self.style_preset = QComboBox()
        self.style_preset.setAccessibleName("Word style presets")
        self.style_preset.addItem("Set the word styles to...", None)
        self.style_preset.addItem("Default", {status: config.DEFAULTS[key]
                                              for status, key, _ in sub_panel.WORD_STYLES})
        self.style_preset.setItemData(
            1, "New words orange and bold, learning words blue, known words plain",
            Qt.ItemDataRole.ToolTipRole)
        for name, tip, styles in sub_panel.WORD_STYLE_PRESETS:
            self.style_preset.addItem(name, styles)
            self.style_preset.setItemData(self.style_preset.count() - 1, tip,
                                          Qt.ItemDataRole.ToolTipRole)
        self.style_preset.setToolTip("Fill in the three rows above. The colour-blind presets "
                                     "keep the colours and add underlines, bold or a size")
        self.style_preset.activated.connect(self._apply_style_preset)
        form.addRow("", self.style_preset)
        self.furigana = self._data_combo(sub_panel.FURIGANA, cfg["furigana"])
        self.furigana.setToolTip("Which words show their reading above them")
        form.addRow("Furigana", self.furigana)
        self.pitch_display = self._data_combo(sub_panel.PITCH_DISPLAY, cfg["pitch_display"])
        self.pitch_display.setToolTip(
            "Show each word's pitch accent (heiban, atamadaka, nakadaka, odaka, kifuku) by "
            "colour: over the word's own colour, or as a line under it that keeps the "
            "new / learning colours. It needs a pitch-accent dictionary in Yomitan (or "
            "Ankime's own)")
        form.addRow("Pitch accent", self.pitch_display)
        self.pitch_colors = PitchColors(cfg["pitch_colors"])
        self.pitch_display.currentIndexChanged.connect(
            lambda _: self.pitch_colors.setEnabled(self.pitch_display.currentData() != "off"))
        self.pitch_colors.setEnabled(cfg["pitch_display"] != "off")
        form.addRow("Pitch colours", self.pitch_colors)
        self.translation_display = self._data_combo(sub_panel.TRANSLATION_DISPLAY,
                                                    cfg["translation_display"])
        self.translation_display.setToolTip(
            "The target-language subtitle under the line. Turned on, it loads from the next "
            "video you open")
        form.addRow("Translation", self.translation_display)
        self.goal = QSpinBox()
        self.goal.setRange(0, 1440)
        self.goal.setSingleStep(5)
        self.goal.setSuffix(" min")
        self.goal.setSpecialValueText("No goal")
        self.goal.setValue(cfg["daily_goal_minutes"])
        self.goal.setToolTip("Minutes to watch each day, shown in the immersion log")
        form.addRow("Daily goal", self.goal)
        self.pause_on_lookup = QCheckBox("Pause while a word's definition is open")
        self.pause_on_lookup.setToolTip("Playback pauses when the popup opens on a word and "
                                        "goes on when it closes (unless you paused yourself)")
        self.pause_on_lookup.setChecked(cfg["pause_on_lookup"])
        form.addRow("", self.pause_on_lookup)

        # Lookup and known words
        form = self._tab("Lookup")
        self.dictionary = self._data_combo(yomitan.DICTIONARIES, cfg["dictionary"])
        self.dictionary.setToolTip(
            "Where words are looked up. Yomitan needs your browser open with yomitan-api; "
            "Ankime's own dictionaries are Yomitan dictionaries imported into Ankime (JMdict "
            "in one click), and need nothing running. Word audio comes from Yomitan only")
        manage = QPushButton("Dictionaries...")
        manage.setToolTip("Download JMdict, import or remove Ankime's own dictionaries")
        manage.clicked.connect(self._open_dictionaries)
        dictionary_row = QHBoxLayout()
        dictionary_row.addWidget(self.dictionary, 1)
        dictionary_row.addWidget(manage)
        form.addRow("Dictionary", dictionary_row)
        self.yomitan = QLineEdit(cfg["yomitan_url"])
        form.addRow("Yomitan API URL", self.yomitan)
        self.dictionary.currentIndexChanged.connect(
            lambda _i: self.yomitan.setEnabled(self.dictionary.currentData() == "yomitan"))
        self.yomitan.setEnabled(cfg["dictionary"] == "yomitan")
        self.scan = QSpinBox()
        self.scan.setRange(1, 100)
        self.scan.setValue(cfg["scan_length"])
        self.scan.setToolTip("Longest word, in characters, Yomitan tries when splitting a line")
        form.addRow("Scan length", self.scan)
        self.parser = self._data_combo(tokenizer.PARSERS, cfg["parser"])
        self.parser.setToolTip(
            "How lines are split into words. MeCab analyses the grammar, so a particle stays "
            "a particle (と|いけません); Yomitan's parser takes the longest dictionary word "
            "it finds (とい|け|ません). MeCab comes with other add-ons, such as "
            "ankimorphs-japanese-mecab: Test setup says whether it is found")
        form.addRow("Parser", self.parser)
        self.interval = QSpinBox()
        self.interval.setRange(1, 3650)
        self.interval.setSuffix(" days")
        self.interval.setValue(cfg["known_interval"])
        self.interval.setToolTip("A word whose strongest card has an interval (or, with FSRS, a "
                                 "stability) of at least this is 'known'")
        form.addRow("Known interval", self.interval)
        self.count_suspended = QCheckBox("Suspended cards count towards known words")
        self.count_suspended.setChecked(cfg["count_suspended"])
        form.addRow("", self.count_suspended)
        self.vocab_sources = VocabSources(cfg["vocab_sources"])
        form.addRow("Also count", self.vocab_sources)
        self.sentence_words = QCheckBox("Every word of a known sentence card is known")
        self.sentence_words.setToolTip(
            "A card of the note type above (its sentence field) or of the note types below "
            "that has reached the known interval makes every word of its sentence known, as "
            "Yomitan splits it. Yomitan has to be running when such a card is new or edited.")
        self.sentence_words.setChecked(cfg["sentence_words"])
        form.addRow("", self.sentence_words)
        self.sentence_sources = VocabSources(cfg["sentence_sources"], "sentence_field")
        self.sentence_sources.setEnabled(cfg["sentence_words"])
        self.sentence_words.toggled.connect(self.sentence_sources.setEnabled)
        form.addRow("Sentence cards", self.sentence_sources)

        # Hotkeys (click the box and press the combination; Clear unbinds)
        form = self._tab("Hotkeys")
        self.hotkey_edits = {}
        for action, key in hotkeys.get().items():
            edit = QKeySequenceEdit(QKeySequence(key))
            if hasattr(edit, "setMaximumSequenceLength"):  # Qt 6.5+; older Ankis lack it
                edit.setMaximumSequenceLength(1)  # one key combination, no chords
            clear = QPushButton("Clear")
            clear.clicked.connect(edit.clear)
            row = QHBoxLayout()
            row.addWidget(edit, 1)
            row.addWidget(clear)
            self.hotkey_edits[action] = edit
            form.addRow(hotkeys.ACTIONS[action], row)

        # Gamepad (click an action's button, then press the gamepad's)
        form = self._tab("Gamepad")
        self.gamepad_on = QCheckBox("Use a gamepad in the player")
        self.gamepad_on.setChecked(cfg["gamepad"])
        self.gamepad_on.setToolTip("Its buttons run the player's actions, like the hotkeys, "
                                   "while the player is the active window")
        form.addRow(self.gamepad_on)
        note = QLabel("The left stick moves through the words of the line, opening each "
                      "one's definition; a button held while another is pressed makes a "
                      "combination (LB + A). A button used in a combination does its own "
                      "action when let go.")
        note.setWordWrap(True)
        form.addRow(note)
        self.gamepad_buttons = GamepadButtons(self, form, gamepad.get(cfg))

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        test = buttons.addButton("Test setup", QDialogButtonBox.ButtonRole.ActionRole)
        test.setToolTip("Check libmpv, ffmpeg, the dictionary and the note type with these "
                        "settings, and fix what can be")
        test.clicked.connect(self.test_setup)
        advanced = buttons.addButton("Advanced (JSON)...", QDialogButtonBox.ButtonRole.ActionRole)
        advanced.setToolTip("Every setting in Anki's config editor, with its documentation")
        advanced.clicked.connect(self.open_json_editor)

        layout = QVBoxLayout(self)
        layout.addWidget(self.tabs, 1)
        layout.addWidget(buttons)
        self.setMinimumWidth(560)
        self.resize(600, 640)

    def _with_download(self, row, tool):
        """`row` (a PathRow) with a button downloading `tool`, where that can be done."""
        if not downloads.can_install():
            return row
        button = QPushButton("Download...")
        button.setToolTip(f"Download {tool} and set it up here: "
                          f"{downloads.TOOLS[tool].version}")
        button.setAccessibleName(f"Download {tool}")

        def install():
            from .downloads import install_tool
            install_tool(self, tool, self._fixed)

        button.clicked.connect(install)
        line = QHBoxLayout()
        line.addWidget(row, 1)
        line.addWidget(button)
        return line

    def _fixed(self, key, value):
        """A setting saved by Test setup, a download or the dictionaries: shown here, so
        that saving the form keeps it."""
        self.cfg = config.get()
        if key == "ffmpeg_path":
            self.ffmpeg.edit.setText(value)
        elif key == "mpv_dll_dir":
            self.mpv_dir.edit.setText(value)
        elif key == "dictionary":
            self.dictionary.setCurrentIndex(max(self.dictionary.findData(value), 0))

    def _open_dictionaries(self):
        from .dictionaries import open_dictionaries
        open_dictionaries(self, self._fixed)

    def _tab(self, title):
        """A new tab holding a form (scrolling, for the long ones), returned to fill."""
        form = QFormLayout()
        inner = QWidget()
        inner.setLayout(form)
        scroll = QScrollArea()
        scroll.setWidget(inner)
        scroll.setWidgetResizable(True)
        self.tabs.addTab(scroll, title)
        return form

    @classmethod
    def _language_combo(cls, current):
        """Dropdown of the languages subtitles are recognised in; a code outside the list
        (set in the JSON config) is kept as an extra item."""
        options = [(code, f"{name} ({code})") for code, name in subtitles.LANGUAGES]
        current = subtitles.lang_code(current)
        if current and current not in dict(options):
            options.append((current, current))
        return cls._data_combo(options, current)

    @staticmethod
    def _data_combo(options, current):
        """Dropdown over (config value, label) pairs."""
        combo = QComboBox()
        for value, label in options:
            combo.addItem(label, value)
        combo.setCurrentIndex(max(combo.findData(current), 0))
        return combo

    @staticmethod
    def _combo(current, items):
        combo = QComboBox()
        combo.setEditable(True)  # keep a deck/note type that no longer exists visible
        combo.addItems(items)
        combo.setCurrentText(current)
        return combo

    def _create_note_type(self):
        values = first_run.create_and_use(self)
        if not values:
            return
        self.cfg = config.get()
        self.note_type.blockSignals(True)
        if self.note_type.findText(values["note_type"]) < 0:
            self.note_type.addItem(values["note_type"])
        self.note_type.setCurrentText(values["note_type"])
        self.note_type.blockSignals(False)
        for combo in self.fields.values():
            combo.clear()  # so _reload_fields takes the saved choices
        self._reload_fields()
        tooltip(f"Created '{values['note_type']}' with fields: "
                f"{', '.join(notes.NOTE_TYPE_FIELDS)}", parent=self)

    def _apply_style_preset(self, index):
        """Fill in the word style rows from the preset chosen ({status: style})."""
        styles = self.style_preset.itemData(index)
        self.style_preset.setCurrentIndex(0)
        if not styles:
            return
        for status, key, _ in sub_panel.WORD_STYLES:
            self.word_styles[key].set_value(styles[status])

    def _apply_preset(self, index):
        """Point the field dropdowns at the preset's fields. If the note type selected isn't
        one of that kind, a note type that is (by its fields, or its name) is selected.
        Other note types with a preset have their words counted as known (Lookup > Also
        count)."""
        preset = self.preset.itemData(index)
        self.preset.setCurrentIndex(0)
        if not preset:
            return
        all_names = _note_type_names()
        if presets.detect(_field_names(self.note_type.currentText())) != preset:
            match = (next((n for n in all_names if presets.detect(_field_names(n)) == preset),
                          None)
                     or next((n for n in all_names if preset.lower() in n.lower()), None))
            if match:
                self.note_type.setCurrentText(match)  # refills the field dropdowns
        note_type = self.note_type.currentText()
        names = _field_names(note_type)
        found = presets.fields_for(preset, names)
        if not found["sentence_field"]:
            showWarning(f"'{note_type}' doesn't look like a {preset} note type: it has no "
                        f"'{presets.PRESETS[preset]['sentence_field']}' field.\n\n"
                        "Import the note type first (or select it above), then choose the "
                        "preset again.", parent=self)
            return
        for key, field in found.items():
            self.fields[key].setCurrentText(field)
        counted = []
        for other in all_names:
            if other != note_type and (word := presets.word_field(_field_names(other))):
                if self.vocab_sources.add(other, word):
                    counted.append(other)
        lacking = presets.missing(preset, names)
        message = f"Fields set for {preset} on '{html.escape(note_type)}'."
        if lacking:
            message += f"<br>Not on this note type, left blank: {html.escape(', '.join(lacking))}"
        if counted:
            message += ("<br>Known words are also read from: "
                        f"{html.escape(', '.join(counted))} (Lookup tab)")
        tooltip(message + "<br>Save to keep them.", parent=self, period=6000)

    def _reload_fields(self):
        """Refill the field dropdowns from the selected note type, keeping saved choices."""
        model = mw.col.models.by_name(self.note_type.currentText())
        names = [f["name"] for f in model["flds"]] if model else []
        for key, combo in self.fields.items():
            current = combo.currentText() if combo.count() else self.cfg[key]
            combo.clear()
            if key not in dict(REQUIRED_FIELDS):
                combo.addItem("")  # blank = skip this field
            combo.addItems(names)
            if current and current not in names:
                combo.addItem(current)  # not on this note type: show it rather than drop it
            combo.setCurrentText(current)

    def _collect(self):
        """The settings as shown, as a config dict (not saved)."""
        cfg = dict(self.cfg)
        cfg["hotkeys"] = {a: e.keySequence().toString() for a, e in self.hotkey_edits.items()}
        cfg["gamepad"] = self.gamepad_on.isChecked()
        cfg["gamepad_buttons"] = self.gamepad_buttons.value()
        cfg["deck"] = self.deck.currentText().strip()
        cfg["note_type"] = self.note_type.currentText().strip()
        for key, combo in self.fields.items():
            cfg[key] = combo.currentText()
        cfg["tags"] = self.tags.text().split()
        cfg["audio_padding"] = round(self.padding.value(), 2)
        cfg["ffmpeg_path"] = self.ffmpeg.text()
        cfg["mpv_dll_dir"] = self.mpv_dir.text()
        cfg["video_output"] = self.video_output.currentData()
        cfg["dictionary"] = self.dictionary.currentData()
        cfg["yomitan_url"] = self.yomitan.text().strip().rstrip("/")
        cfg["scan_length"] = self.scan.value()
        cfg["parser"] = self.parser.currentData()
        cfg["known_interval"] = self.interval.value()
        cfg["count_suspended"] = self.count_suspended.isChecked()
        cfg["seek_seconds"] = self.seek.value()
        cfg["source_lang"] = self.source_lang.currentData()
        cfg["target_lang"] = self.target_lang.currentData()
        cfg["sub_delay_step"] = round(self.sub_step.value(), 2)
        cfg["merge_split_lines"] = self.merge_lines.isChecked()
        cfg["jimaku_api_key"] = self.jimaku_key.text().strip()
        cfg["sub_sync_tool"] = self.sub_sync_tool.text()
        cfg["transcriber"] = self.transcriber.text()
        cfg["transcriber_model"] = self.transcriber_model.text().strip()
        cfg["line_filter"] = self.line_filter.currentData()
        cfg["i1_pause"] = self.i1_pause.currentData()
        cfg["subtitle_font_size"] = self.font_size.value()
        cfg["popup_font_size"] = (popup.FOLLOW_SUBTITLES if self.popup_follow.isChecked()
                                  else self.popup_font_size.value())
        for key, row in self.word_styles.items():
            cfg[key] = row.value()
        cfg["furigana"] = self.furigana.currentData()
        cfg["pitch_display"] = self.pitch_display.currentData()
        cfg["pitch_colors"] = self.pitch_colors.value()
        cfg["translation_display"] = self.translation_display.currentData()
        cfg["daily_goal_minutes"] = self.goal.value()
        cfg["definition_marker"] = self.definition.currentData()
        cfg["bold_word"] = self.bold.isChecked()
        cfg["review_before_adding"] = self.review.isChecked()
        cfg["show_tag"] = self.show_tag.isChecked()
        cfg["normalize_audio"] = self.normalize.isChecked()
        cfg["audio_format"] = self.audio_format.currentData()
        cfg["animated_image"] = self.animated_image.currentData()
        cfg["image_format"] = self.image_format.currentData()
        cfg["image_height"] = self.image_height.value()
        cfg["vocab_sources"] = self.vocab_sources.value()
        cfg["sentence_words"] = self.sentence_words.isChecked()
        cfg["sentence_sources"] = self.sentence_sources.value()
        cfg["pause_on_lookup"] = self.pause_on_lookup.isChecked()
        return cfg

    def open_json_editor(self):
        """Anki's JSON editor for the add-on's config (the Config button of Tools > Add-ons
        opens this form instead), with config.md beside it. The form is shown again
        afterwards, with what was saved there."""
        if config.strip(self._collect()) != config.strip(config.get()) and not askUser(
                "Discard the changes made in this form?", parent=self):
            return
        mgr = mw.addonManager
        try:
            from aqt.addons import ConfigEditor
            self.mgr = mgr  # what ConfigEditor expects of the dialog that opens it
            ConfigEditor(self, mgr.addonFromModule(config.ADDON),
                         mgr.getConfig(config.ADDON)).exec()
        except Exception as e:
            log.exception("couldn't open Anki's config editor")
            showWarning(f"Couldn't open Anki's config editor:\n\n{e}", parent=self)
            return
        self.done(REOPEN)

    def test_setup(self):
        setup_check.SetupCheckDialog(self, self._collect(), self._fixed).exec()

    def save(self):
        cfg = self._collect()
        if not cfg["sentence_field"]:
            showWarning("The sentence field is required.")
            return
        bound = [k for k in cfg["hotkeys"].values() if k]
        if len(bound) != len(set(bound)):
            twice = sorted({k for k in bound if bound.count(k) > 1})
            showWarning(f"Two actions have the same hotkey: {', '.join(twice)}.", parent=self)
            return
        config.write(cfg)  # an open player picks the changes up at once
        self.accept()


def open_settings(*_):
    if mw.col is None:
        showWarning("Open a profile first.")
        return
    if first_run.offer(then=open_settings):
        return  # a new install: the setup wizard first, the settings after
    while SettingsDialog(mw).exec() == REOPEN:
        pass
