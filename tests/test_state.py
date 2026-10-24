"""Knowledge with a fake collection, the stores, the immersion log and the subtitle session."""
import json

import pytest

from ankime.core import config, store
from ankime.core import immersion
from ankime.language import knowledge
from ankime.subs import session
from ankime.core.model import Event


class FakeModels:
    def __init__(self, models):
        self._models = models

    def by_name(self, name):
        return self._models.get(name)


class FakeDb:
    def __init__(self, rows):
        self._rows = rows  # model id -> [(note id, flds, ivl, data, queue[, card id])]

    def all(self, sql, mid):
        return [(*r[:4], self.card_id(r), r[4]) for r in self._rows.get(mid, [])]

    @staticmethod
    def card_id(row):
        return row[5] if len(row) > 5 else row[0]  # one card per note unless given

    def first(self, sql, card_id):
        return next(((r[2], r[3]) for rows in self._rows.values() for r in rows
                     if self.card_id(r) == card_id), None)


class FakeCol:
    def __init__(self, models=None, rows=None):
        self.models = FakeModels(models or {})
        self.db = FakeDb(rows or {})
        self.config = {}
        self.writes = 0

    def get_config(self, key, default=None):
        return self.config.get(key, default)

    def set_config(self, key, value):
        self.config[key] = json.loads(json.dumps(value))
        self.writes += 1

    def all_config(self):
        return dict(self.config)


MODEL = {"id": 1, "flds": [{"name": "Sentence"}, {"name": "Word"}]}
CFG = config.merge({"note_type": "Mining", "word_field": "Word", "known_interval": 21})


def make_knowledge(rows, tmp_path, monkeypatch, cfg=CFG, fsrs=False):
    monkeypatch.setattr(knowledge, "OLD_KNOWN_FILE", tmp_path / "missing.json")
    col = FakeCol({"Mining": MODEL}, {1: rows})
    col.config["fsrs"] = fsrs
    know = knowledge.Knowledge(col=col, config=lambda: cfg)
    know.rebuild()
    return know, col


@pytest.fixture
def know(tmp_path, monkeypatch):
    rows = [(10, "s\x1f食べる", 30, "", 2), (11, "s\x1f<b>猫</b>", 2, "", 2),
            (12, "s\x1f猫", 0, "", 0)]
    return make_knowledge(rows, tmp_path, monkeypatch)[0]


def test_classify(know):
    assert know.classify("食べた", "食べる") == "known"
    assert know.classify("猫") == "learning"
    assert know.classify("犬") == "new"
    assert know.classify("。") == ""


def test_line_kind_counts_distinct_new_words(know):
    assert know.line_kind([("食べた", "", "食べる", ())]) == "known"
    assert know.line_kind([("犬", "", "犬", ()), ("犬", "", "犬", ())]) == "i+1"
    assert know.line_kind([("犬", "", "犬", ()), ("鳥", "", "鳥", ())]) == "harder"


def test_particles_are_not_new_words(know):
    assert know.classify("は", "は") == ""
    assert know.classify("でした", "だ") == ""
    assert know.line_kind([("猫", "", "猫", ()), ("は", "", "は", ()),
                           ("食べた", "", "食べる", ())]) == "known"


def test_other_spellings_count_and_the_best_card_wins(tmp_path, monkeypatch):
    rows = [(10, "s\x1f綺麗", 30, "", 2), (11, "s\x1fきらい", 3, "", 2)]
    know = make_knowledge(rows, tmp_path, monkeypatch)[0]
    assert know.classify("奇麗", "奇麗") == "new"
    assert know.classify("奇麗", "奇麗", ("綺麗", "きれい")) == "known"
    assert know.classify("嫌い", "嫌い", ("きらい",)) == "learning"
    assert know.coverage({"奇麗": {"綺麗"}, "犬": frozenset()}) == {"known": 1, "learning": 0,
                                                                   "new": 1}


def test_suspended_cards_dont_count_unless_asked(tmp_path, monkeypatch):
    rows = [(10, "s\x1f食べる", 30, "", -1), (11, "s\x1f猫", 30, "", -1),
            (12, "s\x1f猫", 3, "", 2)]
    know = make_knowledge(rows, tmp_path, monkeypatch)[0]
    assert know.classify("食べる") == "new" and know.classify("猫") == "learning"
    cfg = {**CFG, "count_suspended": True}
    know = make_knowledge(rows, tmp_path, monkeypatch, cfg)[0]
    assert know.classify("食べる") == "known" and know.classify("猫") == "known"


