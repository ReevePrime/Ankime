"""Ankime's own dictionaries (local_dict.py, deinflect.py): importing a Yomitan dictionary,
splitting lines and answering yomitan-api's calls the way Yomitan does."""
import json
import zipfile

import pytest

from ankime.core.model import Token
from ankime.language import deinflect, local_dict, tokenizer, yomitan


def _gloss(*texts):
    return [{"type": "structured-content",
             "content": {"tag": "ul", "style": {"listStyleType": "circle"},
                         "content": [{"tag": "li", "content": t} for t in texts]}}]


TERMS = [
    # expression, reading, definition tags, rules, score, glossary, sequence, term tags
    ["食べる", "たべる", "1 v1", "v1", 1999800, _gloss("to eat"), 1, ""],
    ["食べる", "たべる", "2 v1", "v1", 1999799, _gloss("to live on"), 1, ""],
    ["食べる", "たべる", "forms", "v1", 1999798, ["食べる", "喰べる"], 1, ""],
    ["綺麗", "きれい", "1 adj-na", "", 999800, _gloss("pretty"), 2, ""],
    ["奇麗", "きれい", "1 adj-na", "", -10200, _gloss("pretty"), 2, ""],
    ["書く", "かく", "1 v5", "v5", 999800, _gloss("to write"), 3, ""],
    ["勉強", "べんきょう", "1 n vs", "vs", 999800, _gloss("study"), 4, ""],
    ["する", "する", "1 vs-i", "vs", 999800, _gloss("to do"), 5, ""],
    ["日本語", "にほんご", "1 n", "", 999800, _gloss("Japanese"), 6, ""],
    ["を", "を", "1 prt", "", 999800, _gloss("object marker"), 7, ""],
    ["橋", "はし", "1 n", "", 999800, _gloss("bridge"), 8, ""],
    ["箸", "はし", "1 n", "", 999700, _gloss("chopsticks"), 9, ""],
    ["菜", "な", "1 n", "", 999900, _gloss("greens"), 10, ""],
    ["な", "な", "1 prt", "", -200, _gloss("sentence-ending particle"), 11, ""],
    ["切る", "きる", "1 v5", "v5", 999800, _gloss("to cut"), 12, ""],
    ["着る", "きる", "1 v1", "v1", 999800, _gloss("to wear"), 13, ""],
    ["来る", "くる", "1 vk", "vk", 999800, _gloss("to come"), 14, ""],
    ["高い", "たかい", "1 adj-i", "adj-i", 999800, _gloss("high"), 15, ""],
    ["悪", "あく", "1 n", "", 0, ["<script>alert(1)</script>",
                                  {"type": "image", "path": "x.png"},
                                  {"type": "structured-content",
                                   "content": [{"tag": "a", "href": "?query=x", "content": "see"},
                                               {"tag": "img", "path": "y.png"},
                                               {"tag": "iframe", "content": "inner"}]}],
     16, ""],
]
META = [
    ["食べる", "freq", {"reading": "たべる", "frequency": {"value": 120, "displayValue": "120"}}],
    ["食べる", "freq", {"reading": "たべない", "frequency": 5}],  # another reading: not this one
    ["橋", "pitch", {"reading": "はし", "pitches": [{"position": 2}]}],
]


def make_dictionary(path, title="Test", terms=TERMS, meta=META, fmt=3, extra=None):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("index.json", json.dumps({"title": title, "format": fmt, "revision": "1"}))
        if terms:
            z.writestr("term_bank_1.json", json.dumps(terms, ensure_ascii=False))
        if meta:
            z.writestr("term_meta_bank_1.json", json.dumps(meta, ensure_ascii=False))
        for name, data in (extra or {}).items():
            z.writestr(name, data)
    return path


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(local_dict, "DB_PATH", tmp_path / "dictionaries.sqlite")
    local_dict._changed()
    yield tmp_path
    local_dict._changed()


@pytest.fixture
def imported(db):
    local_dict.import_zip(make_dictionary(db / "test.zip"))
    return db


def words(line):
    """(surface, base) of each word of `line`, as yomitan.py reads the tokenize answer."""
    answer = local_dict.answer("tokenize", {"text": [line], "scanLength": 20})
    return [(t.surface, t.base) for t in yomitan.parse_tokenize(answer, 1)[0]]


# --- deinflection ------------------------------------------------------------------

@pytest.mark.parametrize("word, base, cls", [
    ("食べなかった", "食べる", "v1"), ("書かれなかった", "書く", "v5"), ("書いている", "書く", "v5"),
    ("食べちゃった", "食べる", "v1"), ("しました", "する", "vs"), ("来なかった", "来る", "vk"),
    ("こない", "くる", "vk"), ("高くなかった", "高い", "adj-i"), ("行った", "行く", "v5"),
    ("書きたかった", "書く", "v5"), ("食べられる", "食べる", "v1"), ("書けば", "書く", "v5"),
])
def test_deinflect_finds_the_dictionary_form_and_its_class(word, base, cls):
    assert (base, cls) in [(form, c) for form, c, _steps in deinflect.deinflect(word)]


