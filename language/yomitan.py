"""Thin client for yomitan-api (https://github.com/yomidevs/yomitan-api), or for Ankime's own
dictionaries (local_dict.py) when the dictionary setting says so: they answer the same calls
in the same shape.

All calls block (on HTTP, or the database): run them via mw.taskman.run_in_background.
"""
import base64
import logging
import sqlite3
import threading
import time
from contextlib import contextmanager

import requests

from ..core import config
from ..core.model import LINE_BREAK, Token
from . import local_dict

# What the popup shows (cheap: no audio, no full glossary), and the pitch-accent category
# the subtitles can be coloured by.
MARKERS = ["expression", "reading", "glossary-brief", "frequencies", "pitch-accents",
           "pitch-accent-categories"]

# What a card takes: card key -> Yomitan marker. "definition" is either glossary marker,
# see the definition_marker config.
CARD_MARKERS = {"word": "expression", "reading": "reading", "pitch": "pitch-accents",
                "frequency": "frequency-harmonic-rank", "word_audio": "audio"}
NO_FREQUENCY = "9999999"  # what frequency-harmonic-rank gives when no dictionary has the word
DEFINITION_MARKERS = [("glossary-brief", "Short glossary"),
                      ("glossary", "Full glossary (every dictionary)")]


log = logging.getLogger(__name__)

CONNECT_TIMEOUT = 2  # seconds; the API is on this computer, it answers at once or not at all


def _session():
    """One HTTP session for every call, from any thread: connections are kept alive between
    calls (a few hundred frequency lookups in a row). The API runs on this computer, so the
    proxy settings of the environment and of Windows are ignored: a system proxy that
    doesn't exempt 127.0.0.1 would otherwise make Yomitan look offline."""
    s = requests.Session()
    s.trust_env = False
    adapter = requests.adapters.HTTPAdapter(pool_maxsize=8)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    return s


_SESSION = _session()


class YomitanError(Exception):
    pass


class YomitanOffline(YomitanError):
    """The API can't be reached (or didn't answer in time), or is still marked offline."""


class Breaker:
    """After the API fails to answer, calls fail at once instead of each waiting for its
    timeout, until a retry is due. Then one call goes through as a probe; each failed probe
    doubles the wait, up to `cap` seconds. Safe from any thread."""

    def __init__(self, base=2.0, cap=60.0, clock=time.monotonic):
        self._base, self._cap, self._clock = base, cap, clock
        self._lock = threading.Lock()
        self.error = None   # message of the failure that made it offline, None = online
        self._wait = 0.0    # seconds between retries
        self._retry_at = 0.0

    @property
    def offline(self):
        return self.error is not None

    def seconds_to_retry(self):
        with self._lock:
            return max(0.0, self._retry_at - self._clock()) if self.offline else 0.0

    def check(self):
        """Raise YomitanOffline unless a call may go out now. Letting a probe through holds
        the other calls back until it is answered."""
        with self._lock:
            if self.error is None:
                return
            now = self._clock()
            if now < self._retry_at:
                raise YomitanOffline(self.error)
            self._retry_at = now + self._cap  # the probe's own failed() reschedules

    def failed(self, error):
        with self._lock:
            self._wait = min(self._wait * 2, self._cap) if self.error else self._base
            self.error = error
            self._retry_at = self._clock() + self._wait

    def succeeded(self):
        with self._lock:
            self.error, self._wait = None, 0.0

    def retry_now(self):
        """Let the next call through (a Retry button, a changed URL)."""
        with self._lock:
            self._retry_at = 0.0


BREAKER = Breaker()

# (config value, label) of the dictionary setting
DICTIONARIES = [("yomitan", "Yomitan in your browser (yomitan-api)"),
                ("local", "Ankime's own (imported dictionaries, no browser needed)")]


def uses_local(cfg=None):
    """Do Ankime's own dictionaries answer, rather than Yomitan?"""
    return (cfg or config.get()).get("dictionary") == "local"


def _post_local(path, body):
    """A call answered by Ankime's own dictionaries. Without any, it is "offline" like
    Yomitan without its browser: the player says so and tries again now and then (an
    import tries at once, see BREAKER.retry_now)."""
    BREAKER.check()
    try:
        result = local_dict.answer(path, body)
    except local_dict.NoDictionary as e:
        BREAKER.failed(str(e))
        raise YomitanOffline(str(e)) from e
    except (local_dict.DictionaryError, sqlite3.Error, OSError, ValueError) as e:
        log.warning("%s failed in the local dictionaries: %s", path, e)
        BREAKER.succeeded()
        raise YomitanError(f"Ankime's dictionaries: {e}") from e
    BREAKER.succeeded()
    return result


