"""Deinflection for the local dictionary (local_dict.py): the dictionary forms a conjugated
word may come from, each with the word class it must have (食べなかった -> 食べる, a v1 verb).
Like Yomitan's, each rule says what class the conjugated form is (食べない conjugates like an
i-adjective) and what class the form it comes from is, so rules chain only where Japanese
does (書かれなかった -> 書かれない -> 書かれる -> 書く), and a dictionary form only counts if the
dictionary's entry has that class. The classes are those of Yomitan dictionaries' rules
column: v1, v5, vs, vk, vz, adj-i. Pure logic.

offline.py has a looser set for splitting lines without any dictionary."""

V1, V5, VS, VK, VZ, ADJ_I = "v1", "v5", "vs", "vk", "vz", "adj-i"
MASU, TE = "masu", "te"  # forms that conjugate further: 書きます -> 書きました, 書いて -> 書いている
WORD_CLASSES = {V1, V5, VS, VK, VZ, ADJ_I}

_GODAN = {  # dictionary ending -> (a, i, e, o rows, past, te-form)
    "う": ("わ", "い", "え", "お", "った", "って"), "く": ("か", "き", "け", "こ", "いた", "いて"),
    "ぐ": ("が", "ぎ", "げ", "ご", "いだ", "いで"), "す": ("さ", "し", "せ", "そ", "した", "して"),
    "つ": ("た", "ち", "て", "と", "った", "って"), "ぬ": ("な", "に", "ね", "の", "んだ", "んで"),
    "ぶ": ("ば", "び", "べ", "ぼ", "んだ", "んで"), "む": ("ま", "み", "め", "も", "んだ", "んで"),
    "る": ("ら", "り", "れ", "ろ", "った", "って"),
}

# Endings after a verb's negative stem (書か-, 食べ-, し-, こ-) and its masu stem (書き-, 食べ-,
# し-, き-): (ending, class of the conjugated form; "" = it doesn't conjugate further).
_AFTER_NEGATIVE = [("ない", ADJ_I), ("ず", ""), ("ずに", ""), ("ぬ", ""), ("ん", ""),
                   ("なきゃ", ""), ("なくちゃ", ""), ("ねえ", ""), ("せる", V1), ("せられる", V1)]
_AFTER_MASU_STEM = [("ます", MASU), ("たい", ADJ_I), ("ながら", ""), ("そう", ""),
                    ("すぎる", V1), ("なさい", ""), ("たがる", V5)]
# The past (た) and te-form (て), and what comes after them: (ending after the past's た /
# だ, class)
_AFTER_PAST = [("", ""), ("ら", ""), ("り", ""), ("ろう", "")]
# What follows a te-form and conjugates: て + いる (食べている, 食べてる) ... -> the te-form
_AFTER_TE = [("いる", V1), ("る", V1), ("おく", V5), ("しまう", V5), ("ある", V5),
             ("くる", VK), ("いく", V5), ("く", V5), ("みる", V1)]
_CONTRACTED = [("ちゃう", "て", V5), ("じゃう", "で", V5), ("とく", "て", V5), ("どく", "で", V5),
               ("ちまう", "て", V5), ("じまう", "で", V5)]
_MASU_FORMS = ["ました", "ません", "ませんでした", "ましょう", "まして", "ませ", "ますれば"]


def _verb(rules, stems, base, base_class):
    """The conjugations of a verb whose stems are `stems` (negative, masu, conditional,
    imperative, volitional, past, te-form, potential, passive...) back to `base`."""
    neg, masu, past, te, cond, imperative, volitional, potential, passive, causative = stems
    for end, cls in _AFTER_NEGATIVE:
        rules.append((neg + end, base, cls, base_class))
    for end, cls in _AFTER_MASU_STEM:
        rules.append((masu + end, base, cls, base_class))
    for end, cls in _AFTER_PAST:
        rules.append((past + end, base, cls, base_class))
    rules.append((te, base, TE, base_class))
    rules += [(cond, base, "", base_class), (volitional, base, "", base_class),
              (potential, base, V1, base_class), (passive, base, V1, base_class),
              (causative, base, V1, base_class)]
    rules += [(form, base, "", base_class) for form in imperative]


