"""The player window. It lays out the widgets and wires them together; each part of the
player is a mixin in this package (playback, playlist, opening episodes, subtitle delay,
getting and aligning subtitles, i+1 lines, cards, condensed audio, fullscreen, Yomitan's
status, searching the library). Here: the hotkeys, the subtitle panel and its words, the
popup, the transcript, known words and dropping videos on the window."""
import html
import logging
import os
from pathlib import Path

from aqt import gui_hooks, mw
from aqt.operations import QueryOp
from aqt.qt import (QApplication, QEvent, QHBoxLayout, QKeySequence, QLabel,
                    QMenu, QPoint, QPushButton, QRect, QShortcut, QSize, QSplitter, Qt, QTimer,
                    QToolButton, QVBoxLayout, QWidget)
from aqt.utils import showWarning, tooltip

from ..cards import media
from ..controls import hotkeys
from ..core import config, state
from ..core.model import Selection, SetupError
from ..language import (frequency, knowledge, known_words, line_filter, tokenizer,
                         yomitan)
from ..subs import session
from ..subs.playlist import VIDEO_SUFFIXES, videos_in
from . import mpv_controller, popup, sub_panel, transcript
from . import video
from .cards import CardsMixin
from .condensed import CondensedAudioMixin
from .episode import EpisodeMixin
from .fullscreen import FullscreenMixin
from .gamepad import GamepadMixin
from .i1 import LineFilterMixin
from .jobs import Jobs
from .library_search import LibrarySearchMixin
from .lookup_status import LookupStatusMixin
from .pitch_accent import PitchAccentMixin
from .playlist_view import PlaylistMixin
from .sub_delay import SubDelayMixin
from .subtitle_search import SubtitleSearchMixin
from .transport import TransportMixin

log = logging.getLogger(__name__)

# Settings that change which words count as known.
KNOWLEDGE_KEYS = ("note_type", "word_field", "known_interval", "count_suspended",
                  "vocab_sources", "sentence_words", "sentence_field", "sentence_sources",
                  "scan_length", "parser", "dictionary")
KNOWLEDGE_DELAY_MS = 1500  # after the collection changes, wait for it to settle, then re-read
POPUP_SCROLL = 80  # px the definition scrolls per press


