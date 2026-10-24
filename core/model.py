"""The shapes passed between the modules. Named tuples: they unpack like the plain tuples they
replace (`surface, furigana, base, alts = token`), compare equal to them, and serialise to
JSON as lists."""
from typing import NamedTuple, Optional, Union


class SetupError(Exception):
    """A setting points at something that isn't there (a note type, a field, a deck,
    libmpv): the message says what to fix. Anything else raised is a bug."""


class Token(NamedTuple):
    """One word (or punctuation) of a line, as Yomitan split it."""
    surface: str                    # as written: 食べた
    furigana: Union[str, tuple]     # ((text, reading), ...) per segment, or "" for none
    base: str                       # dictionary form: 食べる ("" if Yomitan has none)
    alts: tuple[str, ...] = ()      # other spellings and readings of the dictionary form


LINE_BREAK = Token("\n", "", "", ())


class Event(NamedTuple):
    """A line of a subtitle file, in the file's own times (no subtitle delay)."""
    start: float
    end: float
    text: str


class Line(NamedTuple):
    """The line on screen, in video times (subtitle delay included). `text` is "" between
    lines, and the times are then None."""
    text: str
    start: Optional[float]
    end: Optional[float]
    video: Optional[str]


class Span(NamedTuple):
    """What a card takes: the text of one or more lines and their times, in video time."""
    text: str
    start: float
    end: float


class Selection(NamedTuple):
    """The word whose popup is showing."""
    surface: str
    base: str
    index: Optional[int] = None  # its position among the line's tokens, if known