def test_a_suspended_mining_card_still_takes_the_sentence(tmp_path, monkeypatch):
    know = make_knowledge([(10, "s\x1f食べる", 30, "", -1)], tmp_path, monkeypatch)[0]
    assert know.classify("食べる") == "new"
    assert know.note_for("食べる") == 10  # no duplicate card for it
    assert not know.counts(10)


def test_fsrs_stability_measures_cards_when_fsrs_is_on(tmp_path, monkeypatch):
    rows = [(10, "s\x1f食べる", 30, '{"s": 5.2, "d": 6.1}', 2),
            (11, "s\x1f猫", 3, '{"s": 40.0}', 2), (12, "s\x1f犬", 25, "", 2),
            (13, "s\x1f鳥", 25, "{bad json", 2)]
    know = make_knowledge(rows, tmp_path, monkeypatch, fsrs=True)[0]
    assert [know.classify(w) for w in ("食べる", "猫", "犬", "鳥")] == [
        "learning", "known", "known", "known"]
    know = make_knowledge(rows, tmp_path, monkeypatch, fsrs=False)[0]
    assert know.classify("食べる") == "known" and know.classify("猫") == "learning"


def test_a_field_with_several_words_counts_each(tmp_path, monkeypatch):
    know = make_knowledge([(10, "s\x1f食べる, 物", 30, "", 2)], tmp_path, monkeypatch)[0]
    assert know.classify("物") == "known" and know.classify("食べる") == "known"
    assert know.note_for("物") == 10


def test_note_for_finds_existing_mining_note(know):
    assert know.note_for("犬", "食べる") == 10
    assert know.note_for("犬") is None
    know.add_mined("犬", 99)
    assert know.note_for("犬") == 99
    assert know.classify("犬") == "learning"


OWN = f"{knowledge.KNOWN_PREFIX}::this"


def test_manual_words_are_synced_and_seeded_from_old_places(tmp_path, monkeypatch):
    old = tmp_path / "known_words.json"
    old.write_text(json.dumps(["犬"]), encoding="utf-8")
    monkeypatch.setattr(knowledge, "OLD_KNOWN_FILE", old)
    col = FakeCol({"Mining": MODEL}, {1: []})
    know = knowledge.Knowledge(col=col, config=lambda: CFG)
    assert know.is_marked("犬")
    know.set_marked("犬", False)
    assert not knowledge.Knowledge(col=col, config=lambda: CFG).is_marked("犬")
    writes = col.writes
    know.mark(["猫", "鳥", "猫"])
    assert col.writes == writes + 1  # one save for the whole batch
    own = store._decode_changes(col.config[OWN])
    assert {w for w, (marked, _) in own.items() if marked} == {"猫", "鳥"}
    # the single synced list of the previous version wins over the file
    col.config[knowledge.KNOWN_KEY] = ["魚"]
    again = knowledge.Knowledge(col=col, config=lambda: CFG)
    assert again.is_marked("魚") and again.is_marked("猫") and not again.is_marked("犬")


def test_marks_from_two_computers_are_merged(tmp_path, monkeypatch):
    monkeypatch.setattr(knowledge, "OLD_KNOWN_FILE", tmp_path / "missing.json")
    col = FakeCol({"Mining": MODEL}, {1: []})
    # what another computer recorded and synced: 犬 marked, 猫 marked then unmarked later
    col.config[f"{knowledge.KNOWN_PREFIX}::other"] = {"犬": [True, 10.0], "猫": [False, 30.0]}
    col.config[OWN] = {"猫": [True, 20.0], "鳥": [True, 20.0]}
    know = knowledge.Knowledge(col=col, config=lambda: CFG)
    assert know.is_marked("犬") and know.is_marked("鳥")
    assert not know.is_marked("猫")  # the newest change wins, whichever computer made it
    know.mark(["犬"], False)
    assert col.config[f"{knowledge.KNOWN_PREFIX}::other"]["犬"] == [True, 10.0]  # untouched


def test_marks_a_sync_brought_are_read_again(tmp_path, monkeypatch):
    know, col = make_knowledge([], tmp_path, monkeypatch)
    know.mark(["鳥"])
    col.config[f"{knowledge.KNOWN_PREFIX}::other"] = {"10": {"犬": True}}  # synced in
    assert not know.is_marked("犬")
    know.reload_marks()
    assert know.is_marked("犬") and know.is_marked("鳥")


