"""Ankime's own dictionaries: Yomitan dictionaries (the .zip files Yomitan imports: JMdict,
Jitendex, frequency and pitch-accent lists) imported into a database in user_files/, so words
can be looked up without a browser. answer() takes the calls yomitan.py makes of yomitan-api
(tokenize, ankiFields, yomitanVersion) and answers them in the same shape, so the rest of
Ankime doesn't know which one answered.

Lines are split like Yomitan's scanning parser does: at each point, the longest text that is
a dictionary word or a conjugation of one (deinflect.py). Dictionary content is third-party:
it is stored as it comes, and what is shown or put on cards goes through sanitize.clean_html
like Yomitan's answers do. Everything here may run in background threads; each thread has
its own connection to the database."""
import html
import json
import logging
import re
import sqlite3
import threading
import zipfile
import zlib
from collections import OrderedDict

from ..core.store import USER_FILES
from . import deinflect
from . import pitch as _pitch
from .pitch import morae

DB_PATH = USER_FILES / "dictionaries.sqlite"
SCHEMA_VERSION = 1
# A dictionary is a zip of JSON files of some MB each (JMdict: 55 files, 170 MB in all).
# Past these sizes it is something else, or meant to exhaust memory or the disk.
MAX_INDEX = 1024 * 1024           # bytes, index.json
MAX_BANK = 64 * 1024 * 1024       # bytes, one term_bank_*.json / term_meta_bank_*.json
MAX_TOTAL = 2 * 1024 ** 3         # bytes, all of them unpacked
FORMAT = 3                        # Yomitan's dictionary format since 2019
MAX_GROUPS = 5                    # headword groups (other words read the same) per token
NO_FREQUENCY = "9999999"          # what Yomitan gives for frequency-harmonic-rank without one
LOOKUP_CACHE = 20000              # texts whose lookup is remembered (Yomitan does the same)

_BANK = re.compile(r"(term|term_meta)_bank_(\d+)\.json")

log = logging.getLogger(__name__)


class DictionaryError(Exception):
    """A dictionary can't be imported or read: the message says why."""


class NoDictionary(DictionaryError):
    """No dictionary with words has been imported."""


class Cancelled(Exception):
    pass


NO_DICTIONARY = ("No dictionary imported yet: Tools > Ankime > Dictionaries... downloads "
                 "JMdict in one click, or imports any Yomitan dictionary.")


# --- the database --------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS dictionaries (id INTEGER PRIMARY KEY, title TEXT UNIQUE NOT NULL,
    revision TEXT, attribution TEXT, terms INTEGER, meta INTEGER);
CREATE TABLE IF NOT EXISTS terms (dict INTEGER NOT NULL, expression TEXT NOT NULL,
    reading TEXT NOT NULL, def_tags TEXT, rules TEXT, score INTEGER, glossary BLOB,
    sequence INTEGER, term_tags TEXT);
CREATE INDEX IF NOT EXISTS terms_expression ON terms(expression);
CREATE INDEX IF NOT EXISTS terms_reading ON terms(reading);
CREATE INDEX IF NOT EXISTS terms_sequence ON terms(dict, sequence);
CREATE TABLE IF NOT EXISTS meta (dict INTEGER NOT NULL, term TEXT NOT NULL, mode TEXT,
    data TEXT);
CREATE INDEX IF NOT EXISTS meta_term ON meta(term);
"""

_local = threading.local()
_lock = threading.Lock()
_generation = 0   # bumped by every import or removal: the index and caches are rebuilt
_index = None     # _Index of _generation


def _connect():
    """This thread's connection to the database (made, with the tables, if need be)."""
    cons = getattr(_local, "cons", None)
    if cons is None:
        cons = _local.cons = {}
    con = cons.get(str(DB_PATH))
    if con is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(DB_PATH), timeout=30)
        con.execute("PRAGMA journal_mode=WAL")  # lookups go on during an import
        if con.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            con.executescript(_SCHEMA)
            con.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            con.commit()
        cons[str(DB_PATH)] = con
    return con


def _changed():
    global _generation, _index
    with _lock:
        _generation += 1
        _index = None
    _find_cached.clear()
    _headwords_cached.clear()


