"""Splitting lines into words: by MeCab (mecab.py) if the parser setting allows it and it is
installed, else by Yomitan's scanning parser (yomitan.py). Either way the words come back
as [Token], with Yomitan's headwords."""
from ..core import config
from . import mecab, yomitan

# (config value, label)
PARSERS = [("auto", "MeCab if installed, else Yomitan"),
           ("yomitan", "Yomitan's scanning parser")]


def uses_mecab(cfg=None):
    """The MeCab that splits the lines, or None if Yomitan does."""
    cfg = cfg or config.get()
    return mecab.find() if cfg["parser"] == "auto" else None


def analysis_key(cfg=None):
    """What the words of a line depend on, for the caches of them: the scan length, and
    MeCab if it splits the lines (the scan length alone, as before there was a choice, if
    Yomitan does), and Ankime's own dictionaries if they answer rather than Yomitan."""
    cfg = cfg or config.get()
    key = f"mecab:{cfg['scan_length']}" if uses_mecab(cfg) else cfg["scan_length"]
    return f"local:{key}" if yomitan.uses_local(cfg) else key


def tokenize_many(texts, chunk=100, progress=None):
    """Tokens for each text; see yomitan.tokenize_many."""
    found = uses_mecab()
    lines = (lambda batch: mecab.tokenize_lines(batch, found)) if found else None
    return yomitan.tokenize_many(texts, chunk, progress, tokenize_lines=lines)


def tokenize(text):
    """Returns [Token] covering `text` in order."""
    return tokenize_many([text])[0]
