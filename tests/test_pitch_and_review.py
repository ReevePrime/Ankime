"""Pitch-accent colouring of the subtitles (language/pitch.py, the panel, the player's fetch)
and reviewing a card in Anki's Add window before adding it (notes.review_card)."""
from types import MappingProxyType
from unittest import mock

import aqt
import pytest

from ankime.cards import notes
from ankime.core import config
from ankime.core.model import Token
from ankime.language import pitch, yomitan
from ankime.player import sub_panel
from ankime.player.pitch_accent import PitchAccentMixin
from ankime.subs.session import SubtitleSession

from test_notes import CFG, FakeMedia, FakeNote, FIELDS

# --- categories ---------------------------------------------------------------------------


@pytest.mark.parametrize("reading, position, verb, expected", [
    ("さくら", 0, False, "heiban"),
    ("いのち", 1, False, "atamadaka"),
    ("きょうしつ", 2, False, "nakadaka"),  # きょ・う・し・つ: the small ょ is no mora
    ("はし", 2, False, "odaka"),
    ("たかい", 2, True, "kifuku"),
    ("たべる", 0, True, "heiban"),
    ("はし", 3, False, ""),       # past the end
    ("はし", None, False, ""),
    ("はし", True, False, ""),
])
def test_category_from_the_position(reading, position, verb, expected):
    assert pitch.category(reading, position, verb) == expected


def test_verbs_and_adjectives_by_their_rules():
    assert pitch.is_verb_or_adjective("v5 vt")
    assert pitch.is_verb_or_adjective("adj-i")
    assert pitch.is_verb_or_adjective("v5r")
    assert not pitch.is_verb_or_adjective("adj-na")
    assert not pitch.is_verb_or_adjective("")


def test_first_category_skips_what_it_doesnt_know():
    assert pitch.first_category("odaka,heiban") == "odaka"
    assert pitch.first_category(" <b>x</b>, nakadaka") == "nakadaka"
    assert pitch.first_category("") == pitch.first_category(None) == ""


def test_token_category_takes_the_entry_with_the_tokens_reading():
    entries = [{"expression": "今日", "reading": "こんにち", pitch.MARKER: "odaka"},
               {"expression": "今日", "reading": "きょう", pitch.MARKER: "atamadaka"}]
    kyou = Token("今日", (("今日", "きょう"),), "今日")
    assert pitch.token_category(kyou, entries) == "atamadaka"
    # an inflected word: the entry of its dictionary form
    tabeta = Token("食べた", (("食", "た"), ("べた", "")), "食べる")
    assert pitch.token_category(tabeta, [{"expression": "食べる", pitch.MARKER: "kifuku"}]) == (
        "kifuku")
    assert pitch.token_category(tabeta, []) == ""
    assert pitch.token_category(tabeta, [{"expression": "食べる"}]) == ""


# --- the panel ------------------------------------------------------------------------------

def test_render_sub_marks_the_words_pitch():
    tokens = [Token("橋", "", "橋"), Token("\n", "", ""), Token("を", "", "を"),
              Token("渡る", "", "渡る")]
    out = sub_panel.render_sub("x", tokens, lambda *_: "new", ["odaka", "", "bogus"])
    assert 'class="w new p-odaka"' in out
    assert out.count("p-") == 1  # nothing for none, nor for a category it doesn't know


def test_word_pitches_skip_line_breaks():
    tokens = [Token("橋", "", "橋"), Token("\n", "", ""), Token("を", "", "を")]
    assert sub_panel.word_pitches(tokens, lambda t: t.surface) == ["橋", "を"]


def test_pitch_css_lets_only_colours_in():
    css = sub_panel.pitch_css({"heiban": "#112233", "odaka": "red; } body { x: y",
                               "nope": "#000000", "kifuku": ""})
    assert css == "html body .w.p-heiban { --pitch: #112233 !important; }"
    assert sub_panel.pitch_css(None) == ""