def dictionaries():
    """[dict] of the dictionaries imported, in the order they are used: "title",
    "revision", "terms", "meta" (frequency and pitch entries), "attribution"."""
    rows = _connect().execute("SELECT title, revision, terms, meta, attribution FROM "
                              "dictionaries ORDER BY id").fetchall()
    return [dict(zip(("title", "revision", "terms", "meta", "attribution"), row))
            for row in rows]


def remove(title):
    con = _connect()
    with con:
        row = con.execute("SELECT id FROM dictionaries WHERE title = ?", (title,)).fetchone()
        if row:
            _delete(con, row[0])
    con.execute("VACUUM")  # give the disk space back: a dictionary is 100 MB or more
    _changed()


def _delete(con, dict_id):
    con.execute("DELETE FROM terms WHERE dict = ?", (dict_id,))
    con.execute("DELETE FROM meta WHERE dict = ?", (dict_id,))
    con.execute("DELETE FROM dictionaries WHERE id = ?", (dict_id,))


# --- importing -------------------------------------------------------------------

def _read(z, info, limit):
    if info.file_size > limit:
        raise DictionaryError(f"{info.filename} is larger than {limit // 2**20} MB")
    with z.open(info) as f:
        data = f.read(limit + 1)
    if len(data) > limit:
        raise DictionaryError(f"{info.filename} is larger than {limit // 2**20} MB")
    try:
        return json.loads(data.decode("utf-8-sig"))
    except ValueError as e:
        raise DictionaryError(f"{info.filename} isn't valid JSON: {e}") from e


def _text(value):
    return value if isinstance(value, str) else ""


def _term_row(dict_id, row):
    """A term_bank row as stored, or None if it isn't one: [expression, reading,
    definition tags, rules, score, glossary, sequence, term tags]."""
    if not isinstance(row, list) or len(row) < 8 or not isinstance(row[0], str) or not row[0]:
        return None
    if not isinstance(row[5], list):
        return None
    score = row[4] if isinstance(row[4], (int, float)) else 0
    sequence = row[6] if isinstance(row[6], int) else 0
    glossary = zlib.compress(json.dumps(row[5], ensure_ascii=False,
                                        separators=(",", ":")).encode("utf-8"))
    return (dict_id, row[0], _text(row[1]) or row[0], _text(row[2]), _text(row[3]),
            int(score), glossary, sequence, _text(row[7]))


def _meta_row(dict_id, row):
    """A term_meta_bank row as stored, or None: [term, "freq" | "pitch", data]."""
    if (not isinstance(row, list) or len(row) < 3 or not isinstance(row[0], str)
            or row[1] not in ("freq", "pitch")):
        return None
    return dict_id, row[0], row[1], json.dumps(row[2], ensure_ascii=False)


def _bank_order(name):
    m = _BANK.fullmatch(name)
    return (m.group(1), int(m.group(2))) if m else None


