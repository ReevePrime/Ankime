"""Sanitizing dictionary HTML, furigana, the every-line pause, the note type, first run and
word audio."""
import base64
import types

from ankime.cards import media, notes, presets
from ankime.core import config
from ankime.dialogs import first_run
from ankime.language import line_filter, sanitize, yomitan
from ankime.player import popup, sub_panel
from ankime.subs import session


# --- sanitizing dictionary HTML ---------------------------------------------------

def test_clean_html_keeps_structure_and_drops_script():
    dirty = ('<ul class="g" data-sc-content="x"><li onclick="pycmd(1)">to eat'
             '<script>pycmd("add")</script></li></ul><img src=x onerror=alert(1)>'
             '<a href="javascript:alert(1)">link</a><style>body{}</style>')
    assert sanitize.clean_html(dirty) == (
        '<ul class="g" data-sc-content="x"><li>to eat</li></ul><img src="x"><a>link</a>')


def test_clean_html_blocks_remote_content_and_unsafe_style():
    assert sanitize.clean_html('<img src="https://t.example/p.png">') == "<img>"
    assert sanitize.clean_html('<img src="//t.example/p.png">') == "<img>"
    assert sanitize.clean_html('<img src="../../secret.png">') == "<img>"
    assert sanitize.clean_html('<img src="data:image/png;base64,AA">') == (
        '<img src="data:image/png;base64,AA">')
    assert sanitize.clean_html('<img src="yomitan_dict_1.png">') == (
        '<img src="yomitan_dict_1.png">')
    assert sanitize.clean_html('<span style="background:url(http://x)">a</span>') == (
        "<span>a</span>")
    assert sanitize.clean_html('<span style="font-weight:bold">a</span>') == (
        '<span style="font-weight:bold">a</span>')


def test_clean_html_escapes_text_and_closes_tags():
    assert sanitize.clean_html("a &lt;b&gt; <b>c") == "a &lt;b&gt; <b>c</b>"
    assert sanitize.clean_html("<div><span>x</div>") == "<div><span>x</span></div>"
    assert sanitize.clean_html("<iframe><b>x</b></iframe>y") == "y"
    assert sanitize.clean_html("") == "" and sanitize.clean_html(None) == ""


def test_popup_sanitizes_dictionary_html():
    out = popup.render_entries("猫", [{"expression": "<猫>", "reading": "ねこ",
                                      "glossary-brief": "cat<script>x()</script>",
                                      "pitch-accents": '<b onmouseover="x()">0</b>',
                                      "frequencies": "<img src=http://t/x>"}])
    assert "<script" not in out and "onmouseover" not in out and "http://t" not in out
    assert "&lt;猫&gt;" in out and "cat" in out and 'data-cmd="audio"' in out


# --- furigana and the panel -------------------------------------------------------

def test_render_sub_puts_readings_in_ruby():
    tokens = [("食べた", (("食", "た"), ("べた", "")), "食べる", ()), ("\n", "", "", ()),
              ("猫", "", "猫", ())]
    out = sub_panel.render_sub("x", tokens, lambda *_: "new")
    assert "<ruby>食<rt>た</rt></ruby>べた</span>" in out
    assert out.count("<ruby>") == 1 and "<br>" in out


def test_panel_height_grows_for_furigana_and_translation():
    base = sub_panel.panel_height(28)
    assert base == 150
    assert sub_panel.panel_height(28, furigana=True) > base
    assert sub_panel.panel_height(28, translation=True) > base


def test_page_has_no_unreplaced_placeholders():
    for marker in ("$FONTS", "$TR_SCALE", "FURIGANA_VALUES", "TRANSLATION_VALUES"):
        assert marker not in sub_panel.PAGE


