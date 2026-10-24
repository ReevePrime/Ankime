"""Bolding the mined word where it is, subtitle encodings and names, the study day, styles that
would escape their box, ffmpeg and Yomitan timeouts and proxies, background jobs going stale,
undo history, the batch stopping with the player and the log closing for an update."""
import datetime
import subprocess
import threading

import pytest

from ankime.cards import media, notes
from ankime.core import debuglog, immersion
from ankime.language import frequency, knowledge, sanitize, yomitan
from ankime.subs import subtitles
from ankime.core.model import Event, Token
from ankime.player.jobs import Jobs
from ankime.subs.session import SubtitleSession, token_offsets
from ankime.cards.undo import UndoHistory

from test_player import player  # noqa: F401  (a fixture)

# --- the mined word, bolded where it is ------------------------------------------------

def test_sentence_html_bolds_the_word_where_it_was():
    assert notes.sentence_html("日本人は人が", "人", at=4) == "日本人は<b>人</b>が"
    assert notes.sentence_html("日本人は人が", "人") == "日本<b>人</b>は人が"  # no position
    # a position that doesn't hold the word (the text changed): the first one
    assert notes.sentence_html("日本人は人が", "人", at=0) == "日本<b>人</b>は人が"
    assert notes.sentence_html("a<\n人", "人", at=3) == "a&lt;<br><b>人</b>"
    assert notes.sentence_html("猫", "犬") == "猫"


def test_token_offsets_follow_the_text():
    tokens = [Token("日本人", "", "日本人"), Token("は", "", "は"), Token("人", "", "人"),
              Token("?", "", ""), Token("が", "", "が")]
    assert token_offsets("日本人は 人が", tokens) == [0, 3, 5, None, 6]


def test_offsets_in_the_session():
    s = SubtitleSession()
    s.events = [Event(1, 2, "人だ"), Event(3, 4, "日本人は人が")]
    s.tokens = {"日本人は人が": [Token("日本人", "", "日本人"), Token("は", "", "は"),
                              Token("人", "", "人"), Token("が", "", "が")]}
    assert s.token_offset("日本人は人が", 2) == 4
    assert s.token_offset("日本人は人が", 9) is None and s.token_offset("x", 0) is None
    assert s.word_offset("日本人は人が", "人") == 4
    assert s.word_offset("日本人は人が", "猫") is None
    s.set_line("日本人は人が", 3, 4, "v.mkv", "")
    assert s.span_offset(0) == 0
    assert s.extend(0, 0)
    assert s.span(0).text[s.span_offset(0) + 4] == "人"  # "人だ\n" comes first


# --- subtitle files ---------------------------------------------------------------------

def test_decode_reads_euc_jp():
    text = "1\n00:00:01,000 --> 00:00:02,000\n今日はいい天気ですね。\n"
    assert subtitles.decode(text.encode("euc_jp")) == text
    assert subtitles.decode(text.encode("cp932")) == text


def test_sidecars_match_in_any_case(tmp_path):
    for name in ("Ep01.mkv", "EP01.JA.srt", "ep01.En.ass"):
        (tmp_path / name).write_text("")
    source, target = subtitles.find_sidecars(tmp_path / "Ep01.mkv", "ja", "en")
    assert (source.name, target.name) == ("EP01.JA.srt", "ep01.En.ass")


def test_japanese_iteration_marks_and_rare_kanji_are_words():
    for word in ("々", "〆", "ゝ", "ヾ", "㐂", "𠮟"):
        assert knowledge.is_word(word), word
    assert not knowledge.is_word("・") and not knowledge.is_word("abc")


# --- the study day ---------------------------------------------------------------------

def test_study_day_starts_at_the_rollover_hour():
    late = datetime.datetime(2026, 10, 23, 1, 30)
    assert immersion.study_day(late) == "2026-10-22"  # 1:30 am: still the evening before
    assert immersion.study_day(late, rollover=0) == "2026-10-23"
    assert immersion.study_day(datetime.datetime(2026, 10, 23, 4, 0)) == "2026-10-23"


def test_rollover_comes_from_the_collection():
    class Col:
        def __init__(self, value):
            self.value = value

        def get_config(self, key, default):
            return self.value if key == "rollover" else default

    assert immersion._rollover(Col(2)) == 2
    assert immersion._rollover(Col(99)) == immersion.DEFAULT_ROLLOVER
    assert immersion._rollover(Col("x")) == immersion.DEFAULT_ROLLOVER


# --- styles that would leave their box ----------------------------------------------------

def test_positioning_is_dropped_from_styles():
    html = '<span style="position:fixed;inset:0;Z-Index:9;color:red">a</span>'
    assert sanitize.clean_html(html) == '<span style="inset:0;color:red">a</span>'
    assert sanitize.clean_html('<b style="position: absolute">a</b>') == "<b>a</b>"


# --- ffmpeg and Yomitan ---------------------------------------------------------------------

def test_ffmpeg_that_hangs_is_stopped(monkeypatch):
    def hang(*args, timeout=None, **kw):
        raise subprocess.TimeoutExpired(args[0], timeout)

    monkeypatch.setattr(media.subprocess, "run", hang)
    with pytest.raises(media.FfmpegError, match="more than 7 seconds"):
        media.run_ffmpeg("-version", ffmpeg="ffmpeg", timeout=7)


