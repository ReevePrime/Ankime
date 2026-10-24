import sys
from pathlib import Path

import pytest

from ankime.cards import media, notes
from ankime.controls import hotkeys
from ankime.core import config
from ankime.language import knowledge, line_filter, yomitan
from ankime.subs import subtitles
from ankime.subs.playlist import Playlist, natural_key


# --- knowledge -------------------------------------------------------------

def test_clean_word_strips_html_furigana_and_spaces():
    assert knowledge.clean_word("<b>食[た]べる</b>") == "食べる"
    assert knowledge.clean_word("<ruby>食<rt>た</rt></ruby>べる ") == "食べる"
    assert knowledge.clean_word("ｶﾀｶﾅ") == "カタカナ"  # NFKC


def test_card_words_splits_several_words():
    assert knowledge.card_words("食べる, 物") == ["食べる", "物"]  # AnkiMorphs
    assert knowledge.card_words("綺麗、奇麗") == ["綺麗", "奇麗"]
    assert knowledge.card_words("<div>猫[ねこ]</div><div>犬</div>") == ["猫", "犬"]
    assert knowledge.card_words("<b>食べる</b>") == ["食べる"]
    assert knowledge.card_words("") == []


def test_is_word():
    assert knowledge.is_word("食べた")
    assert not knowledge.is_word("、")
    assert not knowledge.is_word("abc")
    assert knowledge.is_word("abc", "食べる")


def test_content_lemmas_drops_particles_punctuation_and_unknown_headwords():
    tokens = [("猫", "ねこ", "猫", ("ねこ",)), ("が", "が", "が", ()),
              ("食べた", "たべた", "食べる", ("たべる",)), ("。", "", "", ()),
              ("タロウ", "たろう", "", ()), ("食べる", "たべる", "食べる", ("喰べる",))]
    assert knowledge.content_lemmas(tokens) == {"猫": {"ねこ"}, "食べる": {"たべる", "喰べる"}}


def test_words_to_reach():
    counts = {"known": 80, "learning": 5, "new": 15}
    assert knowledge.words_to_reach(counts, 90) == 5
    assert knowledge.words_to_reach(counts, 80) == 0
    assert knowledge.words_to_reach({"known": 0, "learning": 0, "new": 3}, 90) == 3  # ceil


# --- playlist ----------------------------------------------------------------

def test_natural_key_sorts_numbers_numerically():
    names = ["ep10.mkv", "ep2.mkv", "Ep1.mkv"]
    assert sorted(names, key=natural_key) == ["Ep1.mkv", "ep2.mkv", "ep10.mkv"]


def test_restore_keeps_index_on_same_video_when_earlier_ones_are_gone():
    p = Playlist()
    data = {"paths": ["a", "b", "c", "d"], "index": 2, "positions": {"c": 12.5, "b": 3.0}}
    assert p.restore(data, exists=lambda path: path != "a")
    assert p.paths == ["b", "c", "d"]
    assert p.current == "c"
    assert p.positions == {"c": 12.5, "b": 3.0}


def test_chosen_subtitles_are_saved_for_videos_in_the_playlist():
    p = Playlist()
    p.replace(["a", "b"])
    p.subtitles = {"a": "a.ass", "gone": "x.srt"}
    data = p.to_json()
    assert data["subtitles"] == {"a": "a.ass"}
    again = Playlist()
    again.restore({**data, "subtitles": {"a": "a.ass", "b": 3}}, exists=lambda path: True)
    assert again.subtitles == {"a": "a.ass"}


def test_restore_when_the_current_video_is_gone():
    p = Playlist()
    p.restore({"paths": ["a", "b"], "index": 1}, exists=lambda path: path == "a")
    assert p.paths == ["a"] and p.index == 0
    p.restore({"paths": ["a"], "index": 0}, exists=lambda path: False)
    assert p.paths == [] and p.index == -1


def test_restore_rejects_garbage():
    p = Playlist()
    assert not p.restore({"paths": ["a"]})
    assert not p.restore([1, 2])
    p.restore({"paths": ["a", 3, None], "index": 0, "positions": {"a": "x"}}, exists=bool)
    assert p.paths == ["a"] and p.positions == {}


