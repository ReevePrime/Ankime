"""Getting subtitles in sync and complete: Jimaku search and downloads, aligning with alass /
ffsubsync, subtitles from the speech with faster-whisper / whisper.cpp, the "this line starts
now" key and sentences split over lines."""
import io
import sys
import zipfile
from pathlib import Path
from unittest import mock

import pytest

from ankime.subs import jimaku, retime, transcribe
from ankime.core.model import Event, Span, Token
from ankime.player import sub_delay
from ankime.player.sub_delay import SubDelayMixin
from ankime.subs.session import SubtitleSession


# --- Jimaku ----------------------------------------------------------------

@pytest.mark.parametrize("name, episode", [
    ("[SubsPlease] Sousou no Frieren - 03 (1080p) [ABCD1234].mkv", 3),
    ("[Group] Show - 12v2 [1080p].mkv", 12),
    ("Show.S01E07.1080p.WEB.x264.mkv", 7),
    ("Show ep05.mp4", 5),
    ("葬送のフリーレン 第3話.mp4", 3),
    ("葬送のフリーレン 10話.mkv", 10),
    ("04.mkv", 4),
    ("Show 2nd Season 08 [1080p].mkv", 8),
    ("Movie [1080p] x265.mkv", None),
])
def test_episode_number(name, episode):
    assert jimaku.episode_number(name) == episode


def test_search_query_is_the_show_folder_without_release_tags():
    assert jimaku.search_query("/anime/[Judas] Sousou no Frieren S01 [1080p]/ep01.mkv") == \
        "Sousou no Frieren"
    assert jimaku.search_query("/anime/Kusuriya no Hitorigoto/Season 2/ep01.mkv") == \
        "Kusuriya no Hitorigoto"


@pytest.mark.skipif(sys.platform != "win32", reason="a Windows path is one name elsewhere")
def test_search_query_of_a_windows_path():
    assert jimaku.search_query("D:\\Anime\\Kusuriya no Hitorigoto\\Season 2\\ep01.mkv") == \
        "Kusuriya no Hitorigoto"
    assert jimaku.search_query("/anime/Bocchi.the.Rock/01.mkv") == "Bocchi the Rock"


def test_best_match_picks_the_episode_or_asks():
    names = ["Show - 01.srt", "Show - 02.srt", "Show - 03.srt"]
    assert jimaku.best_match(names, 2) == 1
    assert jimaku.best_match(names, 9) is None
    assert jimaku.best_match(names, None) is None
    assert jimaku.best_match(["only.ass"], 5) == 0


