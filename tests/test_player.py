"""The player's mixins on a fake mpv: the line filter and the i+1 pause, the seek bar's marks,
a batch of word cards and undo. Qt is stubbed (conftest.py), so only the logic that wires
the parts together is under test, not the widgets."""
from types import MappingProxyType
from unittest import mock

import pytest

from ankime.cards import notes
from ankime.core import config
from ankime.core import immersion
from ankime.core.model import Event, Token
from ankime.player import cards
from ankime.player.cards import CardsMixin
from ankime.player.i1 import LineFilterMixin
from ankime.player.transport import TransportMixin
from ankime.subs.session import SubtitleSession

from test_state import FakeCol, make_knowledge


class FakePlayer:
    """The MpvController properties and commands the mixins use."""

    def __init__(self):
        self.time_pos, self.paused, self.sub_delay = 0.0, False, 0.0
        self.duration, self.path = 100.0, "v.mkv"
        self.seeks = []

    def seek(self, t):
        self.seeks.append(round(t, 2))
        self.time_pos = t


class Player(TransportMixin, LineFilterMixin, CardsMixin):
    """The parts of PlayerWindow under test, with the rest faked."""

    def __init__(self, know):
        self.player, self.knowledge = FakePlayer(), know
        self.session = SubtitleSession()
        self._video, self._loop = "v.mkv", None
        self.log, self.panel, self.transcript = mock.MagicMock(), mock.MagicMock(), mock.MagicMock()
        self.seek_slider = mock.MagicMock()
        self.batch_label, self.batch_box = mock.MagicMock(), mock.MagicMock()
        self.restyled = 0
        self._init_line_filter()
        self._init_cards()

    def _has_video(self):
        return True

    def _restyle(self):
        self.restyled += 1

    def refresh_knowledge(self):
        pass

    def _hide_popup(self):
        pass

    def _want_translation(self):
        return False

    def _relabel_buttons(self):
        pass


DOG, ATE, BIRD = Token("犬", "", "犬", ()), Token("食べた", "", "食べる", ()), Token("鳥", "", "鳥", ())


@pytest.fixture
def player(tmp_path, monkeypatch):
    # 食べる is known; 犬 and 鳥 are new
    know, _ = make_knowledge([(10, "s\x1f食べる", 30, "", 2)], tmp_path, monkeypatch)
    p = Player(know)
    p.session.events = [Event(1, 2, "食べた"), Event(3, 4, "食べた!"), Event(5, 6, "犬"),
                        Event(7, 8, "鳥と犬")]
    p.session.tokens = {"食べた": [ATE], "食べた!": [ATE, Token("!", "", "", ())], "犬": [DOG],
                        "鳥と犬": [BIRD, Token("と", "", "と", ()), DOG]}
    return p


def settings(monkeypatch, **values):
    monkeypatch.setattr(config, "_current", MappingProxyType(config.merge(values)))


# --- the line filter and the i+1 pause ------------------------------------------------

def test_skip_known_jumps_to_the_next_line_with_a_new_word(player, monkeypatch):
    settings(monkeypatch, line_filter="skip_known")
    player.session.set_line("食べた", 1, 2, "v.mkv", "")
    player.player.time_pos = 1.1
    player._on_line_shown()
    assert player.player.seeks == [5.02]


def test_only_i1_leaves_you_alone_while_paused(player, monkeypatch):
    settings(monkeypatch, line_filter="only_i1")
    player.player.paused = True
    player.session.set_line("鳥と犬", 7, 8, "v.mkv", "")
    player._on_line_shown()
    assert player.player.seeks == []  # browsing: stay on the line


def test_pause_before_an_i1_line_goes_back_to_its_start(player, monkeypatch):
    settings(monkeypatch, i1_pause="before")
    player.session.set_line("犬", 5, 6, "v.mkv", "")
    player.player.time_pos = 5.1
    player._on_line_shown()
    assert player.player.paused and player.player.seeks == [5]


def test_pause_after_an_i1_line_waits_for_its_end(player, monkeypatch):
    settings(monkeypatch, i1_pause="after")
    player.session.set_line("犬", 5, 6, "v.mkv", "")
    player.player.time_pos = 5.1
    player._on_line_shown()
    player._check_line_pause(5.5)
    assert not player.player.paused
    player._check_line_pause(5.97)
    assert player.player.paused


def test_next_i1(player):
    player.player.time_pos = 0.5
    player.next_i1()
    player.next_i1()  # from 5.02 the line at 5 is behind, and 鳥と犬 has two new words
    assert player.player.seeks == [5.02]
    player.knowledge.set_marked("犬", True)
    player.next_i1()
    assert player.player.seeks == [5.02, 7.02]


# --- the seek bar's marks --------------------------------------------------------------

def test_seek_marks_follow_the_delay_and_the_cards_added(player):
    player.player.sub_delay = 0.5
    player._update_seek_marks()
    assert player.seek_slider.set_marks.call_args[0][0] == [(55.0, "i+1")]
    player._added(1, "犬", "犬", origin=("v.mkv", 5.5))
    player._added(2, "猫", "猫", origin=("other.mkv", 9.0))  # another video's
    # with a card for 犬, its line is known and 鳥と犬 is the i+1 line
    assert player.seek_slider.set_marks.call_args[0][0] == [(75.0, "i+1"), (55.0, "mined")]