def test_move_block_and_index_follow():
    p = Playlist()
    p.replace(["a", "b", "c", "d"])
    p.index = 1
    assert p.move([1, 2], 1) == {2, 3}
    assert p.paths == ["a", "d", "b", "c"] and p.current == "b"
    assert p.move([0], -1) == {0}  # at the edge: stays
    assert p.paths == ["a", "d", "b", "c"]


def test_remove_keeps_current_video():
    p = Playlist()
    p.replace(["a", "b", "c"])
    p.index = 2
    p.remove([0])
    assert p.current == "c"


def test_remember_forgets_start_and_end():
    p = Playlist()
    p.remember("a", 100.04, 1400)
    assert p.positions == {"a": 100.0}
    p.remember("a", 1395, 1400)
    assert p.positions == {}


# --- yomitan parsing -----------------------------------------------------------

def test_parse_tokenize_flattens_segments_and_finds_base_form():
    response = [{"index": 0, "content": [
        [{"text": "食", "reading": "た", "headwords": [[{"term": "食べる"}]]},
         {"text": "べた", "reading": ""}],
        [{"text": "。", "reading": ""}],
    ]}]
    assert yomitan.parse_tokenize(response, 1) == [[("食べた", (("食", "た"), ("べた", "")),
                                                      "食べる", ()),
                                                    ("。", "", "", ())]]


def test_parse_tokenize_keeps_spellings_and_readings_of_the_first_entry_only():
    response = [{"index": 0, "content": [[{"text": "きれい", "headwords": [
        [{"term": "綺麗", "reading": "きれい"}, {"term": "奇麗", "reading": "きれい"}],
        [{"term": "嫌い", "reading": "きらい"}],
    ]}]]}]
    assert yomitan.parse_tokenize(response, 1) == [[("きれい", "", "綺麗", ("奇麗", "きれい"))]]


def test_parse_tokenize_single_dict_and_missing_lines():
    assert yomitan.parse_tokenize({"content": [[{"text": "猫"}]]}, 2) == [[("猫", "", "", ())],
                                                                          []]


def test_unexpected_answers_become_yomitan_errors(monkeypatch):
    monkeypatch.setattr(yomitan, "_post", lambda *a, **kw: ["not", "a", "dict"])
    for call in (lambda: yomitan.lookup("猫"), lambda: yomitan.card_fields("猫"),
                 lambda: yomitan._tokenize_lines(["猫"], scan_length=20)):
        try:
            call()
        except yomitan.YomitanError as e:
            assert "Unexpected answer" in str(e)
        else:
            raise AssertionError("no YomitanError")


def test_split_lines_marks_line_breaks():
    calls = []

    def fake(lines):
        calls.append(lines)
        return [[(line, "", "", ())] for line in lines]

    out = yomitan.split_lines(["a\nb", "c"], fake)
    assert calls == [["a", "b", "c"]]
    assert out == [[("a", "", "", ()), ("\n", "", "", ()), ("b", "", "", ())],
                   [("c", "", "", ())]]


# --- hotkeys -------------------------------------------------------------------

def test_to_mpv():
    assert hotkeys.to_mpv("Ctrl+Shift+N") == "ctrl+N"
    assert hotkeys.to_mpv("N") == "n"
    assert hotkeys.to_mpv("Shift+Left") == "shift+LEFT"
    assert hotkeys.to_mpv("Space") == "SPACE"
    assert hotkeys.to_mpv("Ctrl+Return") == "ctrl+ENTER"
    assert hotkeys.to_mpv("F5") == "F5"
    assert hotkeys.to_mpv("+") == "+"
    assert hotkeys.to_mpv("Ctrl++") == "ctrl++"
    assert hotkeys.to_mpv("Ctrl+Alt++") == "ctrl+alt++"
    assert hotkeys.to_mpv("#") == "SHARP"