def _post(path, timeout=5, base_url=None, **body):
    """Calls to the configured URL go through BREAKER; an explicit `base_url` (Test setup)
    always tries for real, and always Yomitan."""
    if base_url is None and uses_local():
        return _post_local(path, body)
    url = (base_url or config.get()["yomitan_url"]).rstrip("/")
    breaker = BREAKER if base_url is None else None
    if breaker:
        breaker.check()
    try:
        r = _SESSION.post(f"{url}/{path}", json=body, timeout=(CONNECT_TIMEOUT, timeout))
        r.raise_for_status()
        result = r.json()
    except requests.ConnectionError as e:  # ConnectTimeout too: nothing is listening
        reason = ("didn't answer in time" if isinstance(e, requests.Timeout)
                  else "can't be reached")
        message = (f"The Yomitan API at {url} {reason}. Is your browser open and "
                   "\"Enable Yomitan API\" ticked in Yomitan's settings?")
        log.info("%s: %s", path, message)
        if breaker:
            breaker.failed(message)
        raise YomitanOffline(message) from e
    except requests.Timeout as e:
        # connected, but the answer is slow (a big chunk of lines): Yomitan is up, so this
        # call fails without the others being held back as if it were offline
        log.warning("%s took more than %s s", path, timeout)
        if breaker:
            breaker.succeeded()
        raise YomitanError(f"The Yomitan API took more than {timeout} seconds to "
                           "answer") from e
    except (requests.RequestException, ValueError) as e:
        log.warning("%s failed: %s", path, e)
        if breaker:
            breaker.succeeded()  # it answered: the server is up
        raise YomitanError(f"Yomitan API error: {e}") from e
    if breaker:
        breaker.succeeded()
    return result


@contextmanager
def _parsing(what):
    """Turn an answer of the wrong shape (a yomitan-api update, another server at the URL)
    into a YomitanError, which callers show, rather than a crash."""
    try:
        yield
    except (AttributeError, IndexError, KeyError, TypeError, ValueError) as e:
        raise YomitanError(f"Unexpected answer from the Yomitan API ({what}): "
                           f"{type(e).__name__}: {e}") from e


def version(base_url=None):
    result = _post("yomitanVersion", base_url=base_url)
    with _parsing("version"):
        return result.get("version", "?")


def _segments(node):
    """Flatten tokenize 'content' (a list of segments, possibly nested in lines)."""
    if isinstance(node, dict):
        yield node
    elif isinstance(node, list):
        for child in node:
            yield from _segments(child)


def headwords(segs, lemma="", fallback=True, by_reading=True):
    """(dictionary form, other spellings and readings) of a word: 食べた -> (食べる, (たべる,)),
    きれい -> (綺麗, (奇麗, きれい)). Yomitan puts the headwords on the first furigana segment,
    one group per dictionary entry, best first. Only one group is kept: the others are other
    words (かける: 掛ける, 書ける...). It is the first, or with `lemma` (MeCab's dictionary
    form) the first that has it as a spelling, else as a reading: しない is する, not 市内,
    which Yomitan puts first (a reading only counts if `by_reading`). If none has it, the
    first, unless not `fallback`. Punctuation has none."""
    found = []  # (spellings and readings, spellings) of each group
    for seg in segs:
        for group in seg.get("headwords") or []:
            entries = [h for h in group or () if isinstance(h, dict)]
            terms = [h.get(key) for key in ("term", "reading") for h in entries]
            if terms := list(dict.fromkeys(t for t in terms if isinstance(t, str) and t)):
                found.append((terms, {h.get("term") for h in entries}))
        if found:
            break
    terms = next((t for t, spellings in found if lemma in spellings), None) or next(
        (t for t, _ in found if by_reading and lemma in t),
        found[0][0] if found and (fallback or not lemma) else None)
    return (terms[0], tuple(terms[1:])) if terms else ("", ())


def furigana(segs):
    """The word's furigana: ((text, reading), ...) for its segments, the reading "" where
    there is none (okurigana, kana), or "" if no segment has one."""
    pairs = tuple((s.get("text", ""), s.get("reading", "")) for s in segs)
    return pairs if any(r for _, r in pairs) else ""


def parse_words(results, n):
    """A tokenize response -> for each of the `n` input lines, its words: each a list of
    furigana segments, the first with the word's headwords."""
    if isinstance(results, dict):
        results = [results]
    by_index = {r.get("index", i): r for i, r in enumerate(results)}
    # each item of "content" is one word, possibly split into furigana segments
    return [[list(_segments(word)) for word in by_index.get(i, {}).get("content", [])]
            for i in range(n)]


def segments_text(segs):
    return "".join(s.get("text", "") for s in segs)