def test_deinflect_starts_with_the_text_itself_and_skips_te_forms():
    found = deinflect.deinflect("書いて")
    assert found[0] == ("書いて", None, 0)
    assert all(cls in deinflect.WORD_CLASSES for _form, cls, _steps in found[1:])


def test_word_classes_of_a_rules_column():
    assert deinflect.word_classes("v5k-s vt") == {"v5"}
    assert deinflect.word_classes("vs-i") == {"vs"}
    assert deinflect.word_classes("") == set()


# --- furigana ------------------------------------------------------------------------

@pytest.mark.parametrize("term, reading, expected", [
    ("食べる", "たべる", [("食", "た"), ("べる", "")]),
    ("日本語", "にほんご", [("日本語", "にほんご")]),
    ("お茶", "おちゃ", [("お", ""), ("茶", "ちゃ")]),
    ("する", "する", [("する", "")]),
])
def test_distribute_furigana(term, reading, expected):
    assert local_dict.distribute_furigana(term, reading) == expected


def test_distribute_inflected_keeps_the_ending_bare():
    assert local_dict.distribute_inflected("食べる", "たべる", "食べた") == [("食", "た"), ("べた", "")]
    assert local_dict.distribute_inflected("綺麗", "きれい", "きれい") == [("きれい", "")]


def test_pitch_marks_the_high_morae_and_the_fall():
    assert local_dict.pitch_html("はし", 2).endswith("ꜜ [2]")
    assert local_dict.pitch_html("きょう", 1).startswith(
        '<span style="text-decoration: overline">きょ</span>ꜜ')
    assert "ꜜ" not in local_dict.pitch_html("さくら", 0)
    assert local_dict.pitch_html("はし", 5) == ""


# --- importing -------------------------------------------------------------------------

def test_import_lists_the_dictionary_and_reimporting_replaces_it(imported):
    found = local_dict.dictionaries()
    assert [(d["title"], d["terms"], d["meta"]) for d in found] == [("Test", len(TERMS), 3)]
    local_dict.import_zip(make_dictionary(imported / "again.zip"))
    assert [(d["title"], d["terms"]) for d in local_dict.dictionaries()] == [("Test", len(TERMS))]
    local_dict.remove("Test")
    assert local_dict.dictionaries() == []


@pytest.mark.parametrize("kwargs, message", [
    ({"fmt": 1}, "format 1"),
    ({"terms": None, "meta": None}, "no words"),
])
def test_import_refuses_what_it_cant_read(db, kwargs, message):
    with pytest.raises(local_dict.DictionaryError, match=message):
        local_dict.import_zip(make_dictionary(db / "bad.zip", **kwargs))
    assert local_dict.dictionaries() == []


def test_import_refuses_a_zip_without_an_index_and_oversized_files(db, monkeypatch):
    path = db / "plain.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("term_bank_1.json", "[]")
    with pytest.raises(local_dict.DictionaryError, match="index.json"):
        local_dict.import_zip(path)
    monkeypatch.setattr(local_dict, "MAX_BANK", 100)
    with pytest.raises(local_dict.DictionaryError, match="larger than"):
        local_dict.import_zip(make_dictionary(db / "big.zip"))
    assert local_dict.dictionaries() == []


def test_a_cancelled_import_leaves_nothing(db):
    with pytest.raises(local_dict.Cancelled):
        local_dict.import_zip(make_dictionary(db / "test.zip"), cancelled=lambda: True)
    assert local_dict.dictionaries() == []


def test_malformed_rows_are_skipped(db):
    terms = TERMS[:1] + [["", "x", "", "", 0, [], 0, ""], "nonsense", ["猫", "ねこ"]]
    local_dict.import_zip(make_dictionary(db / "test.zip", terms=terms, meta=[["x", "ipa", 1]]))
    assert local_dict.dictionaries()[0]["terms"] == 1


# --- splitting lines ------------------------------------------------------------------

def test_lines_are_split_into_the_longest_words_through_conjugations(imported):
    assert words("日本語を勉強しました") == [("日本語", "日本語"), ("を", "を"), ("勉強", "勉強"),
                                             ("しました", "する")]
    assert words("書かれなかった") == [("書かれなかった", "書く")]


def test_tokens_have_furigana_and_the_other_spellings(imported):
    answer = local_dict.answer("tokenize", {"text": "食べたきれい", "scanLength": 20})
    tabeta, kirei = yomitan.parse_tokenize(answer, 1)[0]
    assert tabeta == Token("食べた", (("食", "た"), ("べた", "")), "食べる", ("たべる",))
    assert kirei.base == "綺麗" and set(kirei.alts) == {"きれい", "奇麗"}


