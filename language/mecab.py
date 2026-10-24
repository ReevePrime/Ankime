"""MeCab, to split lines into words (the parser setting): a grammatical analysis, where
Yomitan's scanning parser takes the longest dictionary word at each point, so that
と|いけません comes out as とい|け|ません. MeCab isn't bundled: Ankime runs the one another
add-on brings (ankimorphs-japanese-mecab, Japanese Support...) or the one on the PATH, with
its IPAdic dictionary. Yomitan still gives each word its headwords and furigana (see
tokenize_lines), as it does for its own split; MeCab's dictionary form picks among them."""
import codecs
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import NamedTuple, Optional

from ..core.model import Token
from . import yomitan

log = logging.getLogger(__name__)

ADDONS = Path(__file__).resolve().parent.parent.parent  # Anki's addons21 folder
# add-ons known to bring MeCab with IPAdic, tried first: ankimorphs-japanese-mecab (AnkiWeb
# and source installs) and Japanese Support (in its support/ folder)
PREFERRED = ("1974309724", "ankimorphs_japanese_mecab", "3918629684")
if sys.platform.startswith("win"):
    EXECUTABLE = "mecab.exe"
elif sys.platform == "darwin":
    EXECUTABLE = "mecab"
else:
    EXECUTABLE = "mecab.lin"
TIMEOUT = 30  # seconds for one call; MeCab takes milliseconds for a hundred lines

# one line per morpheme: surface, dictionary form, reading, part of speech and its subtype
# (IPAdic's features 6, 7, 0 and 1); MeCab turns the \t and \n into tabs and line breaks
_NODE = r"--node-format=%m\t%f[6]\t%f[7]\t%f[0]\t%f[1]\n"
_UNKNOWN = r"--unk-format=%m\t*\t*\t%f[0]\t%f[1]\n"  # words not in its dictionary
_EOS = r"--eos-format=EOS\n"
_LINE_SEPARATORS = re.compile("[\x00-\x1f\x7f  ]")  # MeCab reads a line each
_KANJI = re.compile("[々〆㐀-䶿一-鿿\U00020000-\U0003134F]")


class MecabError(yomitan.YomitanError):
    """MeCab failed: a YomitanError, so that it is shown wherever a failed split is."""


class Morpheme(NamedTuple):
    surface: str
    lemma: str     # dictionary form ("*" for a word MeCab doesn't know)
    reading: str   # in katakana, or "*"
    pos: str       # part of speech: 動詞, 助動詞, 助詞...
    sub: str       # its subtype: 自立, 非自立, 接続助詞...


class Mecab(NamedTuple):
    command: list       # the executable and its dictionary arguments
    folder: Optional[Path]  # the add-on folder it is in, None for the PATH's
    charset: str


def _run(command, folder, args, data, timeout=TIMEOUT, check=True):
    """MeCab's output for `data` (bytes). Raises OSError or subprocess.SubprocessError (if
    it fails, unless not `check`)."""
    env = startupinfo = None
    if sys.platform.startswith("win"):
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW  # no console window
    elif folder is not None:  # its libmecab is next to it
        env = {**os.environ, "LD_LIBRARY_PATH": str(folder), "DYLD_LIBRARY_PATH": str(folder)}
    return subprocess.run([*command, *args], input=data, capture_output=True,
                          timeout=timeout, env=env, startupinfo=startupinfo,
                          check=check).stdout


def _candidates(addons=None):
    """(command, folder) for each MeCab to try: in add-on folders (with its dictionary next
    to it), the known ones first, then the PATH's."""
    addons = addons or ADDONS
    folders = [addons / name for name in PREFERRED]
    try:
        folders += sorted(p for p in addons.iterdir() if p.is_dir())
    except OSError:
        pass
    seen = set()
    for folder in (f for top in folders for f in (top, top / "support")):
        if folder in seen:
            continue
        seen.add(folder)
        exe = folder / EXECUTABLE
        if exe.is_file() and (folder / "sys.dic").is_file() and (folder / "dicrc").is_file():
            rc = folder / "mecabrc"
            yield [str(exe), "-d", str(folder), *(["-r", str(rc)] if rc.is_file() else [])], \
                folder
    if path := shutil.which("mecab"):
        yield [path], None


def _check(command, folder):
    """A Mecab if this one runs and has an IPAdic dictionary (whose features tokenize_lines
    reads: UniDic's are others), else None."""
    if folder is not None and not sys.platform.startswith("win"):
        exe = Path(command[0])
        if not os.access(exe, os.X_OK):
            try:  # unzipped from an .ankiaddon, it can lose its executable bit
                exe.chmod(0o755)
            except OSError:
                return None
    try:
        # -D (the dictionary's details) exits with 1 even when it prints them
        info = _run(command, folder, ["-D"], b"", timeout=10, check=False).decode(
            "utf-8", "replace")
        charset = re.search(r"^charset:\s*(\S+)", info, re.M).group(1)
        codecs.lookup(charset)
        out = _run(command, folder, [r"--node-format=%H\n", _EOS], "猫\n".encode(charset),
                   timeout=10).decode(charset)
    except (OSError, subprocess.SubprocessError, AttributeError, LookupError,
            UnicodeError) as e:
        log.info("MeCab %s doesn't run: %s", command[0], e)
        return None
    features = out.splitlines()[0].split(",") if out.strip() else []
    if len(features) != 9 or features[0] != "名詞":
        log.info("MeCab %s: not an IPAdic dictionary (%s)", command[0], out[:80])
        return None
    return Mecab(command, folder, charset)