def parse_tokenize(results, n):
    """A tokenize response -> one list of Token per input line; see furigana and
    headwords."""
    return [[Token(segments_text(segs), furigana(segs), *headwords(segs)) for segs in line]
            for line in parse_words(results, n)]


def line_words(lines):
    """Each line's words as Yomitan's scanning parser splits it (see parse_words)."""
    results = _post("tokenize", timeout=30, text=list(lines),
                    scanLength=config.get()["scan_length"])
    with _parsing("tokenize"):
        return parse_words(results, len(lines))


def _tokenize_lines(lines, base_url=None, scan_length=None):
    scan = scan_length or config.get()["scan_length"]
    results = _post("tokenize", timeout=30, base_url=base_url, text=lines, scanLength=scan)
    with _parsing("tokenize"):
        return parse_tokenize(results, len(lines))


def split_lines(texts, tokenize_lines):
    """[Token] for each text, in order, covering the text. Each text is split on line
    breaks for `tokenize_lines`; the breaks come back as model.LINE_BREAK."""
    pieces = [t.split("\n") for t in texts]
    per_line = iter(tokenize_lines([line for p in pieces for line in p]))
    out = []
    for p in pieces:
        tokens = []
        for j in range(len(p)):
            if j:
                tokens.append(LINE_BREAK)
            tokens.extend(next(per_line))
        out.append(tokens)
    return out


def tokenize_many(texts, chunk=100, progress=None, tokenize_lines=None):
    """Tokens for each text, sent to Yomitan `chunk` texts at a time (or to
    `tokenize_lines`: MeCab's, see tokenizer.py). `progress(done, total)` is told how many
    texts are done, before each chunk and once all are."""
    out = []
    for i in range(0, len(texts), chunk):
        if progress:
            progress(i, len(texts))
        out.extend(split_lines(texts[i:i + chunk], tokenize_lines or _tokenize_lines))
    if progress:
        progress(len(texts), len(texts))
    return out


def tokenize(text):
    """Returns [Token] covering `text` in order."""
    return tokenize_many([text])[0]


def lookup(word, max_entries=3):
    """Returns a list of dicts keyed by MARKERS (one per dictionary entry)."""
    result = _post("ankiFields", text=word, type="term", maxEntries=max_entries,
                   includeMedia=False, markers=MARKERS)
    with _parsing("lookup"):
        entries = result.get("fields") or []
        return [e for e in entries if isinstance(e, dict)]


def card_fields(word, definition_marker="glossary-brief", with_media=True, entry=0):
    """Everything a word card takes, from entry number `entry` (0 = the first) Yomitan
    finds for `word`, in the order lookup() gives them: (fields, media). `fields` is keyed
    like CARD_MARKERS plus "definition"; `media` is [(file name, bytes)] for word audio and
    dictionary images, to write to the media folder. None if Yomitan has no such entry."""
    markers = {**CARD_MARKERS, "definition": definition_marker}
    result = _post("ankiFields", timeout=15, text=word, type="term", maxEntries=entry + 1,
                   includeMedia=with_media, markers=list(dict.fromkeys(markers.values())))
    with _parsing("card fields"):
        return _card_fields(result, markers, entry)


def _card_fields(result, markers, index=0):
    entries = result.get("fields") or []
    if len(entries) <= index:
        return None
    entry = entries[index]
    fields = {key: str(entry.get(marker) or "") for key, marker in markers.items()}
    if fields["frequency"] == NO_FREQUENCY:
        fields["frequency"] = ""
    media = []
    for item in (result.get("audioMedia") or []) + (result.get("dictionaryMedia") or []):
        name, content = item.get("ankiFilename"), item.get("content")
        if name and content:
            try:
                media.append((name, base64.b64decode(content)))
            except ValueError:
                pass
    return fields, media


def frequency_rank(word):
    """Rank of `word` in Yomitan's frequency dictionaries (their harmonic mean; 1 = the most
    common word), or None if none of them has it."""
    result = _post("ankiFields", text=word, type="term", maxEntries=1, includeMedia=False,
                   markers=[CARD_MARKERS["frequency"]])
    with _parsing("frequency"):
        entries = result.get("fields") or []
        rank = str(entries[0].get(CARD_MARKERS["frequency"]) or "") if entries else ""
        return int(rank) if rank.isdigit() and rank != NO_FREQUENCY else None


def word_audio(word):
    """(file name, bytes) of the first audio Yomitan's audio sources have for `word`, or
    None."""
    result = _post("ankiFields", timeout=15, text=word, type="term", maxEntries=1,
                   includeMedia=True, markers=["audio"])
    with _parsing("word audio"):
        for item in result.get("audioMedia") or []:
            name, content = item.get("ankiFilename"), item.get("content")
            if name and content:
                try:
                    return name, base64.b64decode(content)
                except ValueError:
                    pass
    return None