def test_a_conjugation_counts_only_for_its_word_class(imported):
    assert words("着ない") == [("着ない", "着る")]   # ichidan: 着る
    assert words("切らない") == [("切らない", "切る")]  # godan: 切る
    assert "切る" not in [base for _, base in words("切ない")]  # neither: not 切る


def test_the_word_written_as_the_text_comes_first(imported):
    assert words("な") == [("な", "な")]  # the particle, though 菜 scores higher


def test_text_that_is_no_word_stays_together(imported):
    assert words("ＡＢ。日本語") == [("ＡＢ。", ""), ("日本語", "日本語")]


def test_tokenize_with_no_dictionary_is_offline(db, monkeypatch):
    monkeypatch.setattr(yomitan, "BREAKER", yomitan.Breaker())
    monkeypatch.setattr(yomitan.config, "get", lambda: {"dictionary": "local",
                                                        "scan_length": 20})
    with pytest.raises(yomitan.YomitanOffline, match="No dictionary"):
        yomitan.tokenize("日本語")
    assert yomitan.BREAKER.offline


def test_yomitan_calls_go_to_the_local_dictionaries_when_set(imported, monkeypatch):
    monkeypatch.setattr(yomitan, "BREAKER", yomitan.Breaker())
    monkeypatch.setattr(yomitan.config, "get", lambda: {"dictionary": "local",
                                                        "scan_length": 20})
    monkeypatch.setattr(yomitan._SESSION, "post", lambda *a, **k: pytest.fail("HTTP"))
    assert [t.base for t in yomitan.tokenize("食べた")] == ["食べる"]
    assert yomitan.frequency_rank("食べる") == 120
    assert yomitan.word_audio("食べる") is None
    assert "Test" in yomitan.version()


def test_the_caches_of_each_dictionary_are_kept_apart():
    cfg = {"parser": "yomitan", "scan_length": 20, "dictionary": "local"}
    assert tokenizer.analysis_key(cfg) == "local:20"
    assert tokenizer.analysis_key({**cfg, "dictionary": "yomitan"}) == 20  # as before


# --- ankiFields ------------------------------------------------------------------------

MARKERS = ["expression", "reading", "glossary-brief", "glossary", "frequencies",
           "frequency-harmonic-rank", "pitch-accents", "pitch-accent-categories", "audio"]


def test_anki_fields_have_every_marker(imported):
    entry = local_dict.answer("ankiFields", {"text": "食べた", "markers": MARKERS,
                                             "maxEntries": 3})["fields"][0]
    assert (entry["expression"], entry["reading"]) == ("食べる", "たべる")
    assert "to eat" in entry["glossary-brief"] and "to live on" in entry["glossary-brief"]
    assert "喰べる" not in entry["glossary-brief"]  # the forms are no definition
    assert "(v1, Test)" in entry["glossary"]
    assert entry["frequency-harmonic-rank"] == "120"
    assert entry["frequencies"] == "<span>Test: 120</span>"
    assert entry["audio"] == ""


def test_anki_fields_list_homonyms_once_per_entry(imported):
    found = local_dict.answer("ankiFields", {"text": "はし", "markers": MARKERS,
                                             "maxEntries": 3})["fields"]
    assert [e["expression"] for e in found] == ["橋", "箸"]
    assert "ꜜ [2]" in found[0]["pitch-accents"] and found[1]["pitch-accents"] == ""
    assert found[0]["pitch-accent-categories"] == "odaka"
    assert found[1]["pitch-accent-categories"] == ""
    assert found[1]["frequency-harmonic-rank"] == local_dict.NO_FREQUENCY
    kirei = local_dict.answer("ankiFields", {"text": "きれい", "markers": ["expression"],
                                             "maxEntries": 3})["fields"]
    assert kirei == [{"expression": "綺麗"}]  # 奇麗 is the same entry


def test_pitch_categories_tell_verbs_and_adjectives_apart(db):
    local_dict.import_zip(make_dictionary(db / "p.zip", meta=[
        ["高い", "pitch", {"reading": "たかい", "pitches": [{"position": 2}]}],
        ["橋", "pitch", {"reading": "はし", "pitches": [{"position": 2}, {"position": 0}]}]]))
    markers = ["pitch-accent-categories"]
    assert local_dict.answer("ankiFields", {"text": "高い", "markers": markers})[
        "fields"][0] == {"pitch-accent-categories": "kifuku"}
    assert local_dict.answer("ankiFields", {"text": "橋", "markers": markers})[
        "fields"][0] == {"pitch-accent-categories": "odaka,heiban"}


def test_dictionary_content_is_escaped_and_pictures_and_links_dropped(imported):
    gloss = local_dict.answer("ankiFields", {"text": "悪", "markers": ["glossary-brief"]})[
        "fields"][0]["glossary-brief"]
    assert "<script>" not in gloss and "&lt;script&gt;" in gloss
    assert "img" not in gloss and "href" not in gloss and "iframe" not in gloss
    assert "see" in gloss and "inner" in gloss