def test_page_has_the_pitch_rules_and_no_placeholders():
    for marker in ("$PITCH_CSS", "$PITCHED", "PITCH_VALUES", "PITCH_CLASSES"):
        assert marker not in sub_panel.PAGE
    assert ".w.p-heiban { --pitch: #1864ab; }" in sub_panel.PAGE
    assert "body.pitch-line .w:is(.p-heiban, .p-atamadaka" in sub_panel.PAGE


def test_every_category_has_colours_and_the_defaults_have_a_setting():
    assert [name for name, _ in pitch.CATEGORIES] == list(sub_panel.PITCH_COLORS)
    assert set(config.DEFAULTS["pitch_colors"]) == set(sub_panel.PITCH_COLORS)
    assert config.DEFAULTS["pitch_display"] in dict(sub_panel.PITCH_DISPLAY)


# --- the player's fetch ---------------------------------------------------------------------

class SyncJobs:
    def run(self, work, done, scope=None):
        future = mock.MagicMock()
        try:
            value = work()
            future.result.return_value = value
        except Exception as e:
            future.result.side_effect = e
        done(future)


class Player(PitchAccentMixin):
    def __init__(self):
        self.session = SubtitleSession()
        self.panel, self.player = mock.MagicMock(), mock.MagicMock(sub_delay=0.0)
        self.knowledge = mock.MagicMock()
        self.knowledge.token_status.return_value = "new"
        self._jobs = SyncJobs()
        self.failed = []
        self._init_pitch_accent()

    def _offline_tokens(self, text):
        return None

    def _yomitan_waiting(self):
        return False

    def _yomitan_failed(self, error):
        self.failed.append(error)
        return isinstance(error, yomitan.YomitanOffline)


@pytest.fixture
def pitch_on(monkeypatch):
    monkeypatch.setattr(config, "get", lambda: MappingProxyType(
        config.merge({"pitch_display": "color"})))


def _show(player, text, tokens, start=1.0):
    from ankime.core.model import Event, Line
    player.session.tokens[text] = tokens
    player.session.line = Line(text, start, start + 1, "v.mkv")
    player.session.events = [Event(start, start + 1, text)]


def test_the_lines_words_are_looked_up_once_and_coloured(pitch_on, monkeypatch):
    calls = []

    def lookup(word):
        calls.append(word)
        return [{"expression": word, pitch.MARKER: {"橋": "odaka"}.get(word, "heiban")}]

    monkeypatch.setattr(yomitan, "lookup", lookup)
    player = Player()
    tokens = [Token("橋", "", "橋"), Token("を", "", "を"), Token("。", "", "")]
    _show(player, "橋を。", tokens)
    player._fetch_pitches()
    assert sorted(calls) == ["を", "橋"]  # not the punctuation
    assert player.panel.restyle.call_args[0][1] == ["odaka", "heiban", ""]
    assert player.session.lookups.get("橋")  # the popup opens at once now
    player._fetch_pitches()
    assert len(calls) == 2


def test_a_popup_lookup_is_reused(pitch_on, monkeypatch):
    monkeypatch.setattr(yomitan, "lookup", mock.MagicMock(side_effect=AssertionError))
    player = Player()
    player.session.lookups.put("橋", [{"expression": "橋", pitch.MARKER: "odaka"}])
    _show(player, "橋", [Token("橋", "", "橋")])
    player._fetch_pitches()
    assert player.session.pitches == {("橋", "橋"): "odaka"}


def test_offline_leaves_the_words_to_fetch_later(pitch_on, monkeypatch):
    monkeypatch.setattr(yomitan, "lookup", mock.MagicMock(side_effect=yomitan.YomitanOffline("x")))
    player = Player()
    _show(player, "橋", [Token("橋", "", "橋")])
    player._fetch_pitches()
    assert player.session.pitches == {} and len(player.failed) == 1
    assert not player._fetching_pitches


