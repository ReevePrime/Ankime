"""Jumping from i+1 line to i+1 line, sorting the playlist by coverage and searching the
library of analysed episodes."""
from unittest import mock

import pytest

from ankime.language import line_filter
from ankime.subs import library, session
from ankime.subs.playlist import Playlist

from test_player import Player, settings
from test_player import player as player  # noqa: F401 (the fixture)
from test_state import know as know  # noqa: F401 (the fixture)


# --- i+1 jump -----------------------------------------------------------------------

def test_i1_jump_only_lets_i1_lines_play():
    assert line_filter.wanted("i+1", "i1_jump")
    assert not line_filter.wanted("known", "i1_jump")
    assert not line_filter.wanted(None, "i1_jump")


def test_jump_target():
    spans = [(10, 12), (20, 22)]
    assert line_filter.jump_target(spans, 0) == 10
    assert line_filter.jump_target(spans, 9.8) is None    # just before: arriving
    assert line_filter.jump_target(spans, 11) is None
    assert line_filter.jump_target(spans, 12.3) is None   # its audio's tail
    assert line_filter.jump_target(spans, 13) == 20
    assert line_filter.jump_target(spans, 23) == "end"
    assert line_filter.jump_target([], 0) == "end"


class JumpPlayer(Player):
    def __init__(self, know):
        super().__init__(know)
        self.playlist = Playlist()
        self.playlist.paths, self.playlist.index = ["v.mkv", "w.mkv"], 0
        self.next_video = mock.MagicMock()


@pytest.fixture
def jumper(player):  # noqa: F811
    p = JumpPlayer(player.knowledge)
    p.session = player.session
    return p


def test_jump_goes_from_i1_line_to_i1_line_then_the_next_episode(jumper, monkeypatch):
    settings(monkeypatch, line_filter="i1_jump")
    # 犬 (5-6) is the only i+1 line: 鳥と犬 has two new words
    jumper._check_line_jump(0.5)
    assert jumper.player.seeks == [5.02]
    jumper._check_line_jump(5.5)
    jumper._check_line_jump(6.3)  # its tail
    assert jumper.player.seeks == [5.02]
    jumper._check_line_jump(6.6)
    jumper.next_video.assert_called_once()


def test_jump_pauses_after_the_last_episode(jumper, monkeypatch):
    settings(monkeypatch, line_filter="i1_jump")
    jumper.playlist.index = 1
    jumper._check_line_jump(7)
    assert jumper.player.paused and not jumper.next_video.called


def test_jump_waits_for_the_analysis_and_leaves_loops_and_other_modes_alone(jumper,
                                                                             monkeypatch):
    settings(monkeypatch, line_filter="only_i1")
    jumper._check_line_jump(0.5)
    settings(monkeypatch, line_filter="i1_jump")
    jumper._loop = (1, 2)
    jumper._check_line_jump(0.5)
    jumper._loop = None
    jumper.session.events = []
    jumper._check_line_jump(0.5)
    assert jumper.player.seeks == []


def test_a_line_you_chose_plays_whatever_the_filter(jumper, monkeypatch):
    settings(monkeypatch, line_filter="i1_jump")
    jumper._choose_line(1, 2)  # 食べた: known
    jumper.session.set_line("食べた", 1, 2, "v.mkv", "")
    jumper.player.time_pos = 1.1
    jumper._on_line_shown()
    jumper._check_line_jump(1.5)
    assert jumper.player.seeks == []
    jumper._check_line_jump(3)  # past it: on to the next i+1 line
    assert jumper.player.seeks == [5.02]


# --- sorting the playlist -------------------------------------------------------------

def test_sort_keeps_the_video_playing():
    p = Playlist()
    p.paths, p.index = ["a", "b", "c", "d"], 1
    score = {"a": 0.5, "c": 0.9}
    p.sort(lambda path: (path not in score, -score.get(path, 0)))
    assert p.paths == ["c", "a", "b", "d"]  # unscored last, in their order
    assert p.current == "b"


# --- the library -----------------------------------------------------------------------

SRT = ("1\n00:00:01,000 --> 00:00:02,000\n犬だ\n\n"
       "2\n00:01:05,000 --> 00:01:06,000\n大きな犬と鳥\n\n"
       "3\n00:02:00,000 --> 00:02:01,000\n食べた\n")
TOKENS = {"犬だ": [("犬", "", "犬", ()), ("だ", "", "だ", ())],
          "大きな犬と鳥": [("大きな", "", "大きな", ()), ("犬", "", "犬", ()),
                     ("と", "", "と", ()), ("鳥", "", "鳥", ())],
          "食べた": [("食べた", "", "食べる", ())]}


@pytest.fixture
def library_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(session, "TOKEN_CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(library, "_words", {})
    sub = tmp_path / "Show" / "ep01.ja.srt"
    sub.parent.mkdir()
    sub.write_text(SRT, encoding="utf-8")
    session.tokenize_file(sub, 20, lambda texts: [TOKENS[t] for t in texts],
                          str(tmp_path / "Show" / "ep01.mkv"))
    return tmp_path


def test_episodes_are_indexed_with_their_video(library_dir):
    [(cache, video, sub)] = session.cached_episodes()
    assert cache.is_file() and video.endswith("ep01.mkv") and sub.endswith("ep01.ja.srt")
    session.tokenize_file(library_dir / "Show" / "ep01.ja.srt", 20, lambda texts: 1 / 0)
    assert len(session.cached_episodes()) == 1  # no video: the index is left as it was


def test_a_pruned_cache_file_leaves_the_index(library_dir):
    [(cache, _, _)] = session.cached_episodes()
    cache.unlink()
    assert session.cached_episodes() == []


def test_clearing_the_cache_keeps_nothing(library_dir):
    session.clear_token_cache()
    assert session.cached_episodes() == []


def test_search_by_dictionary_form_or_as_written(library_dir):
    hits, more = library.search("犬")
    assert not more
    assert sorted((h.text, h.start) for h in hits) == [("大きな犬と鳥", 65.0), ("犬だ", 1.0)]
    assert library.show_name(hits[0].video) == "Show"
    assert [h.text for h in library.search("食べる")[0]] == ["食べた"]
    assert [h.text for h in library.search("べた")[0]] == ["食べた"]
    assert library.search("猫") == ([], False)
    assert library.search(" ") == ([], False)
    assert len(library.search("犬", limit=1)[0]) == 1 and library.search("犬", limit=1)[1]


def test_search_without_the_subtitle_file_has_no_times(library_dir):
    (library_dir / "Show" / "ep01.ja.srt").unlink()
    assert {h.start for h in library.search("犬")[0]} == {None}


def test_rank_puts_i1_lines_first(know):  # noqa: F811
    hits = [library.Hit("v", "大きな犬と鳥", TOKENS["大きな犬と鳥"], 1),
            library.Hit("v", "食べた", TOKENS["食べた"], 2),
            library.Hit("v", "犬だ", TOKENS["犬だ"], 3)]
    assert [(h.text, kind) for h, kind in library.rank(hits, know)] == [
        ("犬だ", "i+1"), ("食べた", "known"), ("大きな犬と鳥", "harder")]