def test_synced_map_merges_by_time_and_ignores_junk():
    col = FakeCol()
    col.config["p::a"] = {"x": [1, 5], "y": "junk", "z": [3, "bad"]}
    col.config["p::b"] = "junk"
    clock = iter([100.0, 200.0])
    m = store.SyncedMap("p", col, legacy=lambda: {"x": 0, "w": 9}, device="me",
                        clock=lambda: next(clock))
    assert m.load() == {"x": 1, "w": 9}
    m.set_many({"x": 2})
    assert col.config["p::me"] == {"100": {"x": 2}}
    assert store.SyncedMap("p", col, device="other").load() == {"x": 2}


def test_synced_map_groups_changes_by_time():
    col = FakeCol()
    m = store.SyncedMap("p", col, device="me", clock=lambda: 100.7)
    m.load()
    m.set_many({w: True for w in "abc"})  # an imported word list: one group, one stamp
    assert col.config["p::me"] == {"100": {"a": True, "b": True, "c": True}}


def test_synced_map_reads_the_older_format_and_rewrites_it():
    col = FakeCol()
    col.config["p::me"] = {"x": [1, 50.5], "y": [2, 60.0]}  # written by an older version
    col.config["p::other"] = {"70": {"y": 3}, "z": [4, 10]}  # a newer one, mixed
    m = store.SyncedMap("p", col, device="me", clock=lambda: 80.0)
    assert m.load() == {"x": 1, "y": 3, "z": 4}
    m.set_many({"w": 5})
    # the own entry is in the new format now; the overridden change to y is gone
    assert col.config["p::me"] == {"50": {"x": 1}, "80": {"w": 5}}
    assert store.SyncedMap("p", col, device="third").load() == {"x": 1, "y": 3, "z": 4, "w": 5}


def test_synced_map_keeps_a_change_another_computer_made_earlier():
    col = FakeCol()
    col.config["p::other"] = {"10": {"x": 1}}
    m = store.SyncedMap("p", col, device="me", clock=lambda: 20.0)
    m.load()
    m.set_many({"x": 2})
    assert col.config["p::me"] == {"20": {"x": 2}}
    assert store.SyncedMap("p", col, device="third").load() == {"x": 2}


def test_a_review_updates_its_card_only(tmp_path, monkeypatch):
    # 猫 has two cards: 40 days and 5 days; 犬 one of 30
    rows = [(10, "s猫", 40, "", 2, 1), (10, "s猫", 5, "", 2, 2),
            (11, "s犬", 30, "", 2, 3)]
    know, col = make_knowledge(rows, tmp_path, monkeypatch)
    assert know.classify("猫") == "known" and know.counts(1) and not know.counts(99)
    col.db._rows[1][0] = (10, "s猫", 1, "", 2, 1)  # lapsed
    know.update_card(1, knowledge.Knowledge.read_card(col, 1))
    assert know.classify("猫") == "learning"  # its other card is only 5 days
    assert know.classify("犬") == "known"
    know.update_card(2, 25)
    assert know.classify("猫") == "known"
    know.update_card(99, 0)  # not a card that counts: nothing happens


def test_missing_note_type_or_field_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(knowledge, "OLD_KNOWN_FILE", tmp_path / "missing.json")
    know = knowledge.Knowledge(col=FakeCol(), config=lambda: CFG)
    with pytest.raises(Exception, match="not found"):
        know.rebuild()


def test_blank_word_field_only_uses_marked_words(tmp_path, monkeypatch):
    monkeypatch.setattr(knowledge, "OLD_KNOWN_FILE", tmp_path / "missing.json")
    know = knowledge.Knowledge(col=FakeCol(), config=lambda: {**CFG, "word_field": ""})
    know.rebuild()
    know.set_marked("猫", True)
    assert know.classify("猫") == "known" and know.classify("犬") == "new"


def test_known_words_from_cards_and_marks(know):
    know.mark(["鳥", "犬"])
    assert know.known_words() == sorted(["食べる", "鳥", "犬"])  # 猫 is still learning
    assert know.known_words(marked_only=True) == sorted(["鳥", "犬"])
    text = knowledge.export_text(know.known_words(), "Ankime known words")
    assert text.startswith("# Ankime known words\n")
    assert knowledge.words_in_list(text) == know.known_words()  # it reads back


