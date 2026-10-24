"""Field presets, the sentence with furigana and the context line."""
from unittest import mock

from ankime.cards import notes, presets
from ankime.core import config
from ankime.subs import session
from ankime.core.model import Event, Token

NIHONGO = [Token("日本語", (("日本語", "にほんご"),), "日本語"), Token("を", "", "を"),
           Token("勉強", (("勉強", "べんきょう"),), "勉強"), Token("する", "", "する")]


def test_furigana_puts_readings_after_their_kanji_with_a_space_between_words():
    text = "日本語を勉強する"
    assert notes.sentence_furigana(text, NIHONGO) == "日本語[にほんご]を 勉強[べんきょう]する"
    assert notes.sentence_furigana(text, []) == text  # not analysed yet


def test_furigana_splits_okurigana_and_keeps_kana_readings_out():
    tokens = [Token("食べた", (("食", "た"), ("べた", "")), "食べる"),
              Token("カメラ", (("カメラ", "カメラ"),), "カメラ")]
    assert notes.sentence_furigana("食べたカメラ", tokens) == "食[た]べたカメラ"


def test_furigana_after_a_space_or_a_kanji_word_still_gets_its_own_space():
    tokens = [Token("日本", (("日本", "にほん"),), "日本"), Token("語", (("語", "ご"),), "語"),
              Token(" ", "", ""), Token("猫", (("猫", "ねこ"),), "猫")]
    # the filter eats one space before a reading: the one in the line stays
    assert notes.sentence_furigana("日本語 猫", tokens) == "日本[にほん] 語[ご]  猫[ねこ]"


def test_furigana_bolds_the_word_and_escapes_and_breaks_lines():
    text = "日本語を\n勉強する<"
    tokens = [*NIHONGO[:2], Token("\n", "", ""), *NIHONGO[2:], Token("<", "", "")]
    assert notes.sentence_furigana(text, tokens, bold="勉強", at=5) == \
        "日本語[にほんご]を<br><b>勉強[べんきょう]</b>する&lt;"
    # a bold cutting into a kanji group takes all of it
    assert notes.sentence_furigana("日本語を", NIHONGO, bold="本") == "<b>日本語[にほんご]</b>を"
    assert notes.sentence_furigana("日本語を勉強", NIHONGO, bold="を") == \
        "日本語[にほんご]<b>を</b>勉強[べんきょう]"


def test_session_tokens_for_several_lines_and_the_line_before():
    s = session.SubtitleSession()
    s.events = [Event(1.0, 2.0, "日本語を"), Event(3.0, 4.0, "勉強する"), Event(5.0, 6.0, "三")]
    s.tokens = {"日本語を": NIHONGO[:2], "勉強する": NIHONGO[2:]}
    tokens = s.tokens_for("日本語を\n勉強する")
    assert notes.sentence_furigana("日本語を\n勉強する", tokens) == \
        "日本語[にほんご]を<br>勉強[べんきょう]する"
    assert s.tokens_for("三") == []
    assert s.line_before(3.0, 0) == "日本語を"
    assert s.line_before(4.0, 1.0) == "日本語を"  # video time: the delay counts
    assert s.line_before(1.0, 0) == ""


LAPIS = ["Expression", "ExpressionFurigana", "ExpressionReading", "ExpressionAudio",
         "SelectionText", "MainDefinition", "DefinitionPicture", "Sentence",
         "SentenceFurigana", "SentenceAudio", "Picture", "Glossary", "Hint",
         "IsWordAndSentenceCard", "IsClickCard", "IsSentenceCard", "IsAudioCard",
         "PitchPosition", "PitchCategories", "Frequency", "FreqSort", "MiscInfo"]
KIKU = [*LAPIS, "SentenceTranslation", "RelatedExpression"]


def test_presets_fill_every_field_key_and_blank_the_rest():
    found = presets.fields_for("Lapis", LAPIS)
    assert set(found) == set(presets.FIELD_KEYS) <= set(config.DEFAULTS)
    assert found["word_field"] == "Expression" and found["frequency_field"] == "FreqSort"
    assert found["pitch_field"] == found["context_field"] == found["key_field"] == ""
    assert presets.missing("Lapis", LAPIS) == []
    # case doesn't matter, a field the note type lacks is blank
    found = presets.fields_for("Kaishi 1.5k", ["word", "sentence", "picture"])
    assert found["word_field"] == "word" and found["audio_field"] == ""
    assert "Sentence Audio" in presets.missing("Kaishi 1.5k", ["word", "sentence"])


def test_presets_are_recognised_by_their_fields():
    assert presets.detect(LAPIS) == "Lapis"
    assert presets.detect(KIKU) == "Kiku"
    assert presets.detect(["Front", "Back"]) is None
    assert presets.word_field(["Word", "Sentence", "Word Meaning", "Sentence Meaning",
                               "Word Audio", "Sentence Audio", "Picture", "Notes",
                               "Sentence Furigana", "Word Furigana"]) == "Word"
    assert presets.word_field(["Front", "Back"]) == ""


class Note(dict):
    tags = []


def test_a_card_gets_furigana_context_and_the_key(monkeypatch, tmp_path):
    fields = ["Key", "Word", "Sentence", "SentenceReading", "Context"]
    cfg = config.merge({"note_type": "JPMN", "deck": "Mining", "sentence_field": "Sentence",
                        "sentence_furigana_field": "SentenceReading",
                        "context_field": "Context", "word_field": "Word", "key_field": "Key",
                        "definition_field": ""})
    fake = mock.MagicMock()
    fake.col.new_note.side_effect = lambda _model: Note.fromkeys(fields, "")
    monkeypatch.setattr(notes, "mw", fake)
    monkeypatch.setattr(config, "get", lambda: cfg)
    note, _deck, _written = notes._build_note(
        "日本語を勉強する", tmp_path / "a.webp", tmp_path / "a.m4a",
        ({"word": "勉強"}, []), "", "勉強", None, tokens=NIHONGO, context="前の行")
    assert note == {"Key": "勉強", "Word": "勉強", "Sentence": "日本語を<b>勉強</b>する",
                    "SentenceReading": "日本語[にほんご]を<b>勉強[べんきょう]</b>する",
                    "Context": "前の行"}
