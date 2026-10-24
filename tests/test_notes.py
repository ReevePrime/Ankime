"""Writing notes: media written for a note that isn't added is taken back."""
from unittest import mock

import pytest

from ankime.cards import notes
from ankime.core import config


class FakeMedia:
    def __init__(self, existing=()):
        self.files = set(existing)
        self.trashed = []

    def add_file(self, path):
        name = str(path).replace("\\", "/").rsplit("/", 1)[-1]
        self.files.add(name)
        return name

    def have(self, name):
        return name in self.files

    def write_data(self, name, data):
        self.files.add(name)
        return name

    def trash_files(self, names):
        self.trashed.extend(names)


class FakeNote(dict):
    id = 1
    tags = []


class FailingOp:
    """add_note / update_note whose operation fails."""

    def __init__(self, **_kwargs):
        self._failure = None

    def success(self, _fn):
        return self

    def failure(self, fn):
        self._failure = fn
        return self

    def run_in_background(self):
        self._failure(RuntimeError("database is locked"))


FIELDS = ["Sentence", "Audio", "Picture", "Word", "Definition"]
CFG = config.merge({"note_type": "Mining", "deck": "Mining", "sentence_field": "Sentence",
                    "audio_field": "Audio", "image_field": "Picture", "word_field": "Word",
                    "definition_field": "Definition"})


@pytest.fixture
def col(monkeypatch):
    fake = mock.MagicMock()
    fake.col.media = FakeMedia(existing={"shared.png"})
    fake.col.new_note.side_effect = lambda _model: FakeNote.fromkeys(FIELDS, "")
    fake.col.get_note.side_effect = lambda _nid: FakeNote.fromkeys(FIELDS, "")
    monkeypatch.setattr(notes, "mw", fake)
    monkeypatch.setattr(notes, "showWarning", mock.MagicMock())
    monkeypatch.setattr(config, "get", lambda: CFG)
    return fake.col


def test_a_failed_add_trashes_only_the_media_it_wrote(col, tmp_path, monkeypatch):
    monkeypatch.setattr(notes, "add_note", FailingOp)
    word = ({"word": "猫", "definition": '<img src="shared.png"><img src="new.png">'},
            [("shared.png", b"x"), ("new.png", b"y")])
    finished = []
    notes.add_card(None, "猫だ", tmp_path / "a.webp", tmp_path / "a.m4a", word=word,
                   on_finished=finished.append)
    assert finished == [False]
    # another card's picture stays
    assert sorted(col.media.trashed) == ["a.m4a", "a.webp", "new.png"]


def test_a_missing_field_is_reported_before_any_media_is_written(col, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "get", lambda: {**CFG, "source_field": "Nope"})
    added = mock.MagicMock()
    monkeypatch.setattr(notes, "add_note", added)
    notes.add_card(None, "猫だ", tmp_path / "a.webp", tmp_path / "a.m4a",
                   origin=("Show/ep1.mkv", 1.0))
    assert not added.called
    assert col.media.files == {"shared.png"} and not col.media.trashed


def test_a_failed_append_trashes_its_audio(col, tmp_path, monkeypatch):
    monkeypatch.setattr(notes, "update_note", FailingOp)
    notes.append_to_note(None, 1, "猫だ", tmp_path / "b.webp", tmp_path / "b.m4a")
    assert col.media.trashed == ["b.m4a"]


class SucceedingOp:
    """update_note whose operation works: it keeps the note it was given."""
    notes = []

    def __init__(self, note, **_kwargs):
        self.notes.append(note)
        self._success = None

    def success(self, fn):
        self._success = fn
        return self

    def failure(self, _fn):
        return self

    def run_in_background(self):
        self._success(None)


def test_an_append_reports_the_fields_before_and_after_and_its_audio(col, tmp_path,
                                                                     monkeypatch):
    monkeypatch.setattr(notes, "update_note", SucceedingOp)
    note = FakeNote.fromkeys(FIELDS, "")
    note["Sentence"] = "犬だ"
    col.get_note.side_effect = lambda _nid: note
    done = []
    notes.append_to_note(None, 1, "猫だ", tmp_path / "b.webp", tmp_path / "b.m4a",
                         on_done=lambda *args: done.append(args))
    assert done == [({"Sentence": "犬だ", "Audio": ""},
                     {"Sentence": "犬だ<br>猫だ", "Audio": "[sound:b.m4a]"}, ["b.m4a"])]


def test_unappend_takes_the_sentence_off_the_end():
    assert notes.unappend("a<br>b", "a", "a<br>b") == "a"
    assert notes.unappend("b", "", "b") == ""
    # edited since, before the sentence: the edit stays
    assert notes.unappend("A!<br>b", "a", "a<br>b") == "A!"
    # the appended sentence itself was edited: can't tell what to take off
    assert notes.unappend("a<br>B", "a", "a<br>b") is None


def test_undoing_an_append_keeps_later_edits_and_trashes_its_audio(col, monkeypatch):
    SucceedingOp.notes = []
    monkeypatch.setattr(notes, "update_note", SucceedingOp)
    note = FakeNote(Sentence="edited<br>猫だ", Audio="[sound:a.m4a]<br>[sound:b.m4a]")
    col.get_note.side_effect = lambda _nid: note
    done = []
    notes.restore_fields(None, 1, {"Sentence": "old", "Audio": "[sound:a.m4a]"},
                         {"Sentence": "old<br>猫だ",
                          "Audio": "[sound:a.m4a]<br>[sound:b.m4a]"},
                         ["b.m4a"], on_done=lambda: done.append(True))
    assert note == {"Sentence": "edited", "Audio": "[sound:a.m4a]"}
    assert col.media.trashed == ["b.m4a"] and done == [True]


def test_undoing_an_append_whose_sentence_was_edited_changes_nothing(col, monkeypatch):
    SucceedingOp.notes = []
    monkeypatch.setattr(notes, "update_note", SucceedingOp)
    note = FakeNote(Sentence="old<br>猫です", Audio="[sound:b.m4a]")
    col.get_note.side_effect = lambda _nid: note
    notes.restore_fields(None, 1, {"Sentence": "old", "Audio": ""},
                         {"Sentence": "old<br>猫だ", "Audio": "[sound:b.m4a]"}, ["b.m4a"])
    assert note == {"Sentence": "old<br>猫です", "Audio": "[sound:b.m4a]"}
    assert not SucceedingOp.notes and not col.media.trashed
    assert notes.showWarning.called