def test_token_cache_round_trips_furigana(tmp_path, monkeypatch):
    monkeypatch.setattr(session, "TOKEN_CACHE_DIR", tmp_path / "cache")
    sub = tmp_path / "ep.srt"
    sub.write_text("1\n00:00:01,000 --> 00:00:02,000\n食べた\n", encoding="utf-8")
    token = ("食べた", (("食", "た"), ("べた", "")), "食べる", ())
    first = session.tokenize_file(sub, 20, lambda texts: [[token] for _ in texts])
    second = session.tokenize_file(sub, 20, lambda texts: 1 / 0)  # from the disk cache
    assert first == second
    assert second[1]["食べた"] == [token]


def test_new_episode_drops_the_last_episodes_tokens():
    s = session.SubtitleSession()
    s.tokens["猫"] = []
    s.new_episode()
    assert s.tokens == {}


# --- pausing after every line -------------------------------------------------------

def test_pause_plan_every_line():
    plan = line_filter.pause_plan
    assert plan("known", "every", 10, 12, 10.1, False) == ("at", 12)
    assert plan(None, "every", 10, 12, 10.1, False) == ("at", 12)  # not analysed yet
    assert plan("i+1", "every", 10, None, 10.1, False) is None


# --- the note type and first run -----------------------------------------------------

class FakeModels:
    def __init__(self):
        self.models = {}

    def by_name(self, name):
        return self.models.get(name)

    def new(self, name):
        return {"name": name, "flds": [], "tmpls": [], "css": ""}

    def new_field(self, name):
        return {"name": name}

    def add_field(self, model, field):
        model["flds"].append(field)

    def new_template(self, name):
        return {"name": name, "qfmt": "", "afmt": ""}

    def add_template(self, model, template):
        model["tmpls"].append(template)

    def add_dict(self, model):
        self.models[model["name"]] = model

    def all_names_and_ids(self):
        return [types.SimpleNamespace(name=name, id=i) for i, name in enumerate(self.models)]

    def add_fields(self, name, *fields):
        self.models[name] = {"name": name, "flds": [{"name": f} for f in fields]}


class FakeCol:
    def __init__(self):
        self.models = FakeModels()


def test_create_note_type_maps_every_field_and_reuses_or_renames():
    col = FakeCol()
    values = notes.create_note_type(col)
    model = col.models.by_name("Ankime")
    assert values["note_type"] == "Ankime"
    assert [f["name"] for f in model["flds"]] == list(notes.NOTE_TYPE_FIELDS)
    assert values["audio_field"] == "Audio" and values["word_field"] == "Word"
    assert set(values) - {"note_type"} <= set(config.DEFAULTS)
    assert notes.create_note_type(col)["note_type"] == "Ankime"  # reused
    assert len(col.models.models) == 1
    col.models.models["Ankime"] = {"name": "Ankime", "flds": [{"name": "Front"}]}
    assert notes.create_note_type(col)["note_type"] == "Ankime 2"  # someone else's Ankime
    for field in notes.NOTE_TYPE_FIELDS:
        assert field + "}}" in notes.NOTE_TYPE_FRONT + notes.NOTE_TYPE_BACK


def test_an_ankime_note_type_from_before_the_new_fields_is_reused():
    col = FakeCol()
    old = [{"name": f} for f in notes.NOTE_TYPE_FIELDS if f not in notes.NOTE_TYPE_ADDED_LATER]
    col.models.models["Ankime"] = {"name": "Ankime", "flds": old}
    values = notes.create_note_type(col)
    assert values["note_type"] == "Ankime" and values["sentence_field"] == "Sentence"
    assert values["context_field"] == values["sentence_furigana_field"] == ""


def test_first_run_only_offers_with_untouched_settings():
    assert first_run.untouched(config.merge({}))
    assert not first_run.untouched(config.merge({"audio_field": "Audio"}))


