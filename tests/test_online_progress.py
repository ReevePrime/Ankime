"""Videos from the web (yt-dlp) and the known words over time."""
import datetime
import json

from ankime.cards import media
from ankime.core import store
from ankime.dialogs import immersion as log_dialog
from ankime.language import knowledge, progress
from ankime.subs import online
from ankime.subs.playlist import Playlist

from test_state import CFG, MODEL, FakeCol

URL = "https://www.youtube.com/watch?v=abc"


# --- web videos -----------------------------------------------------------------

def test_urls_are_told_from_files():
    assert online.is_url(URL) and online.is_url("HTTP://example.com/v.mp4")
    assert not online.is_url(r"C:\videos\ep1.mkv") and not online.is_url("/home/me/ep1.mkv")
    assert not online.is_url("https://") and not online.is_url(None)


def test_web_video_names():
    assert online.display_name(URL) == "www.youtube.com/watch?v=abc"
    assert online.display_name(URL, "猫の動画") == "猫の動画"
    assert online.file_stem('A/B: "c"?') == "A_B_ _c_"
    assert online.file_stem("...") == "video"


def test_playlist_keeps_web_videos_and_their_titles():
    playlist = Playlist()
    data = {"paths": ["/gone.mkv", URL], "index": 1, "positions": {URL: 42.0},
            "titles": {URL: "Title", "/gone.mkv": "x"}}
    assert playlist.restore(data, exists=lambda p: False)
    assert playlist.paths == [URL] and playlist.index == 0
    assert playlist.positions == {URL: 42.0}
    assert playlist.name(URL) == "Title" and playlist.name("/a/ep1.mkv") == "ep1.mkv"
    assert playlist.to_json()["titles"] == {URL: "Title"}


def test_fetch_subtitles_picks_each_language(tmp_path, monkeypatch):
    stem = online._key(URL)
    (tmp_path / f"{stem}.old.srt").write_text("stale")  # from the last time: removed first
    calls = []

    def run(args):
        calls.append(args)
        for name in ("ja-orig.srt", "ja.srt", "en.srt", "info.json"):
            (tmp_path / f"{stem}.{name}").write_text("x")
        (tmp_path / f"{stem}.title.txt").write_text("猫\n", encoding="utf-8")
        return 0, "", ""

    monkeypatch.setattr(online, "_run", run)
    title, source, target = online.fetch_subtitles(URL, tmp_path, "ja", "en", "/ff/ffmpeg")
    assert title == "猫"
    assert source.name == f"{stem}.ja.srt" and target.name == f"{stem}.en.srt"
    assert not (tmp_path / f"{stem}.old.srt").exists()
    args = calls[0]
    assert args[args.index("--ffmpeg-location") + 1] == "/ff/ffmpeg"
    assert "ja(-.*)?" in args[args.index("--sub-langs") + 1]
    # no subtitles in the language: None, not an error; no title: None too
    monkeypatch.setattr(online, "_run", lambda args: (0, "", ""))
    assert online.fetch_subtitles(URL, tmp_path, "ja", None, "ffmpeg") == (None, None, None)


def test_fetch_subtitles_reports_yt_dlp_failing(tmp_path, monkeypatch):
    monkeypatch.setattr(online, "_run", lambda args: (1, "", "ERROR: Video unavailable"))
    try:
        online.fetch_subtitles(URL, tmp_path, "ja")
    except online.OnlineError as e:
        assert "Video unavailable" in str(e) and "yt-dlp -U" in str(e)
    else:
        raise AssertionError("no error")


def test_streams_separate_video_and_audio(monkeypatch):
    info = {"title": "T/1", "requested_formats": [
        {"url": "https://v", "vcodec": "avc1", "acodec": "none", "http_headers": {"A": "1"}},
        {"url": "https://a", "vcodec": "none", "acodec": "mp4a"}]}
    calls = []
    monkeypatch.setattr(online, "_streams", {})
    monkeypatch.setattr(online, "_run", lambda args: calls.append(args) or (0, json.dumps(info), ""))
    video, audio = online.media_sources(URL)
    assert (video.url, video.headers, video.name) == ("https://v", {"A": "1"}, "T_1")
    assert audio.url == "https://a"
    online.media_sources(URL)
    assert len(calls) == 1  # kept: each card doesn't wait for yt-dlp again
    assert online.media_sources("/a/ep1.mkv") == ("/a/ep1.mkv", "/a/ep1.mkv")
    # one file with both
    monkeypatch.setattr(online, "_streams", {})
    monkeypatch.setattr(online, "_run", lambda args: (0, json.dumps(
        {"url": "https://b", "vcodec": "vp9", "acodec": "opus"}), ""))
    video, audio = online.streams(URL)
    assert video == audio and video.url == "https://b"


