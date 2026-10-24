"""What the player knows about the subtitles: the episode's lines, their tokens, the line on
screen and how much of the episode a card covers. Pure logic, apart from tokenize_file."""
import hashlib
import logging
import re
import threading
from collections import Counter, OrderedDict
from pathlib import Path

from ..core.model import LINE_BREAK, Line, Span, Token
from ..core.store import USER_FILES, JsonFile
from ..language import knowledge
from . import subtitles

TOKEN_CACHE_DIR = USER_FILES / "tokens"
TOKEN_CACHE_KEEP = 300   # newest subtitle files whose tokens stay cached on disk
# Which video and subtitle file each cached tokenization is of: the cache is named by the
# file's contents, so without it the library couldn't say where a line was heard.
TOKEN_INDEX = "index.json"
_index_lock = threading.Lock()  # the episode and the playlist scores are written from two threads
log = logging.getLogger(__name__)
_CACHE_VERSION = 3       # bump when the cached token format changes

# A sentence split over subtitle lines: a line ending in a comma or an arrow, or in a particle
# that can't end a sentence (を, に...; not か, よ, の, と or も, which often do: "私も"), goes
# on into the next line if that one follows within MERGE_GAP seconds without overlapping it
# (overlapping lines are two people talking).
_CONTINUES = re.compile(r"[、,，→]$")
CONTINUING_PARTICLES = {"は", "が", "を", "に", "へ", "で", "や"}
MERGE_GAP = 1.0
MERGE_MAX = 3            # lines a merged sentence takes at most


class LRU:
    """Dict with a size limit: the least recently used entry goes first."""

    def __init__(self, size):
        self._size, self._data = size, OrderedDict()

    def get(self, key, default=None):
        if key not in self._data:
            return default
        self._data.move_to_end(key)
        return self._data[key]

    def put(self, key, value):
        self._data[key] = value
        self._data.move_to_end(key)
        while len(self._data) > self._size:
            self._data.popitem(last=False)

    def __contains__(self, key):
        return key in self._data


# --- tokenizing whole files (background thread) ----------------------------------

def _cache_file(sub_path, key):
    digest = hashlib.sha1(f"{_CACHE_VERSION}:{key}:".encode())
    digest.update(Path(sub_path).read_bytes())
    return JsonFile(TOKEN_CACHE_DIR / f"{digest.hexdigest()}.json")


def _cache_files():
    return [f for f in TOKEN_CACHE_DIR.glob("*.json") if f.name != TOKEN_INDEX]


def _prune_cache():
    try:
        files = sorted(_cache_files(), key=lambda f: f.stat().st_mtime)
        for f in files[:-TOKEN_CACHE_KEEP]:
            f.unlink()
    except OSError as e:
        log.info("couldn't prune the token cache: %s", e)


def clear_token_cache():
    """Delete every cached tokenization: they hold Yomitan's word splits and headwords,
    which change with its dictionaries. The index goes with them."""
    for f in TOKEN_CACHE_DIR.glob("*.json"):
        try:
            f.unlink()
        except OSError as e:
            log.info("couldn't delete %s: %s", f, e)


def _index_file():
    return JsonFile(TOKEN_CACHE_DIR / TOKEN_INDEX)


def _index_episode(cache, video, sub_path):
    """Note that `cache` holds the tokens of `video`'s subtitles, `sub_path`; entries whose
    cache file was pruned are dropped on the way."""
    with _index_lock:
        index = _index_file()
        entries = index.load({})
        if not isinstance(entries, dict):
            entries = {}
        entry = {"video": str(video), "sub": str(sub_path)}
        if entries.get(cache.path.name) == entry:
            return
        entries[cache.path.name] = entry
        index.save({name: e for name, e in entries.items()
                    if (TOKEN_CACHE_DIR / name).is_file()})


def cached_episodes():
    """[(cache file, video, subtitle file)] for every episode whose tokens are cached and
    indexed, newest first."""
    with _index_lock:
        entries = _index_file().load({})
    out = []
    for name, entry in (entries.items() if isinstance(entries, dict) else ()):
        f = TOKEN_CACHE_DIR / name
        try:
            video, sub, mtime = entry["video"], entry["sub"], f.stat().st_mtime
        except (OSError, KeyError, TypeError):
            continue  # pruned since, or not an entry
        out.append((mtime, f, video, sub))
    return [(f, video, sub) for _, f, video, sub in sorted(out, reverse=True)]