def _zip(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


def test_subtitle_files_of_an_archive_keep_only_subtitles():
    data = _zip({"Show/ep01.srt": b"1", "Show/ep02.ASS": b"2", "readme.txt": b"x",
                 "__MACOSX/Show/._ep01.srt": b"junk"})
    assert jimaku.subtitle_files("Show.zip", data) == [("ep01.srt", b"1"), ("ep02.ASS", b"2")]
    assert jimaku.subtitle_files("dir/ep01.srt", b"1") == [("ep01.srt", b"1")]
    with pytest.raises(jimaku.JimakuError):
        jimaku.subtitle_files("broken.zip", b"not a zip")


def test_an_archive_that_unpacks_too_large_is_refused(monkeypatch):
    # a few kilobytes of zeros compress to almost nothing: a zip bomb in miniature
    monkeypatch.setattr(jimaku, "MAX_SUBTITLE", 1000)
    bomb = _zip({"ep01.srt": b"0" * 5000})
    assert len(bomb) < 1000
    with pytest.raises(jimaku.JimakuError, match="larger than"):
        jimaku.subtitle_files("Show.zip", bomb)
    monkeypatch.setattr(jimaku, "MAX_UNPACKED", 1500)
    with pytest.raises(jimaku.JimakuError, match="unpack to more than"):
        jimaku.subtitle_files("Show.zip", _zip({"ep01.srt": b"1" * 800, "ep02.srt": b"2" * 800}))
    assert len(jimaku.subtitle_files("Show.zip", _zip({"ep01.srt": b"1" * 800}))) == 1


def test_save_path_is_found_by_name_and_never_overwrites(tmp_path):
    video = tmp_path / "ep01.mkv"
    assert jimaku.save_path(video, "Whatever [JP].SRT", "ja") == tmp_path / "ep01.ja.srt"
    (tmp_path / "ep01.ja.srt").write_text("old")
    assert jimaku.save_path(video, "x.srt", "ja") == tmp_path / "ep01.ja.jimaku.srt"
    (tmp_path / "ep01.ja.jimaku.srt").write_text("old")
    assert jimaku.save_path(video, "x.srt", "ja") == tmp_path / "ep01.ja.jimaku2.srt"


def test_the_key_goes_to_jimaku_only(monkeypatch):
    calls = []

    def get(url, headers, **kwargs):
        calls.append((url, headers))
        return mock.MagicMock(status_code=200, ok=True, json=lambda: [])

    monkeypatch.setattr(jimaku.requests, "get", get)
    jimaku.search("frieren", "KEY")
    jimaku._request("https://elsewhere.example/file.srt", "KEY")
    assert calls[0][1]["Authorization"] == "KEY"
    assert "Authorization" not in calls[1][1]
    with pytest.raises(jimaku.JimakuError, match="API key"):
        jimaku.search("frieren", "")


def test_a_refused_key_says_so(monkeypatch):
    monkeypatch.setattr(jimaku.requests, "get",
                        lambda *a, **k: mock.MagicMock(status_code=401, ok=False))
    with pytest.raises(jimaku.JimakuError, match="refused the API key"):
        jimaku.search("frieren", "KEY")


def test_files_lists_subtitles_and_archives(monkeypatch):
    found = [{"name": "ep01.srt"}, {"name": "ep01.mkv"}, {"name": "all.zip"},
             {"name": "all.rar"}]
    monkeypatch.setattr(jimaku, "_request",
                        lambda *a, **k: mock.MagicMock(json=lambda: found))
    assert [f["name"] for f in jimaku.files(1, "KEY")] == ["ep01.srt", "all.zip"]


# --- aligning --------------------------------------------------------------

def test_aligner_commands():
    assert retime.tool_kind("C:/tools/alass-cli.exe") == "alass"
    assert retime.tool_kind("/usr/bin/ffsubsync") == "ffsubsync"
    assert retime.tool_kind("ffmpeg") is None
    assert retime.command("alass-cli", "v.mkv", "in.srt", "out.srt") == \
        ["alass-cli", "v.mkv", "in.srt", "out.srt"]
    assert retime.command("ffsubsync", "v.mkv", "in.srt", "out.srt") == \
        ["ffsubsync", "v.mkv", "-i", "in.srt", "-o", "out.srt"]


def test_aligned_path_never_overwrites_the_input(tmp_path):
    video = tmp_path / "ep01.mkv"
    assert retime.aligned_path("/tmp/x.ASS", video, "ja") == tmp_path / "ep01.ja.synced.ass"
    (tmp_path / "ep01.ja.synced.ass").write_text("")
    assert retime.aligned_path(tmp_path / "ep01.ja.synced.ass", video, "ja") == \
        tmp_path / "ep01.ja.synced2.ass"


def test_align_refuses_another_program():
    with pytest.raises(retime.RetimeError, match="isn't alass or ffsubsync"):
        retime.align("v.mkv", "in.srt", "out.srt", tool="ffmpeg")


# --- subtitles from the speech -----------------------------------------------

def test_transcriber_kinds():
    assert transcribe.tool_kind("C:/XXL/faster-whisper-xxl.exe") == "faster-whisper"
    assert transcribe.tool_kind("/usr/bin/whisper-ctranslate2") == "faster-whisper"
    assert transcribe.tool_kind("C:/whisper/whisper-cli.exe") == "whisper.cpp"
    assert transcribe.tool_kind("ffmpeg") is None
    assert transcribe.tool_kind("alass-cli") is None


def test_transcriber_commands():
    audio, out = Path("t/audio.wav"), Path("t")
    assert transcribe.command("faster-whisper-xxl", audio, out, "ja", "large-v3") == [
        "faster-whisper-xxl", str(audio), "--language", "ja", "--output_dir", str(out),
        "--output_format", "srt", "--vad_filter", "True", "--model", "large-v3"]
    cpp = transcribe.command("whisper-cli", audio, out, "ja", "m/ggml-large-v3.bin")
    assert cpp[:7] == ["whisper-cli", "-f", str(audio), "-l", "ja", "-osrt", "-of"]
    assert cpp[7] == str(out / "audio")  # whisper.cpp adds the .srt
    assert cpp[-2:] == ["-m", "m/ggml-large-v3.bin"]
    assert "--model" not in transcribe.command("whisper-ctranslate2", audio, out, "ja")


def test_progress_is_read_from_the_lines_printed():
    assert transcribe.parse_progress("[01:02.500 --> 01:05.000] こんにちは") == 65.0
    assert transcribe.parse_progress("[00:01:02.000 --> 01:00:05,250]  text") == 3605.25
    assert transcribe.parse_progress("Detected language 'ja'") is None


def test_whisper_subtitles_never_overwrite(tmp_path):
    video = tmp_path / "ep01.mkv"
    assert transcribe.subtitle_path(video, "ja") == tmp_path / "ep01.ja.whisper.srt"
    (tmp_path / "ep01.ja.whisper.srt").write_text("")
    assert transcribe.subtitle_path(video, "ja") == tmp_path / "ep01.ja.whisper2.srt"


def test_whisper_cpp_model_is_found_next_to_it(tmp_path):
    tool = tmp_path / "build" / "bin" / "whisper-cli.exe"
    tool.parent.mkdir(parents=True)
    tool.write_text("")
    assert transcribe.find_ggml_model(tool) is None
    models = tmp_path / "models"
    models.mkdir()
    (models / "ggml-base.bin").write_bytes(b"x" * 10)
    (models / "ggml-large-v3.en.bin").write_bytes(b"x" * 100)  # English only
    (tool.parent / "ggml-small.bin").write_bytes(b"x" * 20)
    assert transcribe.find_ggml_model(tool).name == "ggml-small.bin"


def test_transcribe_refuses_another_program():
    with pytest.raises(transcribe.TranscribeError, match="isn't faster-whisper or whisper"):
        transcribe.transcribe("v.mkv", "out.srt", tool="ffmpeg", model="")


def test_whisper_cpp_without_a_model_says_what_to_get(tmp_path):
    tool = tmp_path / "whisper-cli.exe"
    tool.write_text("")
    with pytest.raises(transcribe.TranscribeError, match="needs a model"):
        transcribe.transcribe("v.mkv", "out.srt", tool=str(tool), model="")


def test_run_reports_progress_and_failures():
    seen = []
    script = "print('[00:30.000 --> 00:31.000] a'); print('[01:00.000 --> 01:01.000] b')"
    transcribe._run([sys.executable, "-c", script], seen.append, 120)
    assert seen == [pytest.approx(31 / 120), pytest.approx(61 / 120)]
    with pytest.raises(transcribe.TranscribeError, match="failed:\nCUDA out of memory"):
        transcribe._run([sys.executable, "-c",
                         "import sys; print('CUDA out of memory'); sys.exit(1)"], None, None)
    assert not transcribe._running


def test_transcribe_writes_the_subtitles(tmp_path, monkeypatch):
    """ffmpeg takes the audio playing out; the transcriber's .srt is copied next to the
    video."""
    calls = []

    def ffmpeg(*args, **kwargs):
        calls.append(args)
        Path(args[-1]).write_bytes(b"RIFF")

    def run(args, progress, duration):
        Path(args[args.index("--output_dir") + 1], "audio.srt").write_text(
            "1\n00:00:01,000 --> 00:00:02,000\nこんにちは\n", encoding="utf-8")

    monkeypatch.setattr(transcribe.media, "run_ffmpeg", ffmpeg)
    monkeypatch.setattr(transcribe, "_run", run)
    out = tmp_path / "ep01.ja.whisper.srt"
    assert transcribe.transcribe(tmp_path / "ep01.mkv", out, audio_stream=2,
                                 tool="faster-whisper-xxl", model="") == out
    assert "こんにちは" in out.read_text(encoding="utf-8")
    args = list(calls[0])
    assert args[args.index("-map") + 1] == "0:2"  # the audio track playing
    assert "16000" in calls[0]


# --- this line starts now ----------------------------------------------------

def _session(events, tokens=None):
    s = SubtitleSession()
    s.events = [Event(*e) for e in events]
    s.tokens = tokens or {}
    return s


def test_sync_delay_moves_the_nearest_line_to_now():
    s = _session([(10, 12, "a"), (20, 22, "b"), (30, 32, "c")])
    assert s.sync_delay(23.0, 0.0) == pytest.approx(3.0)       # b was 3 s early
    assert s.sync_delay(17.0, 0.0) == pytest.approx(-3.0)      # b was 3 s late
    assert s.sync_delay(26.0, 5.0) == pytest.approx(6.0)       # b shows at 25 now
    assert s.sync_delay(26.0, 0.0, index=0) == pytest.approx(16.0)
    assert _session([]).sync_delay(5.0, 0.0) is None


class _Player:
    def __init__(self, time_pos, paused, delay):
        self.time_pos, self.paused, self.sub_delay = time_pos, paused, delay


class _Window(SubDelayMixin):
    def __init__(self, session, player):
        self.session, self.player, self.delays = session, player, []

    def _has_video(self):
        return True

    def _set_sub_delay(self, delay):
        self.delays.append(round(delay, 3))


def test_sync_key_allows_for_the_reaction_while_playing():
    s = _session([(10, 12, "a"), (20, 22, "b")])
    playing = _Window(s, _Player(23.2, False, 0.0))
    playing.sync_line()
    assert playing.delays == [round(23.2 - sub_delay.SYNC_REACTION - 20, 3)]
    paused = _Window(s, _Player(23.2, True, 0.0))
    paused.sync_to_event(0)  # from the transcript: that line, wherever it is
    paused.sync_to_event(7)  # no such line: nothing happens
    assert paused.delays == [13.2]


# --- sentences split over lines ---------------------------------------------

P = Token  # surface, furigana, base, alts


def test_a_comma_or_a_continuing_particle_joins_the_next_line():
    s = _session([(1, 2, "でも、"), (2.1, 3, "私は"), (3.2, 4, "行くよ"), (4.1, 5, "次")],
                 {"私は": [P("私", "", "私", ()), P("は", "", "は", ())]})
    assert [s.continues(i) for i in range(4)] == [True, True, False, False]
    assert s.sentence_extra(0) == [0, 2]
    assert s.sentence_extra(1) == [1, 1]
    assert s.sentence_extra(2) == [2, 0]
    assert s.sentence_extra(3) == [0, 0]


def test_lines_that_can_end_a_sentence_are_not_joined():
    tokens = {"私も": [P("私", "", "私", ()), P("も", "", "も", ())],
              "ちょっと": [P("ちょっと", "", "ちょっと", ())]}
    s = _session([(1, 2, "私も"), (2.1, 3, "ちょっと"), (3.1, 4, "x")], tokens)
    assert not s.continues(0) and not s.continues(1)


def test_a_gap_or_an_overlap_keeps_lines_apart():
    s = _session([(1, 2, "それで、"), (4, 5, "a、"), (5.0, 6, "b、"), (5.5, 7, "c")])
    assert not s.continues(0)      # 2 s of silence
    assert s.continues(1)          # straight on
    assert not s.continues(2)      # c starts while b is on screen: someone else talking


def test_merged_sentences_take_at_most_three_lines():
    s = _session([(i, i + 0.9, f"{i}、") for i in range(6)])
    assert s.sentence_extra(2) == [2, 0]
    assert s.sentence_extra(0) == [0, 2]


def test_set_line_takes_the_whole_sentence_and_a_card_from_the_transcript_too():
    s = _session([(1, 2, "でも、"), (2.1, 3, "行く")])
    s.set_line("行く", 3.1, 4.0, "v.mkv", "", merge_delay=1.0)
    assert s.extra == [1, 0]
    assert s.span(1.0) == Span("でも、\n行く", 2.0, 4.0)
    s.set_line("でも、", 2.0, 3.0, "v.mkv", "")  # merging off
    assert s.extra == [0, 0]
    assert s.event_span(0, 1.0, merge=True) == Span("でも、\n行く", 2.0, 4.0)
    assert s.event_span(0, 1.0) == Span("でも、", 2.0, 3.0)