def test_new_words_of_a_line_in_order(know):
    tokens = [("鳥", "", "鳥", ()), ("が", "", "が", ()), ("猫", "", "猫", ()),
              ("食べた", "", "食べる", ()), ("犬", "", "犬", ()), ("鳥", "", "鳥", ()),
              ("タロウ", "", "", ())]  # no headword: a name, never new
    assert know.new_words(tokens) == ["鳥", "犬"]
    know.mark(know.new_words(tokens))
    assert know.line_kind(tokens) == "known"


# --- stores ------------------------------------------------------------------

def test_json_file_atomic_and_dirty_check(tmp_path):
    f = store.JsonFile(tmp_path / "sub" / "a.json")
    assert f.load("default") == "default"
    assert f.save({"a": 1})
    assert not f.save({"a": 1})  # unchanged: not rewritten
    assert f.save({"a": 2})
    assert store.JsonFile(f.path).load() == {"a": 2}
    assert list(f.path.parent.iterdir()) == [f.path]  # no temp file left behind


def test_json_file_load_counts_as_saved(tmp_path):
    path = tmp_path / "a.json"
    path.write_text('{"x": [1]}', encoding="utf-8")
    f = store.JsonFile(path)
    f.load()
    assert not f.save({"x": [1]})


def test_synced_value_skips_unchanged_writes():
    col = FakeCol()
    value = store.SyncedValue("k", col)
    assert value.load([]) == []
    value.save([1])
    value.save([1])
    assert col.writes == 1


def test_immersion_log():
    col = FakeCol()
    log = immersion.ImmersionLog(col, today=lambda: "2026-10-22", device="me")
    log.add_watch(30.4)
    log.add_watch(30)
    log.add_cards()
    log.add_cards(-5)  # never below zero
    log.add_cards(2)
    log.flush()
    assert col.config[f"{immersion.LOG_PREFIX}::me"] == {"2026-10-22": [60, 2]}
    again = immersion.ImmersionLog(col, today=lambda: "2026-10-23", device="me")
    assert again.today() == (0.0, 0)
    assert again.recent() == [("2026-10-22", 60.0, 2)]
    assert immersion.fmt_duration(3725) == "1h 02m"


def test_immersion_log_adds_up_computers_and_the_old_entry():
    col = FakeCol()
    col.config[immersion.LOG_KEY] = {"2026-10-21": [100, 1]}
    col.config[f"{immersion.LOG_PREFIX}::other"] = {"2026-10-22": [300, 2], "bad": "x"}
    log = immersion.ImmersionLog(col, today=lambda: "2026-10-22", device="me")
    log.add_watch(60)
    log.flush()
    assert log.today() == (360.0, 2)
    assert log.days == {"2026-10-21": [100.0, 1], "2026-10-22": [360.0, 2]}
    assert col.config[f"{immersion.LOG_PREFIX}::me"] == {"2026-10-22": [60, 0]}  # own only
    assert log.streak() == 2
    col.config[f"{immersion.LOG_PREFIX}::other"] = {"2026-10-22": [600, 3]}  # synced in
    log.reload_others()
    assert log.today() == (660.0, 3)
    col.config[f"{immersion.LOG_PREFIX}::other"] = {"2026-10-22": [300, 2], "bad": "x"}
    log.reload_others()
    assert log.last_days(3) == [("2026-10-20", 0.0), ("2026-10-21", 100.0),
                                ("2026-10-22", 360.0)]


def test_streak():
    days = {"2026-10-20": (600, 0), "2026-10-21": (0, 1), "2026-10-22": (10, 0)}
    assert immersion.streak(days, "2026-10-22") == 2  # today has barely started: not yet
    assert immersion.streak(days, "2026-10-21") == 2
    assert immersion.streak(days, "2026-10-24") == 0


def test_show_stats_count_cards_per_show_with_seasons_together():
    day = 1792000000000  # mid-October 2026 in ms, as a note id
    rows = [(day, " anime ankime::Frieren::Season_1 "), (day + 2, " ankime::frieren::Season_2 "),
            (day + 1, " ankime::Mushishi "), (day, " ankime:: ankime::::x other "),
            (day, " Ankime::Mushishi ankime::Mushishi::Season_1 ")]
    stats = immersion.show_stats(rows, rollover=0)
    last = immersion.study_day(immersion.datetime.datetime.fromtimestamp(day / 1000), 0)
    assert stats == [("Frieren", 2, last), ("Mushishi", 2, last)]


