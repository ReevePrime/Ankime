"""Card mining from the player: the line range a card takes, sentence and word cards, the
batch of words queued from the transcript, undo (of cards, and of words marked known) and
editing the last card. The media is cut in a background thread (cards/media.py), then the note
is written on the main thread (cards/notes.py)."""
import html
import logging

from aqt import mw
from aqt.utils import askUser, showWarning, tooltip

from ..cards import media, notes
from ..cards.batch import WordBatch
from ..cards.undo import UndoHistory
from ..core import config
from ..core.model import Span
from ..language import line_filter, yomitan
from ..subs import online
from ..subs.session import token_offsets

log = logging.getLogger(__name__)


class CardsMixin:
    def _init_cards(self):
        self._undo = UndoHistory()
        self._batch = WordBatch()
        self._last_note = None    # id of the note added or changed last, to edit it
        self._mined = {}          # note id -> (video, start) of the cards added this session,
                                  # marked on the seek bar
        self._line_cards = {}     # note id -> (video, start, text) of this session's sentence
                                  # cards, to catch a line added twice
        self._card_days = {}      # note id -> day it counts for in the immersion log
        self._extraction = []     # indices of the episode's lines still to add a card for;
                                  # the first is being added
        self._extraction_total = 0

    # --- the card's line range -----------------------------------------------

    def _extend(self, side):
        if not self.session.events:
            tooltip("Still analysing the subtitles...")
        elif not self.session.extend(side, self.player.sub_delay):
            tooltip("No line to add there")
        else:
            self._range_changed()

    def include_previous(self):
        self._extend(0)

    def include_next(self):
        self._extend(1)

    def reset_range(self):
        self.session.extra = [0, 0]
        self._range_changed()

    def _range_changed(self):
        self._show_range()
        if self._loop is not None:
            self._start_loop()
        span = self.session.span(self.player.sub_delay)
        if span:
            tooltip(html.escape(span.text).replace("\n", "<br>"))

    def _show_range(self):
        before, after = self.session.extra
        parts = [f"+{before} before" if before else "", f"+{after} after" if after else ""]
        self.panel.set_range(" · ".join(p for p in parts if p))

    # --- adding --------------------------------------------------------------

    def _mine(self, span, translation, lemma=None, surface=None, append_to=None,
              on_finished=None, on_screen=True, in_batch=False, entry=None, surface_at=None):
        """Cut the media for `span` (a model.Span) and add a card, or add the sentence to
        the note `append_to`. With a `lemma`, the word fields are filled from Yomitan and
        `surface` (the word as written) is bolded, at index `surface_at` of the span's text
        if known; `entry` is (text looked up, entry number)
        to fill them from that dictionary entry rather than from the first one for `lemma`.
        `on_screen`: the span is the line being watched, so a paused frame within it is the
        picture. `on_finished(ok)` runs once the note is written (or it failed)."""
        video, (text, start, end) = self._video, span
        title = self.playlist.titles.get(video)
        # a web video's audio is a file of its own (online.streams): ffmpeg's pick is right
        audio_stream = None if online.is_url(video) else self._audio_stream()
        paused_at = self.player.time_pos if on_screen and self.player.paused else None
        picture_at = media.picture_time(start, end, paused_at)
        origin = (video, start)
        tokens = self.session.tokens_for(text)
        context = self.session.line_before(start, self.player.sub_delay)
        cfg = config.get()
        # a batch or the episode's lines would open one Add window after the other
        review = cfg["review_before_adding"] and not in_batch
        definition = cfg["definition_marker"]
        with_media = bool(cfg["word_audio_field"]) or definition == "glossary"

        def work():
            picture_source, audio_source = online.media_sources(video, title)
            image, audio = media.extract_media(picture_source, start, end, audio_stream,
                                               picture_at, audio_source)
            if not lemma or append_to is not None:
                return image, audio, None
            try:
                word = (yomitan.card_fields(lemma, definition, with_media) if entry is None
                        else yomitan.card_fields(entry[0], definition, with_media,
                                                 entry=entry[1]))
            except Exception:
                media.discard(image, audio)
                raise
            return image, audio, word or ({"word": lemma}, [])

        def finish(ok):
            if on_finished:
                on_finished(ok)

        def done(future):
            try:
                image, audio, word = future.result()
            except yomitan.YomitanOffline as e:
                self._yomitan_failed(e)
                if self._batch.cancel() > 1:  # more than the word that failed
                    tooltip("Yomitan is offline: the rest of the batch was not added")
                else:
                    tooltip("Yomitan is offline: the card was not added")
                finish(False)
                return
            except Exception as e:
                log.warning("couldn't make the card's media / fields: %s", e)
                showWarning(f"Failed to add card:\n\n{e}", parent=self)
                finish(False)
                return
            if append_to is not None:
                notes.append_to_note(
                    self, append_to, text, image, audio, translation, surface, origin,
                    on_done=lambda old, new, written: self._appended(
                        append_to, old, new, written, lemma or surface),
                    on_finished=finish,
                    bold_at=surface_at, tokens=tokens, context=context)
            else:
                added_word = (word[0].get("word") or lemma) if word else None
                if review and self.player is not None and not self.player.paused:
                    self.player.paused = True  # while you edit the card
                (notes.review_card if review else notes.add_card)(
                    self, text, image, audio, word, translation, surface, origin,
                    on_added=lambda nid: self._added(nid, added_word, text, in_batch,
                                                     origin),
                    on_finished=finish, bold_at=surface_at, tokens=tokens, context=context)

        mw.taskman.run_in_background(work, done)

    def _added(self, note_id, word, text, in_batch=False, origin=None):
        self._card_days[note_id] = self.log.add_cards(1)
        if self.player is None:  # added after the player closed (from Anki's Add window):
            self.log.flush()     # its log was saved already, and there is nothing to show
            return
        self._last_note = note_id
        if origin:
            self._mined[note_id] = origin
            if not word:
                self._line_cards[note_id] = (*origin, text)
            self._update_seek_marks()
        self._undo.added(note_id, word or text, batch=in_batch)  # a batch: one record
        self._undo_changed()
        if word:
            self.knowledge.add_mined(word, note_id)
            self._restyle()  # the word turns 'learning' now, and on every later line
        if not in_batch:
            tooltip(f"Added: {html.escape(word or text)}")

    def _appended(self, note_id, old_fields, new_fields, media_added, word):
        self._last_note = note_id
        self._undo.appended(note_id, old_fields, new_fields, media_added, word)
        self._undo_changed()
        tooltip("Sentence added to your existing card")

    def edit_last_card(self):
        """Open the note added (or added to) last in the browser, to edit it."""
        if self._last_note is None:
            tooltip("No card added yet")
            return
        from aqt import dialogs
        dialogs.open("Browser", mw, search=(f"nid:{self._last_note}",))

    def _translation_for(self, start, end, fallback):
        if not self._want_translation():
            return ""
        return self.session.translation_for(start, end, self.player.sub_delay, fallback)

    def _carded_this_session(self, span):
        """Was a sentence card for the lines of `span` added this session (and not undone)?"""
        video = self._video
        return any(v == video and t == span.text and abs(s - span.start) < 0.5
                   for v, s, t in self._line_cards.values())

    def _line_added_before(self, span):
        """Was a sentence card for the lines of `span` added this session? Then ask before
        adding another."""
        if not self._carded_this_session(span):
            return False
        return not askUser("You already added a card for this line. Add another one?",
                           parent=self)

    def mine_line(self):
        span = self.session.span(self.player.sub_delay) if self._has_video() else None
        if span is None:
            tooltip("No subtitle on screen")
            return
        if self._line_added_before(span):
            return
        self._mine(span, self._translation_for(span.start, span.end, self.session.translation))

    def mine_event(self, index):
        """Add a card for the episode's line `index` (from the transcript), whichever line
        is on screen."""
        if not self._has_video() or not 0 <= index < len(self.session.events):
            return
        span = self.session.event_span(index, self.player.sub_delay,
                                       merge=config.get()["merge_split_lines"])
        if self._line_added_before(span):
            return
        self._mine(span, self._translation_for(span.start, span.end, ""), on_screen=False)

    def play_word_audio(self):
        """Play the selected word's audio from Yomitan's audio sources, through Anki's
        player (mpv is busy with the video)."""
        selected = self.session.selected
        if not selected:
            tooltip("Click a word first")
            return
        entries = self.session.lookups.get(selected.surface)
        lemma = self._selected_lemma(entries) if entries else selected.base or selected.surface

        def work():
            found = yomitan.word_audio(lemma)
            if found is None:
                return None
            name, data = found
            path = media.scratch_file(name)
            path.write_bytes(data)
            return path

        def done(future):
            try:
                path = future.result()
            except yomitan.YomitanError as e:
                if not self._yomitan_failed(e):
                    tooltip(html.escape(str(e)))
                return
            except Exception as e:
                tooltip(f"Couldn't play the audio: {html.escape(str(e))}")
                return
            if path is None:
                tooltip(f"No audio for {html.escape(lemma)}")
                return
            from aqt.sound import av_player
            av_player.play_file(str(path))  # swept from the temp folder after a day

        self._jobs.run(work, done)

    def add_word_card(self):
        """Hotkey twin of the popup's first button: adds the selected word, or its sentence
        to the card you already have for it."""
        self.add_word()

    def add_word(self, force_new=False, entry=0):
        """A card for the selected word from its dictionary entry number `entry` (or its
        sentence added to the card you have for that word, unless `force_new`)."""
        selected = self.session.selected
        entries = self.session.lookups.get(selected.surface) if selected else None
        span = self.session.span(self.player.sub_delay) if self._has_video() else None
        if not entries or span is None:
            tooltip("Click a word first" if not selected else "Nothing to add")
            return
        entry = entry if 0 <= entry < len(entries) else 0
        lemma = self._selected_lemma(entries, entry)
        at = self.session.token_offset(self.session.line.text, selected.index)
        if at is not None:
            at += self.session.span_offset(self.player.sub_delay)
        existing = None if force_new else self._existing_note(entries, entry)
        self._hide_popup()
        # the entry as the popup listed it: the same lookup, of the word as written
        self._mine(span, self._translation_for(span.start, span.end, self.session.translation),
                   lemma=lemma, surface=selected.surface, append_to=existing,
                   entry=(selected.surface, entry), surface_at=at)

    # --- a batch of words from the transcript --------------------------------

    def add_words(self, lemmas):
        """Batch-add new words from the transcript, each with the line best suited to it
        (see SubtitleSession.best_line_for)."""
        if not self._has_video() or not self.session.events:
            tooltip("Open a video and wait for its subtitles to be analysed")
            return
        if self._extraction:
            tooltip("Cards are being added for the episode's lines: wait for them, or Stop "
                    "them")
            return
        if self._batch.add(lemmas):
            self._undo.start_batch()  # a new batch: a new undo record
            self._next_batch_word()
        else:
            self._show_batch()

    def cancel_batch(self):
        """Stop the batch of words, or the cards for the episode's lines."""
        lines = self._cancel_extraction()
        dropped = self._batch.cancel()
        self._batch_status("")
        if lines > 1:
            tooltip(f"Cancelled: {lines - 1} line{'s' if lines != 2 else ''} not added "
                    "(the card being made is still added)")
        elif dropped > 1:
            tooltip(f"Cancelled: {dropped - 1} word{'s' if dropped != 2 else ''} not added "
                    "(the card being made is still added)")

    def _stop_batch(self, why):
        """Drop the words (or lines) still queued, saying `why` ("another video was
        opened")."""
        lines = self._cancel_extraction()
        dropped = self._batch.cancel()
        self._batch_status("")
        if lines > 1:
            tooltip(f"{why[0].upper()}{why[1:]}: {lines - 1} line"
                    f"{'s' if lines != 2 else ''} of the episode not added")
        elif dropped > 1:
            tooltip(f"{why[0].upper()}{why[1:]}: {dropped - 1} word"
                    f"{'s' if dropped != 2 else ''} of the batch not added")

    def _batch_status(self, text):
        """The batch's progress, in the transcript and under the video (the transcript may be
        hidden); "" hides it."""
        self.transcript.show_batch(text)
        self.batch_label.setText(text)
        self.batch_box.setVisible(bool(text))

    def _show_batch(self, lemma=None):
        lemma = lemma or (self._batch.queue[0] if self._batch.queue else "")
        if self._batch.queue:
            self._batch_status(f"Adding {self._batch.position()}/{self._batch.total}: {lemma}")
        else:
            self._batch_status("")

    def _next_batch_word(self):
        if self.player is None:  # closed meanwhile: the rest isn't added
            self._batch.cancel()
            return
        # the line is picked when the word's turn comes: words mined before it in the batch
        # are no longer new, which can make more lines i+1
        nxt = self._batch.next(lambda lemma: self.session.best_line_for(lemma, self.knowledge),
                               lambda lemma: self.knowledge.classify(lemma, lemma) == "new")
        if nxt is None:
            self._batch_status("")
            total = self._batch.finish()
            if total:
                tooltip(f"Done: {total} word{'s' if total != 1 else ''}. "
                        "Undo removes them all at once.")
            return
        lemma, (i, surface) = nxt
        delay = self.player.sub_delay if self.player else 0
        start, end, text = self.session.events[i]
        start, end = start + delay, end + delay
        self._show_batch(lemma)

        def finished(ok):
            # the note exists by now, so the next word's line is picked knowing this word
            self._batch.done(lemma)
            if not ok and self._batch.cancel():  # the same problem would hit every word
                tooltip("The rest of the batch was not added")
            self._next_batch_word()

        self._mine(Span(text, start, end), self._translation_for(start, end, ""), lemma=lemma,
                   surface=surface, on_finished=finished, on_screen=False, in_batch=True,
                   surface_at=self.session.word_offset(text, lemma))

    # --- a card for every line of the episode ---------------------------------

    def lines_to_extract(self, mode):
        """Indices of the episode's lines that extracting it with `mode` (see
        line_filter.EXTRACTIONS) adds a card for: those of that kind, but not those added
        this session already."""
        delay = self.player.sub_delay if self.player else 0
        return [i for i, (start, _, text) in enumerate(self.session.events)
                if line_filter.extracted(self._line_kind(text), mode)
                and not self._carded_this_session(Span(text, start + delay, 0))]

    def extract_lines(self, mode):
        """A sentence card for each of the episode's lines that `mode` picks, one after the
        other; Undo takes them all back at once. On an i+1 line, its new word is bolded."""
        if not self._has_video() or not self.session.events:
            tooltip("Open a video and wait for its subtitles to be analysed")
            return
        if self._extraction or self._batch.queue:
            tooltip("Cards are being added already: wait for them, or Stop them")
            return
        lines = self.lines_to_extract(mode)
        if not lines:
            tooltip("No line to add a card for")
            return
        label = dict(line_filter.EXTRACTIONS)[mode].lower()
        n = len(lines)
        if not askUser(f"Add {n} card{'s' if n != 1 else ''}, one for each of {label} of this "
                       "episode?\n\nUndo takes them all back at once.", parent=self):
            return
        self._extraction, self._extraction_total = lines, n
        self._undo.start_batch()
        self._next_extracted_line()

    def _next_extracted_line(self):
        if self.player is None:  # closed meanwhile: the rest isn't added
            self._extraction = []
            return
        if not self._extraction:
            self._batch_status("")
            if self._extraction_total:
                n, self._extraction_total = self._extraction_total, 0
                tooltip(f"Done: {n} card{'s' if n != 1 else ''}. "
                        "Undo removes them all at once.")
            return
        i = self._extraction[0]
        start, end, text = self.session.events[i]
        delay = self.player.sub_delay
        start, end = start + delay, end + delay
        done = self._extraction_total - len(self._extraction) + 1
        self._batch_status(f"Adding line {done}/{self._extraction_total}")
        surface, at = self._new_word_of(text)
        run = self._extraction

        def finished(ok):
            if self._extraction is not run:  # stopped meanwhile (and maybe started again)
                return
            run.pop(0)
            if not ok and run:  # the same problem would hit every line
                self._extraction, self._extraction_total = [], 0
                tooltip("The rest of the lines were not added")
            self._next_extracted_line()

        self._mine(Span(text, start, end), self._translation_for(start, end, ""),
                   surface=surface, on_finished=finished, on_screen=False, in_batch=True,
                   surface_at=at)

    def _new_word_of(self, text):
        """(surface, offset in `text`) of the new word of an i+1 line, to bold it; (None,
        None) for any other line."""
        tokens = self.session.tokens.get(text, ())
        if self.knowledge.line_kind(tokens) != "i+1":
            return None, None
        for token, at in zip(tokens, token_offsets(text, tokens)):
            if self.knowledge.token_status(token.surface, token.base, token.alts) == "new":
                return token.surface, at
        return None, None

    def _cancel_extraction(self):
        """Drop the lines still to add (the one being added included); returns how many."""
        dropped = len(self._extraction)
        self._extraction, self._extraction_total = [], 0
        return dropped

    # --- undo ----------------------------------------------------------------

    def _marked(self, words, marked):
        """Mark (or unmark) `words` as known, undoably."""
        before = {w: self.knowledge.is_marked(w) for w in words}
        self.knowledge.mark(words, marked)
        self._undo.marked(before, marked)
        self._undo_changed()

    def _undo_tip(self):
        """The Undo button's tooltip: what pressing it would do."""
        label = self._undo.label()
        if not label:
            return ("Nothing to undo yet. Undo takes back the cards added, sentences added "
                    "to a card and words marked known")
        return f"Undo: {label}. Press again for the one before"

    def _undo_changed(self):
        self._relabel_buttons()

    def undo_last_card(self):
        """Undo the newest card added, sentence appended or words marked known; pressed
        again, the one before (up to undo.UNDO_DEPTH)."""
        record = self._undo.pop()
        if record is None:
            tooltip("Nothing to undo")
            return
        self._undo_changed()

        def removed(ids):
            days = {}  # off the day each card counted for, which may not be today
            for note_id in ids:
                self._mined.pop(note_id, None)
                self._line_cards.pop(note_id, None)
                day = self._card_days.pop(note_id, None)
                days[day] = days.get(day, 0) + 1
            for day, n in days.items():
                self.log.add_cards(-n, day=day)
            self.refresh_knowledge()
            self._restyle()
            tooltip("Card removed" if len(ids) == 1 else f"{len(ids)} cards removed")

        if record.kind == "added":
            ids = list(record.data)
            notes.remove_notes(self, ids, on_done=lambda: removed(ids))
        elif record.kind == "appended":
            note_id, old_fields, new_fields, media_added = record.data
            notes.restore_fields(self, note_id, old_fields, new_fields, media_added,
                                 on_done=lambda: tooltip("Sentence removed from the card"))
        else:
            before = record.data
            for marked in (True, False):
                words = [w for w, was in before.items() if was == marked]
                if words:
                    self.knowledge.mark(words, marked)
            self._restyle()
            words = ", ".join(before) if len(before) <= 3 else f"{len(before)} words"
            tooltip(f"Undone: marking {html.escape(words)} as known")
