"""Splitting a line into words without Yomitan, for when it isn't running, from what Ankime
already knows: Yomitan's own answers for the episodes analysed before (the token cache), the
words of your cards and marks, and the particles. Like Yomitan, it takes the longest word
starting at each position, through conjugations too (食べなかった -> 食べる). What it doesn't
know is split by script and has no dictionary form, like a name: never "new".

A rough split, so the player only shows it: i+1, the line filter, the scores and cards wait
for Yomitan, and the line is tokenized again once Yomitan is back."""
import re
from collections import Counter

from ..core.model import LINE_BREAK, Token
from . import knowledge

# --- conjugations ----------------------------------------------------------------
# (inflected ending, ending it comes from). They chain (書かれなかった -> 書かれる -> 書く), and
# a form only counts once it is a word in the lexicon, so they can be loose.

_GODAN = {  # dictionary ending -> (a, i, e, o rows, past "ta", "te")
    "う": ("わ", "い", "え", "お", "った", "って"), "く": ("か", "き", "け", "こ", "いた", "いて"),
    "ぐ": ("が", "ぎ", "げ", "ご", "いだ", "いで"), "す": ("さ", "し", "せ", "そ", "した", "して"),
    "つ": ("た", "ち", "て", "と", "った", "って"), "ぬ": ("な", "に", "ね", "の", "んだ", "んで"),
    "ぶ": ("ば", "び", "べ", "ぼ", "んだ", "んで"), "む": ("ま", "み", "め", "も", "んだ", "んで"),
    "る": ("ら", "り", "れ", "ろ", "った", "って"),
}
# after the negative stem (書か-), the masu stem (書き-, 食べ-)
_AFTER_A = ("ない", "なかった", "なくて", "なければ", "ず", "ずに", "れる", "せる", "せられる")
_AFTER_I = ("ます", "ました", "ません", "ませんでした", "ましょう", "たい", "たかった", "たくない",
            "ながら", "そう", "すぎる")


def _rules():
    rules = []
    for base, (a, i, e, o, ta, te) in _GODAN.items():
        rules += [(a + end, base) for end in _AFTER_A]
        rules += [(i + end, base) for end in _AFTER_I]
        rules += [(e + "ば", base), (e + "る", base), (e, base), (o + "う", base),
                  (ta, base), (te, base), (ta + "ら", base), (ta + "り", base)]
    # ichidan (食べる): the stem is the word without る
    rules += [(end, "る") for end in (*_AFTER_A, *_AFTER_I, "た", "て", "たら", "たり", "れば",
                                       "ろ", "よう", "られる", "させる", "させられる")]
    rules += [  # i-adjectives
        ("く", "い"), ("くて", "い"), ("くない", "い"), ("くなかった", "い"), ("かった", "い"),
        ("ければ", "い"), ("さ", "い"), ("そう", "い"), ("すぎる", "い"),
        # する, 来る, the copula
        ("した", "する"), ("して", "する"), ("しない", "する"), ("しなかった", "する"),
        ("します", "する"), ("しました", "する"), ("しません", "する"), ("しよう", "する"),
        ("される", "する"), ("させる", "する"), ("すれば", "する"), ("しろ", "する"),
        ("きた", "くる"), ("きて", "くる"), ("こない", "くる"), ("きます", "くる"),
        ("だった", "だ"), ("でした", "です"),
        # te-form + いる / しまう / おく / おる, also contracted: back to the te-form (their
        # own conjugations are the godan and ichidan rules': 言っておこう -> 言っておく)
        ("ている", "て"), ("てる", "て"), ("ていた", "て"), ("てた", "て"), ("ています", "て"),
        ("てます", "て"), ("ていない", "て"), ("てない", "て"), ("ちゃう", "て"),
        ("ちゃった", "て"), ("でいる", "で"), ("でる", "で"), ("でいた", "で"), ("でた", "で"),
        ("じゃう", "で"), ("じゃった", "で"), ("ておく", "て"), ("とく", "て"), ("でおく", "で"),
        ("どく", "で"), ("ておる", "て"), ("とる", "て"), ("でおる", "で"), ("どる", "で"),
    ]
    by_last = {}  # last character of the inflected ending -> rules, longest ending first
    for inflected, base in sorted(dict.fromkeys(rules), key=lambda r: -len(r[0])):
        by_last.setdefault(inflected[-1], []).append((inflected, base))
    return by_last


