"""Which words do I know? Built from the cards in the collection (main thread only)."""
import json
import logging
import re
import threading
import unicodedata

from anki.utils import strip_html

from ..core.model import SetupError
from ..core.store import USER_FILES, JsonFile, SyncedMap, SyncedValue
from . import tokenizer, yomitan

log = logging.getLogger(__name__)

# Words marked as known used to live here, on one machine only, then in one synced list
# (KNOWN_KEY); now each computer keeps its own marks and unmarks in the collection's config
# (KNOWN_PREFIX::<device>, see store.SyncedMap), so marks made on two computers between
# syncs are all kept. The older places are read as marks made before any other.
OLD_KNOWN_FILE = USER_FILES / "known_words.json"
KNOWN_KEY = "ankime_known_words"
KNOWN_PREFIX = "ankime_known"

# The words of known sentence cards' sentences (sentence_words), as the lines are split:
# {"analysis": tokenizer.analysis_key, "words": {sentence: [dictionary forms]}}. Only a
# sentence that is new or was edited is split when the cards are read again.
SENTENCE_CACHE = USER_FILES / "sentence_words.json"
SENTENCE_CHUNK = 100  # sentences per Yomitan call; those done are kept if a later one fails
_sentence_lock = threading.Lock()  # the player and the browser can read the cards at once

# Ankime is for Japanese only: anything with kana or kanji counts as a word; punctuation,
# digits and Latin do not. Kanji: the unified ideographs, extension A and the rarer ones past
# U+20000; with the iteration marks (々 ゝ ゞ ヽ ヾ) and 〆.
_JAPANESE = re.compile("[ぁ-ゖゝゞァ-ヺー-ヾ々〆㐀-䶿一-鿿\U00020000-\U0003134F]")
_RUBY = re.compile(r"<(rt|rp)>.*?</\1>", re.S | re.I)
_BRACKET_FURIGANA = re.compile(r"\[[^\]]*\]")
_LINE_BREAK = re.compile(r"<br\s*/?>|</div>|</p>", re.I)
_WORD_SEPARATOR = re.compile(r"[,、;\n]")  # after NFKC, so ， and ； too
_COLUMNS = re.compile(r"[\t,]")  # of a word list
# Anki's furigana, " 食[た]べる": the space only marks where the kanji start
_ANKI_FURIGANA = re.compile(r" ?([^ \[\]]+)\[[^\]]*\]")

# Grammar, not vocabulary: particles and the copula / polite endings. They are in every
# line, so they would drown out the words worth learning in the coverage score.
PARTICLES = {
    "は", "が", "を", "に", "へ", "で", "と", "も", "の", "や", "か", "ね", "よ", "ぞ", "ぜ",
    "さ", "わ", "な", "ば", "て", "し", "から", "まで", "より", "って", "けど", "けれど",
    "けれども", "だけ", "しか", "ほど", "くらい", "ぐらい", "など", "なんて", "でも", "ので",
    "のに", "ばかり", "こそ", "さえ", "ながら", "たり", "とか", "なあ", "かな", "かい",
    "だ", "です", "ます",
}


def normalize(word):
    return unicodedata.normalize("NFKC", word).strip()


_norm = normalize


def _plain(field):
    text = _BRACKET_FURIGANA.sub("", _RUBY.sub("", field))
    return _norm(strip_html(text))


def clean_word(field):
    """Word-field contents -> plain headword: no HTML, no furigana, no spaces."""
    return _plain(field).replace(" ", "")


def card_words(field):
    """Word-field contents -> the headwords in it. A field can hold several, separated by
    commas or line breaks: AnkiMorphs' am-study-morphs ("食べる, 物"), or spellings
    ("綺麗、奇麗")."""
    text = _plain(_LINE_BREAK.sub(",", field))
    return [word for part in _WORD_SEPARATOR.split(text) if (word := part.replace(" ", ""))]


def _stability(data):
    """FSRS stability, in days, from a card's data column ('{"s": 41.2, ...}'), or None."""
    if not data or not data.startswith("{"):
        return None
    try:
        s = json.loads(data).get("s")
    except (ValueError, AttributeError):
        return None
    return s if isinstance(s, (int, float)) and not isinstance(s, bool) else None