def test_default_hotkeys_are_unique_and_cover_every_action():
    defaults = config.DEFAULTS["hotkeys"]
    assert set(defaults) == set(hotkeys.ACTIONS)
    keys = [key for key in defaults.values() if key]
    assert len(keys) == len(set(keys))


def test_hotkeys_get_reads_the_given_config():
    cfg = config.merge({"hotkeys": {"mine_line": "Ctrl+M"}})
    keys = hotkeys.get(cfg)
    assert keys["mine_line"] == "Ctrl+M"
    assert keys["next_i1"] == config.DEFAULTS["hotkeys"]["next_i1"]


# --- notes and media (pure parts) -------------------------------------------------------

def test_source_text_and_show_tag():
    video = "/anime/Sousou no Frieren/Season 1/Frieren - 03.mkv"
    assert notes.show_tag(video) == "ankime::Sousou_no_Frieren::Season_1"
    assert notes.source_text(video, 754.2) == "Sousou no Frieren/Season 1 · Frieren - 03 · 12:34"
    assert notes.show_tag("/anime/Show: Part 2/ep1.mkv") == "ankime::Show__Part_2"
    assert notes.show_tag("ep1.mkv") == "" and notes.source_text("ep1.mkv", 5) == "ep1 · 00:05"


@pytest.mark.skipif(sys.platform != "win32", reason="a Windows path is one name elsewhere")
def test_show_tag_of_a_windows_path():
    assert notes.show_tag(r"C:\Videos\Show: Part 2\ep1.mkv") == "ankime::Show__Part_2"


def test_sentence_html_escapes_bolds_once_and_breaks_lines():
    assert notes.sentence_html("a<b>\n猫と猫", "猫") == "a&lt;b&gt;<br><b>猫</b>と猫"
    assert notes.sentence_html("猫", None) == "猫"


def test_image_and_audio_args():
    assert "libwebp" in media.image_args("webp", 480)
    assert media.image_args("jpg", 0) == ["-frames:v", "1", "-q:v", "3"]
    assert media.audio_args(None) == ["-vn", "-sn", "-dn", "-c:a", "aac", "-b:a", "128k"]
    assert media.audio_args(2, 3.5, "opus") == [
        "-vn", "-sn", "-dn", "-map", "0:2", "-af", "volume=3.50dB", "-c:a", "libopus",
        "-b:a", "64k"]
    assert "libaom-av1" in media.animated_args("avif", 480)
    assert media.animated_args("webp", 0)[:5] == ["-an", "-sn", "-dn", "-vf", "fps=10"]


def test_audio_format_falls_back_to_m4a():
    assert media.audio_format({"audio_format": "opus"}) == "opus"
    assert media.audio_format({"audio_format": "wav"}) == "m4a"


def test_clip_span_keeps_short_lines_and_cuts_long_ones_around_the_picture():
    assert media.clip_span(10, 13, 11.5) == (10, 3)
    assert media.clip_span(10, 30, 20) == (16, 8)
    assert media.clip_span(10, 30, 11) == (10, 8)   # not before the line
    assert media.clip_span(10, 30, 29) == (22, 8)   # nor past its end


def test_condensed_spans_pad_lines_and_merge_those_close_together():
    times = [(10, 12), (12.5, 14), (30, 31), (1, 2), (31.2, 33)]
    assert media.condensed_spans(times, pad=0.25) == [
        (0.75, 2.25), (9.75, 14.25), (29.75, 33.25)]
    assert media.condensed_spans([(0.1, 1)], pad=0.5) == [(0.0, 1.5)]  # not before 0
    assert media.condensed_spans([], pad=0.25) == []


def test_condensed_filter_keeps_the_spans_and_normalizes_if_asked():
    chain = media.condensed_filter([(1, 2.5), (10, 12)], normalize=False)
    assert "aselect='between(t,1.000,2.500)+between(t,10.000,12.000)'" in chain
    assert chain.startswith("asetpts=PTS-STARTPTS,") and "loudnorm" not in chain
    assert "loudnorm" in media.condensed_filter([(1, 2)], normalize=True)