def test_yomitan_ignores_proxy_settings():
    assert yomitan._SESSION.trust_env is False


def test_frequencies_are_fetched_a_few_at_a_time():
    seen, lock, both = set(), threading.Lock(), threading.Barrier(2, timeout=5)

    def rank_of(lemma):
        with lock:
            seen.add(threading.get_ident())
        both.wait()  # only passes if two lookups run at the same time
        return len(lemma)

    assert frequency.fetch(["猫", "犬犬"], rank_of, workers=2) == {"猫": 1, "犬犬": 2}
    assert len(seen) == 2


def test_frequency_fetch_raises_the_first_error():
    def rank_of(lemma):
        raise yomitan.YomitanOffline("down")

    with pytest.raises(yomitan.YomitanOffline):
        frequency.fetch(["猫", "犬"], rank_of)


def test_tokenize_many_reports_its_progress(monkeypatch):
    monkeypatch.setattr(yomitan, "_tokenize_lines", lambda lines: [[] for _ in lines])
    seen = []
    yomitan.tokenize_many(["a"] * 5, chunk=2, progress=lambda n, total: seen.append(n))
    assert seen == [0, 2, 4, 5]


# --- background jobs ----------------------------------------------------------------------

def test_jobs_drop_results_once_stale():
    pending = []
    jobs = Jobs(lambda work, done: pending.append((work, done)))
    results = []
    jobs.run(lambda: 1, lambda f: results.append(("old episode", f)), scope="episode")
    jobs.run(lambda: 2, lambda f: results.append(("lookup", f)))
    jobs.invalidate("episode")
    jobs.run(lambda: 3, lambda f: results.append(("new episode", f)), scope="episode")
    for work, done in pending[:3]:
        done(work())
    assert results == [("lookup", 2), ("new episode", 3)]
    jobs.run(lambda: 4, lambda f: results.append(("after close", f)))
    jobs.close()
    work, done = pending[3]
    done(work())
    assert len(results) == 2


# --- undo ---------------------------------------------------------------------------------

def test_undo_history_groups_a_batch_and_says_what_it_undoes():
    h = UndoHistory(depth=3)
    assert not h and h.label() == "" and h.pop() is None
    h.added(1, "食べる")
    assert h.label() == "remove the card for 食べる"
    h.start_batch()
    for i, word in enumerate("犬猫鳥魚", start=2):
        h.added(i, word, batch=True)
    assert h.label() == "remove the batch of 4 cards (犬、猫、鳥 and 1 more)"
    h.marked({"犬": False}, True)
    assert h.label() == "unmark 犬 as known"
    h.appended(9, {"Sentence": "x"}, {"Sentence": "x<br>y"}, ["y.m4a"], "猫")
    assert h.label() == "remove the sentence added to the card for 猫"
    assert [r.kind for r in h] == ["added", "marked", "appended"]  # the oldest went
    h.pop()
    h.pop()
    record = h.pop()
    assert record.data == [2, 3, 4, 5]
    h.added(10, "蛇", batch=True)  # the batch's record was undone: a new one
    assert [r.data for r in h] == [[10]]


def test_long_sentence_cards_get_a_short_label():
    h = UndoHistory()
    h.added(1, "これはとても長い字幕の行でカードになったものです")
    assert h.label() == "remove the card for これはとても長い字幕の行でカードになっ…"


# --- the batch and the player --------------------------------------------------------------

def test_the_batch_stops_when_the_player_closes(player, monkeypatch):  # noqa: F811
    mined = []

    def fake_mine(span, translation, lemma=None, on_finished=None, **_kw):
        mined.append(lemma)
        player.player = None  # the window closes while the first card is being made
        on_finished(True)

    monkeypatch.setattr(player, "_mine", fake_mine)
    player.add_words(["犬", "鳥"])
    assert mined == ["犬"] and not player._batch.queue


def test_a_card_added_after_closing_is_logged(player):  # noqa: F811
    player.player = None
    player._added(1, "犬", "犬")
    player.log.flush.assert_called_once()


def test_opening_another_video_stops_the_batch(player, monkeypatch):  # noqa: F811
    monkeypatch.setattr(player, "_mine", lambda *a, **kw: None)  # the first card is pending
    player.add_words(["犬", "鳥"])
    player._stop_batch("another video was opened")
    assert not player._batch.queue
    assert player.batch_box.setVisible.call_args[0][0] is False


# --- updating the add-on -------------------------------------------------------------------

def test_the_log_file_closes_for_an_update_and_stays_closed(tmp_path, monkeypatch):
    """Anki renames user_files/ before updating the add-on, which Windows refuses while the
    log is open; a closed handler still on the logger would open it again."""
    import logging
    monkeypatch.setattr(debuglog, "USER_FILES", tmp_path)
    monkeypatch.setattr(debuglog, "LOG_FILE", tmp_path / "ankime.log")
    logger = logging.getLogger("ankime")
    debuglog.install()
    try:
        logger.info("before")
        debuglog.uninstall()
        logger.info("after")
        (tmp_path / "ankime.log").rename(tmp_path / "moved.log")
        assert "before" in (tmp_path / "moved.log").read_text(encoding="utf-8")
        assert not (tmp_path / "ankime.log").exists()
    finally:
        debuglog.uninstall()