def test_nothing_is_fetched_with_the_setting_off(monkeypatch):
    monkeypatch.setattr(yomitan, "lookup", mock.MagicMock(side_effect=AssertionError))
    player = Player()
    _show(player, "橋", [Token("橋", "", "橋")])
    player._fetch_pitches()
    assert player._pitch_classes([Token("橋", "", "橋")]) == []


# --- reviewing a card in the Add window -----------------------------------------------------

class Note(FakeNote):
    @property
    def fields(self):
        return list(self.values())


@pytest.fixture
def col(monkeypatch):
    fake = mock.MagicMock()
    fake.col.media = FakeMedia()
    fake.col.new_note.side_effect = lambda _model: Note.fromkeys(FIELDS, "")
    fake.col.decks.id_for_name.return_value = 7
    monkeypatch.setattr(notes, "mw", fake)
    monkeypatch.setattr(notes, "showWarning", mock.MagicMock())
    monkeypatch.setattr(notes, "_REVIEW", notes._Review())
    monkeypatch.setattr(config, "get", lambda: CFG)
    return fake.col


@pytest.fixture
def add_window(monkeypatch):
    window = mock.MagicMock()
    window.editor.note.fields = ["", ""]
    dialogs = mock.MagicMock(_dialogs={"AddCards": [None, None]})
    dialogs.open.return_value = window
    monkeypatch.setattr(aqt, "dialogs", dialogs, raising=False)
    return window


def _review(tmp_path, **kwargs):
    for name in ("a.webp", "a.m4a"):
        (tmp_path / name).write_bytes(b"x")
    notes.review_card(None, "猫だ", tmp_path / "a.webp", tmp_path / "a.m4a", **kwargs)


def test_review_fills_the_add_window_instead_of_adding(col, add_window, tmp_path, monkeypatch):
    monkeypatch.setattr(notes, "add_note", mock.MagicMock(side_effect=AssertionError))
    added, finished = [], []
    _review(tmp_path, on_added=added.append, on_finished=finished.append)
    note, deck_id = add_window.set_note.call_args[0]
    assert note["Sentence"] == "猫だ" and note["Audio"] == "[sound:a.m4a]" and deck_id == 7
    assert finished == [True] and added == []
    assert not (tmp_path / "a.m4a").exists()  # the scratch file: the collection has a copy
    note.id = 42
    notes._REVIEW._added(note)
    assert added == [42]
    notes._REVIEW._added(note)  # another note added from the window later: not ours
    assert added == [42]


def test_closing_the_window_without_adding_takes_the_media_back(col, add_window, tmp_path):
    _review(tmp_path)
    closed = add_window.destroyed.connect.call_args[0][0]
    closed()
    assert sorted(col.media.trashed) == ["a.m4a", "a.webp"]
    assert not notes._REVIEW.waiting()


def test_a_card_waiting_in_the_window_is_replaced_only_if_you_say_so(col, add_window,
                                                                     tmp_path, monkeypatch):
    _review(tmp_path)
    ask = mock.MagicMock(return_value=False)
    monkeypatch.setattr(notes, "askUser", ask)
    finished = []
    _review(tmp_path, on_finished=finished.append)
    assert finished == [False] and add_window.set_note.call_count == 1
    assert not col.media.trashed
    ask.return_value = True
    _review(tmp_path, on_finished=finished.append)
    assert finished == [False, True] and add_window.set_note.call_count == 2
    # the same file names were written again for the new card: nothing to take back
    assert col.media.trashed == []


def test_a_card_youre_writing_in_the_window_is_not_overwritten(col, add_window, tmp_path,
                                                               monkeypatch):
    add_window.editor.note.fields = ["my own card", ""]
    aqt.dialogs._dialogs["AddCards"] = [None, add_window]
    monkeypatch.setattr(notes, "askUser", mock.MagicMock(return_value=False))
    _review(tmp_path)
    assert not add_window.set_note.called and col.media.files == set()