class PlayerWindow(TransportMixin, PlaylistMixin, EpisodeMixin, SubDelayMixin,
                   SubtitleSearchMixin, LineFilterMixin, CardsMixin, CondensedAudioMixin,
                   FullscreenMixin, LookupStatusMixin, LibrarySearchMixin, GamepadMixin,
                   PitchAccentMixin, QWidget):
    def __init__(self, on_closed):
        # What can fail is tried first: on failure, no half-built window is left behind.
        output = mpv_controller.video_output(config.get()["video_output"])
        mpv_module = mpv_controller.load_mpv(config.get()["mpv_dll_dir"])
        # A window of its own, not a dialog on Anki's: it has minimize / maximize buttons and
        # its own taskbar entry, and doesn't stay on top of Anki's main window.
        super().__init__(None, Qt.WindowType.Window)
        self._on_closed = on_closed
        self._mpv_module = mpv_module
        self._video_output = output  # "opengl" | "window", see mpv_controller.video_output
        self._jobs = Jobs()        # background work, dropped once stale (see jobs.py)
        self.player = None         # MpvController, once the window exists
        self.session = session.SubtitleSession()
        self.knowledge = knowledge.Knowledge()
        self._popup_anchor = None  # global rectangle of the word whose popup is showing
        self._hotkeys = hotkeys.get()
        self._shortcuts = []
        self._action_buttons = []  # (button, text, action, key in label, tip), see _action_button
        self._warned = set()       # warnings already shown this session
        self._panel_widths = state.get("panel_widths")  # side panel -> px
        self._paused_for_popup = False  # playback was paused by the popup (pause_on_lookup)
        self._knowledge_reads = 0  # cards read so far: an older read that ends late is dropped
        self._knowledge_timer = QTimer(self)  # re-read the cards once the collection settles
        self._knowledge_timer.setSingleShot(True)
        self._knowledge_timer.setInterval(KNOWLEDGE_DELAY_MS)
        self._knowledge_timer.timeout.connect(self.refresh_knowledge)
        self._ranks = frequency.FrequencyRanks()
        self._fetching_ranks = False
        self._init_transport()
        self._init_playlist()
        self._init_episode()
        self._init_sub_delay()
        self._init_line_filter()
        self._init_cards()
        self._init_condensed()
        self._init_subtitle_search()
        self._init_fullscreen()
        self._init_lookup_status()
        self._init_library()
        self._init_gamepad()
        self._init_pitch_accent()
        self._build_ui()
        try:
            self._create_player()
        except Exception:
            self.video.release()
            self.popup.close()
            self.overlay.close()
            self.deleteLater()
            raise
        media.clean_temp()
        self.refresh_knowledge()
        self._start_timers()
        self._start_gamepad()
        self._restore_playlist()
        self.setAcceptDrops(True)
        config.on_change(self._config_changed)
        # reviews and cards added or edited elsewhere (the browser, Yomitan) change which
        # words are known
        gui_hooks.operation_did_execute.append(self._collection_changed)
        gui_hooks.reviewer_did_answer_card.append(self._card_answered)
        # a sync brings other computers' reviews, marks, delays and watch time
        gui_hooks.sync_did_finish.append(self._synced)

    def _build_ui(self):
        self.setWindowTitle("Ankime Player")
        self.resize(1200, 760)

        # Video surface
        self.video = video.create(self._video_output)
        self.video.setMinimumHeight(300)

        # Subtitle panel (one <span> per word) and the definition popup beside the word
        self.panel = sub_panel.SubtitlePanel(self, on_lookup=self.lookup_word,
                                             on_dismiss=self._hide_popup,
                                             on_retry=self.retry_yomitan)
        self._apply_subtitle_style()
        self.popup = popup.Popup(self, on_command=self._popup_command,
                                 scale=self._popup_scale)
        self._build_overlay()

        self.transcript = transcript.TranscriptPanel(
            on_seek=self._seek_to_event, on_mine=self.mine_event, on_add_words=self.add_words,
            on_mark_known=self.mark_words_known, on_cancel_batch=self.cancel_batch,
            on_mark_line_known=self.mark_event_known,
            on_copy=lambda i: self._copy(self.session.events[i].text),
            on_sync=self.sync_to_event,
            search=lambda q: self.session.lines_matching(q),
            word_sort=state.get("new_words_sort"), on_word_sort=self._word_sort_changed)
        self.transcript.setMinimumWidth(200)
        self.transcript.hide()

        # playlist | video | transcript, the side panels resizable
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.addWidget(self._build_playlist_box())
        self.splitter.addWidget(self.video)
        self.splitter.addWidget(self.transcript)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.splitterMoved.connect(self._remember_panel_widths)

        # what is below the video, the subtitles aside: hidden in fullscreen
        self.controls = QWidget()
        bars = QVBoxLayout(self.controls)
        bars.setContentsMargins(0, 0, 0, 0)
        bars.addLayout(self._seek_bar())
        bars.addLayout(self._transport_bar())
        self.button_box = QWidget()
        buttons = self._button_bar()
        buttons.setContentsMargins(0, 0, 0, 0)
        self.button_box.setLayout(buttons)

        layout = QVBoxLayout(self)
        layout.addWidget(self.splitter, 1)
        layout.addWidget(self.controls)
        layout.addWidget(self.panel)
        layout.addWidget(self.button_box)
        self._bind_hotkeys()

    # --- side panels -----------------------------------------------------------

    def _side_panels(self):
        """{name in the panel_widths setting: widget} for the panels beside the video."""
        return {"playlist": self.playlist_box, "transcript": self.transcript}

    def _remember_panel_widths(self, *_):
        sizes = self.splitter.sizes()
        for name, widget in self._side_panels().items():
            if widget.isVisible():
                self._panel_widths[name] = sizes[self.splitter.indexOf(widget)]

    def _show_side_panel(self, widget, on):
        """Show or hide a panel beside the video, at the width it had last."""
        widget.setVisible(on)
        if not self._fullscreen:  # else they are laid out on the way out
            self._apply_panel_widths()

    def _apply_panel_widths(self):
        total = sum(self.splitter.sizes()) or self.splitter.width()
        sizes = [0] * self.splitter.count()
        for name, widget in self._side_panels().items():
            if not widget.isHidden():
                sizes[self.splitter.indexOf(widget)] = max(
                    widget.minimumWidth(), int(self._panel_widths.get(name, 260)))
        video = self.splitter.indexOf(self.video)
        sizes[video] = max(total - sum(sizes), self.video.minimumWidth())
        self.splitter.setSizes(sizes)

    def _button_bar(self):
        open_btn = QPushButton("Open video...")
        open_btn.setToolTip("Open a video. You can also drop videos or folders on the window")
        open_btn.clicked.connect(self.choose_video)
        self.playlist_btn = QPushButton("Playlist")
        self.playlist_btn.setCheckable(True)
        self.playlist_btn.toggled.connect(lambda on: self._show_side_panel(self.playlist_box, on))
        self.transcript_btn = self._action_button("Transcript", "toggle_transcript",
                                                  key_in_label=True, run=False)
        self.transcript_btn.setCheckable(True)
        self.transcript_btn.toggled.connect(self._show_transcript)
        replay_btn = self._action_button("Replay line", "replay_line", key_in_label=True)
        mine_btn = self._action_button("Add card", "mine_line", key_in_label=True)
        undo_btn = self._action_button("Undo", "undo_last_card", key_in_label=True,
                                       tip=self._undo_tip)
        i1_btn = self._action_button("Next i+1", "next_i1", key_in_label=True,
                                     tip="Jump to the next line with exactly one new word")
        fullscreen_btn = self._action_button(
            "⛶ Fullscreen", "toggle_fullscreen",
            tip="Fullscreen on / off, also by double-clicking the video. Esc leaves it")
        buttons = QHBoxLayout()
        for b in (open_btn, self.playlist_btn, self.transcript_btn, replay_btn, i1_btn,
                  mine_btn, undo_btn, self._more_button(), fullscreen_btn):
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            buttons.addWidget(b)
        self.batch_box = self._batch_row()
        self.score_label = QLabel("")
        self.score_label.setToolTip(
            "Distinct words in this episode's subtitles (dictionary forms, no particles) "
            "that are known / learning / new, and how many new words you would have to learn "
            "for 90% / 95% of them to be known or learning")
        buttons.addStretch(1)
        buttons.addWidget(self.batch_box)
        buttons.addWidget(self._transcribe_row())
        buttons.addWidget(self.score_label)
        return buttons

    def _batch_row(self):
        """The progress of a batch of words being added, with a way to stop it: the same as
        in the transcript, which may be hidden."""
        box = QWidget()
        row = QHBoxLayout(box)
        row.setContentsMargins(0, 0, 0, 0)
        self.batch_label = QLabel()
        self.batch_label.setTextFormat(Qt.TextFormat.PlainText)  # it shows words from files
        cancel = QPushButton("Stop")
        cancel.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        cancel.setToolTip("Stop adding the words still queued (the card being made now is "
                          "still added)")
        cancel.clicked.connect(self.cancel_batch)
        row.addWidget(self.batch_label)
        row.addWidget(cancel)
        box.hide()
        return box

    def _more_button(self):
        """What is used less often, in a menu: fewer buttons in the row."""
        more = QToolButton()
        more.setText("More")
        more.setToolTip("Open a video from the web, get, load or generate subtitles, align them, edit the last card, add a card "
                        "for every line, condensed audio, mark words known (a line's, or "
                        "from a list), export them, search your library, re-analyse the "
                        "subtitles, immersion log")
        more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(more)
        url = menu.addAction("Open URL...", self.open_url)
        url.setToolTip("Play a video from YouTube or another site, with its subtitles (needs "
                       "yt-dlp on the PATH). The address on the clipboard is filled in")
        menu.addSeparator()
        load = menu.addAction("Load subtitles...", self.load_subtitle_file)
        load.setToolTip("Use another subtitle file for this video (one in a Subs folder, or "
                        "named differently from the video). Remembered for the video")
        jimaku = menu.addAction("Search Jimaku...", self.search_jimaku)
        jimaku.setToolTip("Find Japanese subtitles for this video on jimaku.cc and save them "
                          "next to it (needs a Jimaku API key, see the settings)")
        align = menu.addAction("Align subtitles to the audio", self.align_subtitles)
        align.setToolTip("Shift the subtitles to match the speech, with alass or ffsubsync "
                         "(see the settings). The aligned copy is saved next to the video")
        generate = menu.addAction("Generate subtitles from speech", self.generate_subtitles)
        generate.setToolTip("For shows with no subtitles on Jimaku: make them from the audio "
                            "playing with faster-whisper or whisper.cpp (see the settings). "
                            "Saved next to the video as ep01.ja.whisper.srt")
        self._edit_action = menu.addAction("", self.edit_last_card)
        self._label_edit_action()
        self._extract_menu(menu.addMenu("Add a card for every line"))
        condensed = menu.addAction("Save condensed audio...", self.export_condensed_audio)
        condensed.setToolTip("Save the episode's audio with only its subtitled lines, one "
                             "after the other, to listen to without the video")
        self._line_known_action = menu.addAction("", self.mark_line_known)
        self._line_known_action.setToolTip("Every new word of the line on screen (names don't "
                                           "count). Undo takes them back")
        self._label_line_known_action()
        known = menu.addAction("Mark known from a word list...", self.import_known_words)
        known.setToolTip("Mark the words of a file as known: JPDB's review history (the "
                         "ones past the known interval), a Migaku export (the known ones), "
                         "or a list, one per line or the first Japanese column of a TSV / "
                         "CSV. Undo takes them back")
        export = menu.addAction("Export known words...", self.export_known_words)
        export.setToolTip("Save the known words to a text file, one per line, for Ankime, "
                          "JPDB or Migaku (the file type): a backup of the words marked "
                          "known, or the words to take to another tool")
        self._library_action = menu.addAction("", self.search_library)
        self._library_action.setToolTip("Find the lines with a word in every episode you have "
                                        "watched or scored: more example sentences, i+1 "
                                        "ones first. Double-click one to play it")
        self._label_library_action()
        reanalyse = menu.addAction("Re-analyse subtitles", self.reanalyse_subtitles)
        reanalyse.setToolTip("After adding or removing Yomitan dictionaries: split the "
                             "subtitles into words and get their frequencies again, instead "
                             "of using what was saved from before")
        menu.addAction("Immersion log...", self.show_log)
        menu.setToolTipsVisible(True)
        more.setMenu(menu)
        return more

    def _extract_menu(self, menu):
        """Which lines of the episode to add a card for, each with how many cards that
        makes, counted when the menu opens."""
        actions = {mode: menu.addAction("", lambda m=mode: self.extract_lines(m))
                   for mode, _ in line_filter.EXTRACTIONS}
        tips = {"all": "A sentence card for each line of the episode",
                "i1": "A sentence card for each line with exactly one new word, bolded",
                "new": "A sentence card for each line with at least one new word"}

        def count():
            ready = self._has_video() and bool(self.session.events)
            for mode, label in line_filter.EXTRACTIONS:
                n = len(self.lines_to_extract(mode)) if ready else 0
                actions[mode].setText(f"{label} ({n})" if ready else label)
                actions[mode].setEnabled(n > 0)

        for mode, action in actions.items():
            action.setToolTip(f"{tips[mode]}. Lines added this session are skipped; Undo "
                              "takes them all back at once")
        menu.setToolTipsVisible(True)
        menu.aboutToShow.connect(count)

    # --- hotkeys and the buttons that show them -----------------------------------

    def _action_button(self, text, action, key_in_label=False, run=True, tip=None, icon=None):
        """A button for a hotkey action. `text` and `tip` may be functions, for a label or
        tooltip that depends on the settings or the state; `icon` is a QStyle standard icon shown before it. The tooltip is
        `tip` (by default the action's name in the settings) and the key: "Previous video
        (Ctrl+Left)"; the key also goes in the label with `key_in_label`. Both follow the
        settings. The action's name is also the accessible name, for the buttons that are
        just a symbol. Without `run`, clicking it doesn't run the action."""
        btn = QPushButton()
        btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # keep Space for the shortcut
        btn.setAccessibleName(hotkeys.ACTIONS[action])
        if icon is not None:
            btn.setIcon(self.style().standardIcon(icon))
        if run:
            btn.clicked.connect(lambda _checked=False: getattr(self, action)())
        entry = (btn, text, action, key_in_label, tip or hotkeys.ACTIONS[action])
        self._action_buttons.append(entry)
        self._label_button(*entry)
        return btn

    def _label_button(self, btn, text, action, key_in_label, tip):
        label = text() if callable(text) else text
        tip = tip() if callable(tip) else tip
        key = self._hotkeys.get(action)
        btn.setText(f"{label} ({key})" if key and key_in_label else label)
        btn.setToolTip(f"{tip} ({key})" if key else tip)

    def _relabel_buttons(self):
        for entry in self._action_buttons:
            self._label_button(*entry)
        self._label_edit_action()
        self._label_line_known_action()
        self._label_library_action()

    def _label_library_action(self):
        key = self._hotkeys.get("search_library")
        label = "Search your library..."
        self._library_action.setText(f"{label} ({key})" if key else label)

    def _label_line_known_action(self):
        key = self._hotkeys.get("mark_line_known")
        label = "Mark every new word on this line known"
        self._line_known_action.setText(f"{label} ({key})" if key else label)

    def _label_edit_action(self):
        key = self._hotkeys.get("edit_last_card")
        self._edit_action.setText(f"Edit last card ({key})" if key else "Edit last card")

    def _bind_hotkeys(self):
        """Shortcuts when Qt has focus, in the popup too; mpv gets the same keys for when
        its own window has focus (the native video output; the OpenGL one never takes it)."""
        for shortcut in self._shortcuts:
            shortcut.setEnabled(False)
            shortcut.deleteLater()
        keys = {action: key for action, key in self._hotkeys.items() if key}
        if not any(QKeySequence(k) == QKeySequence("Esc") for k in keys.values()):
            keys["escape"] = "Esc"  # unless Esc is some action's hotkey
        self._shortcuts = [QShortcut(QKeySequence(key), window, activated=getattr(self, action))
                           for action, key in keys.items()
                           for window in (self, self.popup, self.overlay)]
        if self.player is not None:
            self.player.set_keys(self._mpv_keys())

    def _mpv_keys(self):
        return {action: hotkeys.to_mpv(key) for action, key in self._hotkeys.items() if key}

    def _config_changed(self, old, new):
        """Settings saved while the player is open: apply them now."""
        if old["hotkeys"] != new["hotkeys"]:
            self._hotkeys = hotkeys.get(new)
            self._bind_hotkeys()
        if old["hotkeys"] != new["hotkeys"] or old["seek_seconds"] != new["seek_seconds"]:
            self._relabel_buttons()
        if any(old[key] != new[key] for key in ("subtitle_font_size", *(
                key for _, key, _ in sub_panel.WORD_STYLES),
                                                "furigana", "translation_display",
                                                "pitch_display", "pitch_colors")):
            self._apply_subtitle_style()
        if old["pitch_display"] != new["pitch_display"]:
            self._restyle_line()
            self._fetch_pitches()
        if old["translation_display"] != new["translation_display"]:
            self._show_translation(self.player.secondary_text if self.player else "")
        if any(old[key] != new[key] for key in ("parser", "scan_length", "dictionary")):
            self._analyse_again()  # the caches are of the other split: kept for going back
        if any(old[key] != new[key] for key in KNOWLEDGE_KEYS):
            self.refresh_knowledge()  # everything is re-coloured once the cards are read
        if old["pause_on_lookup"] and not new["pause_on_lookup"]:
            self._paused_for_popup = False
        if yomitan.BREAKER.offline and any(old[key] != new[key]
                                           for key in ("yomitan_url", "dictionary")):
            self.retry_yomitan()  # whatever made it offline may not apply any more
        self._gamepad_config_changed(old, new)

    # --- mpv -------------------------------------------------------------

    def _create_player(self):
        wid = self.video.winId() if self.video.native else None
        p = self.player = mpv_controller.MpvController(
            self, self._mpv_module, self._mpv_keys(), wid=wid)
        try:
            self.video.attach(p)
        except SetupError as e:
            self.player = None
            p.terminate()
            if mpv_controller.window_problem():
                raise
            raise SetupError(f"{e}\n\nSetting Video output to \"Native window\" in Tools > "
                             "Ankime > Settings (Media tab) may work.") from e
        p.subtitle_changed.connect(self._on_subtitle)
        p.translation_changed.connect(self._on_translation)
        p.pause_changed.connect(self._on_pause_changed)
        p.volume_changed.connect(self._on_volume_changed)
        p.speed_changed.connect(self._on_speed_changed)
        p.file_loaded.connect(self._select_subtitle_tracks)
        p.seeked.connect(lambda: self._update_seek_bar(self.player.time_pos))
        p.end_reached.connect(self.next_video)
        p.key_pressed.connect(lambda action: getattr(self, action)())
        self._restore_volume_and_speed()

    def _has_video(self):
        return self.player is not None and bool(self._video) and bool(self.player.path)

    # --- known words -------------------------------------------------------

    def refresh_knowledge(self):
        """Re-read the cards, in the background: when the player opens, for every new video,
        when the settings change which cards count, and after the collection changes (a
        review, a card added or edited elsewhere). Everything coloured by word status
        follows once they are read. A problem is shown once a session, not on every
        video."""
        self._knowledge_timer.stop()
        cfg = config.get()
        self._knowledge_reads += 1
        read = self._knowledge_reads

        def done(result):
            if read != self._knowledge_reads or self.player is None:
                return  # a newer read is on its way, or the player was closed
            self.knowledge.apply(cfg["known_interval"], *result)
            self._warned.clear()  # fixed: should it break again, say so again
            self._restyle()

        def failed(error):
            if read != self._knowledge_reads or self.player is None:
                return
            if isinstance(error, SetupError):
                log.warning("couldn't read the known words: %s", error)
            else:
                log.error("couldn't read the known words", exc_info=error)
            self._warn_once(f"Ankime could not read your known words:\n\n{error}")

        QueryOp(parent=self, op=lambda col: knowledge.Knowledge.read_cards(col, cfg),
                success=done).failure(failed).run_in_background()

    def _collection_changed(self, changes, handler):
        if handler is not None and handler is getattr(mw, "reviewer", None) and not any(
                getattr(changes, name, False) for name in ("note", "note_text")):
            return  # a card answered in the reviewer: _card_answered updates just that card
        if any(getattr(changes, name, False) for name in ("card", "note", "note_text")):
            self._knowledge_timer.start()  # several changes in a row: one read

    def _card_answered(self, _reviewer, card, _ease):
        """A review: only the card answered changes, and most reviews are of cards that
        don't count towards known words at all, so no need to read them all again."""
        if self.player is None or not self.knowledge.counts(card.id):
            return
        try:
            days = knowledge.Knowledge.read_card(mw.col, card.id)
        except Exception as e:
            log.warning("couldn't read card %s: %s", card.id, e)
            self._knowledge_timer.start()
            return
        if self.knowledge.update_card(card.id, days):
            self._knowledge_timer.start()  # a sentence card just became known: read its words
        self._restyle()

    def _synced(self):
        """A sync finished while the player is open: what the other computers changed is
        read again (it is only read when the player opens otherwise)."""
        if self.player is None:
            return
        self.knowledge.reload_marks()
        self._reload_sub_offsets()
        self.log.reload_others()
        self.refresh_knowledge()  # the reviews; everything is re-coloured once they are read
        self._restyle()

    def _warn_once(self, message):
        if message not in self._warned:
            self._warned.add(message)
            showWarning(message, parent=self)

    def _restyle(self):
        """Re-colour everything that depends on word status after a change."""
        self._restyle_line()
        self._update_badge()
        self._update_score()
        self._refresh_transcript_status()
        self._refresh_popup()
        self._update_seek_marks()

    def toggle_known(self):
        """Mark / unmark the selected word as known."""
        if not self.session.selected:
            tooltip("Click a word first")
            return
        surface, base, _ = self.session.selected
        if not knowledge.is_word(surface, base):
            tooltip("Not a word")
            return
        key = base or surface
        marked = not self.knowledge.is_marked(key)
        self._marked([key], marked)
        self._restyle()
        tooltip(f"{html.escape(key)}: {'marked as known' if marked else 'no longer marked'}")

    mark_known = toggle_known

    def mark_words_known(self, lemmas):
        self._marked(lemmas, True)
        self._restyle()
        tooltip(f"Marked {len(lemmas)} word{'s' if len(lemmas) != 1 else ''} as known")

    def mark_line_known(self):
        """Mark every new word of the line on screen known, undoably."""
        line = self.session.line
        self._mark_line_words(line.text if line else "")

    def mark_event_known(self, index):
        """The same for a line of the transcript."""
        self._mark_line_words(self.session.events[index].text)

    def _mark_line_words(self, text):
        if not text:
            tooltip("No line on screen")
            return
        tokens = self.session.tokens.get(text)
        if tokens is None:
            tooltip("This line isn't split into words yet: is Yomitan running?")
            return
        words = self.knowledge.new_words(tokens)
        if not words:
            tooltip("No new words on this line")
            return
        self._marked(words, True)
        self._restyle()
        tooltip(f"Marked known: {html.escape('、'.join(words))}")

    def import_known_words(self):
        """Mark the words of a word list known, undoably (see known_words)."""
        known_words.import_word_list(self, self)

    def export_known_words(self):
        known_words.export_known_words(self, self)

    def reanalyse_subtitles(self):
        """Forget what Yomitan said about the subtitles (word splits, headwords, frequency
        ranks), on disk too, and ask it again: its dictionaries changed."""
        session.clear_token_cache()
        knowledge.clear_sentence_cache()
        if config.get()["sentence_words"]:
            self.refresh_knowledge()  # the sentence cards' words, split again
        self._analyse_again()

    def _analyse_again(self):
        """Split the subtitles into words again: Yomitan's dictionaries or the parser
        changed. The caches on disk are named by what they depend on, so a split of
        another parser isn't read back."""
        self._lexicon = None  # learnt from the analyses just thrown away
        self._ranks.clear()
        self.session.forget_analysis()
        self._hide_popup()
        # playlist scores: one being worked out may come from the old cache
        self._jobs.invalidate("scores")
        self._scoring = False
        self._episode_lemmas.clear()
        self._unscored.clear()
        self._relabel_playlist()
        self._retokenize_line()
        files = self._episode_files
        if files and files[0] == self._video and files[1]:
            self._jobs.invalidate("episode")  # an analysis on its way may be the old one
            self._load_episode(*files)
        self._score_next_episode()
        tooltip("Analysing the subtitles again...")

    # --- transcript ----------------------------------------------------------

    def toggle_transcript(self):
        self.transcript_btn.toggle()

    def _show_transcript(self, on):
        self._show_side_panel(self.transcript, on)
        if on:
            self._refresh_transcript_lines()
            self._refresh_transcript_status()

    def _kinds(self):
        return [self._line_kind(text) for _, _, text in self.session.events]

    def _refresh_transcript_lines(self):
        if self.transcript.isVisible() and self.player is not None:
            self.transcript.set_lines(self.session.events, self.player.sub_delay, self._kinds())
            self._follow_line()

    def _refresh_transcript_status(self):
        if not self.transcript.isVisible():
            return
        self.transcript.set_kinds(self._kinds())
        counts = self.session.new_word_counts(self.knowledge)
        if self.transcript.word_sort() != "frequency":
            self.transcript.set_words(counts)
            return
        missing = self._ranks.missing(lemma for lemma, _ in counts)
        if missing:
            self._fetch_ranks(missing)
        self.transcript.set_words(self._ranks.sort(counts), self._ranks.ranks)

    def _word_sort_changed(self, mode):
        state.save(new_words_sort=mode)
        self._refresh_transcript_status()

    def _fetch_ranks(self, lemmas):
        """Ask Yomitan for the frequency rank of `lemmas` in the background; the list is
        sorted again once they are in (and cached on disk, see language/frequency.py)."""
        if self._fetching_ranks or self._yomitan_waiting():
            return  # one fetch at a time; offline: fetched once Yomitan is back
        self._fetching_ranks = True
        self.transcript.set_words_note("Getting the frequency of the new words...")

        def done(future):
            self._fetching_ranks = False
            self.transcript.set_words_note("")
            try:
                found = future.result()
            except yomitan.YomitanError as e:
                if not self._yomitan_failed(e):
                    self.transcript.set_words_note(f"No frequencies: {e}")
                return
            self._ranks.update(found)
            self._yomitan_answered()
            self._refresh_transcript_status()

        self._jobs.run(lambda: frequency.fetch(lemmas, yomitan.frequency_rank), done)

    def _follow_line(self):
        line = self.session.line
        if self.transcript.isVisible() and line and line.text:
            self.transcript.show_line(
                self.session.index_at(line.start, self.player.sub_delay, line.text))

    def _seek_to_event(self, index):
        """Play line `index` of the episode: you chose it, the line filter doesn't skip it."""
        if self._has_video() and 0 <= index < len(self.session.events):
            ev, delay = self.session.events[index], self.player.sub_delay
            self._choose_line(ev.start + delay, ev.end + delay)
            self._seek_to_line(ev.start + delay)

    # --- subtitle panel ----------------------------------------------------

    @staticmethod
    def _want_translation():
        """Is the translation wanted on cards?"""
        return bool(config.get()["translation_field"])

    def _on_translation(self, text):
        if self._want_translation() and text:
            self.session.translation = text  # may arrive just after its line
        self._show_translation(text)

    def _show_translation(self, text):
        shown = config.get()["translation_display"] != "off"
        self.panel.set_translation(text if shown else "")

    def cycle_translation(self):
        """Hidden -> blurred -> shown. Turned on for the first time, the translation track
        loads with the next video unless a translation field already loads it."""
        values = [value for value, _ in sub_panel.TRANSLATION_DISPLAY]
        current = config.get()["translation_display"]
        nxt = values[(values.index(current) + 1) % len(values)] if current in values else "on"
        config.save(translation_display=nxt)
        label = dict(sub_panel.TRANSLATION_DISPLAY)[nxt]
        if nxt != "off" and self.player is not None and not self.player.secondary_text \
                and not self._want_translation():
            label += " (from the next video)"
        tooltip(f"Translation: {label}")

    def _on_subtitle(self, text, start, end):
        p = self.player
        if p is None:
            return
        translation = p.secondary_text if self._want_translation() else ""
        merge = p.sub_delay if config.get()["merge_split_lines"] else None
        if not self.session.set_line(text, start, end, p.path, translation,
                                     keep_range=self._loop is not None, merge_delay=merge):
            return
        self._pause_at = None
        self._hide_popup()
        if text and config.get()["i1_pause"] == "every" and self._loop is None:
            self._arm_line_pause(None)  # doesn't need the words: armed before they come
        if self._loop_follow and text and start is not None:
            self._loop_follow = False
            self._start_loop()
        self._follow_line()
        if text in self.session.tokens:
            self._show_tokens(text, self.session.tokens[text])
            return
        self.panel.set_sub(sub_panel.render_sub(text))  # plain text until tokenized
        self._show_range()
        if not text:
            return
        if self._yomitan_waiting():
            self._show_offline_tokens(text)  # tokenized for real once Yomitan is back
            return
        line = self.session.line

        def done(future):
            try:
                tokens = future.result()
            except yomitan.YomitanError as e:
                went_offline = self._yomitan_failed(e)
                if self.session.line is line:
                    if went_offline:
                        self._show_offline_tokens(text)
                    else:
                        self.panel.set_message(sub_panel.render_error(str(e)))
                return
            self.session.tokens[text] = tokens
            self._learn_tokens([tokens])
            if self.session.line is line:
                self._show_tokens(text, tokens)
            self._yomitan_answered()

        self._jobs.run(lambda: tokenizer.tokenize(text), done)

    def _retokenize_line(self):
        """Tokenize the line on screen if it isn't (Yomitan was offline, or the tokens were
        dropped), keeping the card's range."""
        line = self.session.line
        if line and line.text and line.text not in self.session.tokens:
            extra = list(self.session.extra)
            self.session.line = None  # so the line counts as new and is tokenized
            self._on_subtitle(line.text, line.start, line.end)
            self.session.extra = extra
            self._show_range()

    def _show_tokens(self, text, tokens):
        self.panel.set_sub(sub_panel.render_sub(text, tokens, self.knowledge.token_status,
                                                self._pitch_classes(tokens)))
        self._fetch_pitches()
        self._show_range()
        self._update_badge()
        self._on_line_shown()

    # --- popup -------------------------------------------------------------

    def lookup_word(self, word, base, rect, index=None):
        line = self.session.line
        selection = self.session.selected = Selection(word, base, index)
        x, y, w, h = rect
        self._popup_anchor = QRect(self.panel.mapToGlobal(QPoint(x, y)), QSize(w, h))
        cached = self.session.lookups.get(word)
        if cached is not None:
            self._show_popup(cached)
            return

        def done(future):
            if self.session.line is not line or self.session.selected != selection:
                return  # moved on meanwhile
            try:
                entries = future.result()
            except yomitan.YomitanError as e:
                self._yomitan_failed(e)
                self.popup.show_at(self._popup_anchor, sub_panel.render_error(str(e)))
                return
            self.session.lookups.put(word, entries)
            self._show_popup(entries)
            self._yomitan_answered()

        self._jobs.run(lambda: yomitan.lookup(word), done)

    def _selected_lemma(self, entries, entry=0):
        """Dictionary form of the selected word in its dictionary entry number `entry`."""
        surface, base, _ = self.session.selected
        return (entries[entry].get("expression") if len(entries) > entry else "") or (
            base or surface)

    def _existing_note(self, entries, entry=0):
        """Id of your note for the selected word's entry `entry`, or None. The first entry
        is the word as Yomitan split the line, so its dictionary form and the text as
        written count too; the others are other words."""
        surface, base, _ = self.session.selected
        lemma = self._selected_lemma(entries, entry)
        return self.knowledge.note_for(lemma, base, surface) if entry == 0 else (
            self.knowledge.note_for(lemma))

    def _popup_scale(self):
        """The popup's text grows with the subtitles: their zoom (Ctrl+wheel over them) and
        fullscreen's enlargement, for a screen seen from across the room."""
        scale = self.panel.zoomFactor()
        return scale * sub_panel.FULLSCREEN_SCALE if self._fullscreen else scale

    def _popup_html(self, entries):
        surface, base, _ = self.session.selected
        duplicates = [self._existing_note(entries, i) is not None for i in range(len(entries))]
        # with a gamepad connected, its buttons too: "Add card (Ctrl+Shift+N / X)"
        pad = self._gamepad_labels()
        keys = {action: " / ".join(k for k in (key, pad.get(action)) if k)
                for action, key in self._hotkeys.items()}
        return popup.render_entries(surface, entries, self.knowledge.is_marked(base or surface),
                                    duplicates, keys)

    def _show_popup(self, entries):
        self.popup.show_at(self._popup_anchor, self._popup_html(entries))
        p = self.player
        if config.get()["pause_on_lookup"] and p is not None and p.path and not p.paused:
            p.paused = True
            self._paused_for_popup = True

    def _refresh_popup(self):
        if self.popup.isVisible() and self.session.selected:
            entries = self.session.lookups.get(self.session.selected.surface)
            if entries is not None:
                self.popup.update_content(self._popup_html(entries))

    def _hide_popup(self):
        """Close the popup; playback goes on if it was paused for it (pause_on_lookup)."""
        self.popup.hide()
        if self._paused_for_popup:
            self._paused_for_popup = False
            if self.player is not None and self.player.paused:
                self.player.paused = False

    def _popup_command(self, cmd):
        """A button of the popup: "add:<entry>", "append:<entry>", "known", "audio", or
        "close" (Esc in the popup)."""
        name, _, index = cmd.partition(":")
        entry = int(index) if index.isdigit() else 0
        if name == "add":
            self.add_word(force_new=True, entry=entry)
        elif name == "append":
            self.add_word(entry=entry)
        elif name == "known":
            self.toggle_known()
        elif name == "audio":
            self.play_word_audio()
        elif name == "close":
            self._hide_popup()

    # --- copying lines -------------------------------------------------------------

    def copy_line(self):
        """Copy the line on screen, with the lines the card takes, to the clipboard: the
        panel's words can't be selected (a click looks them up)."""
        span = self.session.span(self.player.sub_delay) if self._has_video() else None
        line = self.session.line
        text = span.text if span else (line.text if line else "")
        if not text:
            tooltip("No subtitle on screen")
            return
        self._copy(text)

    @staticmethod
    def _copy(text):
        QApplication.clipboard().setText(text)
        tooltip(f"Copied: {html.escape(text)}".replace("\n", "<br>"))

    def word_previous(self):
        self.panel.move_selection(-1)

    def word_next(self):
        self.panel.move_selection(1)

    def show_definition(self):
        """Open the selected word's definition (the first word's if none is), or close it."""
        if self.popup.isVisible():
            self._hide_popup()
        else:
            self.panel.lookup_selection()

    def popup_scroll_up(self):
        self.popup.scroll(-POPUP_SCROLL)

    def popup_scroll_down(self):
        self.popup.scroll(POPUP_SCROLL)

    # --- dropping videos on the window ----------------------------------------

    @staticmethod
    def _dropped_paths(event):
        """Local videos and folders in a drag, in the order given."""
        mime = event.mimeData()
        if not mime.hasUrls():
            return []
        paths = [url.toLocalFile() for url in mime.urls() if url.isLocalFile()]
        return [p for p in paths
                if os.path.isdir(p) or Path(p).suffix.lower() in VIDEO_SUFFIXES]

    def dragEnterEvent(self, event):
        if self._dropped_paths(event):
            event.acceptProposedAction()

    def dropEvent(self, event):
        videos = []
        for path in self._dropped_paths(event):
            videos.extend(videos_in(path) if os.path.isdir(path) else [path])
        if not videos:
            tooltip("No videos there")
            return
        event.acceptProposedAction()
        self._remember_dir(Path(videos[0]).parent)
        self._append(videos)
        tooltip(f"Added {len(videos)} video{'s' if len(videos) != 1 else ''} to the playlist")

    # --- window ------------------------------------------------------------

    def moveEvent(self, event):
        self._hide_popup()  # it would float away from its word
        self._place_overlay()
        super().moveEvent(event)

    def resizeEvent(self, event):
        self._hide_popup()
        self._place_overlay()
        super().resizeEvent(event)

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange:
            self._window_state_changed()

    def escape(self):
        """Esc closes the popup first, then leaves fullscreen. It never closes the player:
        a key pressed once too often shouldn't end the session (the window's close button
        and the system's shortcut do)."""
        if self.popup.isVisible():
            self._hide_popup()
        elif self._fullscreen:
            self.toggle_fullscreen()

    def closeEvent(self, event):
        self._jobs.close()
        self._batch.cancel()  # the card being made is still added
        self._cancel_extraction()
        config.remove_listener(self._config_changed)
        for hook, fn in ((gui_hooks.operation_did_execute, self._collection_changed),
                         (gui_hooks.reviewer_did_answer_card, self._card_answered),
                         (gui_hooks.sync_did_finish, self._synced)):
            try:
                hook.remove(fn)
            except ValueError:
                pass
        self._knowledge_timer.stop()
        self._stop_timers()
        self._stop_gamepad()
        self._retry_timer.stop()
        self._remember_position()
        self.log.flush()
        self.popup.close()
        self.overlay.close()
        if self.player is not None:
            self._save_player_settings()
            player, self.player = self.player, None
            self.video.release()  # mpv's render context can't outlive mpv
            player.terminate()
        self._on_closed()
        super().closeEvent(event)