_found = None  # (Mecab or None,), once looked for
_find_lock = threading.Lock()


def find():
    """The MeCab to use, or None if none is installed. Looked for once a session."""
    global _found
    with _find_lock:
        if _found is None:
            found = next((m for m in (_check(*c) for c in _candidates()) if m), None)
            if found:
                log.info("MeCab: %s", " ".join(found.command))
            _found = (found,)
        return _found[0]


def describe(found):
    """Where a MeCab is, for the setup check: its add-on's name, or its path."""
    if found.folder is None:
        return found.command[0]
    top = found.folder.parent if found.folder.name == "support" else found.folder
    try:
        name = json.loads((top / "meta.json").read_text(encoding="utf-8")).get("name")
    except (OSError, ValueError, AttributeError):
        name = None
    return f"the {name} add-on" if name else str(found.folder)


def parse(lines, found=None):
    """MeCab's morphemes of each line (none may hold a line break)."""
    found = found or find()
    if found is None:
        raise MecabError("MeCab isn't installed")
    data = "".join(_LINE_SEPARATORS.sub(" ", line) + "\n" for line in lines)
    try:
        out = _run(found.command, found.folder, [_NODE, _UNKNOWN, _EOS],
                   data.encode(found.charset, "replace")).decode(found.charset, "replace")
    except (OSError, subprocess.SubprocessError) as e:
        raise MecabError(f"MeCab failed: {e}") from e
    out_lines = [[]]
    for row in out.splitlines():
        if row == "EOS":
            out_lines.append([])
        elif len(fields := row.split("\t")) == 5:
            out_lines[-1].append(Morpheme(*fields))
    out_lines.pop()  # after the last EOS
    if len(out_lines) != len(lines):
        raise MecabError(f"MeCab answered {len(out_lines)} lines for {len(lines)}")
    return out_lines


# --- morphemes -> words ------------------------------------------------------------

_CONJUGATING = {"動詞", "形容詞", "助動詞"}
_GRAMMAR = {"助動詞", "助詞"}  # auxiliaries and particles
_ENDING_PARTICLES = {"て", "で", "ば", "たり", "だり"}
# contractions of て and a verb, which MeCab gives as verbs of their own: 思っ+てる,
# 放っ+といた, 食べ+ちゃう
_CONTRACTED = {"てる", "でる", "とく", "どく", "ちゃう", "じゃう", "ちまう", "じまう", "てく",
               "でく"}


def _joins(word, m):
    """Whether morpheme `m` goes on the end of `word` (its morphemes so far), as Yomitan
    reads conjugated words: a verb, an adjective or an auxiliary takes its auxiliaries
    (探さ+ない, いけ+ませ+ん, で+なけれ+ば), て / で / ば / たり, the verbs after て
    (食べ+て+しまっ+た) and their contractions (思っ+てる), and the passive and causative
    (食べ+られる). A particle such as と never does."""
    if word[0].pos not in _CONJUGATING:
        return False
    if m.pos == "助動詞":
        return True
    if m.pos == "助詞":
        return m.surface in _ENDING_PARTICLES and m.sub in ("接続助詞", "並立助詞")
    if m.pos == "動詞":
        return (m.sub == "接尾" or m.lemma in _CONTRACTED
                or (m.sub == "非自立" and word[-1].surface in ("て", "で")))
    return False


def words(text, morphemes):
    """[(surface, [morphemes])] covering `text` in order: the morphemes grouped into words,
    and what MeCab passed over (spaces) as words with no morphemes."""
    out, at = [], 0
    for m in morphemes:
        i = text.find(m.surface, at) if m.surface else -1
        if i < 0:
            continue
        if i > at:
            out.append((text[at:i], []))
        if out and out[-1][1] and i == at and _joins(out[-1][1], m):
            out[-1] = (out[-1][0] + m.surface, [*out[-1][1], m])
        else:
            out.append((m.surface, [m]))
        at = i + len(m.surface)
    if at < len(text):
        out.append((text[at:], []))
    return out