def load_cached_tokens(cache_path):
    """{line text: tokens} from a cache file, or {} if it can't be read."""
    cached = JsonFile(cache_path).load({})
    if not isinstance(cached, dict):
        return {}
    try:
        return {text: [_token(t) for t in tokens] for text, tokens in cached.items()}
    except (TypeError, ValueError):
        return {}


def all_cached_tokens():
    """The tokens of every line in the cache, a list per line: what Yomitan has said about
    the words of every episode analysed so far (offline.Lexicon learns from them)."""
    for path in _cache_files():
        yield from load_cached_tokens(path).values()


def _furigana(value):
    """Furigana as cached (JSON lists) -> as yomitan.parse_tokenize gives it (tuples)."""
    return value if isinstance(value, str) else tuple(tuple(pair) for pair in value)


def _token(cached):
    """A token as cached (a JSON list) -> Token."""
    surface, furigana, base, alts = cached
    return Token(surface, _furigana(furigana), base, tuple(alts))


def tokenize_file(sub_path, key, tokenize_many, video=None):
    """(events, {line text: tokens}) for a subtitle file. Tokens are cached on disk by the
    file's contents and `key` (what they depend on: tokenizer.analysis_key), so reopening an
    episode skips the Yomitan round trip; with `video`, the cache is indexed as that video's,
    for searching the library."""
    events = subtitles.load_events(sub_path)
    unique = list(dict.fromkeys(text for _, _, text in events))
    try:
        cache = _cache_file(sub_path, key)
    except OSError:
        cache = None
    cached = cache.load({}) if cache else {}
    if isinstance(cached, dict) and all(text in cached for text in unique):
        tokens = {text: [_token(t) for t in cached[text]] for text in unique}
        cache.path.touch()  # keeps it among the newest when pruning
    else:
        tokens = dict(zip(unique, tokenize_many(unique)))
        if cache:
            cache.save(tokens)
            _prune_cache()
    if cache and video and cache.path.is_file():
        _index_episode(cache, video, sub_path)
    return events, tokens


def token_offsets(text, tokens):
    """Where each token starts in `text` (None for one that isn't found), so that a word can
    be found where it is in the line rather than where its text first appears."""
    out, at = [], 0
    for token in tokens:
        i = text.find(token.surface, at) if token.surface else -1
        out.append(i if i >= 0 else None)
        if i >= 0:
            at = i + len(token.surface)
    return out


# --- the episode and the line on screen ------------------------------------------