def _card_days(ivl, data, fsrs):
    """How strong a card is, in days: its FSRS stability if FSRS is on and the card has
    one, else its interval."""
    stability = _stability(data) if fsrs else None
    return ivl if stability is None else stability


def is_word(surface, base=""):
    return bool(_JAPANESE.search(surface) or _JAPANESE.search(base))


def _source_fields(col, sources, setting, field_key):
    """(n, note type id, field index) for each of `sources` ({"note_type": ...,
    `field_key`: ...}) with a field. Raises SetupError if a note type or field is missing."""
    out = []
    for n, source in enumerate(sources):
        if not isinstance(source, dict):
            raise SetupError(f"{setting}: {source!r} isn't "
                             f'{{"note_type": ..., "{field_key}": ...}}')
        note_type, field = source.get("note_type", ""), source.get(field_key, "")
        if not field:
            continue
        model = col.models.by_name(note_type)
        if model is None:
            raise SetupError(f"Note type '{note_type}' not found")
        names = [f["name"] for f in model["flds"]]
        if field not in names:
            raise SetupError(f"Note type '{note_type}' has no field '{field}'")
        out.append((n, model["id"], names.index(field)))
    return out


_CARDS_SQL = ("select n.id, n.flds, c.ivl, c.data, c.id, c.queue from cards c join notes n "
              "on c.nid = n.id where n.mid = ?")


def read_sentence_cards(col, cfg, fsrs):
    """{card id: (its days, the plain text of its sentence)} for the cards of the mining note
    type's sentence field and of `sentence_sources`; suspended cards as in read_cards."""
    sources = [{"note_type": cfg["note_type"], "sentence_field": cfg["sentence_field"]},
               *cfg["sentence_sources"]]
    out = {}
    for _, mid, idx in _source_fields(col, sources, "sentence_sources", "sentence_field"):
        for _, flds, card_ivl, data, card_id, queue in col.db.all(_CARDS_SQL, mid):
            if queue == -1 and not cfg["count_suspended"]:
                continue
            if text := sentence_text(flds.split("\x1f")[idx]):
                if card_id in out:  # a note type listed twice, with another field
                    text = f"{out[card_id][1]}\n{text}"
                out[card_id] = (_card_days(card_ivl, data, fsrs), text)
    return out


def sentence_words(texts, key):
    """{text: (its words, as content_lemmas gives them)} for `texts`, from SENTENCE_CACHE,
    asking Yomitan for the ones not in it. If Yomitan can't be reached, the texts it didn't
    split are left out: they are asked for again on the next read. The cache keeps only
    `texts`, so a sentence edited or no longer known drops out of it."""
    texts = list(dict.fromkeys(texts))
    with _sentence_lock:
        cache = JsonFile(SENTENCE_CACHE)
        stored = cache.load({})
        words = (stored.get("words") if isinstance(stored, dict)
                 and stored.get("analysis") == key else None)
        words = ({t: w for t, w in words.items() if isinstance(w, list)}
                 if isinstance(words, dict) else {})
        todo = [t for t in texts if t not in words]
        try:
            for i in range(0, len(todo), SENTENCE_CHUNK):
                chunk = todo[i:i + SENTENCE_CHUNK]
                for text, tokens in zip(chunk, tokenizer.tokenize_many(chunk)):
                    words[text] = list(content_lemmas(tokens))
        except yomitan.YomitanError as e:
            log.info("couldn't split %d sentences into words: %s",
                     sum(t not in words for t in todo), e)
        found = {t: words[t] for t in texts if t in words}
        cache.save({"analysis": key, "words": found})
    return {t: tuple(w) for t, w in found.items()}


def clear_sentence_cache():
    """Forget the sentences' words: Yomitan's dictionaries changed."""
    with _sentence_lock:
        try:
            SENTENCE_CACHE.unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            log.info("couldn't delete %s: %s", SENTENCE_CACHE, e)


def _main_col():
    from aqt import mw
    return mw.col


def _addon_config():
    from ..core import config
    return config.get()