def combine(text, mecab_words, yomitan_words):
    """The words of `text`: Yomitan's (lists of segments, see yomitan.parse_words) where
    they start and end where MeCab's do, so that set phrases stay whole (あの子, しょうがない),
    and MeCab's where Yomitan's longest match runs into one of them (とい, in と|いけません).
    A Yomitan word over several of MeCab's that holds a particle is taken apart too: は|ね,
    not 羽. [(surface, morphemes, segments)]: the morphemes of MeCab's word (None for a
    Yomitan word over several), and Yomitan's segments for it (None if it isn't one of
    Yomitan's)."""
    spans, at = {}, 0  # start -> (end, segments) of Yomitan's words
    for segs in yomitan_words:
        end = at + len(yomitan.segments_text(segs))
        if end > at:
            spans[at] = (end, segs)
        at = end
    if at != len(text):
        spans = {}  # Yomitan's words aren't the whole text (shouldn't happen): MeCab's alone
    out, i, at = [], 0, 0
    while i < len(mecab_words):
        end, segs = spans.get(at, (None, None))
        j, reach = i, at
        while end is not None and reach < end and j < len(mecab_words):
            reach += len(mecab_words[j][0])
            j += 1
        covered = mecab_words[i:j]
        if end is not None and reach == end and (
                j - i == 1 or not any(m.pos == "助詞" for _, ms in covered for m in ms)):
            out.append((covered[0][0], covered[0][1], segs) if j - i == 1
                       else (text[at:end], None, segs))
            i, at = j, end
            continue
        surface, ms = mecab_words[i]
        out.append((surface, ms, None))
        i, at = i + 1, at + len(surface)
    return out


def _lemma(morphemes):
    first = morphemes[0]
    return first.lemma if first.lemma not in ("", "*") else first.surface


def _hiragana(katakana):
    return "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in katakana)


def _furigana(morphemes):
    """Furigana from MeCab's readings, for a word Yomitan can't read as a whole: each
    morpheme with kanji gets its reading (okurigana and all)."""
    pairs = tuple((m.surface, _hiragana(m.reading) if _KANJI.search(m.surface)
                   and m.reading not in ("", "*") else "") for m in morphemes)
    return pairs if any(r for _, r in pairs) else ""


def _is_word(morphemes):
    return bool(morphemes) and morphemes[0].pos not in ("記号", "補助記号", "空白")


def tokenize_lines(lines, found=None, line_words=None):
    """[Token] for each line, as yomitan._tokenize_lines gives them, from MeCab's split and
    Yomitan's (see combine). A word of MeCab's that Yomitan's split doesn't have is looked
    up on its own: Yomitan's furigana and headwords for it as written (探さない: 探す), or
    else for its dictionary form (食べてしまった, if it isn't one word to Yomitan: 食べる).
    Of Yomitan's headwords, those with MeCab's dictionary form are taken (see
    yomitan.headwords: しない is する); an auxiliary or a particle has MeCab's unless
    Yomitan has it spelled the same (なけれ is ない, not 泣ける; ませ is ます, not 増す). A word Yomitan has no headword for (a name)
    has none, as with Yomitan's own split: it isn't a word to learn."""
    line_words = line_words or yomitan.line_words

    def one_word(texts):  # Yomitan's segments for each text, if it is one word to Yomitan
        return {t: ws[0] if len(ws) == 1 else None
                for t, ws in zip(texts, line_words(texts) if texts else [])}

    def base_of(segs, lemma, grammar=False):
        if grammar:
            found = (yomitan.headwords(segs, lemma, fallback=False, by_reading=False)
                     if segs is not None else "")
            return found if found and found[0] else (lemma, ())
        return yomitan.headwords(segs, lemma) if segs is not None else ("", ())

    per_line = [combine(line, words(line, ms), ys) for line, ms, ys
                in zip(lines, parse(lines, found), line_words(lines))]
    alone = one_word(list(dict.fromkeys(
        surface for line in per_line for surface, ms, segs in line
        if segs is None and _is_word(ms))))
    per_line = [[(surface, ms, segs if segs is not None else alone.get(surface))
                 for surface, ms, segs in line] for line in per_line]
    by_lemma = one_word(list(dict.fromkeys(
        _lemma(ms) for line in per_line for surface, ms, segs in line
        if _is_word(ms) and not base_of(segs, _lemma(ms))[0] and _lemma(ms) != surface)))

    out = []
    for line in per_line:
        tokens = []
        for surface, ms, segs in line:
            if ms is None:  # a word of Yomitan's over several of MeCab's
                tokens.append(Token(surface, yomitan.furigana(segs), *yomitan.headwords(segs)))
                continue
            if not _is_word(ms):
                tokens.append(Token(surface, "", "", ()))
                continue
            lemma = _lemma(ms)
            base, alts = base_of(segs, lemma, ms[0].pos in _GRAMMAR)
            furigana = yomitan.furigana(segs) if segs is not None else _furigana(ms)
            if not base and by_lemma.get(lemma) is not None:
                base, alts = base_of(by_lemma[lemma], lemma)
                furigana = _furigana(ms) if base else furigana
            tokens.append(Token(surface, furigana, base, alts))
        out.append(tokens)
    return out