class SubtitleSession:
    def __init__(self):
        self.tokens = {}          # line text -> tokens, for the episode playing
        self.lookups = LRU(500)   # word -> Yomitan entries, kept across lines
        self.pitches = {}         # (surface, base) -> pitch-accent category ("" for none)
        self.events = []          # (start, end, text) of the episode's lines, file times
        self.target_events = []   # same for the translation file, if there is one
        self.line = None          # model.Line on screen
        self.translation = ""     # target-language text of the line on screen
        self.selected = None      # model.Selection: the word whose popup is showing
        self.extra = [0, 0]       # lines added before / after the line on screen, for a card

    def new_episode(self):
        self.tokens = {}  # the next episode's file brings its own (cached on disk)
        self.events, self.target_events = [], []
        self.line, self.translation, self.selected = None, "", None
        self.extra = [0, 0]

    def forget_analysis(self):
        """Drop what Yomitan said about the lines and words (Yomitan's dictionaries changed):
        the lines are tokenized again and the words looked up again."""
        self.tokens = {}
        self.lookups = LRU(500)
        self.pitches = {}

    def set_line(self, text, start, end, video, translation, keep_range=False,
                 merge_delay=None):
        """A new line is on screen. False if it is the one already there (the same text can
        come as a new line: the start time tells them apart). A new line resets the card's
        range to just itself, unless `keep_range` (a looped range coming round again); the
        gap between lines doesn't. With `merge_delay` (the subtitle delay), the range is
        rather the lines its sentence is split over (see sentence_extra)."""
        if self.line and (text, start) == (self.line.text, self.line.start):
            return False
        self.line = Line(text, start, end, video)
        self.translation = translation
        self.selected = None
        if text and not keep_range:
            self.extra = [0, 0]
            i = None if merge_delay is None else self.index_at(start, merge_delay, text)
            if i is not None:
                self.extra = self.sentence_extra(i)
        return True

    # --- kinds of lines ----------------------------------------------------

    def line_kind(self, know, text):
        """'known' | 'i+1' | 'harder' for a line, None until it is tokenized."""
        tokens = self.tokens.get(text)
        return None if tokens is None else know.line_kind(tokens)

    def next_line_time(self, now, delay, accept):
        """Start, in video time, of the first line after `now` whose kind `accept`s."""
        for ev in self.events:
            if ev.start + delay > now + 0.1 and accept(ev.text):
                return ev.start + delay
        return None

    # --- syncing the subtitles -------------------------------------------------

    def sync_delay(self, now, delay, index=None):
        """The subtitle delay that makes a line start at video time `now`: line `index`, or
        else the line whose start (with the current `delay`) is nearest. None without lines."""
        if index is None:
            if not self.events:
                return None
            index = min(range(len(self.events)),
                        key=lambda i: abs(self.events[i].start + delay - now))
        return now - self.events[index].start

    # --- sentences split over lines ---------------------------------------------

    def continues(self, i):
        """Does the sentence of line `i` go on into line i + 1?"""
        if not 0 <= i < len(self.events) - 1:
            return False
        line, after = self.events[i], self.events[i + 1]
        if not line.end - 0.05 <= after.start <= line.end + MERGE_GAP:
            return False
        text = line.text.rstrip()
        if _CONTINUES.search(text):
            return True
        words = [t for t in self.tokens.get(line.text, ()) if t.surface.strip()]
        return bool(words) and words[-1].surface in CONTINUING_PARTICLES

    def sentence_extra(self, i):
        """[lines before, lines after] line `i` that its sentence is split over, MERGE_MAX
        lines in all at most."""
        before = after = 0
        while before < MERGE_MAX - 1 and self.continues(i - before - 1):
            before += 1
        while before + after < MERGE_MAX - 1 and self.continues(i + after):
            after += 1
        return [before, after]

    def event_span(self, i, delay, merge=False):
        """The Span a card for line `i` of the episode takes, in video time: with `merge`,
        with the lines its sentence is split over."""
        before, after = self.sentence_extra(i) if merge else (0, 0)
        lines = self.events[i - before:i + after + 1]
        return Span("\n".join(ev.text for ev in lines), lines[0].start + delay,
                    max(ev.end for ev in lines) + delay)

    # --- neighbours and ranges ---------------------------------------------

    def index_at(self, start, delay, text=None):
        """Index in `events` of the line shown at video time `start`, or None. Lines with the
        same text are preferred: two lines can start together."""
        if start is None or not self.events:
            return None
        candidates = [i for i, ev in enumerate(self.events) if ev.text == text] if text else []
        candidates = candidates or range(len(self.events))
        best = min(candidates, key=lambda i: abs(self.events[i].start + delay - start))
        return best if abs(self.events[best].start + delay - start) < 1.0 else None

    def neighbour_time(self, now, delay, step):
        """Start (video time) of the line after the playhead (step +1), or of the line before
        the one on screen (step -1; before the playhead between lines)."""
        starts = [ev.start + delay for ev in self.events]
        if step > 0:
            return next((s for s in starts if s > now + 0.1), None)
        on_screen = self.line and self.line.text and self.line.start is not None
        ref = self.line.start if on_screen else now
        return max((s for s in starts if s < ref - 0.05), default=None)

    def extend(self, side, delay):
        """Add the line before (side 0) or after (side 1) to the card's range. False at the
        edge of the episode, or when the line on screen isn't one of the file's lines."""
        if not self.line:
            return False
        i = self.index_at(self.line.start, delay, self.line.text)
        if i is None:
            return False
        if side == 0 and i - self.extra[0] - 1 < 0:
            return False
        if side == 1 and i + self.extra[1] + 1 >= len(self.events):
            return False
        self.extra[side] += 1
        return True

    def span(self, delay):
        """The Span a card would take, in video time: the line on screen plus the lines
        added with extend(). None without a timed line."""
        line = self.line
        if not line or not line.text or line.start is None or line.end is None:
            return None
        if not any(self.extra):
            return Span(line.text, line.start, line.end)
        i = self.index_at(line.start, delay, line.text)
        if i is None:
            return Span(line.text, line.start, line.end)
        lines = self.events[max(0, i - self.extra[0]):i + self.extra[1] + 1]
        return Span("\n".join(ev.text for ev in lines),
                    min(lines[0].start + delay, line.start), max(lines[-1].end + delay, line.end))

    def span_offset(self, delay):
        """Where the line on screen starts in span(delay).text: after the lines added
        before it."""
        line = self.line
        if not line or not self.extra[0]:
            return 0
        i = self.index_at(line.start, delay, line.text)
        if i is None:
            return 0
        return sum(len(ev.text) + 1 for ev in self.events[max(0, i - self.extra[0]):i])

    def tokens_for(self, text):
        """The tokens of a card's text: one line, or several joined by line breaks. Lines not
        tokenized yet have none."""
        if text in self.tokens:
            return self.tokens[text]
        out, piece = [], []
        for part in text.split("\n"):  # a line may hold line breaks itself
            piece.append(part)
            joined = "\n".join(piece)
            if joined in self.tokens:
                out.extend([LINE_BREAK] if out else [])
                out.extend(self.tokens[joined])
                piece = []
        return out

    def line_before(self, start, delay):
        """Text of the last line that starts before video time `start`: the context of a
        card starting there. "" for the episode's first line."""
        before = [ev for ev in self.events if ev.start + delay < start - 0.05]
        return max(before, key=lambda ev: ev.start).text if before else ""

    def token_offset(self, text, index):
        """Where token number `index` of `text` starts in it, or None."""
        tokens = self.tokens.get(text)
        if tokens is None or index is None or not 0 <= index < len(tokens):
            return None
        return token_offsets(text, tokens)[index]

    def word_offset(self, text, lemma):
        """Where the first word of `text` whose dictionary form is `lemma` starts, or None."""
        tokens = self.tokens.get(text, ())
        for token, at in zip(tokens, token_offsets(text, tokens)):
            if knowledge.normalize(token.base or token.surface) == lemma:
                return at
        return None

    def translation_for(self, start, end, delay, fallback=""):
        """The translation file's lines overlapping [start, end] (video time), or `fallback`
        (what mpv showed with the line) when the file has none or there is no file."""
        lines = [t for s, e, t in self.target_events
                 if s + delay < end - 0.05 and e + delay > start + 0.05]
        return "\n".join(lines) if lines else fallback

    # --- the episode's words -----------------------------------------------

    def new_word_counts(self, know):
        """[(dictionary form, number of lines it appears in)] for the episode's new words,
        most frequent first."""
        counts = Counter()
        for _, _, text in self.events:
            tokens = self.tokens.get(text, ())
            counts.update(know.unknown_words(tokens) & knowledge.content_lemmas(tokens).keys())
        return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))

    def best_line_for(self, lemma, know):
        """(index, surface) of the line a card for `lemma` should use, or None if no line has
        it: one where it is the only new word (i+1) if there is any, then the shortest, then
        the first."""
        best = None
        for i, (_, _, text) in enumerate(self.events):
            tokens = self.tokens.get(text, ())
            surface = next((surface for surface, _, base, _ in tokens
                            if knowledge.normalize(base or surface) == lemma), None)
            if surface is None:
                continue
            rank = (know.line_kind(tokens) != "i+1", len(text))
            if best is None or rank < best[0]:
                best = rank, (i, surface)
        return best and best[1]

    def lines_matching(self, query):
        """Indices of the lines containing `query`, as written or as a dictionary form."""
        q = knowledge.normalize(query)
        if not q:
            return list(range(len(self.events)))
        return [i for i, (_, _, text) in enumerate(self.events)
                if line_matches(q, text, self.tokens.get(text, ()))]


def line_matches(q, text, tokens):
    """Does a line hold `q` (normalized), as written or as one of its words' dictionary
    form?"""
    return q in text or any(knowledge.normalize(base) == q for _, _, base, _ in tokens)