def test_condensed_audio_runs_ffmpeg_once_and_long_filters_go_through_a_file(
        tmp_path, monkeypatch):
    calls = []

    def fake_ffmpeg(*args, **_kw):
        calls.append(args)
        out = Path(args[-1])
        out.write_bytes(b"audio")
        if "-filter_script:a" in args:
            script = Path(args[args.index("-filter_script:a") + 1])
            assert "between(t," in script.read_text(encoding="utf-8")

    monkeypatch.setattr(media, "run_ffmpeg", fake_ffmpeg)
    media.TEMP.parent.mkdir()  # the system's temp folder
    (tmp_path / "out").mkdir()
    out = tmp_path / "out" / "ep.condensed.mp3"
    assert media.condensed_audio("ep.mkv", [(10, 12), (20, 21)], out, 1) == pytest.approx(3 + 4 * config.get()["audio_padding"])
    assert out.read_bytes() == b"audio" and len(calls) == 1
    assert "-af" in calls[0] and calls[0][calls[0].index("-map") + 1] == "0:1"
    assert "libmp3lame" in calls[0]
    assert [f.name for f in out.parent.iterdir()] == [out.name]  # no .part left

    monkeypatch.setattr(media, "MAX_INLINE_FILTER", 10)
    media.condensed_audio("ep.mkv", [(10, 12)], out)
    assert "-filter_script:a" in calls[1] and "-af" not in calls[1]
    with pytest.raises(ValueError):
        media.condensed_audio("ep.mkv", [(10, 12)], out.with_suffix(".wav"))
    assert list(media.TEMP.iterdir()) == []  # the filter's file is gone


def test_extracting_an_episode_picks_lines_by_kind():
    kinds = ["known", "i+1", "harder", None]
    assert [k for k in kinds if line_filter.extracted(k, "all")] == kinds
    assert [k for k in kinds if line_filter.extracted(k, "i1")] == ["i+1"]
    assert [k for k in kinds if line_filter.extracted(k, "new")] == ["i+1", "harder"]


def test_loudness_is_parsed_and_gain_keeps_peaks_under_the_ceiling():
    report = b'[Parsed_loudnorm_0 @ 0x1]\n{\n\t"input_i" : "-26.00",\n\t"input_tp" : "-8.00"\n}\n'
    assert media.parse_loudness(report) == (-26.0, -8.0)
    assert media.parse_loudness(b'{"input_i" : "-inf", "input_tp" : "-inf"}') is None
    assert media.parse_loudness(b"no report") is None
    assert media.normalize_gain((-26.0, -8.0)) == 6.5      # the peak limits it, not 10 dB
    assert media.normalize_gain((-10.0, -0.5)) == -6.0     # too loud: quieter
    assert media.normalize_gain((-60.0, -40.0)) == 20.0    # boost capped
    assert media.normalize_gain((-16.05, -5.0)) is None    # already there
    assert media.normalize_gain(None) is None


# --- subtitles -----------------------------------------------------------------

def test_lang_codes():
    assert "jpn" in subtitles.lang_codes("ja")
    assert subtitles.lang_codes("xx") == {"xx"}
    assert subtitles.lang_codes("") == set()
    assert subtitles.lang_code("JPN") == "ja" and subtitles.lang_code("pt-br") == "pt-br"
    assert ("ja", "Japanese") in subtitles.LANGUAGES


JA_SRT = "1\n00:00:01,000 --> 00:00:02,000\n今日はいい天気ですね。\n"
EN_SRT = "1\n00:00:01,000 --> 00:00:02,000\nIt's nice weather today, isn't it? (Tokyo)\n"


def test_find_sidecars_uses_a_file_chosen_by_hand_while_it_exists(tmp_path):
    video = tmp_path / "ep01.mkv"
    (tmp_path / "Subs").mkdir()
    chosen = tmp_path / "Subs" / "[Jimaku] Show 01.ass"
    for path in (video, chosen):
        path.write_text("")
    (tmp_path / "ep01.srt").write_text(JA_SRT, encoding="utf-8")
    assert subtitles.find_sidecars(video, "ja", "en", str(chosen))[0] == chosen
    chosen.unlink()
    assert subtitles.find_sidecars(video, "ja", "en", str(chosen))[0].name == "ep01.srt"