_RULES = _rules()
_MAX_CHAIN = 3
# what a dictionary form a conjugation comes from ends in: verbs, i-adjectives, the copula
_DICTIONARY_ENDINGS = set("うくぐすつぬぶむるいだ")


def deinflect(word):
    """The forms `word` may be a conjugation of, nearest first (not `word` itself)."""
    seen, frontier, out = {word}, [word], []
    for _ in range(_MAX_CHAIN):
        following = []
        for form in frontier:
            for inflected, base in _RULES.get(form[-1:], ()):
                if form.endswith(inflected) and len(form) > len(inflected):
                    candidate = form[:-len(inflected)] + base
                    if candidate not in seen:
                        seen.add(candidate)
                        following.append(candidate)
        out.extend(following)
        frontier = following
    return [form for form in out if form[-1] in _DICTIONARY_ENDINGS]


# --- the lexicon -----------------------------------------------------------------

class Lexicon:
    """What words look like: Yomitan's tokens by surface (the one it gave most often), and
    the dictionary forms it gave with their other spellings."""

    def __init__(self, token_lists=()):
        self._seen = {}   # surface -> Counter of the tokens Yomitan gave for it
        self._best = {}   # surface -> the most common of them
        self.bases = {}   # dictionary form -> other spellings and readings
        self.longest = 1  # characters in the longest surface
        for particle in knowledge.PARTICLES:
            self._remember(Token(particle, "", particle, ()))
        for tokens in token_lists:
            self.add(tokens)

    def add(self, tokens):
        """Learn from Yomitan's tokens of a line."""
        for token in tokens:
            if token != LINE_BREAK and token[0].strip():
                self._remember(Token(*token))

    def _remember(self, token):
        counts = self._seen.setdefault(token.surface, Counter())
        counts[token] += 1
        best = self._best.get(token.surface)
        if best is None or counts[token] > counts[best]:
            self._best[token.surface] = token
        if token.base:
            self.bases.setdefault(token.base, token.alts)
        self.longest = max(self.longest, len(token.surface))

    def get(self, surface):
        return self._best.get(surface)


# --- splitting -------------------------------------------------------------------

_SCRIPTS = [(re.compile("[ぁ-ゖゝゞ]"), "hiragana"), (re.compile("[ァ-ヺー-ヾ]"), "katakana"),
            (re.compile("[々〆㐀-䶿一-鿿\U00020000-\U0003134F]"), "kanji")]


def _script(char):
    return next((name for pattern, name in _SCRIPTS if pattern.match(char)), "other")


def _match(text, i, lexicon, has_word, limit):
    """The longest word of `text` starting at `i`, as a Token, or None."""
    for end in range(min(len(text), i + limit), i, -1):
        surface = text[i:end]
        token = lexicon.get(surface)
        if token is not None:
            return token
        if has_word(surface):
            return Token(surface, "", surface, lexicon.bases.get(surface, ()))
        if end - i < 2 or _script(surface[-1]) != "hiragana":
            continue  # conjugations end in kana
        for base in deinflect(surface):
            if base in lexicon.bases or has_word(base):
                return Token(surface, "", base, lexicon.bases.get(base, ()))
    return None


def tokenize(text, lexicon, has_word=lambda _word: False, limit=20):
    """[Token] covering `text` in order, like yomitan.tokenize. `has_word(word)` says whether
    a card or a mark has the word; `limit` is the longest word looked for, in characters."""
    limit = min(limit, max(lexicon.longest, 8))
    tokens, unknown = [], ""

    def flush():
        nonlocal unknown
        if unknown:
            tokens.append(Token(unknown, "", "", ()))
            unknown = ""

    i = 0
    while i < len(text):
        char = text[i]
        if char == "\n":
            flush()
            tokens.append(LINE_BREAK)
            i += 1
            continue
        token = _match(text, i, lexicon, has_word, limit)
        if token is not None:
            flush()
            tokens.append(token)
            i += len(token.surface)
            continue
        if unknown and _script(unknown[-1]) != _script(char):
            flush()
        unknown += char
        i += 1
    flush()
    return tokens