def test_immersion_log_reads_shows_from_the_collection():
    col = FakeCol()
    col.db.all = lambda sql, pattern: [(1792000000000, " ankime::Show_A ")]
    log = immersion.ImmersionLog(col, today=lambda: "2026-10-22", device="me")
    assert [(name, n) for name, n, _ in log.shows()] == [("Show A", 1)]


# --- subtitle session -----------------------------------------------------------

EVENTS = [Event(1.0, 2.0, "一"), Event(3.0, 4.0, "二"), Event(5.0, 6.0, "三"),
          Event(7.0, 8.0, "二")]


@pytest.fixture
def sess():
    s = session.SubtitleSession()
    s.events = list(EVENTS)
    s.tokens = {"一": [("猫", "", "猫", ())], "二": [("犬", "", "犬", ()), ("猫", "", "猫", ())],
                "三": [("食べた", "", "食べる", ())]}
    return s


def test_index_at_prefers_same_text_and_handles_delay(sess):
    assert sess.index_at(3.5, 0.5, "二") == 1
    assert sess.index_at(7.0, 0.0, "二") == 3
    assert sess.index_at(20.0, 0.0, "二") is None


def test_span_with_extended_range(sess):
    sess.set_line("二", 3.0, 4.0, "v", "")
    assert sess.span(0) == ("二", 3.0, 4.0)
    assert sess.extend(0, 0) and sess.extend(1, 0)
    assert sess.span(0) == ("一\n二\n三", 1.0, 6.0)
    assert not sess.extend(0, 0)  # nothing before the first line
    sess.set_line("三", 5.0, 6.0, "v", "")
    assert sess.extra == [0, 0]  # a new line starts from scratch


def test_set_line_dedupes_and_keeps_range_when_looping(sess):
    assert sess.set_line("二", 3.0, 4.0, "v", "")
    assert not sess.set_line("二", 3.0, 4.0, "v", "")
    sess.extend(1, 0)
    sess.set_line("", None, None, "v", "")  # the gap keeps the range
    assert sess.extra == [0, 1]
    sess.set_line("二", 3.0, 4.0, "v", "", keep_range=True)
    assert sess.extra == [0, 1]


def test_neighbour_time(sess):
    sess.set_line("二", 3.0, 4.0, "v", "")
    assert sess.neighbour_time(3.5, 0, 1) == 5.0
    assert sess.neighbour_time(3.5, 0, -1) == 1.0
    sess.set_line("", None, None, "v", "")
    assert sess.neighbour_time(4.5, 0, -1) == 3.0  # between lines: the one before
    assert sess.neighbour_time(7.5, 0, 1) is None


def test_translation_for_overlap(sess):
    sess.target_events = [(0.9, 2.1, "one"), (2.9, 4.0, "two"), (4.0, 5.2, "edge")]
    assert sess.translation_for(1.0, 4.0, 0) == "one\ntwo"
    assert sess.translation_for(10, 11, 0, "fallback") == "fallback"


def test_new_word_counts_and_search(sess, know):
    assert sess.new_word_counts(know) == [("犬", 2)]
    assert sess.best_line_for("犬", know) == (1, "犬")
    assert sess.best_line_for("魚", know) is None
    assert sess.lines_matching("食べる") == [2]  # by dictionary form
    assert sess.lines_matching("二") == [1, 3]
    assert sess.lines_matching("") == [0, 1, 2, 3]


def test_lru():
    lru = session.LRU(2)
    lru.put("a", 1)
    lru.put("b", 2)
    lru.get("a")
    lru.put("c", 3)
    assert "a" in lru and "c" in lru and "b" not in lru


def test_tokenize_file_uses_disk_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(session, "TOKEN_CACHE_DIR", tmp_path / "cache")
    sub = tmp_path / "ep.srt"
    sub.write_text("1\n00:00:01,000 --> 00:00:02,000\n猫\n", encoding="utf-8")
    calls = []

    def tokenize_many(texts):
        calls.append(texts)
        return [[(t, "", t, ("alt",))] for t in texts]

    first = session.tokenize_file(sub, 20, tokenize_many)
    second = session.tokenize_file(sub, 20, tokenize_many)
    assert first == second == ([(1.0, 2.0, "猫")], {"猫": [("猫", "", "猫", ("alt",))]})
    assert len(calls) == 1
    session.tokenize_file(sub, 10, tokenize_many)  # another scan length: another cache
    assert len(calls) == 2


