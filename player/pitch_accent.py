"""Pitch-accent colouring of the subtitles (the pitch_display setting): each word's category
(heiban, atamadaka...) comes from the same lookup as its popup, done in the background for the
words of the line on screen and of the next one, and is kept for the session."""
from ..core import config
from ..language import knowledge, pitch, yomitan
from . import sub_panel


class PitchAccentMixin:
    def _init_pitch_accent(self):
        self._fetching_pitches = False
        self._pitches_wanted = False  # a line came during a fetch: fetch its words after it

    def _pitch_classes(self, tokens):
        """The pitch-accent category of each word span of `tokens` (see
        sub_panel.word_pitches), [] while the setting is off."""
        if config.get()["pitch_display"] == "off":
            return []
        pitches = self.session.pitches
        return sub_panel.word_pitches(tokens, lambda t: pitches.get((t.surface, t.base), ""))

    def _line_tokens(self):
        line = self.session.line
        return (self.session.tokens.get(line.text) or self._offline_tokens(line.text)
                if line else None)

    def _restyle_line(self):
        """Re-colour the words of the line on screen: their status and pitch accent."""
        tokens = self._line_tokens()
        if tokens:
            self.panel.restyle(sub_panel.word_classes(tokens, self.knowledge.token_status),
                               self._pitch_classes(tokens))

    def _pitch_texts(self):
        """The line on screen and the next one: the next line's words are coloured as soon as
        it shows."""
        line = self.session.line
        if not line or not line.text:
            return []
        texts = [line.text]
        if line.start is not None and self.player is not None:
            i = self.session.index_at(line.start, self.player.sub_delay, line.text)
            if i is not None and i + 1 < len(self.session.events):
                texts.append(self.session.events[i + 1].text)
        return texts

    def _fetch_pitches(self):
        """Look up, in the background, the words of the line on screen (and the next) whose
        pitch accent isn't known yet, then colour them. One fetch at a time."""
        if config.get()["pitch_display"] == "off" or self._yomitan_waiting():
            return
        if self._fetching_pitches:
            self._pitches_wanted = True
            return
        words = {}  # (surface, base) -> token
        for text in self._pitch_texts():
            tokens = self.session.tokens.get(text) or self._offline_tokens(text) or ()
            for token in tokens:
                key = (token.surface, token.base)
                if key not in self.session.pitches and knowledge.is_word(*key):
                    words[key] = token
        if not words:
            return
        # the popup's lookups are reused, and the ones made here open its popup at once
        cached = {surface: self.session.lookups.get(surface) for surface, _ in words}

        def work():
            found = {}
            for surface, entries in cached.items():
                if entries is None:
                    try:
                        entries = yomitan.lookup(surface)
                    except yomitan.YomitanOffline:
                        raise
                    except yomitan.YomitanError:
                        continue  # tried again with a later line
                found[surface] = entries
            return found

        def done(future):
            self._fetching_pitches = False
            try:
                found = future.result()
            except yomitan.YomitanError as e:
                self._yomitan_failed(e)
                return
            for surface, entries in found.items():
                if cached[surface] is None:
                    self.session.lookups.put(surface, entries)
            for key, token in words.items():
                if token.surface in found:
                    self.session.pitches[key] = pitch.token_category(token, found[token.surface])
            self._restyle_line()
            if self._pitches_wanted:
                self._pitches_wanted = False
                self._fetch_pitches()

        self._fetching_pitches = True
        self._jobs.run(work, done)
