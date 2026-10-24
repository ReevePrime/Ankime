"""Splitting lines without Yomitan (offline.py), and choosing how mpv shows the video."""
import json

import pytest

from ankime.language import offline
from ankime.player import mpv_controller
from ankime.subs import session
from ankime.core.model import LINE_BREAK, SetupError, Token

from test_state import know  # noqa: F401  (fixture)

TABETA = Token("食べた", (("食", "た"), ("べた", "")), "食べる", ("たべる",))
NEKO = Token("猫", (("猫", "ねこ"),), "猫", ("ねこ",))


def surfaces(tokens):
    return [t.surface for t in tokens]


@pytest.mark.parametrize("word, base", [
    ("食べなかった", "食べる"), ("書かれなかった", "書く"), ("読んでる", "読む"),
    ("言っておこう", "言う"), ("狩っちゃったら", "狩る"), ("高くなかった", "高い"),
    ("勉強しました", "勉強する"), ("来なかった", "来る"), ("行きます", "行く"),
])
def test_deinflect_finds_the_dictionary_form(word, base):
    assert base in offline.deinflect(word)


def test_deinflect_is_the_same_every_run():
    # the nearest form first; the rules' order mustn't depend on string hashing
    assert offline.deinflect("書いた")[:2] == offline.deinflect("書いた")[:2]
    assert offline.deinflect("書いた").index("書く") < offline.deinflect("書いた").index("書いる")


def test_deinflect_gives_only_dictionary_forms():
    assert set(offline.deinflect("読んでる")) == {"読む", "読ぶ", "読ぬ"}  # not 読んで
    assert offline.deinflect("猫") == []


def test_lexicon_keeps_the_token_yomitan_gave_most_often():
    other = Token("食べた", "", "食べ", ())
    lex = offline.Lexicon([[TABETA], [other], [TABETA]])
    assert lex.get("食べた") == TABETA
    assert lex.bases["食べる"] == ("たべる",) and "食べ" in lex.bases
    assert lex.get("は") == ("は", "", "は", ())  # particles are known from the start


def test_lexicon_skips_line_breaks_and_blanks():
    lex = offline.Lexicon([[NEKO, LINE_BREAK, Token(" ", "", "", ())]])
    assert lex.get("\n") is None and lex.get(" ") is None


def test_tokenize_takes_the_longest_known_word_with_yomitans_token():
    lex = offline.Lexicon([[NEKO, Token("猫舌", "", "猫舌", ())]])
    assert offline.tokenize("猫舌は猫", lex) == [
        Token("猫舌", "", "猫舌", ()), Token("は", "", "は", ()), NEKO]


def test_tokenize_follows_conjugations_to_a_known_dictionary_form():
    lex = offline.Lexicon([[TABETA]])
    tokens = offline.tokenize("食べなかった", lex)
    assert tokens == [Token("食べなかった", "", "食べる", ("たべる",))]


def test_tokenize_uses_the_words_of_cards():
    lex = offline.Lexicon()
    tokens = offline.tokenize("走らなかった", lex, has_word=lambda w: w == "走る")
    assert tokens == [Token("走らなかった", "", "走る", ())]


def test_unknown_text_is_split_by_script_without_a_dictionary_form():
    tokens = offline.tokenize("学校はテレビ！", offline.Lexicon())
    assert surfaces(tokens) == ["学校", "は", "テレビ", "！"]
    assert [t.base for t in tokens] == ["", "は", "", ""]


def test_tokenize_keeps_line_breaks():
    tokens = offline.tokenize("猫\n猫", offline.Lexicon([[NEKO]]))
    assert tokens == [NEKO, LINE_BREAK, NEKO]


def test_offline_words_get_their_status(know):  # noqa: F811
    lex = offline.Lexicon()
    tokens = offline.tokenize("食べなかった犬", lex, know.has)
    assert [know.token_status(*t[:1], t.base, t.alts) for t in tokens] == ["known", "other"]


def test_knowledge_has_cards_and_marks(know):  # noqa: F811
    assert know.has("食べる") and know.has("猫") and not know.has("犬")
    know.mark(["犬"])
    assert know.has("犬")


def test_all_cached_tokens_reads_every_episode_and_skips_old_formats(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    monkeypatch.setattr(session, "TOKEN_CACHE_DIR", cache)
    cache.mkdir()
    (cache / "a.json").write_text(json.dumps({"猫": [["猫", "", "猫", ["ねこ"]]]}),
                                  encoding="utf-8")
    (cache / "old.json").write_text(json.dumps({"犬": [["犬", "いぬ", "犬"]]}), encoding="utf-8")
    (cache / session.TOKEN_INDEX).write_text("{}", encoding="utf-8")
    assert list(session.all_cached_tokens()) == [[Token("猫", "", "猫", ("ねこ",))]]


# --- how mpv shows the video ------------------------------------------------------

@pytest.fixture
def system(monkeypatch):
    """Set what this system can do: (OpenGL problem, native window problem)."""
    def set_problems(opengl="", window=""):
        monkeypatch.setattr(mpv_controller, "opengl_problem", lambda: opengl)
        monkeypatch.setattr(mpv_controller, "window_problem", lambda: window)
    return set_problems


def test_auto_takes_opengl(system):
    system()
    assert mpv_controller.video_output("auto") == "opengl"


def test_auto_falls_back_to_a_native_window_without_opengl(system):
    system(opengl="OpenGL isn't available")
    assert mpv_controller.video_output("auto") == "window"


def test_without_opengl_or_native_windows_it_says_why(system):
    system(opengl="OpenGL isn't available", window="on Wayland, ...")
    with pytest.raises(SetupError, match="OpenGL isn't available"):
        mpv_controller.video_output("auto")


def test_opengl_asked_for_doesnt_fall_back(system):
    system(opengl="OpenGL isn't available")
    with pytest.raises(SetupError):
        mpv_controller.video_output("opengl")


def test_native_window_asked_for(system):
    system()
    assert mpv_controller.video_output("window") == "window"
    system(window="on Wayland, mpv can't be given a window to draw into")
    with pytest.raises(SetupError, match="Wayland"):
        mpv_controller.video_output("window")
