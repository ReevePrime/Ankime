"""Searching your library: every episode whose subtitles were analysed (their tokens are
cached on disk, see session.py), for the lines with a word, to find more example sentences
than one episode has. Pure logic, apart from reading the cache; search() runs in the
background."""
from pathlib import Path
from typing import NamedTuple, Optional

from ..language import knowledge
from . import session, subtitles

SEARCH_LIMIT = 300  # lines found at most

# cache file -> (its mtime, {line text: the normalized dictionary forms of its words}): the
# cache files are read once, not on every search
_words = {}


class Hit(NamedTuple):
    """A line of an episode with the word searched for."""
    video: str
    text: str
    tokens: list
    start: Optional[float]  # in the subtitle file's time; None if the file can't be read


def show_name(video):
    """The show an episode is of: its folder's name."""
    return Path(video).parent.name


def _line_words(cache_path):
    """{line text: dictionary forms} of a cache file, read again only once it changed."""
    try:
        mtime = cache_path.stat().st_mtime_ns
    except OSError:
        return {}
    known = _words.get(cache_path)
    if known and known[0] == mtime:
        return known[1]
    lines = {text: frozenset(knowledge.normalize(base) for _, _, base, _ in tokens if base)
             for text, tokens in session.load_cached_tokens(cache_path).items()}
    _words[cache_path] = (mtime, lines)
    return lines


def _starts(sub_path, texts):
    """{text: start of its first line} in a subtitle file, for the `texts` found in it."""
    try:
        events = subtitles.load_events(sub_path)
    except Exception:
        return {}  # moved, or an extracted track since swept from the scratch folder
    starts = {}
    for start, _, text in events:
        if text in texts:
            starts.setdefault(text, start)
    return starts


def search(query, limit=SEARCH_LIMIT):
    """(hits, more): the lines of every indexed episode with `query`, as written or as a
    word's dictionary form (as the transcript's search), the newest episodes first and a
    line once per episode; `more` if there were more than `limit`."""
    q = knowledge.normalize(query)
    hits = []
    if not q:
        return hits, False
    episodes = session.cached_episodes()
    for gone in _words.keys() - {cache_path for cache_path, _, _ in episodes}:
        del _words[gone]  # pruned from the cache
    for cache_path, video, sub in episodes:
        found = [text for text, words in _line_words(cache_path).items()
                 if q in text or q in words]
        if not found:
            continue
        tokens = session.load_cached_tokens(cache_path)
        starts = _starts(sub, set(found))
        for text in found:
            if len(hits) == limit:
                return hits, True
            hits.append(Hit(video, text, tokens.get(text, []), starts.get(text)))
    return hits, False


KIND_ORDER = {"i+1": 0, "known": 1, "harder": 2}


def rank(hits, know):
    """[(hit, its kind)], the best example sentences first: i+1 lines (the word is the only
    new one), then lines you fully know, then the others; shorter lines first among
    those. Main thread: it reads the known words."""
    kinds = [know.line_kind(hit.tokens) if hit.tokens else None for hit in hits]
    order = sorted(range(len(hits)),
                   key=lambda i: (KIND_ORDER.get(kinds[i], 3), len(hits[i].text)))
    return [(hits[i], kinds[i]) for i in order]
