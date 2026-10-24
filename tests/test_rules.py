"""The config cache, the i+1 rules, the word batch and the scratch folder."""
from unittest import mock

import pytest

from ankime.cards import batch, media
from ankime.core import config
from ankime.language import line_filter, yomitan


# --- config ------------------------------------------------------------------------

def test_merge_fills_defaults_and_merges_hotkeys_per_action():
    cfg = config.merge({"deck": "Mining", "hotkeys": {"mine_line": "Ctrl+M"}})
    assert cfg["deck"] == "Mining"
    assert cfg["seek_seconds"] == config.DEFAULTS["seek_seconds"]
    assert cfg["hotkeys"]["mine_line"] == "Ctrl+M"
    assert cfg["hotkeys"]["next_i1"] == config.DEFAULTS["hotkeys"]["next_i1"]
    assert config.merge(None) == config.DEFAULTS


@pytest.fixture
def addon_manager(monkeypatch):
    manager = mock.MagicMock()
    manager.getConfig.return_value = {"deck": "Mining"}
    monkeypatch.setattr(config, "_manager", lambda: manager)
    monkeypatch.setattr(config, "_current", None)
    monkeypatch.setattr(config, "_listeners", [])
    return manager


def test_get_reads_once_and_is_read_only(addon_manager):
    assert config.get()["deck"] == "Mining"
    config.get()
    assert addon_manager.getConfig.call_count == 1
    with pytest.raises(TypeError):
        config.get()["deck"] = "Other"


def test_save_writes_keeps_the_rest_and_tells_listeners(addon_manager):
    seen = []
    config.on_change(lambda old, new: seen.append((old["line_filter"], new["line_filter"])))
    config.save(line_filter="only_i1")
    written = addon_manager.writeConfig.call_args[0][1]
    assert written["line_filter"] == "only_i1" and written["deck"] == "Mining"
    assert config.get()["line_filter"] == "only_i1"
    assert seen == [("off", "only_i1")]
    assert addon_manager.getConfig.call_count == 1  # served from the cache, not the disk


def test_edits_in_ankis_config_editor_apply(addon_manager):
    config.install()
    (_addon, updated), _ = addon_manager.setConfigUpdatedAction.call_args
    updated({"deck": "Edited"})
    assert config.get()["deck"] == "Edited"


# --- line filter and the i+1 pause ------------------------------------------------

@pytest.mark.parametrize("kind, mode, expected", [
    ("known", "off", True), ("known", "skip_known", False), ("harder", "skip_known", True),
    ("i+1", "only_i1", True), ("harder", "only_i1", False), (None, "only_i1", False),
])
def test_wanted(kind, mode, expected):
    assert line_filter.wanted(kind, mode) is expected


def test_on_line_shown():
    assert line_filter.on_line_shown("i+1", "only_i1", paused=False) == "play"
    assert line_filter.on_line_shown("known", "skip_known", paused=False) == "skip"
    assert line_filter.on_line_shown("known", "skip_known", paused=True) == "stay"


def test_pause_plan():
    plan = line_filter.pause_plan
    assert plan("harder", "after", 10, 12, 10.1, False) is None
    assert plan("i+1", "off", 10, 12, 10.1, False) is None
    assert plan("i+1", "after", 10, 12, 10.1, False) == ("at", 12)
    assert plan("i+1", "after", 10, 12, 10.1, False, replay=True) == ("at", 12)
    assert plan("i+1", "before", 10, 12, 10.1, False) == ("now", 10)
    assert plan("i+1", "before", 10, 12, 11.0, False) is None  # scrubbed into the line
    assert plan("i+1", "before", 10, 12, 10.1, True) is None   # paused: browsing
    assert plan("i+1", "before", 10, 12, 10.1, False, replay=True) is None


def test_reached():
    assert not line_filter.reached(None, 5)
    assert not line_filter.reached(12, None)
    assert not line_filter.reached(12, 11.9)
    assert line_filter.reached(12, 11.96)


def test_next_filter_cycles():
    assert line_filter.next_filter("off") == "skip_known"
    assert line_filter.next_filter("only_i1") == "i1_jump"
    assert line_filter.next_filter("i1_jump") == "off"
    assert line_filter.next_filter("garbage") == "off"


# --- word batch -------------------------------------------------------------------

def test_batch_runs_words_in_order_and_drops_the_unusable():
    lines = {"猫": (0, "猫"), "犬": (3, "犬"), "鳥": (5, "鳥")}
    mined = {"犬"}
    b = batch.WordBatch()
    assert b.add(["猫", "犬", "魚", "鳥", "猫"])  # idle: the caller starts it
    assert not b.add(["猫", "鳥"])                # already queued, and running
    assert b.total == 4

    def nxt():
        return b.next(lines.get, lambda lemma: lemma not in mined)

    assert nxt() == ("猫", (0, "猫")) and b.position() == 1
    b.done("猫")
    assert nxt() == ("鳥", (5, "鳥"))  # 犬 was mined meanwhile, 魚 is in no line
    assert b.position() == 4
    b.done("鳥")
    assert nxt() is None
    assert b.finish() == 4 and b.finish() == 0


