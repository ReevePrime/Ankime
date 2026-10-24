"""Splitting lines with MeCab: its morphemes grouped into words, combined with Yomitan's split,
and the headwords picked by MeCab's dictionary form. MeCab and Yomitan are faked, with what
the real ones answer (ankimorphs-japanese-mecab's IPAdic)."""
import subprocess

import pytest

from ankime.core import config
from ankime.language import mecab, tokenizer, yomitan

M = mecab.Morpheme
# MeCab's morphemes: surface, dictionary form, reading, part of speech and subtype
SAGASANAI = [M("仕事", "仕事", "シゴト", "名詞", "サ変接続"), M("を", "を", "ヲ", "助詞", "格助詞"),
             M("探さ", "探す", "サガサ", "動詞", "自立"), M("ない", "ない", "ナイ", "助動詞", ""),
             M("と", "と", "ト", "助詞", "接続助詞"), M("いけ", "いける", "イケ", "動詞", "非自立"),
             M("ませ", "ます", "マセ", "助動詞", ""), M("ん", "ん", "ン", "助動詞", ""),
             M("ね", "ね", "ネ", "助詞", "終助詞")]
LINE = "仕事を探さないといけませんね"


def seg(text, *groups, reading=""):
    """A Yomitan furigana segment, with headword groups of (term, reading)."""
    return {"text": text, "reading": reading,
            "headwords": [[{"term": t, "reading": r} for t, r in g] for g in groups]}


def surfaces(words):
    return [surface for surface, *_ in words]


def test_morphemes_are_grouped_into_words_as_yomitan_reads_them():
    assert surfaces(mecab.words(LINE, SAGASANAI)) == [
        "仕事", "を", "探さない", "と", "いけません", "ね"]
    tabete = [M("食べ", "食べる", "タベ", "動詞", "自立"), M("て", "て", "テ", "助詞", "接続助詞"),
              M("しまっ", "しまう", "シマッ", "動詞", "非自立"), M("た", "た", "タ", "助動詞", "")]
    assert surfaces(mecab.words("食べてしまった", tabete)) == ["食べてしまった"]
    omotteru = [M("思っ", "思う", "オモッ", "動詞", "自立"), M("てる", "てる", "テル", "動詞", "非自立")]
    assert surfaces(mecab.words("思ってる", omotteru)) == ["思ってる"]
    # an auxiliary takes its endings; a noun doesn't take the copula
    denakereba = [M("で", "だ", "デ", "助動詞", ""), M("なけれ", "ない", "ナケレ", "助動詞", ""),
                  M("ば", "ば", "バ", "助詞", "接続助詞")]
    assert surfaces(mecab.words("でなければ", denakereba)) == ["でなければ"]
    hana = [M("花", "花", "ハナ", "名詞", "一般"), M("です", "です", "デス", "助動詞", "")]
    assert surfaces(mecab.words("花です", hana)) == ["花", "です"]


def test_what_mecab_passes_over_stays_in_the_words():
    words = mecab.words("あ　猫", [M("あ", "あ", "ア", "感動詞", ""), M("猫", "猫", "ネコ", "名詞", "一般")])
    assert words == [("あ", [words[0][1][0]]), ("　", []), ("猫", [words[2][1][0]])]


def test_yomitan_words_are_kept_where_they_agree_with_mecabs():
    mecab_words = mecab.words("あの子と", [
        M("あの", "あの", "アノ", "連体詞", ""), M("子", "子", "コ", "名詞", "一般"),
        M("と", "と", "ト", "助詞", "格助詞")])
    yomitan_words = [[seg("あの子", [("あの子", "あのこ")])], [seg("と", [("と", "と")])]]
    combined = mecab.combine("あの子と", mecab_words, yomitan_words)
    assert [(s, ms is None) for s, ms, _ in combined] == [("あの子", True), ("と", False)]


def test_mecabs_words_where_yomitans_run_across_them():
    yomitan_words = [[seg(t)] for t in ("仕事", "を", "探さない", "とい", "け", "ません", "ね")]
    combined = mecab.combine(LINE, mecab.words(LINE, SAGASANAI), yomitan_words)
    assert surfaces(combined) == ["仕事", "を", "探さない", "と", "いけません", "ね"]
    # Yomitan's where it agrees, MeCab's where it doesn't
    assert [segs is not None for _, _, segs in combined] == [True, True, True, False, False, True]