def test_first_run_guesses_the_fields_of_a_note_type():
    lapis = ["Expression", "ExpressionFurigana", "ExpressionReading", "ExpressionAudio",
             "SelectionText", "MainDefinition", "Sentence", "SentenceFurigana",
             "SentenceAudio", "Picture", "Glossary", "MiscInfo", "FreqSort"]
    preset, fields = first_run.guess_fields(lapis)
    assert preset == "Lapis" and fields["word_field"] == "Expression"
    assert fields["audio_field"] == "SentenceAudio" and fields["source_field"] == "MiscInfo"
    # no preset: by their names, each field used once
    preset, fields = first_run.guess_fields(["Front", "Back"])
    assert preset is None and fields["sentence_field"] == "Front"
    assert fields["definition_field"] == "Back" and fields["word_field"] == ""
    preset, fields = first_run.guess_fields(["Vocab", "Meaning", "Sentence", "Audio", "Image"])
    assert (fields["sentence_field"], fields["word_field"], fields["definition_field"],
            fields["audio_field"], fields["image_field"]) == (
        "Sentence", "Vocab", "Meaning", "Audio", "Image")
    assert set(fields) == set(presets.FIELD_KEYS)


def test_first_run_finds_the_note_types_people_mine_into():
    col = FakeCol()
    col.models.add_fields("Basic", "Front", "Back")
    col.models.add_fields("Kaishi 1.5k", "Word", "Word Reading", "Word Meaning", "Word Audio",
                          "Sentence", "Sentence Meaning", "Sentence Furigana",
                          "Sentence Audio", "Notes", "Pitch Accent", "Picture")
    col.models.add_fields("Mining", "Sentence", "Word", "Picture", "SentenceAudio")
    assert first_run.mined_note_types(col) == [("Kaishi 1.5k", "Kaishi 1.5k")]
    assert first_run.known_word_sources(col, "Basic") == [
        {"note_type": "Kaishi 1.5k", "word_field": "Word"}]
    assert first_run.known_word_sources(col, "Kaishi 1.5k") == []  # the cards' own


def test_first_run_summary():
    values = config.merge({"deck": "Mining <1>", "vocab_sources": [
        {"note_type": "Kaishi 1.5k", "word_field": "Word"}]})
    created = first_run.summary(values, create=True)
    assert "Mining &lt;1&gt;" in created and "new <b>Ankime</b> note type" in created
    assert "interval reaches 21 days" in created and "Kaishi 1.5k cards count" in created
    assert "Yomitan, at http://127.0.0.1:19633" in created and "libmpv" not in created
    basic = first_run.summary({**values, "dictionary": "local", "mpv_dll_dir": "C:/mpv"})
    assert "<b>Basic</b> notes (sentence → Front, definition → Back)" in basic
    assert "only the ones you mark known" in basic and "Ankime's own dictionaries" in basic
    assert "libmpv: C:/mpv" in basic


def test_first_run_offers_the_wizard_once_on_a_new_install(monkeypatch):
    saved, runs = {}, []
    monkeypatch.setattr(first_run.state, "get", lambda key: saved.get(key, False))
    monkeypatch.setattr(first_run.state, "save", lambda **values: saved.update(values))
    monkeypatch.setattr(first_run, "run_wizard", lambda *args: runs.append(args))
    monkeypatch.setattr(first_run.config, "get", lambda: config.merge({}))
    assert first_run.offer() and len(runs) == 1 and saved["note_type_offered"]
    assert not first_run.offer() and len(runs) == 1  # once
    saved.clear()
    monkeypatch.setattr(first_run.config, "get", lambda: config.merge({"word_field": "W"}))
    assert not first_run.offer() and not runs[1:] and saved["note_type_offered"]


# --- word audio and scratch files ------------------------------------------------------

def test_word_audio(monkeypatch):
    answers = iter([{"audioMedia": [{"ankiFilename": "neko.mp3",
                                     "content": base64.b64encode(b"mp3").decode()}]},
                    {"audioMedia": []}])
    monkeypatch.setattr(yomitan, "_post", lambda *a, **k: next(answers))
    assert yomitan.word_audio("猫") == ("neko.mp3", b"mp3")
    assert yomitan.word_audio("猫") is None


