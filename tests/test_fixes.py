"""Config written as differences from the defaults, the player's state file, names in i+1
lines, CSS in dictionary HTML, word lists, frequency ranks and the popup's entries."""
import json
from unittest import mock

import pytest

from ankime.core import config, state
from ankime.language import frequency, knowledge, known_words, sanitize, word_lists, yomitan
from ankime.player import popup
from ankime.core.model import Token

from test_state import make_knowledge


# --- config: only what differs from the defaults is written ------------------------

def test_strip_keeps_only_changes_and_drops_unknown_keys():
    cfg = config.merge({"deck": "Mining", "hotkeys": {"mine_line": "Ctrl+M", "next_i1": "N"}})
    cfg["auto_pause"] = True  # a setting of an older version
    assert config.strip(cfg) == {"deck": "Mining", "hotkeys": {"mine_line": "Ctrl+M"}}
    assert config.strip(config.DEFAULTS) == {}
    unbound = config.merge({"hotkeys": {"mine_line": ""}})
    assert config.strip(unbound) == {"hotkeys": {"mine_line": ""}}  # unbinding is a change


@pytest.fixture
def manager(monkeypatch, tmp_path):
    mgr = mock.MagicMock()
    monkeypatch.setattr(config, "_manager", lambda: mgr)
    monkeypatch.setattr(config, "_current", None)
    monkeypatch.setattr(config, "_listeners", [])
    monkeypatch.setattr(state, "_file", state.JsonFile(tmp_path / "state.json"))
    monkeypatch.setattr(state, "_state", None)
    return mgr


def test_loading_tidies_an_old_full_config_and_moves_the_state_out(manager):
    # what older versions wrote: every setting, and the player's state with them
    manager.getConfig.return_value = {**config.DEFAULTS, "deck": "Mining", "volume": 70,
                                      "last_video_dir": "D:/anime"}
    manager.addonMeta.return_value = {"config": manager.getConfig.return_value}
    assert config.get()["deck"] == "Mining"
    assert manager.writeConfig.call_args[0][1] == {"deck": "Mining"}
    assert state.get("volume") == 70 and state.get("last_video_dir") == "D:/anime"
    assert state.get("speed") == 1.0  # not in the old config: the default


def test_a_tidy_config_isnt_rewritten(manager):
    manager.getConfig.return_value = {**config.DEFAULTS, "deck": "Mining"}
    manager.addonMeta.return_value = {"config": {"deck": "Mining"}}
    config.get()
    manager.writeConfig.assert_not_called()


def test_saving_writes_differences_only(manager):
    manager.getConfig.return_value = dict(config.DEFAULTS)
    config.save(deck="Mining")
    config.save(deck=config.DEFAULTS["deck"])  # back to the default: follows it again
    assert manager.writeConfig.call_args[0][1] == {}


def test_edits_in_ankis_editor_are_tidied(manager):
    manager.getConfig.return_value = dict(config.DEFAULTS)
    config.install()
    (_addon, edited), _ = manager.setConfigUpdatedAction.call_args
    edited({**config.DEFAULTS, "seek_seconds": 10})
    assert manager.writeConfig.call_args[0][1] == {"seek_seconds": 10}
    assert config.get()["seek_seconds"] == 10


def test_state_falls_back_on_bad_values(manager):
    state.save(volume="loud", panel_widths={"playlist": 300})
    assert state.get("volume") == 100
    assert state.get("panel_widths") == {"playlist": 300, "transcript": 320}
    with pytest.raises(KeyError):
        state.save(nonsense=1)


# --- names and fragments don't make a line i+1 -------------------------------------

def test_tokens_without_a_dictionary_form_arent_new_words(tmp_path, monkeypatch):
    know, _ = make_knowledge([(10, "s\x1f食べる", 30, "", 2)], tmp_path, monkeypatch)
    name, ate = Token("ナルト", "", "", ()), Token("食べた", "", "食べる", ())
    line = [name, Token("が", "", "が", ()), ate]
    assert know.token_status(*name[::2], ()) == "other"
    assert know.unknown_words(line) == set()
    assert know.line_kind(line) == "known"  # was "i+1", with ナルト as its new word
    assert know.line_kind([name, Token("犬", "", "犬", ())]) == "i+1"
    know.set_marked("ナルト", True)  # marked known as written: it counts as such
    assert know.token_status("ナルト") == "known"
    assert know.token_status("。") == ""  # punctuation stays nothing