def test_a_yomitan_word_over_a_particle_is_taken_apart():
    mecab_words = mecab.words("とはね", [M("と", "と", "ト", "助詞", "格助詞"),
                                       M("は", "は", "ハ", "助詞", "係助詞"),
                                       M("ね", "ね", "ネ", "助詞", "終助詞")])
    combined = mecab.combine("とはね", mecab_words, [[seg("と")], [seg("はね", [("羽", "はね")])]])
    assert surfaces(combined) == ["と", "は", "ね"]


def test_another_text_from_yomitan_leaves_mecabs_words():
    mecab_words = mecab.words("あの子", [M("あの", "あの", "アノ", "連体詞", ""),
                                       M("子", "子", "コ", "名詞", "一般")])
    assert surfaces(mecab.combine("あの子", mecab_words, [[seg("あの子だ")]])) == ["あの", "子"]


def test_headwords_follow_mecabs_dictionary_form():
    shinai = [seg("しない", [("市内", "しない")], [("竹刀", "しない")], [("する", "する")])]
    assert yomitan.headwords(shinai) == ("市内", ("しない",))
    assert yomitan.headwords(shinai, "する") == ("する", ())
    assert yomitan.headwords(shinai, "見る") == ("市内", ("しない",))  # none: the first
    assert yomitan.headwords(shinai, "見る", fallback=False) == ("", ())
    # a spelling wins over a reading; a reading only counts if asked
    mase = [seg("ませ", [("増す", "ます")], [("ます", "ます")])]
    assert yomitan.headwords(mase, "ます") == ("ます", ())
    assert yomitan.headwords([seg("ませ", [("増す", "ます")])], "ます", fallback=False,
                             by_reading=False) == ("", ())


def fake_yomitan(known):
    """line_words for a Yomitan that reads each text of `known` as one word with those
    segments, and anything else as one word per character, with no headwords."""
    def line_words(texts):
        return [[known[t]] if t in known else [[seg(c)] for c in t] for t in texts]
    return line_words


def test_tokenize_lines_gives_each_word_yomitans_headwords(monkeypatch):
    monkeypatch.setattr(mecab, "parse", lambda lines, found=None: [SAGASANAI])
    known = {"探さない": [seg("探", [("探す", "さがす")], reading="さが"), seg("さない")],
             "いけません": [seg("いけません", [("行けません", "いけません")], [("いける", "いける")])],
             "仕事": [seg("仕事", [("仕事", "しごと")], reading="しごと")],
             "と": [seg("と", [("と", "と")])], "を": [seg("を", [("を", "を")])],
             "ね": [seg("ね", [("ね", "ね")])]}
    def line_words(texts):
        if texts == [LINE]:  # Yomitan's own split of the line, とい and all
            return [[[seg("仕事", [("仕事", "しごと")], reading="しごと")], [seg("を")],
                     known["探さない"], [seg("とい", [("問い", "とい")])], [seg("け")],
                     [seg("ません")], [seg("ね")]]]
        return fake_yomitan(known)(texts)

    [tokens] = mecab.tokenize_lines([LINE], found=object(), line_words=line_words)
    assert [(t.surface, t.base) for t in tokens] == [
        ("仕事", "仕事"), ("を", "を"), ("探さない", "探す"), ("と", "と"),
        ("いけません", "いける"), ("ね", "ね")]
    assert tokens[2].furigana == (("探", "さが"), ("さない", ""))


def test_a_word_yomitan_doesnt_read_whole_takes_its_dictionary_forms(monkeypatch):
    tabete = [M("食べ", "食べる", "タベ", "動詞", "自立"), M("て", "て", "テ", "助詞", "接続助詞"),
              M("しまっ", "しまう", "シマッ", "動詞", "非自立"), M("た", "た", "タ", "助動詞", ""),
              M("田中", "田中", "タナカ", "名詞", "固有名詞")]
    monkeypatch.setattr(mecab, "parse", lambda lines, found=None: [tabete])
    known = {"食べる": [seg("食べる", [("食べる", "たべる")])]}
    [tokens] = mecab.tokenize_lines(["食べてしまった田中"], found=object(),
                                    line_words=fake_yomitan(known))
    assert [(t.surface, t.base) for t in tokens] == [("食べてしまった", "食べる"), ("田中", "")]
    assert tokens[0].furigana == (("食べ", "たべ"), ("て", ""), ("しまっ", ""), ("た", ""))


