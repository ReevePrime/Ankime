"""Load the add-on as package `ankime` without Anki: aqt and anki are stubbed, so only the
pure logic is under test."""
import importlib.util
import re
import sys
import types
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent


def _stub(name, **attrs):
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    sys.modules[name] = module
    return module


def _strip_html(text):
    return re.sub(r"<[^>]*>", "", text)


if "aqt" not in sys.modules:
    _stub("anki")
    _stub("anki.utils", strip_html=_strip_html)
    aqt = _stub("aqt", mw=mock.MagicMock(), gui_hooks=mock.MagicMock())
    aqt.qt = _stub("aqt.qt", __getattr__=lambda name: mock.MagicMock(name=name))
    aqt.utils = _stub("aqt.utils", showWarning=mock.MagicMock(), tooltip=mock.MagicMock(),
                      askUser=mock.MagicMock(), askUserDialog=mock.MagicMock(),
                      openLink=mock.MagicMock())
    _stub("aqt.operations", QueryOp=mock.MagicMock())
    _stub("aqt.operations.note", add_note=mock.MagicMock(), remove_notes=mock.MagicMock(),
          update_note=mock.MagicMock())
    _stub("aqt.webview", AnkiWebView=object)

    # the package itself, without running __init__.py (it registers menu items)
    spec = importlib.util.spec_from_loader("ankime", loader=None, is_package=True)
    package = importlib.util.module_from_spec(spec)
    package.__path__ = [str(ROOT)]
    sys.modules["ankime"] = package

    # a fixed device id: the real one is made once and saved in user_files/
    importlib.import_module("ankime.core.store")._device = "this"


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _private_scratch_folders(tmp_path, monkeypatch):
    """No test may touch the real scratch folders in the system's temp folder, nor the
    sentence words cached in user_files/, nor depend on a MeCab installed here."""
    media = importlib.import_module("ankime.cards.media")
    for name in ("TEMP", "OLD_TEMP", "SHARED_TEMP"):
        monkeypatch.setattr(media, name, tmp_path / "scratch" / name.lower())
    knowledge = importlib.import_module("ankime.language.knowledge")
    monkeypatch.setattr(knowledge, "SENTENCE_CACHE", tmp_path / "sentence_words.json")
    # no MeCab unless a test brings one: not this computer's
    monkeypatch.setattr(importlib.import_module("ankime.language.mecab"), "_found", (None,))
