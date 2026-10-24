"""Finding the source- and target-language subtitles that belong to a video."""
import re
from pathlib import Path

from ..core.model import Event

SUB_EXTS = (".srt", ".ass", ".ssa", ".vtt")

# Spellings of the same language in file names and track tags: the ISO 639-1 code first (what
# the settings store), then 639-2 and others, the English name last.
_LANGUAGES = [
    ("ja", "jpn", "jp", "japanese"), ("en", "eng", "english"), ("es", "spa", "spanish"),
    ("fr", "fre", "fra", "french"), ("de", "ger", "deu", "german"), ("it", "ita", "italian"),
    ("pt", "por", "portuguese"), ("ru", "rus", "russian"), ("ko", "kor", "korean"),
    ("zh", "chi", "zho", "chinese"),
]
# (code, label) for the settings' dropdowns
LANGUAGES = [(group[0], group[-1].capitalize()) for group in _LANGUAGES]


# What separates the video's name from the language tags in a subtitle file's name.
_TAG_SEPARATOR = re.compile(r"[.\-_ \[\]()]+")

# Folder names that don't identify a show by themselves ("Show/Season 1").
_GENERIC_FOLDER = re.compile(r"(season|s)\s*\d+|specials?|extras?|subs?|videos?", re.I)


def delay_key(folder):
    """Key under which a folder's subtitle delay is remembered. The delay syncs with the
    collection, so it is the show's folder name, not a path that only exists on one
    computer; a generic name ("Season 1") keeps its parent's ("Show/Season 1").
    Accepts Windows and POSIX paths alike (keys saved elsewhere can be either)."""
    parts = [p for p in re.split(r"[\\/]", str(folder)) if p and not p.endswith(":")]
    if not parts:
        return ""
    if len(parts) > 1 and _GENERIC_FOLDER.fullmatch(parts[-1]):
        return f"{parts[-2]}/{parts[-1]}"
    return parts[-1]


# Embedded tracks that are pictures, not text: nothing to read or tokenize.
IMAGE_CODECS = {"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "xsub"}

# ASS styles fansubbers give to typesetting and songs, not to spoken dialogue.
_NON_DIALOGUE_STYLE = re.compile(
    r"\b(signs?|songs?|lyrics?|karaoke|op|ed|opening|ending|title|credits?|insert|staff)\b",
    re.I)
# Override tags only typesetting uses: drawings (\p1), moves, karaoke timing (\k, \kf, \ko).
_NON_DIALOGUE_TAG = re.compile(r"\\(p[1-9]|move\(|k[fo]?\d)")


def is_dialogue(event):
    """False for comments, signs, songs and karaoke in an ASS file; True for spoken lines
    (and for every line of SRT / VTT files, which have no styles)."""
    if event.is_comment or not event.plaintext.strip():
        return False
    return not (_NON_DIALOGUE_STYLE.search(event.style or "")
                or _NON_DIALOGUE_TAG.search(event.text))


# Kana and kanji: how much of a decoded text reads as Japanese.
_JAPANESE_CHAR = re.compile("[ぁ-ゖァ-ヺー々一-鿿]")


def decode(data):
    """Text of a subtitle file. Japanese subtitles are often Shift-JIS (cp932), sometimes
    EUC-JP or UTF-16, rather than UTF-8; UTF-16 is only trusted with its byte-order mark.
    The same bytes can be valid in both Shift-JIS and EUC-JP: the one giving the most kana
    and kanji wins (Shift-JIS on a tie)."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    texts = []
    for encoding in ("cp932", "euc_jp"):
        try:
            texts.append(data.decode(encoding))
        except UnicodeDecodeError:
            pass
    if texts:
        return max(texts, key=lambda t: len(_JAPANESE_CHAR.findall(t)))
    return data.decode("utf-8", "replace")


def load_events(path):
    """Dialogue of a subtitle file as [Event] (times in seconds), ordered by start.
    Times are the file's own: they don't include the subtitle delay set in the player."""
    from ..vendor import pysubs2
    subs = pysubs2.SSAFile.from_string(decode(Path(path).read_bytes()))
    return sorted(Event(ev.start / 1000, ev.end / 1000, t)
                  for ev in subs if is_dialogue(ev) and (t := ev.plaintext.strip()))


_LATIN_LETTER = re.compile("[A-Za-zＡ-Ｚａ-ｚ]")
JAPANESE_SHARE = 0.3  # of the letters of a file's dialogue, for it to read as Japanese


def reads_as_japanese(path):
    """Is the dialogue of a subtitle file mostly kana and kanji? An untagged file next to
    the video may as well be the English one. False if it can't be read."""
    try:
        text = "".join(ev.text for ev in load_events(path))
    except Exception:
        return False
    japanese = len(_JAPANESE_CHAR.findall(text))
    return japanese > 0 and japanese >= JAPANESE_SHARE * (
        japanese + len(_LATIN_LETTER.findall(text)))


def lang_codes(code):
    """Every spelling of the language `code` ("ja" -> {"ja", "jpn", ...})."""
    code = code.strip().lower()
    return next((set(group) for group in _LANGUAGES if code in group),
                {code} if code else set())


def lang_code(code):
    """The ISO 639-1 spelling of a known language ("jpn" -> "ja"); anything else as given."""
    code = code.strip().lower()
    return next((group[0] for group in _LANGUAGES if code in group), code)


def find_sidecars(video, source_lang, target_lang, chosen=None):
    """(source, target) subtitle files next to `video`, either may be None.

    Names are matched on the video's stem plus language tags: `ep01.ja.srt`, `ep01.eng.srt`,
    in any case (`EP01.JA.srt`). An untagged `ep01.srt` counts as the source language if
    its text reads as Japanese. `chosen` is a source file picked by hand ("Load
    subtitles..."): it is the source as long as it exists."""
    video = Path(video)
    chosen = Path(chosen) if chosen and Path(chosen).is_file() else None
    source_codes, target_codes = lang_codes(source_lang), lang_codes(target_lang)
    source = target = None
    untagged = []
    stem = video.stem.lower()
    for f in sorted(video.parent.iterdir()):
        if f.suffix.lower() not in SUB_EXTS or not f.name.lower().startswith(stem):
            continue
        rest = f.stem[len(video.stem):]
        if rest and not _TAG_SEPARATOR.match(rest):
            continue  # another video's file: "ep10.ja.srt" for "ep1.mkv"
        tags = {t.lower() for t in _TAG_SEPARATOR.split(rest) if t}
        if tags & source_codes:
            source = source or f
        elif tags & target_codes:
            target = target or f
        elif not tags:
            untagged.append(f)
    if chosen or source:
        return chosen or source, target
    return next((f for f in untagged if reads_as_japanese(f)), None), target