def _rules():
    rules = []  # (conjugated ending, ending it comes from, class of the conjugated form,
    #             class of the form it comes from)
    for base, (a, i, e, o, ta, te) in _GODAN.items():
        _verb(rules, (a, i, ta, te, e + "ば", [e], o + "う", e + "る", a + "れる", a + "せる"),
              base, V5)
    # 行く: 行った, 行って (not 行いた)
    rules += [("った", "く", "", V5), ("ったら", "く", "", V5), ("って", "く", TE, V5)]
    # ichidan: the stem is the word without る
    _verb(rules, ("", "", "た", "て", "れば", ["ろ", "よ"], "よう", "られる", "られる", "させる"),
          "る", V1)
    rules.append(("れる", "る", V1, V1))  # potential without ら (見れる)
    # する and 来る (in kana and with 来)
    _verb(rules, ("し", "し", "した", "して", "すれば", ["しろ", "せよ"], "しよう", "できる",
                  "される", "させる"), "する", VS)
    rules.append(("せず", "する", "", VS))
    for ku, ko, ki in (("くる", "こ", "き"), ("来る", "来", "来")):
        _verb(rules, (ko, ki, ki + "た", ki + "て", ku[:-1] + "れば", [ko + "い"], ko + "よう",
                      ko + "られる", ko + "られる", ko + "させる"), ku, VK)
    # -ずる verbs (感ずる) conjugate like -じる ones
    _verb(rules, ("じ", "じ", "じた", "じて", "ずれば", ["じろ"], "じよう", "じられる",
                  "じられる", "じさせる"), "ずる", VZ)
    # i-adjectives
    rules += [(end, "い", cls, ADJ_I) for end, cls in [
        ("く", ""), ("くて", TE), ("くない", ADJ_I), ("かった", ""), ("かったら", ""),
        ("ければ", ""), ("きゃ", ""), ("さ", ""), ("そう", ""), ("すぎる", V1), ("かろう", ""),
        ("くなる", V5), ("げ", "")]]
    # the polite forms of ます, the te-form's helpers
    rules += [(form, "ます", "", MASU) for form in _MASU_FORMS]
    for te in ("て", "で"):
        rules += [(te + end, te, cls, TE) for end, cls in _AFTER_TE]
    rules += [(end, te, cls, TE) for end, te, cls in _CONTRACTED]
    by_last = {}  # last character of the conjugated ending -> rules, longest ending first
    for rule in sorted(dict.fromkeys(rules), key=lambda r: -len(r[0])):
        if rule[0]:
            by_last.setdefault(rule[0][-1], []).append(rule)
    return by_last


_RULES = _rules()
MAX_STEPS = 6


def deinflect(text):
    """[(form, word class or None, steps)]: `text` itself (class None: any word), then the
    forms it may be a conjugation of, nearest first, each with the class its dictionary
    entry must have. Forms whose class is only MASU or TE (a te-form, not a dictionary
    form) are left out."""
    out, frontier, seen = [(text, None, 0)], [(text, None)], {(text, None)}
    for steps in range(1, MAX_STEPS + 1):
        following = []
        for form, cls in frontier:
            for end, base, in_cls, out_cls in _RULES.get(form[-1:], ()):
                if not form.endswith(end) or len(form) == len(end) and base == "":
                    continue
                if cls is not None and cls != in_cls:
                    continue  # this form isn't what the rule conjugates
                candidate = (form[:-len(end)] + base, out_cls)
                if candidate[0] and candidate not in seen:
                    seen.add(candidate)
                    following.append(candidate)
        out += [(form, cls, steps) for form, cls in following if cls in WORD_CLASSES]
        frontier = following
        if not frontier:
            break
    return out


def word_classes(rules):
    """The classes of a dictionary entry's rules column ("v5", "v5k-s", "v1 vt"...)."""
    out = set()
    for rule in (rules or "").split():
        if rule.startswith("v5"):
            out.add(V5)
        elif rule.startswith("v1"):
            out.add(V1)
        elif rule.startswith("vs"):
            out.add(VS)
        elif rule in WORD_CLASSES:
            out.add(rule)
    return out