def test_batch_restarts_its_count_once_done():
    b = batch.WordBatch()
    b.add(["猫"])
    b.done("猫")
    assert b.add(["犬"]) and b.total == 1


def test_batch_cancel_drops_the_rest_quietly():
    b = batch.WordBatch()
    b.add(["猫", "犬"])
    assert b.cancel() == 2
    assert b.next(lambda lemma: (0, lemma), lambda lemma: True) is None
    assert b.finish() == 0  # nothing to report as done
    assert b.cancel() == 0


# --- Yomitan offline ---------------------------------------------------------------

class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def test_breaker_fails_fast_and_backs_off():
    clock = Clock()
    b = yomitan.Breaker(base=2, cap=8, clock=clock)
    b.check()  # online: calls go out
    b.failed("down")
    assert b.offline and b.seconds_to_retry() == 2
    with pytest.raises(yomitan.YomitanOffline, match="down"):
        b.check()  # no waiting for a timeout
    clock.now += 2
    b.check()  # the retry is due: this call is the probe...
    with pytest.raises(yomitan.YomitanOffline):
        b.check()  # ...and the others still fail fast meanwhile
    b.failed("down")
    assert b.seconds_to_retry() == 4
    for _ in range(3):
        b.failed("down")
    assert b.seconds_to_retry() == 8  # capped
    b.retry_now()
    b.check()
    b.succeeded()
    assert not b.offline and b.seconds_to_retry() == 0
    b.failed("down again")
    assert b.seconds_to_retry() == 2  # starts over


def test_post_trips_the_breaker_only_for_the_configured_url(monkeypatch):
    import requests
    calls = []

    def refuse(url, **kw):
        calls.append(url)
        raise requests.ConnectionError("refused")

    monkeypatch.setattr(yomitan, "BREAKER", yomitan.Breaker())
    monkeypatch.setattr(yomitan._SESSION, "post", refuse)
    monkeypatch.setattr(yomitan.config, "get", lambda: {"yomitan_url": "http://x"})
    for _ in range(3):
        with pytest.raises(yomitan.YomitanOffline):
            yomitan.lookup("猫")
    assert calls == ["http://x/ankiFields"]  # only the first one went out
    with pytest.raises(yomitan.YomitanOffline):
        yomitan.version(base_url="http://x")  # Test setup: always for real
    assert len(calls) == 2


def test_a_slow_answer_is_not_offline_but_no_connection_is(monkeypatch):
    import requests
    errors = iter([requests.ReadTimeout("slow"), requests.ConnectTimeout("nobody")])

    def post(url, **kw):
        raise next(errors)

    monkeypatch.setattr(yomitan, "BREAKER", yomitan.Breaker())
    monkeypatch.setattr(yomitan._SESSION, "post", post)
    monkeypatch.setattr(yomitan.config, "get", lambda: {"yomitan_url": "http://x"})
    with pytest.raises(yomitan.YomitanError) as slow:
        yomitan.lookup("猫")
    assert not isinstance(slow.value, yomitan.YomitanOffline)
    assert not yomitan.BREAKER.offline
    with pytest.raises(yomitan.YomitanOffline):
        yomitan.lookup("猫")
    assert yomitan.BREAKER.offline


# --- scratch folder ---------------------------------------------------------------

def test_clean_temp_without_folders_and_old_folder_removed(tmp_path, monkeypatch):
    monkeypatch.setattr(media, "TEMP", tmp_path / "ankime")
    monkeypatch.setattr(media, "OLD_TEMP", tmp_path / "anime_player")
    monkeypatch.setattr(media, "SHARED_TEMP", tmp_path / "shared")
    media.clean_temp()  # nothing made yet: no error, nothing created
    assert not (tmp_path / "ankime").exists()
    old = tmp_path / "anime_player"
    old.mkdir()
    (old / "stale.m4a").write_bytes(b"")
    monkeypatch.setattr(media, "TEMP_MAX_AGE", -1)
    media.clean_temp()
    assert not old.exists()


def test_picture_time_uses_the_paused_frame_within_the_line():
    assert media.picture_time(10, 14) == 12
    assert media.picture_time(10, 14, paused_at=13.2) == 13.2
    assert media.picture_time(10, 14, paused_at=14.3) == 14.3  # just after: still on purpose
    assert media.picture_time(10, 14, paused_at=30) == 12      # paused elsewhere