def test_a_malformed_vocab_source_is_a_setup_error(tmp_path, monkeypatch):
    cfg = config.merge({"note_type": "Mining", "word_field": "Word",
                        "vocab_sources": ["Core 2k"]})
    with pytest.raises(knowledge.SetupError, match="vocab_sources"):
        make_knowledge([], tmp_path, monkeypatch, cfg=cfg)


# --- CSS in dictionary HTML can't load anything --------------------------------------

@pytest.mark.parametrize("style", [
    "background:url(http://t/x.png)",
    "background:" + chr(92) + "75 rl(http://t/x.png)",  # a CSS escape spelling url(
    "background:image-set('http://t/x.png' 1x)",
    "background:-webkit-image-set('http://t/x.png' 1x)",
    "background:cross-fade(url(a), url(b))",
    "content:attr(x);behavior:url(x.htc)",
    "width:expression(alert(1))",
])
def test_unsafe_styles_are_dropped(style):
    assert sanitize.clean_html(f'<span style="{style}">a</span>') == "<span>a</span>"


@pytest.mark.parametrize("style", [
    "color:rgb(10, 20, 30);font-weight:bold", "margin-left:calc(1em + 2px)",
    "color:var(--accent)", "transform:translateY(-2px) scale(0.9)",
])
def test_harmless_styles_are_kept(style):
    assert sanitize.clean_html(f'<span style="{style}">a</span>') == (
        f'<span style="{style}">a</span>')


# --- word lists ------------------------------------------------------------------

def test_words_in_list():
    text = ("# my words\n食べる\n\n1\tの\n2,猫,ねこ\n<b>犬</b>[いぬ]\nhello\n食べる\n"
            "綺麗\tきれい\tpretty\n")
    assert knowledge.words_in_list(text) == ["食べる", "の", "猫", "犬", "綺麗"]


def _jpdb_card(spelling, *reviews):
    return {"vid": 1, "spelling": spelling, "reading": "",
            "reviews": [{"timestamp": day * word_lists.DAY, "grade": grade, "from_anki": False}
                        for day, grade in reviews]}


def test_jpdb_review_history_keeps_the_known_words():
    data = {"cards_vocabulary_jp_en": [
        _jpdb_card("食べる", (0, "okay"), (5, "okay"), (30, "okay")),   # passed after 25 days
        _jpdb_card("猫", (0, "okay"), (5, "easy")),                     # still young
        _jpdb_card("犬", (0, "known")),                                 # set aside as known
        _jpdb_card("鳥", (0, "okay"), (40, "something")),               # failed last time
        _jpdb_card("魚"),                                               # never reviewed
    ], "cards_vocabulary_en_jp": [_jpdb_card("猫", (0, "never-forget"))],
       "cards_kanji_keyword_char": [_jpdb_card("食", (0, "known"))]}
    found = word_lists.import_list(json.dumps(data, ensure_ascii=False), 21)
    assert found.source == "JPDB review history"
    assert found.words == ["食べる", "犬", "猫"]  # 猫 is known in the other deck
    assert found.skipped == 2  # 鳥, 魚; kanji cards aren't words


def test_migaku_exports_keep_the_known_words():
    csv_text = ("\ufeffdictForm,secondary,knownStatus\n食べる◴たべる,たべる,KNOWN\n"
                "猫,ねこ,LEARNING\n犬,いぬ,known\nhello,,KNOWN\n")
    found = word_lists.import_list(csv_text, 21)
    assert found == word_lists.WordList("Migaku word list", ["食べる", "犬"], 1)
    rows = [{"word": "鳥", "status": "KNOWN"}, {"word": "魚", "status": "IGNORED"}]
    assert word_lists.import_list(json.dumps({"words": rows}), 21).words == ["鳥"]


def test_plain_lists_still_read_every_word():
    assert word_lists.import_list("# mine\n食べる◴たべる\n1\tの\n", 21) == (
        word_lists.WordList("word list", ["食べる", "の"], 0))
    assert word_lists.import_list("rank\tword\n1\t猫\n2\t犬\n", 21).words == ["猫", "犬"]


@pytest.mark.parametrize("fmt", word_lists.EXPORT_FORMATS)
def test_every_export_format_reads_back(fmt):
    text = word_lists.export_list(["犬", "猫"], fmt, "Ankime known words")
    assert text.startswith("# ") == fmt.header
    assert word_lists.import_list(text, 21).words == ["犬", "猫"]
    assert word_lists.format_for_filter(word_lists.export_filter(fmt)) is fmt


def test_sentence_text_drops_html_furigana_and_sound():
    field = ("<div>猫が 魚[さかな]を<b>食[た]べた</b>。</div><div><ruby>犬<rt>いぬ</rt></ruby>"
             "も</div>[sound:line.mp3]<img src=\"a.webp\">")
    assert knowledge.sentence_text(field) == "猫が魚を食べた。\n犬も"
    assert knowledge.sentence_text("<br>") == ""