def test_ffmpeg_reads_web_streams_with_their_headers():
    stream = online.Stream("https://v", {"Referer": "https://site"}, "a:b")
    assert media._input(stream) == ["-headers", "Referer: https://site\r\n", "-i", "https://v"]
    assert media._input("/a/ep 1.mkv") == ["-i", "/a/ep 1.mkv"]
    assert media._stem(stream) == "a_b" and media._stem("/a/ep 1.mkv") == "ep_1"


# --- known words over time ----------------------------------------------------------

def ordinal(day):
    return datetime.date.fromisoformat(day).toordinal()


def test_card_spans_follow_the_reviews():
    today = ordinal("2026-01-10")
    reviews = [(ordinal("2026-01-01"), -600, 0), (ordinal("2026-01-02"), 25, 1),
               (ordinal("2026-01-02"), 30, 1),        # the day's last review counts
               (ordinal("2026-01-04"), 0, 3),         # filtered, not rescheduled: ignored
               (ordinal("2026-01-05"), 1, 2),         # a lapse
               (ordinal("2026-01-08"), 22, 1)]
    assert progress.card_spans(reviews, True, 21, today) == [
        (ordinal("2026-01-02"), ordinal("2026-01-05")), (ordinal("2026-01-08"), today + 1)]
    # what the card is now wins for today (FSRS stability, a reschedule without a log)
    assert progress.card_spans(reviews, False, 21, today) == [
        (ordinal("2026-01-02"), ordinal("2026-01-05")), (ordinal("2026-01-08"), today)]
    assert progress.card_spans([], True, 21, today) == [(None, today + 1)]
    assert progress.card_spans([], False, 21, today) == []


def test_history_counts_each_word_once():
    jan = {d: ordinal(f"2026-01-0{d}") for d in range(1, 6)}
    cards = {1: (30, ("犬",)), 2: (30, ("犬", "猫")), 3: (40, ("鳥",)), 4: (5, ("魚",))}
    reviews = {1: [(jan[2], 25, 1)], 2: [(jan[3], 25, 1)], 4: [(jan[2], 30, 1)]}
    marked = datetime.datetime(2026, 1, 4, 12).timestamp()
    marks = {"猫": marked, "馬": marked, "古": 0}
    history = progress.history(21, cards, marks, reviews, "2026-01-05", 4)
    assert history == [("2026-01-02", 4),  # 犬, 魚 + 鳥 (no reviews) and 古 (legacy mark)
                       ("2026-01-03", 5),  # 猫 (card 2); 犬 twice, once
                       ("2026-01-04", 6),  # 馬 marked; 猫 marked too: once
                       ("2026-01-05", 5)]  # 魚's card is under the interval now
    assert progress.history(21, {}, {}, {}, "2026-01-05", 4) == [("2026-01-05", 0)]


def test_known_history_reads_the_review_log():
    class Db:
        def all(self, sql):
            assert "cid in (1,2)" in sql
            day = datetime.datetime(2026, 1, 2, 12).timestamp() * 1000
            return [(1, day, 30, 1), (2, day, 3, 1)]

    col = FakeCol()
    col.db = Db()
    inputs = (21, {1: (30, ("犬",)), 2: (3, ("猫",))}, {})
    assert progress.known_history(col, inputs, "2026-01-03", 4) == [
        ("2026-01-02", 1), ("2026-01-03", 1)]


def test_history_inputs_take_known_sentences_and_mark_times(tmp_path, monkeypatch):
    monkeypatch.setattr(knowledge, "OLD_KNOWN_FILE", tmp_path / "missing.json")
    monkeypatch.setattr(store, "device_id", lambda: "me")
    col = FakeCol({"Mining": MODEL}, {1: [(10, "猫が\x1f犬", 30, "", 2)]})
    col.config[f"{knowledge.KNOWN_PREFIX}::other"] = {"100": {"鳥": True, "魚": False}}
    col.config[knowledge.KNOWN_KEY] = ["古"]
    know = knowledge.Knowledge(col=col, config=lambda: CFG)
    know.rebuild()
    know.apply(21, *know.read_cards(col, CFG)[:3], {10: (30, ("猫",)), 11: (5, None)})
    threshold, cards, marks = know.history_inputs()
    assert threshold == 21 and cards == {10: (30, ("犬", "猫"))}
    assert marks == {"鳥": 100.0, "古": 0.0}


def test_log_dialog_known_rows_and_summary():
    history = [(f"d{i}", k) for i, k in enumerate([0, 2, 2, 5])]
    assert log_dialog.known_rows(history) == [("d3", 5, 3), ("d1", 2, 2)]
    assert log_dialog.known_summary(history, days=2) == (
        "Known words: 5 (+3 in the last 2 days).")
    assert log_dialog.known_summary(history, days=30) == (
        "Known words: 5 (+5 in the last 30 days).")
    assert log_dialog.known_summary([]) == ""