def test_a_grammar_word_keeps_mecabs_dictionary_form(monkeypatch):
    morphemes = [M("ませ", "ます", "マセ", "助動詞", "")]
    monkeypatch.setattr(mecab, "parse", lambda lines, found=None: [morphemes])
    [tokens] = mecab.tokenize_lines(["ませ"], found=object(), line_words=fake_yomitan(
        {"ませ": [seg("ませ", [("増す", "ます")])]}))
    assert tokens[0].base == "ます"


def test_parse_reads_mecabs_output(monkeypatch):
    out = ("探さ\t探す\tサガサ\t動詞\t自立\r\nない\tない\tナイ\t助動詞\t\r\nEOS\r\n"
           "EOS\r\n").encode()
    sent = []

    def run(command, folder, args, data, timeout=None, check=True):
        sent.append(data)
        return out

    monkeypatch.setattr(mecab, "_run", run)
    found = mecab.Mecab(["mecab"], None, "utf8")
    assert mecab.parse(["探さない", "a\tb"], found) == [
        [M("探さ", "探す", "サガサ", "動詞", "自立"), M("ない", "ない", "ナイ", "助動詞", "")], []]
    assert sent == ["探さない\na b\n".encode()]  # a line holds no tab or line break
    monkeypatch.setattr(mecab, "_run", lambda *a, **k: b"EOS\n")
    with pytest.raises(mecab.MecabError, match="1 lines for 2"):
        mecab.parse(["a", "b"], found)

    def fails(*a, **k):
        raise subprocess.CalledProcessError(1, "mecab")

    monkeypatch.setattr(mecab, "_run", fails)
    with pytest.raises(yomitan.YomitanError, match="MeCab failed"):  # shown as a failed split
        mecab.parse(["a"], found)


def test_mecab_is_looked_for_in_the_add_ons_the_known_ones_first(tmp_path):
    def install(folder):
        folder.mkdir(parents=True)
        for name in (mecab.EXECUTABLE, "sys.dic", "dicrc", "mecabrc"):
            (folder / name).write_text("")

    install(tmp_path / "aaa_other")
    install(tmp_path / "3918629684" / "support")
    install(tmp_path / "1974309724")
    (tmp_path / "no_dictionary").mkdir()
    (tmp_path / "no_dictionary" / mecab.EXECUTABLE).write_text("")
    folders = [folder for _, folder in mecab._candidates(tmp_path) if folder is not None]
    assert folders == [tmp_path / "1974309724", tmp_path / "3918629684" / "support",
                       tmp_path / "aaa_other"]
    command, _ = next(mecab._candidates(tmp_path))
    assert command[1:3] == ["-d", str(tmp_path / "1974309724")] and command[3] == "-r"


def test_only_an_ipadic_mecab_is_used(monkeypatch):
    def run(output):
        def fake(command, folder, args, data, timeout=None, check=True):
            return b"charset:\tutf8\n" if args == ["-D"] else output.encode()
        return fake

    monkeypatch.setattr(mecab, "_run", run("名詞,一般,*,*,*,*,猫,ネコ,ネコ\nEOS\n"))
    assert mecab._check(["mecab"], None) == mecab.Mecab(["mecab"], None, "utf8")
    unidic = "名詞,普通名詞,一般,*,*,*,ネコ,猫,猫,ネコ,猫,ネコ,和,*,*,*,*\nEOS\n"
    monkeypatch.setattr(mecab, "_run", run(unidic))
    assert mecab._check(["mecab"], None) is None


def test_the_parser_setting_picks_the_split_and_names_the_caches(monkeypatch):
    found = mecab.Mecab(["mecab"], None, "utf8")
    monkeypatch.setattr(mecab, "_found", (found,))
    cfg = config.merge({})
    assert tokenizer.uses_mecab(cfg) is found
    assert tokenizer.analysis_key(cfg) == f"mecab:{cfg['scan_length']}"
    yomitan_only = {**cfg, "parser": "yomitan"}
    assert tokenizer.uses_mecab(yomitan_only) is None
    assert tokenizer.analysis_key(yomitan_only) == cfg["scan_length"]  # as before: kept
    monkeypatch.setattr(mecab, "_found", (None,))
    assert tokenizer.analysis_key(cfg) == cfg["scan_length"]