def test_best_line_for_prefers_i1_then_the_shortest_line(know):
    s = session.SubtitleSession()
    s.events = [(1.0, 2.0, "犬と鳥と魚がいた"), (3.0, 4.0, "大きな犬と鳥"), (5.0, 6.0, "犬だ"),
                (7.0, 8.0, "あの犬は猫が好き")]
    dog, bird, fish = ("犬", "", "犬", ()), ("鳥", "", "鳥", ()), ("魚", "", "魚", ())
    cat = ("猫", "", "猫", ())
    s.tokens = {"犬と鳥と魚がいた": [dog, bird, fish], "大きな犬と鳥": [dog, bird],
                "犬だ": [dog], "あの犬は猫が好き": [dog, cat]}
    # 犬だ and あの犬は猫が好き are both i+1 (猫 is learning): the shorter wins
    assert s.best_line_for("犬", know) == (2, "犬")
    # no i+1 line has 鳥: the shortest one does
    assert s.best_line_for("鳥", know) == (1, "鳥")


def test_clear_token_cache_makes_files_tokenized_again(tmp_path, monkeypatch):
    monkeypatch.setattr(session, "TOKEN_CACHE_DIR", tmp_path / "cache")
    session.clear_token_cache()  # nothing cached yet: no error
    sub = tmp_path / "ep.srt"
    sub.write_text("1\n00:00:01,000 --> 00:00:02,000\n猫\n", encoding="utf-8")
    calls = []

    def tokenize_many(texts):
        calls.append(texts)
        return [[(t, "", t, ())] for t in texts]

    session.tokenize_file(sub, 20, tokenize_many)
    session.clear_token_cache()
    session.tokenize_file(sub, 20, tokenize_many)
    assert len(calls) == 2


# --- sentence cards (sentence_words) ------------------------------------------

SENTENCE_CFG = {**CFG, "sentence_field": "Sentence", "sentence_words": True}
SPLITS = {"猫が魚を食べた": [("猫", "", "猫", ()), ("が", "", "が", ()), ("魚", "", "魚", ()),
                          ("を", "", "を", ()), ("食べた", "", "食べる", ())],
          "犬が走る": [("犬", "", "犬", ()), ("が", "", "が", ()), ("走る", "", "走る", ())],
          "鳥だ": [("鳥", "", "鳥", ()), ("だ", "", "だ", ())]}


@pytest.fixture
def yomitan_calls(monkeypatch):
    """Yomitan splits the sentences of SPLITS; the texts it was asked about, call by call."""
    calls = []

    def tokenize_many(texts):
        calls.append(list(texts))
        return [SPLITS[t] for t in texts]

    monkeypatch.setattr(knowledge.tokenizer, "tokenize_many", tokenize_many)
    return calls


def test_every_word_of_a_known_sentence_card_is_known(tmp_path, monkeypatch, yomitan_calls):
    rows = [(10, "猫が<b>魚</b>を食べた\x1f魚", 30, "", 2), (11, "犬が走る\x1f", 3, "", 2)]
    know = make_knowledge(rows, tmp_path, monkeypatch, SENTENCE_CFG)[0]
    assert [know.classify(w, w) for w in ("猫", "魚", "食べる")] == ["known"] * 3
    assert know.classify("が", "が") == ""
    # a sentence card that isn't known yet doesn't make its words learning
    assert know.classify("犬", "犬") == "new" and know.classify("走る", "走る") == "new"
    assert yomitan_calls == [["猫が魚を食べた"]]  # only known cards' sentences are split
    assert know.known_words() == ["猫", "食べる", "魚"]
    assert know.known_words(marked_only=True) == []


def test_sentence_words_are_off_by_default(tmp_path, monkeypatch, yomitan_calls):
    know = make_knowledge([(10, "猫が魚を食べた\x1f魚", 30, "", 2)], tmp_path, monkeypatch,
                          {**SENTENCE_CFG, "sentence_words": False})[0]
    assert know.classify("猫", "猫") == "new" and know.classify("魚", "魚") == "known"
    assert yomitan_calls == [] and not know.counts(99)