# --- a batch of word cards, and undo ---------------------------------------------------

def test_a_batch_is_one_undo_record(player, monkeypatch):
    mined = []

    def fake_mine(span, translation, lemma=None, surface=None, on_finished=None,
                  in_batch=False, **_kw):
        mined.append((lemma, span.text))
        player._added(100 + len(mined), lemma, span.text, in_batch, ("v.mkv", span.start))
        on_finished(True)

    monkeypatch.setattr(player, "_mine", fake_mine)
    player.log.add_cards.return_value = "2026-10-22"
    player.add_words(["犬", "鳥"])
    # 犬 from its i+1 line; 鳥's line is i+1 too once 犬 has a card
    assert mined == [("犬", "犬"), ("鳥", "鳥と犬")]
    assert [(r.kind, r.data) for r in player._undo] == [("added", [101, 102])]
    assert player._undo_tip().startswith("Undo: remove the batch of 2 cards (犬、鳥)")
    assert player.batch_box.setVisible.call_args[0][0] is False  # done: hidden

    removed = []
    monkeypatch.setattr(notes, "remove_notes",
                        lambda _parent, ids, on_done: (removed.append(ids), on_done()))
    player.undo_last_card()
    assert removed == [[101, 102]]
    assert player._mined == {}
    player.log.add_cards.assert_called_with(-2, day="2026-10-22")


def test_extracting_the_episode_adds_a_card_per_line_as_one_undo_record(player, monkeypatch):
    mined = []

    def fake_mine(span, translation, surface=None, on_finished=None, in_batch=False,
                  surface_at=None, **_kw):
        mined.append((span.text, surface, surface_at))
        player._added(100 + len(mined), None, span.text, in_batch, ("v.mkv", span.start))
        on_finished(True)

    monkeypatch.setattr(player, "_mine", fake_mine)
    monkeypatch.setattr(cards, "askUser", lambda *_a, **_kw: True)
    player.player.sub_delay = 0.5
    assert player.lines_to_extract("all") == [0, 1, 2, 3]
    assert player.lines_to_extract("new") == [2, 3]
    player._added(1, None, "犬", origin=("v.mkv", 5.5))  # its line was added already
    assert player.lines_to_extract("i1") == []
    player._line_cards.clear()
    player.extract_lines("new")
    # the new word of the i+1 line is bolded; 鳥と犬 has two, so none is
    assert mined == [("犬", "犬", 0), ("鳥と犬", None, None)]
    assert [(r.kind, r.data) for r in player._undo][-1] == ("added", [101, 102])
    assert player.batch_box.setVisible.call_args[0][0] is False  # done: hidden
    assert player._extraction == []


def test_stopping_an_extraction_drops_the_lines_left(player, monkeypatch):
    pending = []
    monkeypatch.setattr(player, "_mine",
                        lambda span, *_a, on_finished=None, **_kw: pending.append(on_finished))
    monkeypatch.setattr(cards, "askUser", lambda *_a, **_kw: True)
    player.extract_lines("all")
    assert len(pending) == 1
    player.add_words(["犬"])  # refused while lines are being added
    assert player._batch.queue == []
    player.cancel_batch()
    assert player._extraction == []
    pending[0](True)  # the card being made when it stopped: nothing more is started
    assert len(pending) == 1


def test_undo_takes_a_card_off_the_day_it_was_added(player, monkeypatch):
    day = ["2026-10-21"]
    player.log = immersion.ImmersionLog(FakeCol(), today=lambda: day[0], device="me")
    player._added(1, None, "犬", origin=("v.mkv", 5.0))
    day[0] = "2026-10-22"  # the study day rolled over before the undo
    player._added(2, None, "鳥と犬", origin=("v.mkv", 7.0))
    monkeypatch.setattr(notes, "remove_notes",
                        lambda _parent, ids, on_done: on_done())
    player.undo_last_card()
    player.undo_last_card()
    assert player.log.days == {"2026-10-21": [0.0, 0], "2026-10-22": [0.0, 0]}


def test_adding_a_line_twice_asks_first(player, monkeypatch):
    mined = []
    monkeypatch.setattr(player, "_mine", lambda span, *_a, **_kw: mined.append(span.text))
    ask = mock.MagicMock(return_value=False)
    monkeypatch.setattr(cards, "askUser", ask)
    player._added(1, "犬", "犬", origin=("v.mkv", 5.0))  # a word card from it: not the same
    player.mine_event(2)
    assert mined == ["犬"] and not ask.called
    player._added(2, None, "犬", origin=("v.mkv", 5.0))  # its sentence card
    player.mine_event(2)  # declined
    player.mine_event(3)  # another line
    assert mined == ["犬", "鳥と犬"] and ask.call_count == 1
    ask.return_value = True
    player.mine_event(2)
    assert mined == ["犬", "鳥と犬", "犬"]


def test_undo_marking_words_known(player):
    player._marked(["犬", "鳥"], True)
    assert player.knowledge.is_marked("犬")
    player.undo_last_card()
    assert not player.knowledge.is_marked("犬") and not player.knowledge.is_marked("鳥")
    assert not player._undo