def import_zip(path, progress=None, cancelled=None):
    """Import the Yomitan dictionary at `path`, replacing one of the same title. Returns
    its title. `progress(done, total)` is told how many of its files are read;
    `cancelled()` returning True stops it (raising Cancelled), leaving nothing behind."""
    try:
        z = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as e:
        raise DictionaryError(f"Not a zip file that can be read: {e}") from e
    with z:
        infos = {info.filename: info for info in z.infolist() if not info.is_dir()}
        if "index.json" not in infos:
            raise DictionaryError("This isn't a Yomitan dictionary: it has no index.json.")
        index = _read(z, infos["index.json"], MAX_INDEX)
        if not isinstance(index, dict) or not _text(index.get("title")):
            raise DictionaryError("Its index.json has no title.")
        fmt = index.get("format", index.get("version"))
        if fmt != FORMAT:
            raise DictionaryError(f"It is in Yomitan's dictionary format {fmt}; Ankime reads "
                                  f"format {FORMAT}, what Yomitan dictionaries have been in "
                                  "since 2019. Look for a newer version of it.")
        banks = sorted((name for name in infos if _bank_order(name)), key=_bank_order)
        if not banks:
            raise DictionaryError("It has no words, frequencies or pitch accents (it may be a "
                                  "kanji dictionary, which Ankime doesn't use).")
        if sum(infos[name].file_size for name in banks) > MAX_TOTAL:
            raise DictionaryError(f"It would unpack to more than {MAX_TOTAL // 2**30} GB.")
        title = index["title"]
        con = _connect()
        counts = {"term": 0, "term_meta": 0}
        try:
            with con:  # one transaction: all of it, or (an error, Cancel) nothing
                old = con.execute("SELECT id FROM dictionaries WHERE title = ?",
                                  (title,)).fetchone()
                if old:
                    _delete(con, old[0])
                dict_id = con.execute(
                    "INSERT INTO dictionaries (title, revision, attribution) VALUES (?, ?, ?)",
                    (title, _text(index.get("revision")),
                     _text(index.get("attribution"))[:2000])).lastrowid
                for done, name in enumerate(banks):
                    if progress:
                        progress(done, len(banks))
                    if cancelled and cancelled():
                        raise Cancelled()
                    kind = _bank_order(name)[0]
                    rows = _read(z, infos[name], MAX_BANK)
                    if not isinstance(rows, list):
                        continue
                    if kind == "term":
                        found = [r for r in (_term_row(dict_id, row) for row in rows) if r]
                        con.executemany("INSERT INTO terms VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                        found)
                    else:
                        found = [r for r in (_meta_row(dict_id, row) for row in rows) if r]
                        con.executemany("INSERT INTO meta VALUES (?, ?, ?, ?)", found)
                    counts[kind] += len(found)
                if not counts["term"] and not counts["term_meta"]:
                    raise DictionaryError("None of its entries could be read.")
                con.execute("UPDATE dictionaries SET terms = ?, meta = ? WHERE id = ?",
                            (counts["term"], counts["term_meta"], dict_id))
        finally:
            _changed()
    if progress:
        progress(len(banks), len(banks))
    log.info("imported %s: %s terms, %s meta", title, counts["term"], counts["term_meta"])
    return title


# --- looking words up ------------------------------------------------------------

class _Index:
    """What is needed to scan a line fast: every expression and reading of the
    dictionaries (so a text that is no word is known not to be without the database),
    and the dictionaries' order."""

    def __init__(self, con):
        self.order = {}        # dictionary id -> position (the first imported first)
        self.titles = {}
        for position, (dict_id, title) in enumerate(con.execute(
                "SELECT id, title FROM dictionaries ORDER BY id")):
            self.order[dict_id] = position
            self.titles[dict_id] = title
        self.forms = set()
        for (form,) in con.execute("SELECT expression FROM terms UNION SELECT reading FROM terms"):
            self.forms.add(form)


def _get_index():
    global _index
    with _lock:
        if _index is None:
            _index = _Index(_connect())
        return _index


_HIRAGANA = re.compile("[ぁ-ゖ]")
_KATAKANA_SHIFT = {code: code - 0x60 for code in range(0x30A1, 0x30F7)}


def kata_to_hira(text):
    return text.translate(_KATAKANA_SHIFT)


def _is_kana(char):
    return "぀" <= char <= "ヿ"


def _is_japanese(char):
    return ("぀" <= char <= "ヿ" or "㐀" <= char <= "鿿"
            or "ｦ" <= char <= "ﾟ" or char in "々〆〇ヶ" or ord(char) >= 0x20000)


_TERM_COLUMNS = "rowid, dict, expression, reading, def_tags, rules, score, sequence"


class _Cache:
    """find()'s results by text, the most recent LOOKUP_CACHE, for the index's generation."""

    def __init__(self):
        self._lock = threading.Lock()
        self._items = OrderedDict()

    def get(self, key):
        with self._lock:
            if key in self._items:
                self._items.move_to_end(key)
                return self._items[key]
        return None

    def put(self, key, value):
        with self._lock:
            self._items[key] = value
            if len(self._items) > LOOKUP_CACHE:
                self._items.popitem(last=False)

    def clear(self):
        with self._lock:
            self._items.clear()


_find_cached = _Cache()
_headwords_cached = _Cache()


def find(text):
    """(length, matches) for the longest start of `text` that is a word or a conjugation
    of one: matches are (row, steps) with row = (rowid, dict, expression, reading,
    def_tags, rules, score, sequence), best first. (0, []) if none is."""
    index = _get_index()
    key = (_generation, text)
    cached = _find_cached.get(key)
    if cached is not None:
        return cached
    result = _find(text, index)
    _find_cached.put(key, result)
    return result


def _find(text, index):
    con = _connect()
    for end in range(len(text), 0, -1):
        source = text[:end]
        candidates = {}  # form -> [(class, steps)]
        for variant in dict.fromkeys((source, kata_to_hira(source))):
            forms = (deinflect.deinflect(variant) if _HIRAGANA.match(variant[-1])
                     else [(variant, None, 0)])
            for form, cls, steps in forms:
                if form in index.forms:
                    candidates.setdefault(form, []).append((cls, steps))
        if not candidates:
            continue
        marks = ",".join("?" * len(candidates))
        rows = con.execute(f"SELECT {_TERM_COLUMNS} FROM terms WHERE expression IN ({marks}) "
                           f"OR reading IN ({marks})", [*candidates, *candidates]).fetchall()
        matches = []  # (row, steps, written as the text: like Yomitan, those come first)
        for row in rows:
            classes = deinflect.word_classes(row[5])
            found = [(s, form == row[2]) for form in {row[2], row[3]}
                     for cls, s in candidates.get(form, ()) if cls is None or cls in classes]
            if found:
                steps = min(s for s, _ in found)
                matches.append((row, steps, any(e for s, e in found if s == steps)))
        if matches:
            matches.sort(key=lambda m: (m[1], not m[2], -m[0][6],
                                        index.order.get(m[0][1], 99), m[0][0]))
            return end, [(row, steps) for row, steps, _exact in matches]
    return 0, []


def _entries(matches, index, limit):
    """The words of `matches`, best first, one per dictionary entry: [(expression,
    reading, dict id, sequence)]. Other spellings of an entry already listed (奇麗 after
    綺麗, in a dictionary that numbers its entries) are left out."""
    out, seen_terms, seen_entries = [], set(), set()
    for row, _steps in matches:
        _rowid, dict_id, expression, reading, _tags, _rules, _score, sequence = row
        entry = (dict_id, sequence) if sequence and sequence > 0 else None
        if (expression, reading) in seen_terms or entry in seen_entries:
            continue
        seen_terms.add((expression, reading))
        if entry:
            seen_entries.add(entry)
        out.append((expression, reading, dict_id, sequence))
        if len(out) >= limit:
            break
    return out


def _headwords(entry):
    """The headword group of an entry: its spellings and readings, its own first."""
    key = (_generation, entry)
    group = _headwords_cached.get(key)
    if group is None:
        group = _headword_group(entry)
        _headwords_cached.put(key, group)
    return [dict(h) for h in group]


def _headword_group(entry):
    expression, reading, dict_id, sequence = entry
    group = [(expression, reading)]
    if sequence and sequence > 0:
        rows = _connect().execute(
            "SELECT expression, reading FROM terms WHERE dict = ? AND sequence = ? "
            "GROUP BY expression, reading ORDER BY MAX(score) DESC, MIN(rowid)",
            (dict_id, sequence)).fetchall()
        group += [tuple(r) for r in rows if tuple(r) != (expression, reading)]
    return [{"term": term, "reading": rd} for term, rd in group]


# --- furigana (after Yomitan's distributeFurigana) --------------------------------

def _segmentize(reading, normalized, groups, start):
    if start >= len(groups):
        return [] if not reading else None
    is_kana, text, text_normalized = groups[start]
    if is_kana:
        if normalized.startswith(text_normalized):
            rest = _segmentize(reading[len(text):], normalized[len(text):], groups, start + 1)
            if rest is not None:
                return [(text, "")] + rest
        return None
    result = None
    for i in range(len(reading), len(text) - 1, -1):
        rest = _segmentize(reading[i:], normalized[i:], groups, start + 1)
        if rest is not None:
            if result is not None:
                return None  # more than one way to split it: ambiguous
            result = [(text, reading[:i])] + rest
        if start == len(groups) - 1:
            break
    return result


def distribute_furigana(term, reading):
    """[(text, reading)] of `term`'s kanji and kana: 食べる, たべる -> [(食, た), (べる, "")]."""
    if not reading or reading == term:
        return [(term, "")]
    groups = []
    for char in term:
        kana = _is_kana(char)
        if groups and groups[-1][0] == kana:
            groups[-1][1] += char
        else:
            groups.append([kana, char])
    groups = [(kana, text, kata_to_hira(text) if kana else None) for kana, text in groups]
    segments = _segmentize(reading, kata_to_hira(reading), groups, 0)
    return segments if segments is not None else [(term, reading)]


def _stem_length(a, b):
    n = 0
    while n < min(len(a), len(b)) and a[n] == b[n]:
        n += 1
    return n


def distribute_inflected(term, reading, source):
    """[(text, reading)] of `source`, a conjugation of `term` (or `term` as written): the
    stem it shares with the term gets the term's furigana, the conjugated ending none."""
    stem = _stem_length(kata_to_hira(term), kata_to_hira(source))
    main = term
    reading_stem = _stem_length(kata_to_hira(reading), kata_to_hira(source))
    if reading_stem > 0 and reading_stem >= stem:  # written in kana: きれい for 綺麗
        main, stem = reading, reading_stem
        reading = source[:stem] + reading[stem:]
    segments = []
    if stem > 0:
        main = source[:stem] + main[stem:]
        consumed = 0
        for text, rd in distribute_furigana(main, reading):
            start, consumed = consumed, consumed + len(text)
            if consumed <= stem:
                segments.append((text, rd))
                if consumed == stem:
                    break
            else:
                if start < stem:
                    segments.append((main[start:stem], ""))
                break
    if stem < len(source):
        rest = source[stem:]
        if segments and not segments[-1][1]:
            segments[-1] = (segments[-1][0] + rest, "")
        else:
            segments.append((rest, ""))
    return segments


# --- answering yomitan-api's calls -------------------------------------------------

def _require_words(index):
    if not index.forms:
        raise NoDictionary(NO_DICTIONARY)


def tokenize_line(line, scan_length=20):
    """The words of `line` as yomitan-api's tokenize gives them: each a list of furigana
    segments {"text", "reading"}, the first with the word's "headwords" (a group per
    dictionary entry, best first). Text that is no word is kept together, without any."""
    index = _get_index()
    words, loose, i = [], None, 0
    while i < len(line):
        char = line[i]
        length, matches = find(line[i:i + max(1, scan_length)])
        if matches and (length > 1 or _is_japanese(char)):
            source = line[i:i + length]
            entries = _entries(matches, index, MAX_GROUPS)
            expression, reading = entries[0][:2]
            segments = [{"text": text, "reading": rd}
                        for text, rd in distribute_inflected(expression, reading, source)]
            segments[0]["headwords"] = [_headwords(entry) for entry in entries]
            words.append(segments)
            loose = None
            i += length
        else:
            if loose is None:
                loose = {"text": char, "reading": ""}
                words.append([loose])
            else:
                loose["text"] += char
            i += 1
    return words


def _definitions(expression, reading, index):
    """The definition rows of a word, from every dictionary, in order: (dict id, def_tags,
    rules, glossary items, term_tags)."""
    rows = _connect().execute(
        "SELECT dict, def_tags, rules, glossary, term_tags FROM terms WHERE expression = ? "
        "AND reading = ?", (expression, reading)).fetchall()
    rows.sort(key=lambda r: index.order.get(r[0], 99))
    out = []
    for dict_id, def_tags, rules, glossary, term_tags in rows:
        try:
            items = json.loads(zlib.decompress(glossary).decode("utf-8"))
        except (zlib.error, ValueError):
            continue
        out.append((dict_id, def_tags or "", rules or "", items, term_tags or ""))
    return out


# Structured content: the tags Yomitan dictionaries use, and their style keys (camelCase)
_SC_TAGS = {"br", "ruby", "rt", "rp", "table", "thead", "tbody", "tfoot", "tr", "td", "th",
            "span", "div", "ol", "ul", "li", "details", "summary"}
_SC_STYLES = {"fontStyle", "fontWeight", "fontSize", "color", "background", "backgroundColor",
              "textDecorationLine", "textDecorationStyle", "textDecorationColor", "borderColor",
              "borderStyle", "borderRadius", "borderWidth", "verticalAlign", "textAlign",
              "textEmphasis", "textShadow", "margin", "marginTop", "marginLeft", "marginRight",
              "marginBottom", "padding", "paddingTop", "paddingLeft", "paddingRight",
              "paddingBottom", "wordBreak", "whiteSpace", "listStyleType"}
_EM_STYLES = {"marginTop", "marginLeft", "marginRight", "marginBottom"}  # numbers are em


def _style(style):
    if not isinstance(style, dict):
        return ""
    decls = []
    for key, value in style.items():
        if key not in _SC_STYLES or not isinstance(value, (str, int, float)):
            continue
        if isinstance(value, (int, float)):
            value = f"{value}em" if key in _EM_STYLES else str(value)
        prop = re.sub(r"[A-Z]", lambda m: "-" + m.group().lower(), key)
        decls.append(f"{prop}: {value}")
    return html.escape("; ".join(decls), quote=True)


def _content_html(node, depth=0):
    """HTML of structured content (strings, lists, {"tag", "content", "style"...})."""
    if depth > 50:
        return ""
    if isinstance(node, str):
        return html.escape(node).replace("\n", "<br>")
    if isinstance(node, list):
        return "".join(_content_html(child, depth + 1) for child in node)
    if not isinstance(node, dict):
        return ""
    tag = node.get("tag")
    inner = _content_html(node.get("content", ""), depth + 1)
    if tag == "br":
        return "<br>"
    if tag == "a":  # a link to another entry: no navigating in the popup or on cards
        return f"<span style=\"text-decoration: underline\">{inner}</span>"
    if tag not in _SC_TAGS:  # img (pictures aren't imported), anything unknown
        return inner if tag != "img" else ""
    attrs = ""
    if style := _style(node.get("style")):
        attrs += f' style="{style}"'
    for key in ("title", "lang"):
        if isinstance(node.get(key), str):
            attrs += f' {key}="{html.escape(node[key], quote=True)}"'
    for key, name in (("colSpan", "colspan"), ("rowSpan", "rowspan")):
        if isinstance(node.get(key), int):
            attrs += f' {name}="{node[key]}"'
    if tag == "details" and node.get("open"):
        attrs += " open"
    return f"<{tag}{attrs}>{inner}</{tag}>"


def _item_html(item):
    """HTML of one glossary item (a definition): a string, {"type": "text"}, structured
    content. Pictures and deinflection notes give ""."""
    if isinstance(item, str):
        return html.escape(item)
    if isinstance(item, dict):
        if item.get("type") == "text":
            return html.escape(_text(item.get("text")))
        if item.get("type") == "structured-content":
            return _content_html(item.get("content"))
    return ""


def glossary_html(definitions, index, full=False):
    """Yomitan's glossary-brief (`full` False: the definitions) or glossary (with each
    one's tags and dictionary) marker."""
    senses = []
    for dict_id, def_tags, _rules, items, _term_tags in definitions:
        tags = def_tags.split()
        if tags[:1] == ["forms"] and not full:
            continue  # the entry's spellings: not a definition
        parts = [p for p in (_item_html(i) for i in items) if p]
        if not parts:
            continue
        body = parts[0] if len(parts) == 1 else "<ul>" + "".join(
            f"<li>{p}</li>" for p in parts) + "</ul>"
        if full:
            label = [t for t in tags if not t.isdigit()] + [index.titles.get(dict_id, "")]
            body = f"<i>({html.escape(', '.join(t for t in label if t))})</i> {body}"
        senses.append(body)
    if not senses:
        return ""
    inner = senses[0] if len(senses) == 1 else "<ol>" + "".join(
        f"<li>{s}</li>" for s in senses) + "</ol>"
    return f'<div style="text-align: left;" class="yomitan-glossary">{inner}</div>'


def _meta(expression, reading, mode):
    """[(dict id, data)] of a word's frequency or pitch entries, for this reading."""
    out = []
    for dict_id, data in _connect().execute(
            "SELECT dict, data FROM meta WHERE term = ? AND mode = ?", (expression, mode)):
        try:
            data = json.loads(data)
        except ValueError:
            continue
        if isinstance(data, dict) and "reading" in data:
            if data.get("reading") != reading:
                continue
            data = data.get("frequency" if mode == "freq" else "pitches", data)
        out.append((dict_id, data))
    return out


def _frequency_value(data):
    """(number, display text) of a frequency entry: 1234, "1234", {"value", "displayValue"}."""
    if isinstance(data, dict):
        value, shown = data.get("value"), data.get("displayValue")
    else:
        value, shown = data, None
    if isinstance(value, str):
        value = int(value) if value.isdigit() else None
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        value = None
    return value, _text(shown) or ("" if value is None else str(value))


def frequencies(expression, reading, index):
    """(frequencies marker HTML, frequency-harmonic-rank)."""
    shown, ranks = [], {}
    for dict_id, data in _meta(expression, reading, "freq"):
        value, text = _frequency_value(data)
        if text:
            shown.append(f"{html.escape(index.titles.get(dict_id, ''))}: {html.escape(text)}")
        if value and value > 0:
            ranks[dict_id] = min(value, ranks.get(dict_id, value))
    rank = (str(round(len(ranks) / sum(1 / v for v in ranks.values()))) if ranks
            else NO_FREQUENCY)
    return " · ".join(f"<span>{s}</span>" for s in shown), rank


def pitch_html(reading, position):
    """The reading with its high morae overlined and ꜜ where the pitch falls, then [n]."""
    parts = morae(reading)
    if not parts or not isinstance(position, int) or not 0 <= position <= len(parts):
        return ""
    out = []
    for i, mora in enumerate(parts, 1):
        high = (i == 1) if position == 1 else (1 < i and (position == 0 or i <= position))
        text = html.escape(mora)
        out.append(f'<span style="text-decoration: overline">{text}</span>' if high else text)
        if i == position:
            out.append("ꜜ")
    return f"{''.join(out)} [{position}]"


def pitch_accents(expression, reading):
    found = []
    for _dict_id, pitches in _meta(expression, reading, "pitch"):
        for pitch in pitches if isinstance(pitches, list) else ():
            if isinstance(pitch, dict) and (shown := pitch_html(reading, pitch.get("position"))):
                found.append(shown)
    return "・".join(dict.fromkeys(found))


def pitch_categories(expression, reading, definitions):
    """The pitch-accent-categories marker: "heiban", "odaka,heiban"... as Yomitan gives it;
    `definitions` (see _definitions) tell verbs and adjectives (kifuku) apart."""
    verb = any(_pitch.is_verb_or_adjective(rules) for _d, _t, rules, _i, _tt in definitions)
    found = []
    for _dict_id, pitches in _meta(expression, reading, "pitch"):
        for p in pitches if isinstance(pitches, list) else ():
            if isinstance(p, dict) and (name := _pitch.category(reading, p.get("position"),
                                                                verb)):
                found.append(name)
    return ",".join(dict.fromkeys(found))


def anki_fields(text, markers, max_entries=1):
    """yomitan-api's ankiFields answer for `text`: up to `max_entries` dictionary entries,
    each with `markers` (expression, reading, glossary-brief, glossary, frequencies,
    frequency-harmonic-rank, pitch-accents, pitch-accent-categories; audio is always empty:
    there are no audio sources without Yomitan)."""
    index = _get_index()
    _require_words(index)
    _length, matches = find(text)
    fields = []
    for expression, reading, _dict_id, _sequence in _entries(matches, index, max_entries):
        definitions = _definitions(expression, reading, index)
        freq_html, rank = frequencies(expression, reading, index)
        values = {"expression": expression, "reading": reading,
                  "glossary-brief": glossary_html(definitions, index),
                  "glossary": glossary_html(definitions, index, full=True),
                  "frequencies": freq_html, "frequency-harmonic-rank": rank,
                  "pitch-accents": pitch_accents(expression, reading),
                  "pitch-accent-categories": pitch_categories(expression, reading,
                                                              definitions),
                  "audio": ""}
        fields.append({marker: values.get(marker, "") for marker in markers})
    return {"fields": fields, "audioMedia": [], "dictionaryMedia": []}


def answer(path, body):
    """Answer yomitan-api's call `path` with `body`, as it would."""
    if path == "yomitanVersion":
        index = _get_index()
        _require_words(index)
        return {"version": f"Ankime's dictionaries ({', '.join(index.titles.values())})"}
    if path == "tokenize":
        index = _get_index()
        _require_words(index)
        texts = body.get("text", "")
        texts = [texts] if isinstance(texts, str) else list(texts)
        scan = int(body.get("scanLength") or 20)
        return [{"index": i, "content": tokenize_line(t, scan)} for i, t in enumerate(texts)]
    if path == "ankiFields":
        return anki_fields(_text(body.get("text")), list(body.get("markers") or ()),
                           int(body.get("maxEntries") or 1))
    raise DictionaryError(f"Ankime's dictionaries can't answer {path}")