def test_sentences_are_split_once_and_edits_again(tmp_path, monkeypatch, yomitan_calls):
    rows = [(10, "猫が魚を食べた\x1f", 30, "", 2)]
    make_knowledge(rows, tmp_path, monkeypatch, SENTENCE_CFG)
    know, col = make_knowledge(rows, tmp_path, monkeypatch, SENTENCE_CFG)
    assert yomitan_calls == [["猫が魚を食べた"]] and know.classify("猫", "猫") == "known"
    col.db._rows[1][0] = (10, "犬が走る\x1f", 30, "", 2)  # edited
    know.rebuild()
    assert yomitan_calls[1:] == [["犬が走る"]]
    assert know.classify("犬", "犬") == "known" and know.classify("猫", "猫") == "new"
    cached = json.loads(knowledge.SENTENCE_CACHE.read_text(encoding="utf-8"))
    assert cached == {"analysis": CFG["scan_length"], "words": {"犬が走る": ["犬", "走る"]}}
    knowledge.clear_sentence_cache()
    know.rebuild()
    assert yomitan_calls[2:] == [["犬が走る"]]


def test_sentences_yomitan_couldnt_split_wait_for_the_next_read(tmp_path, monkeypatch):
    def offline(texts):
        raise knowledge.yomitan.YomitanOffline("no Yomitan")

    monkeypatch.setattr(knowledge.tokenizer, "tokenize_many", offline)
    rows = [(10, "猫が魚を食べた\x1f", 30, "", 2)]
    know, _ = make_knowledge(rows, tmp_path, monkeypatch, SENTENCE_CFG)
    assert know.classify("猫", "猫") == "new" and know.counts(10)
    monkeypatch.setattr(knowledge.tokenizer, "tokenize_many", lambda ts: [SPLITS[t] for t in ts])
    know.rebuild()
    assert know.classify("猫", "猫") == "known"


def test_a_review_of_a_sentence_card(tmp_path, monkeypatch, yomitan_calls):
    # 猫 is in two known sentences (cards 1 and 2); card 3 isn't known yet
    rows = [(10, "猫が魚を食べた\x1f", 40, "", 2, 1), (11, "猫が魚を食べた\x1f", 25, "", 2, 2),
            (12, "犬が走る\x1f", 5, "", 2, 3)]
    know, col = make_knowledge(rows, tmp_path, monkeypatch, SENTENCE_CFG)
    assert not know.update_card(1, 2)  # lapsed: card 2 still knows 猫
    assert know.classify("猫", "猫") == "known"
    assert not know.update_card(2, 3)
    assert know.classify("猫", "猫") == "new"  # no known sentence has it: not 'learning'
    assert know.update_card(3, 30)  # known now: its sentence has to be split, read again
    assert know.classify("犬", "犬") == "new"
    col.db._rows[1][2] = (12, "犬が走る", 30, "", 2, 3)
    know.rebuild()
    assert know.classify("犬", "犬") == "known" and yomitan_calls[-1] == ["犬が走る"]


def test_sentence_sources_and_suspended_sentence_cards(tmp_path, monkeypatch, yomitan_calls):
    monkeypatch.setattr(knowledge, "OLD_KNOWN_FILE", tmp_path / "missing.json")
    other = {"id": 2, "flds": [{"name": "Front"}, {"name": "Expression"}]}
    col = FakeCol({"Mining": MODEL, "Core": other},
                  {1: [(10, "猫が魚を食べた\x1f魚", 30, "", -1)], 2: [(20, "鳥だ\x1f鳥", 30, "", 2)]})
    cfg = {**SENTENCE_CFG, "sentence_sources": [{"note_type": "Core", "sentence_field": "Front"}]}
    know = knowledge.Knowledge(col=col, config=lambda: cfg)
    know.rebuild()
    assert know.classify("鳥", "鳥") == "known" and know.classify("猫", "猫") == "new"
    cfg = {**cfg, "count_suspended": True}
    know.rebuild()
    assert know.classify("猫", "猫") == "known"
    cfg = {**cfg, "sentence_sources": [{"note_type": "Core", "sentence_field": "Back"}]}
    with pytest.raises(knowledge.SetupError, match="no field 'Back'"):
        know.rebuild()
    cfg = {**cfg, "sentence_sources": ["Core"]}
    with pytest.raises(knowledge.SetupError, match="sentence_sources"):
        know.rebuild()