class Knowledge:
    """`col` and `config` (a function returning the add-on's settings, see core/config.py) default
    to Anki's; tests pass their own."""

    def __init__(self, col=None, config=None):
        self._col = (lambda: col) if col is not None else _main_col
        self._config = config or _addon_config
        self._days = {}        # word -> interval (or FSRS stability) of its strongest card,
                               # in days (0 = new/learning card)
        self._notes = {}       # word -> id of a note of the mining note type for it
        self._cards = {}       # card id -> (its days, (the words it counts for))
        self._word_cards = {}  # word -> ids of the cards that count for it
        self._sentences = {}   # sentence card id -> (its days, the words of its sentence,
                               # None until read: only a known card's are, see read_cards)
        self._sentence_days = {}        # word -> days of its strongest sentence card
        self._sentence_word_cards = {}  # word -> ids of the sentence cards that have it
        self._threshold = 0
        self._manual_store = SyncedMap(KNOWN_PREFIX, col, legacy=lambda: self._legacy_marks(col))
        self._manual = self._load_manual()

    # --- building ---------------------------------------------------------

    def rebuild(self):
        """Re-read the cards now (see read_cards)."""
        cfg = self._config()
        self.apply(cfg["known_interval"], *self.read_cards(self._col(), cfg))

    def apply(self, threshold, days, notes, cards=None, sentences=None):
        """Use what read_cards found (main thread)."""
        self._threshold, self._days, self._notes = threshold, days, notes
        self._cards = cards or {}
        self._word_cards = {}
        for card_id, (_, words) in self._cards.items():
            for word in words:
                self._word_cards.setdefault(word, []).append(card_id)
        self._sentences = sentences or {}
        self._sentence_days, self._sentence_word_cards = {}, {}
        for card_id, (card_days, words) in self._sentences.items():
            for word in words or ():
                self._sentence_word_cards.setdefault(word, []).append(card_id)
                self._sentence_days[word] = max(self._sentence_days.get(word, 0), card_days)

    @staticmethod
    def read_cards(col, cfg):
        """(days, notes, cards) from every card of the mining note type and of the extra
        `vocab_sources`: {word: interval (or FSRS stability) of its strongest card, in days,
        0 for a new / learning card}, {word: id of a mining note for it} and {card id: (its
        days, (the words it counts for))}, so that a review updates its card (update_card)
        rather than all of them being read again. One SQL query
        per note type; it doesn't touch the Knowledge, so it can run in the background (as
        a QueryOp). Raises SetupError if a note type or word field is missing. Without a
        word field, only the words marked as known count. Suspended cards don't count unless
        `count_suspended`, but a mining note whose cards are all suspended is still in
        `notes`: a sentence goes onto it rather than onto a duplicate. With FSRS on, a card
        is measured by its stability rather than its interval.

        A fourth dict, {card id: (its days, the words of its sentence)}, has the sentence
        cards (read_sentence_cards) if `sentence_words` is on: every word of a known card's
        sentence is known. Only known cards' sentences are split into words (by Yomitan, see
        sentence_words); the others, and those Yomitan couldn't be asked about, have None."""
        sources = [{"note_type": cfg["note_type"], "word_field": cfg["word_field"]},
                   *cfg["vocab_sources"]]
        fsrs = col.get_config("fsrs", False)

        days, notes, cards = {}, {}, {}
        for n, mid, idx in _source_fields(col, sources, "vocab_sources", "word_field"):
            for nid, flds, card_ivl, data, card_id, queue in col.db.all(_CARDS_SQL, mid):
                words = card_words(flds.split("\x1f")[idx])
                if n == 0:  # only the mining note type: sentences get added to those
                    for word in words:
                        notes.setdefault(word, nid)
                if queue == -1 and not cfg["count_suspended"]:
                    continue
                card_days = _card_days(card_ivl, data, fsrs)
                if words:  # a note type can be listed twice, with another field
                    counted = cards.get(card_id, (0, ()))[1]
                    cards[card_id] = (card_days, (*counted, *words))
                for word in words:
                    days[word] = max(days.get(word, 0), card_days, 0)
        if not cfg["sentence_words"]:
            return days, notes, cards, {}
        found = read_sentence_cards(col, cfg, fsrs)
        threshold = cfg["known_interval"]
        split = sentence_words([text for card_days, text in found.values()
                                if card_days >= threshold], tokenizer.analysis_key(cfg))
        sentences = {card_id: (card_days, split.get(text) if card_days >= threshold else None)
                     for card_id, (card_days, text) in found.items()}
        return days, notes, cards, sentences

    @staticmethod
    def read_card(col, card_id):
        """A card's days (see read_cards), as the collection has them now."""
        row = col.db.first("select ivl, data from cards where id = ?", card_id)
        return _card_days(row[0], row[1], col.get_config("fsrs", False)) if row else 0

    def counts(self, card_id):
        """Does this card count towards known words (as of the last read_cards)?"""
        return card_id in self._cards or card_id in self._sentences

    def update_card(self, card_id, card_days):
        """A card that counts was reviewed: `card_days` is its new interval (or stability).
        Its words take their strongest card's days again, which are fewer after a lapse.
        True if the cards have to be read again: a sentence card has just become known, and
        its sentence hasn't been split into words yet."""
        if card_id in self._cards:
            words = self._cards[card_id][1]
            self._cards[card_id] = (card_days, words)
            for word in words:
                self._days[word] = max([0, *(self._cards[c][0]
                                             for c in self._word_cards.get(word, ()))])
        if card_id not in self._sentences:
            return False
        words = self._sentences[card_id][1]
        self._sentences[card_id] = (card_days, words)
        if words is None:
            return card_days >= self._threshold
        for word in words:
            self._sentence_days[word] = max(self._sentences[c][0]
                                            for c in self._sentence_word_cards[word])
        return False

    def add_mined(self, word, note_id=None):
        """A card for `word` now exists: it is at least 'learning'."""
        word = _norm(word)
        if word:
            self._days.setdefault(word, 0)
            if note_id:
                self._notes.setdefault(word, note_id)

    def note_for(self, *words):
        """Id of an existing mining note for the first of `words` that has one, or None."""
        for word in words:
            if word and (nid := self._notes.get(_norm(word))):
                return nid
        return None

    # --- mark as known ----------------------------------------------------

    def is_marked(self, word):
        return _norm(word) in self._manual

    def set_marked(self, word, marked):
        self.mark([word], marked)

    def mark(self, words, marked=True):
        """Mark (or unmark) `words` as known, saving the synced marks once."""
        words = {_norm(w) for w in words}
        if marked:
            self._manual |= words
        else:
            self._manual -= words
        self._manual_store.set_many({w: bool(marked) for w in words})

    @staticmethod
    def _legacy_marks(col):
        """{word: True} from the single synced list of older versions, or else from the
        file before it."""
        words = SyncedValue(KNOWN_KEY, col).load()
        if words is None:
            try:
                words = json.loads(OLD_KNOWN_FILE.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                words = []
        try:
            return {_norm(w): True for w in words}
        except TypeError:
            return {}

    def known_words(self, marked_only=False):
        """Every word that counts as known, sorted: those marked known, and (unless
        `marked_only`) those whose strongest card is at least the known interval, or that are
        in the sentence of a sentence card that is."""
        words = set(self._manual)
        if not marked_only:
            words |= {w for w, days in self._days.items() if days >= self._threshold}
            words |= {w for w, days in self._sentence_days.items() if days >= self._threshold}
        return sorted(words)

    def history_inputs(self):
        """What progress.known_history needs, copied (main thread): the known interval,
        {card id: (its days now, the words it counts for)} of the word cards and of the
        known sentence cards, and {word: time it was marked known, 0 if not known} of the
        words marked known."""
        cards = dict(self._cards)
        for card_id, (card_days, words) in self._sentences.items():
            if words:  # a sentence card that is known (only those were split into words)
                cards[card_id] = (card_days, (*cards.get(card_id, (0, ()))[1], *words))
        stamps = self._manual_store.load_stamped()
        marks = {word: float(stamps.get(word, (True, 0))[1] or 0) for word in self._manual}
        return self._threshold, cards, marks

    def _load_manual(self):
        return {word for word, marked in self._manual_store.load().items() if marked is True}

    def reload_marks(self):
        """Read the marks again, every computer's: a sync may have brought new ones."""
        self._manual = self._load_manual()

    # --- classifying ------------------------------------------------------

    def _status(self, word):
        """A word of a known sentence card's sentence is known; one of a sentence card not
        known yet isn't 'learning': you may not have learnt it from that card."""
        if word in self._manual or self._sentence_days.get(word, -1) >= self._threshold:
            return "known"
        days = self._days.get(word)
        if days is None:
            return None
        return "known" if days >= self._threshold else "learning"

    def classify(self, surface, base="", alts=()):
        """'new' | 'learning' | 'known' for a word, or '' for punctuation, particles and the
        like. A card for the dictionary form (食べた -> 食べる), another spelling or reading of
        it (`alts`: 綺麗 -> 奇麗, きれい) or the text as written counts; the best one wins."""
        if not is_word(surface, base) or _norm(base or surface) in PARTICLES:
            return ""
        statuses = {self._status(_norm(key)) for key in (base, *alts, surface) if key}
        return next((s for s in ("known", "learning") if s in statuses), "new")

    def has(self, word):
        """Whether a card or a mark has `word`, known or still learning."""
        return self._status(_norm(word)) is not None

    def token_status(self, surface, base="", alts=()):
        """classify() for a token as Yomitan split a line. A Japanese token with no
        dictionary form is a name or a fragment Yomitan can't look up: not a word to learn,
        so it is 'other' (never new, no colour) unless you have a card for it or marked it,
        as written."""
        if base or not is_word(surface):
            return self.classify(surface, base, alts)
        return self._status(_norm(surface)) or "other"

    def new_words(self, tokens):
        """The distinct new words (dictionary forms) among (surface, reading, base, alts)
        tokens, in the order they first appear. Names and fragments (no dictionary form)
        aren't: the same words as content_lemmas counts."""
        return list(dict.fromkeys(_norm(base or surface) for surface, _, base, alts in tokens
                                  if self.token_status(surface, base, alts) == "new"))

    def unknown_words(self, tokens):
        """new_words, as a set."""
        return set(self.new_words(tokens))

    def line_kind(self, tokens):
        """'known' (no new word), 'i+1' (exactly one) or 'harder' (several). Words you are
        still learning don't count as new."""
        unknown = len(self.unknown_words(tokens))
        return "known" if unknown == 0 else "i+1" if unknown == 1 else "harder"

    def coverage(self, lemmas):
        """Counts {'known', 'learning', 'new'} over {dictionary form: other spellings}, as
        content_lemmas gives."""
        counts = {"known": 0, "learning": 0, "new": 0}
        for lemma, alts in lemmas.items():
            if status := self.classify(lemma, lemma, alts):
                counts[status] += 1
        return counts


def content_lemmas(tokens):
    """{dictionary form: its other spellings and readings} for the distinct words among
    (surface, reading, base, alts) tokens that are worth learning: no particles, no
    punctuation, and nothing Yomitan has no headword for (names and fragments you can't look
    up anyway)."""
    lemmas = {}
    for surface, _, base, alts in tokens:
        if base and is_word(surface, base) and (lemma := _norm(base)) not in PARTICLES:
            lemmas[lemma] = lemmas.get(lemma, frozenset()) | {_norm(a) for a in alts}
    return lemmas


def words_in_list(text):
    """The words of a word list, to mark them known: one per line, as plain text or as
    tab- or comma-separated columns (a frequency list, an export), where the first column
    holding Japanese is the word ("1<tab>の", "食べる,たべる"). Blank lines and "#" comments
    are skipped; furigana and HTML are stripped. In order, without repeats."""
    words = []
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        word = next((w for col in _COLUMNS.split(line) if is_word(w := clean_word(col))),
                    None)
        if word:
            words.append(word)
    return list(dict.fromkeys(words))


def export_text(words, title):
    """A word list of `words`, one per line, under a "#" comment line saying what it is:
    words_in_list reads it back."""
    return "".join(f"{line}\n" for line in (f"# {title}", *words))


def sentence_text(field):
    """A sentence field's contents -> its plain text, to tokenize: no HTML, furigana or
    [sound:] tags; line breaks kept."""
    text = _ANKI_FURIGANA.sub(r"\1", _RUBY.sub("", _LINE_BREAK.sub("\n", field)))
    text = strip_html(_BRACKET_FURIGANA.sub("", text))
    return "\n".join(line for line in (_norm(part) for part in text.split("\n")) if line)


def words_to_reach(counts, percent):
    """How many new words you would have to learn for `percent`% of an episode's distinct
    words to be known or learning (the same bar as i+1: words in progress count)."""
    total = sum(counts.values())
    needed = -(-percent * total // 100)  # ceil, without float error
    return max(0, needed - counts["known"] - counts["learning"])