def test_note_sentences_skips_notes_without_the_field():
    class Col:
        notes = {1: {"Sentence": "猫だ"}, 2: {"Front": "犬"}, 3: {"Sentence": "<br>"}}

        def get_note(self, nid):
            return self.notes[nid]

    assert known_words.note_sentences(Col(), [1, 2, 3], "Sentence") == (["猫だ"], 1)


# --- frequency ranks --------------------------------------------------------------

def test_frequency_ranks_are_cached_and_sort_the_words(tmp_path):
    ranks = frequency.FrequencyRanks(tmp_path / "f.json")
    assert ranks.missing(["猫", "犬", "猫"]) == ["猫", "犬"]
    ranks.update(frequency.fetch(["猫", "犬", "鵺"], {"猫": 900, "犬": 300, "鵺": None}.get))
    again = frequency.FrequencyRanks(tmp_path / "f.json")
    assert again.missing(["猫", "犬", "鵺", "鳥"]) == ["鳥"]  # None is an answer too
    counts = [("鳥", 9), ("猫", 3), ("鵺", 5), ("犬", 1)]
    assert again.sort(counts) == [("犬", 1), ("猫", 3), ("鳥", 9), ("鵺", 5)]


def test_frequency_rank(monkeypatch):
    answers = iter([{"fields": [{"frequency-harmonic-rank": "1234"}]},
                    {"fields": [{"frequency-harmonic-rank": yomitan.NO_FREQUENCY}]},
                    {"fields": []}])
    monkeypatch.setattr(yomitan, "_post", lambda *a, **k: next(answers))
    assert [yomitan.frequency_rank("猫") for _ in range(3)] == [1234, None, None]


# --- the popup: a card from any entry ----------------------------------------------

def test_card_fields_takes_the_entry_asked_for(monkeypatch):
    calls = []

    def post(path, **body):
        calls.append(body["maxEntries"])
        return {"fields": [{"expression": "掛ける"}, {"expression": "書ける"}]}

    monkeypatch.setattr(yomitan, "_post", post)
    assert yomitan.card_fields("かける", entry=1)[0]["word"] == "書ける"
    assert calls == [2]
    assert yomitan.card_fields("かける", entry=5) is None


def test_every_entry_offers_a_card():
    entries = [{"expression": "掛ける"}, {"expression": "書ける"}, {"expression": "欠ける"}]
    out = popup.render_entries("かける", entries, duplicates=[False, True, False],
                               hotkeys={"add_word_card": "Ctrl+Shift+N"})
    assert 'data-cmd="add:0"' in out and "(Ctrl+Shift+N)" in out
    assert 'data-cmd="append:1"' in out and 'data-cmd="add:1"' in out  # you have 書ける
    assert 'data-cmd="add:2"' in out and 'data-cmd="append:2"' not in out
    assert out.count('data-cmd="known"') == 1 and out.count('data-cmd="audio"') == 1
    assert out.count("(Ctrl+Shift+N)") == 1  # the hotkey is the first entry's


def test_a_self_closing_dropped_tag_keeps_what_follows():
    assert sanitize.clean_html("<b>one</b><svg/><i>two</i> three") == (
        "<b>one</b><i>two</i> three")
    assert sanitize.clean_html("a<svg><b>x</b></svg>b") == "ab"


def test_clearing_frequency_ranks_forgets_them_on_disk(tmp_path):
    ranks = frequency.FrequencyRanks(tmp_path / "f.json")
    ranks.update({"猫": 10, "犬": None})
    ranks.clear()
    assert ranks.missing(["猫"]) == ["猫"]
    assert frequency.FrequencyRanks(tmp_path / "f.json").ranks == {}


def test_learning_style_of_older_versions_becomes_an_underline(manager):
    manager.getConfig.return_value = {"learning_style": "dotted", "deck": "Mining"}
    manager.addonMeta.return_value = {"config": manager.getConfig.return_value}
    assert config.get()["learning_word_style"]["underline"] == "dotted"
    assert config.get()["learning_word_style"]["color"] == ""  # the rest: the defaults
    assert manager.writeConfig.call_args[0][1] == {
        "deck": "Mining", "learning_word_style": {"underline": "dotted"}}
    assert config.migrate({"learning_style": "color"}) == {}
    assert config.migrate({"learning_style": "underline",
                           "learning_word_style": {"underline": "wavy"}}) == {
        "learning_word_style": {"underline": "wavy"}}  # set since: kept