def test_scratch_file_keeps_only_the_name(tmp_path, monkeypatch):
    monkeypatch.setattr(media, "TEMP", tmp_path / "t")
    path = media.scratch_file("../../evil/x.mp3")
    assert path.parent == tmp_path / "t" and path.name.endswith("_x.mp3")
    path = media.scratch_file("..\\..\\y.mp3")
    assert path.parent == tmp_path / "t" and path.name.endswith("_y.mp3")


def test_word_css_by_default_keeps_the_page_look():
    css = sub_panel.word_css(sub_panel.word_styles(config.DEFAULTS))
    assert "html body .w.new { font-weight: bold; font-style: normal; text-decoration: none; }" in css
    assert "color" not in css and "background" not in css and "font-size" not in css


def test_word_style_presets_are_complete_and_valid():
    for name, _, styles in sub_panel.WORD_STYLE_PRESETS:
        assert set(styles) == {status for status, _, _ in sub_panel.WORD_STYLES}, name
        for style in styles.values():
            assert set(style) == set(config.DEFAULTS["new_word_style"])
            assert style["underline"] in dict(sub_panel.UNDERLINES)
            assert sub_panel.word_size(style) == style["size"]
        # new and learning words differ by more than their colour
        new, learning = styles["new"], styles["learning"]
        assert (new["underline"], new["bold"], new["size"]) != (
            learning["underline"], learning["bold"], learning["size"])


def test_popup_text_size():
    cfg = dict(config.DEFAULTS)
    assert popup.text_size(cfg) == 15
    assert popup.text_size({**cfg, "popup_font_size": 22}) == 22
    assert popup.text_size({**cfg, "popup_font_size": 99}) == 40  # within FONT_SIZES
    assert popup.text_size({**cfg, "popup_font_size": "x"}) == 15
    follow = {**cfg, "popup_font_size": popup.FOLLOW_SUBTITLES}
    assert popup.text_size(follow) == 15
    assert popup.text_size({**follow, "subtitle_font_size": 56}) == 30
    # the subtitles' zoom and fullscreen enlarge it past the setting's range
    assert popup.text_size({**cfg, "popup_font_size": 40}, 1.2 * 1.5) == 72
    assert popup.text_size(cfg, None) == 15


def test_word_css_from_chosen_styles():
    css = sub_panel.word_css({
        "new": {"color": "#D55E00", "background": "#000000", "bold": True, "italic": True,
                "underline": "wavy", "size": 130},
        "learning": {"underline": "dotted", "size": 999},
        "known": {"size": 10}})
    assert "html body .w.new { font-weight: bold; font-style: italic; text-decoration: " \
           "underline wavy 2px; text-underline-offset: .2em; font-size: 130%; " \
           "color: #D55E00 !important; }" in css
    assert "html body .w.new:not(:hover):not(.sel) { background: #000000; }" in css
    assert "font-size: 200%" in css and "font-size: 50%" in css  # kept within WORD_SIZES
    assert sub_panel.word_scale({"new": {"size": 130}, "known": {"size": 80}}) == 1.3
    assert sub_panel.word_scale({"new": {"size": 80}}) == 1.0


def test_word_css_lets_nothing_else_onto_the_page():
    css = sub_panel.word_css({
        "new": {"color": "red; } body { display: none", "background": "url(http://x)",
                "underline": "solid; color: red", "size": "big", "bold": "yes"},
        "learning": "blue"})
    assert css == ("html body .w.new { font-weight: bold; font-style: normal; "
                   "text-decoration: none; }")


def test_panel_height_grows_for_larger_words():
    assert sub_panel.panel_height(28, word_scale=1.5) > sub_panel.panel_height(28)
    assert sub_panel.panel_height(28, word_scale=0.5) == sub_panel.panel_height(28)
