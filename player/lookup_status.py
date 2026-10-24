"""Yomitan's status in the player. When the API stops answering, the panel says so once
("Yomitan offline · Retry") instead of every line showing the error, lines are no longer sent
to it, and it is tried again with a growing wait (yomitan.Breaker). Meanwhile the line on
screen is split into words from what Ankime already knows (language/offline.py), only to be shown.
Once Yomitan answers, what failed meanwhile is done again."""
import logging

from aqt.qt import QTimer

from ..core import config
from ..language import offline, yomitan
from ..subs import session
from . import sub_panel

log = logging.getLogger(__name__)


class LookupStatusMixin:
    def _init_lookup_status(self):
        self._probing = False
        self._offline_shown = False  # the panel says "Yomitan offline"
        self._retry_timer = QTimer(self)
        self._retry_timer.setSingleShot(True)
        self._retry_timer.timeout.connect(self._probe_yomitan)
        self._lexicon = None          # offline.Lexicon, once read from the token cache
        self._reading_lexicon = False
        self._offline_line = None     # (text, tokens) of the line split offline on screen

    def _yomitan_waiting(self):
        """Is Yomitan known to be offline? Then don't ask it: the retry timer does."""
        return yomitan.BREAKER.offline

    def _yomitan_answered(self):
        """A call succeeded: if the panel still says offline, Yomitan is back."""
        if self._offline_shown and not yomitan.BREAKER.offline:
            self._yomitan_back()

    def _yomitan_failed(self, error):
        """True if `error` means Yomitan is offline: the panel shows it, and a retry is
        scheduled. Other errors (an answer of the wrong shape) are the caller's to show."""
        if not isinstance(error, yomitan.YomitanOffline):
            return False
        self._offline_shown = True
        self.panel.set_status(sub_panel.render_offline(str(error)))
        if not self._probing and not self._retry_timer.isActive():
            wait = yomitan.BREAKER.seconds_to_retry()
            self._retry_timer.start(int(wait * 1000) + 50)
        return True

    # --- without Yomitan --------------------------------------------------------

    def _show_offline_tokens(self, text):
        """Yomitan is offline: show the line split from what Ankime already knows. The
        tokens aren't kept with the episode's: i+1, the line filter and cards don't go by
        them, and the line is tokenized again once Yomitan is back."""
        if self._lexicon is None:
            self._read_lexicon()  # then shows the line on screen
            return
        tokens = offline.tokenize(text, self._lexicon, self.knowledge.has,
                                  config.get()["scan_length"])
        self._offline_line = (text, tokens)
        self.panel.set_sub(sub_panel.render_sub(text, tokens, self.knowledge.token_status,
                                                self._pitch_classes(tokens)))
        self._show_range()

    def _offline_tokens(self, text):
        """The offline tokens of `text` if it is the line shown split offline, or None."""
        if self._offline_line and self._offline_line[0] == text:
            return self._offline_line[1]
        return None

    def _read_lexicon(self):
        """Read every line Yomitan analysed before, in the background (once)."""
        if self._reading_lexicon:
            return
        self._reading_lexicon = True

        def done(future):
            self._reading_lexicon = False
            try:
                self._lexicon = future.result()
            except Exception as e:
                log.warning("couldn't read the token cache for offline splitting: %s", e)
                self._lexicon = offline.Lexicon()  # the cards and particles still help
            line = self.session.line
            if line and line.text and line.text not in self.session.tokens and \
                    self._yomitan_waiting():
                self._show_offline_tokens(line.text)

        self._jobs.run(lambda: offline.Lexicon(session.all_cached_tokens()), done)

    def _learn_tokens(self, token_lists):
        """Yomitan's tokens for some lines: the offline lexicon learns them too."""
        if self._lexicon is not None:
            for tokens in token_lists:
                self._lexicon.add(tokens)

    def retry_yomitan(self):
        """The Retry button: try now rather than when the wait is over."""
        yomitan.BREAKER.retry_now()
        self._retry_timer.stop()
        self._probe_yomitan()

    def _probe_yomitan(self):
        if self._probing or self.player is None:
            return
        self._probing = True
        self.panel.set_status(sub_panel.render_offline(yomitan.BREAKER.error or "", checking=True))

        def done(future):
            self._probing = False
            try:
                future.result()
            except yomitan.YomitanError as e:
                if self._yomitan_failed(e):
                    return
            self._yomitan_back()

        self._jobs.run(yomitan.version, done)

    def _yomitan_back(self):
        """Yomitan answers again: redo what couldn't be done without it."""
        self._offline_shown = False
        self._retry_timer.stop()
        self.panel.set_status("")
        self._retokenize_line()
        files = self._episode_files
        if files and files[0] == self._video and not self.session.events:
            self._load_episode(*files)
        self._score_next_episode()
        self._refresh_transcript_status()  # frequency ranks that couldn't be fetched
