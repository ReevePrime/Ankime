"""Word lists from and for other tools: what import_list reads (JPDB's review history,
Migaku's word list export, or a plain list) and the formats export_list writes. Pure: no
Anki, so it is tested on its own."""
import csv
import io
import json
from typing import NamedTuple

from . import knowledge

# JPDB: Settings > Export your review history, a vocabulary-reviews.json of
# {"cards_vocabulary_jp_en": [{"vid", "spelling", "reading", "reviews": [{"timestamp",
# "grade", "from_anki"}]}], "cards_vocabulary_en_jp": [...], "cards_kanji_...": [...]}.
# Kanji cards aren't words.
JPDB_DECKS = ("cards_vocabulary_jp_en", "cards_vocabulary_en_jp")
JPDB_KNOWN = {"known", "never-forget"}  # set aside as known, never reviewed again
JPDB_PASS = {"hard", "okay", "easy", "pass", *JPDB_KNOWN}
DAY = 86400

# Migaku's exports, as CSV or JSON: a word (its dictionary form, sometimes "食べる◴たべる",
# word and reading) and a status, KNOWN / LEARNING / UNKNOWN / IGNORED. The names vary
# between Migaku's versions and the exporters, so any of these is taken.
MIGAKU_WORD = ("dictform", "word", "term", "expression", "headword", "spelling")
MIGAKU_STATUS = ("knownstatus", "status", "state")
MIGAKU_SEPARATOR = "◴"


class WordList(NamedTuple):
    source: str      # what the file was, to say so: "JPDB review history"...
    words: list      # the known words in it, in order, without repeats
    skipped: int     # words in it that aren't known yet (still learning, unknown)


def _headword(text):
    return knowledge.clean_word(str(text).split(MIGAKU_SEPARATOR)[0])


def _unique(words):
    return list(dict.fromkeys(w for w in words if knowledge.is_word(w)))


def _jpdb_known(reviews, known_days):
    """A JPDB card is known if it was set aside as known, or its last review passed at least
    `known_days` after the one before: the interval it had then is past Ankime's known
    interval (the export has no intervals, only the reviews)."""
    if not reviews:
        return False
    last = reviews[-1]
    if last.get("grade") in JPDB_KNOWN:
        return True
    if last.get("grade") not in JPDB_PASS:
        return False
    if len(reviews) < 2:
        return known_days <= 0
    try:
        gap = float(last["timestamp"]) - float(reviews[-2]["timestamp"])
    except (KeyError, TypeError, ValueError):
        return False
    return gap >= known_days * DAY


def read_jpdb(data, known_days):
    known, seen = [], []
    for deck in JPDB_DECKS:
        for card in data.get(deck) or ():
            if not isinstance(card, dict) or not (word := _headword(card.get("spelling", ""))):
                continue
            seen.append(word)
            reviews = sorted((r for r in card.get("reviews") or () if isinstance(r, dict)),
                             key=lambda r: r.get("timestamp") or 0)
            if _jpdb_known(reviews, known_days):
                known.append(word)
    words = _unique(known)
    # a word in both decks counts once: known in either is enough
    return WordList("JPDB review history", words, len(set(_unique(seen)) - set(words)))


def _key(keys, names):
    """The first of `names` among `keys`, compared without case or "_", or None."""
    keys = {str(k).strip().lower().replace("_", ""): k for k in keys}
    return next((keys[n] for n in names if n in keys), None)


def _field(row, names):
    key = _key(row, names)
    return None if key is None else row[key]


def _read_migaku_rows(rows):
    """Only the KNOWN words, if there is a status; else every one (a list with a header)."""
    source = ("Migaku word list" if any(_key(r, MIGAKU_STATUS) is not None for r in rows)
              else "word list")
    known, skipped = [], []
    for row in rows:
        word = _headword(_field(row, MIGAKU_WORD) or "")
        status = str(_field(row, MIGAKU_STATUS) or "known").strip().lower()
        if not word:
            continue
        (known if status == "known" else skipped).append(word)
    words = _unique(known)
    return WordList(source, words, len(set(_unique(skipped)) - set(words)))


def _migaku_rows(data):
    """The word rows of a Migaku JSON export: a list of them, or a list under some key."""
    if isinstance(data, dict):
        data = next((v for v in data.values() if isinstance(v, list)), [])
    return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []


def _csv_rows(text):
    """The rows of a CSV / TSV with a header that names a word column, else None."""
    dialect = "excel-tab" if "\t" in text.split("\n", 1)[0] else "excel"
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    if not reader.fieldnames or _key(reader.fieldnames, MIGAKU_WORD) is None:
        return None
    return list(reader)


def import_list(text, known_days):
    """The known words of a file from another tool, as a WordList: JPDB's review history
    (vocabulary-reviews.json), a Migaku export (CSV or JSON, with a word and a status
    column: only the KNOWN ones), or else a plain word list (knowledge.words_in_list, every
    word of it). `known_days` is Ankime's known interval, for JPDB."""
    text = text.lstrip("\ufeff")
    stripped = text.strip()
    if stripped[:1] in "[{":
        try:
            data = json.loads(stripped)
        except ValueError:
            data = None
        if isinstance(data, dict) and any(deck in data for deck in JPDB_DECKS):
            return read_jpdb(data, known_days)
        if data is not None:
            return _read_migaku_rows(_migaku_rows(data))
    rows = _csv_rows(text)
    if rows is not None:
        return _read_migaku_rows(rows)
    words = [_headword(w) for w in knowledge.words_in_list(text)]
    return WordList("word list", _unique(words), 0)


# --- export -------------------------------------------------------------------------

class ExportFormat(NamedTuple):
    name: str        # in the save dialog's file types
    suffix: str
    header: bool     # Ankime's "#" comment line, saying what the list is


# JPDB (a deck's "Add vocabulary from text") and Migaku ("Import known words") read one
# word per line; a comment line would be read as text too, so they get none.
EXPORT_FORMATS = [
    ExportFormat("Word list, one per line", "txt", True),
    ExportFormat("JPDB, one word per line", "txt", False),
    ExportFormat("Migaku, one word per line", "txt", False),
]


def export_filter(fmt):
    return f"{fmt.name} (*.{fmt.suffix})"


def format_for_filter(selected):
    """The ExportFormat the save dialog's chosen file type stands for (the first if none)."""
    return next((f for f in EXPORT_FORMATS if export_filter(f) == selected), EXPORT_FORMATS[0])


def export_list(words, fmt, title):
    """`words` as a file in `fmt`: import_list reads every format back."""
    return knowledge.export_text(words, title) if fmt.header else "".join(
        f"{word}\n" for word in words)
