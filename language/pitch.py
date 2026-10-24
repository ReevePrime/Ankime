"""Pitch-accent categories (heiban, atamadaka...) of words, for colouring the subtitles. Yomitan
gives them as its pitch-accent-categories marker ("heiban", or "heiban,odaka" for a word with
two accents); Ankime's own dictionaries compute them the same way from the accent's position
(see category)."""

# (category, label), in the order the settings list them
CATEGORIES = [("heiban", "Heiban (flat)"), ("atamadaka", "Atamadaka (falls after the first mora)"),
              ("nakadaka", "Nakadaka (falls inside the word)"),
              ("odaka", "Odaka (falls after the last mora)"),
              ("kifuku", "Kifuku (verbs and adjectives with a fall)")]
MARKER = "pitch-accent-categories"
_SMALL_KANA = set("ゃゅょぁぃぅぇぉゎャュョァィゥェォヮ")
# Yomitan's deinflection rules of verbs and i-adjectives: their accent is heiban or kifuku
_VERB_OR_ADJECTIVE = {"v1", "v5", "vk", "vs", "vz", "adj-i"}


def morae(reading):
    """The reading's morae: きょう -> きょ, う."""
    out = []
    for char in reading:
        if out and char in _SMALL_KANA:
            out[-1] += char
        else:
            out.append(char)
    return out


def is_verb_or_adjective(rules):
    """Do a term's rules ("v5 vt", "adj-i") make it a verb or an i-adjective?"""
    return any(r in _VERB_OR_ADJECTIVE or r.startswith(("v1", "v5"))
               for r in (rules or "").split())


def category(reading, position, verb_or_adjective=False):
    """The category of the accent at mora `position` of `reading` (0 = no fall), as Yomitan
    names it, or "" if the position doesn't fit the reading."""
    count = len(morae(reading))
    if not isinstance(position, int) or isinstance(position, bool) or not (
            0 <= position <= count) or not count:
        return ""
    if position == 0:
        return "heiban"
    if verb_or_adjective:
        return "kifuku"
    if position == 1:
        return "atamadaka"
    return "odaka" if position == count else "nakadaka"


def first_category(value):
    """The first known category of a pitch-accent-categories value, or ""."""
    names = dict(CATEGORIES)
    return next((c for c in (p.strip() for p in str(value or "").split(",")) if c in names), "")


def _reading(token):
    if isinstance(token.furigana, str):
        return token.surface
    return "".join(reading or text for text, reading in token.furigana)


def token_category(token, entries):
    """The category of `token` (model.Token) from the dictionary entries looked up for its
    surface (yomitan.lookup): from the entry for its dictionary form, the one with its reading
    if there are several (今日: きょう, not こんにち), else the first. "" if none has one."""
    if not entries:
        return ""
    word = token.base or token.surface
    same = [e for e in entries if e.get("expression") == word]
    if token.surface == word:
        reading = _reading(token)
        same = [e for e in same if e.get("reading") == reading] or same
    for entry in same or entries[:1]:
        if found := first_category(entry.get(MARKER)):
            return found
    return ""