def test_find_sidecars(tmp_path):
    video = tmp_path / "ep01.mkv"
    for name in ("ep01.mkv", "ep01.eng.srt", "ep02.ja.srt", "ep01.notes.txt"):
        (tmp_path / name).write_text("")
    (tmp_path / "ep01.srt").write_text(JA_SRT, encoding="utf-8")
    source, target = subtitles.find_sidecars(video, "ja", "en")
    assert source.name == "ep01.srt" and target.name == "ep01.eng.srt"
    (tmp_path / "ep01.ja.ass").write_text("")
    assert subtitles.find_sidecars(video, "ja", "en")[0].name == "ep01.ja.ass"


def test_an_untagged_file_is_the_source_only_if_it_reads_as_japanese(tmp_path):
    video = tmp_path / "ep01.mkv"
    video.write_text("")
    (tmp_path / "ep01.ass").write_text(EN_SRT, encoding="utf-8")
    assert subtitles.find_sidecars(video, "ja", "en")[0] is None
    (tmp_path / "ep01.srt").write_text(JA_SRT, encoding="utf-8")  # the other untagged one
    assert subtitles.find_sidecars(video, "ja", "en")[0].name == "ep01.srt"
    (tmp_path / "ep01.srt").write_text("")  # empty: nothing to go by
    assert subtitles.find_sidecars(video, "ja", "en")[0] is None


def test_find_sidecars_ignores_episodes_with_a_longer_name(tmp_path):
    for name in ("ep1.mkv", "ep10.ja.srt", "ep10.srt", "ep1[ja].srt"):
        (tmp_path / name).write_text("")
    source, _ = subtitles.find_sidecars(tmp_path / "ep1.mkv", "ja", "en")
    assert source.name == "ep1[ja].srt"


def test_decode_handles_common_subtitle_encodings():
    text = "1\n00:00:01,000 --> 00:00:02,000\nテスト\n"
    for data in (text.encode("utf-8"), b"\xef\xbb\xbf" + text.encode("utf-8"),
                 text.encode("cp932"), text.encode("utf-16")):
        assert subtitles.decode(data) == text


def test_load_events_reads_shift_jis(tmp_path):
    path = tmp_path / "a.srt"
    path.write_bytes(SRT.encode("cp932"))
    assert subtitles.load_events(path)[0] == (0.5, 0.9, "最初")


def test_delay_key_is_the_same_on_every_computer():
    assert subtitles.delay_key(r"C:\Anime\Frieren") == "Frieren"
    assert subtitles.delay_key("/home/me/anime/Frieren/") == "Frieren"
    assert subtitles.delay_key(r"D:\Videos\Frieren\Season 2") == "Frieren/Season 2"
    assert subtitles.delay_key("Frieren") == "Frieren"  # already migrated


SRT = """1
00:00:01,000 --> 00:00:02,000
こんにちは

2
00:00:00,500 --> 00:00:00,900
最初
"""

ASS = """[Script Info]
ScriptType: v4.00+

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,20,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,2,2,10,10,10,1
Style: Signs,Arial,20,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,2,2,10,10,10,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,台詞
Dialogue: 0,0:00:01.00,0:00:02.00,Signs,,0,0,0,,看板
Dialogue: 0,0:00:03.00,0:00:04.00,Default,,0,0,0,,{\\k20}カラ{\\k20}オケ
Comment: 0,0:00:05.00,0:00:06.00,Default,,0,0,0,,メモ
"""


def test_load_events_sorted(tmp_path):
    path = tmp_path / "a.srt"
    path.write_text(SRT, encoding="utf-8")
    assert subtitles.load_events(path) == [(0.5, 0.9, "最初"), (1.0, 2.0, "こんにちは")]


def test_load_events_keeps_only_dialogue(tmp_path):
    path = tmp_path / "a.ass"
    path.write_text(ASS, encoding="utf-8")
    assert subtitles.load_events(path) == [(1.0, 2.0, "台詞")]
