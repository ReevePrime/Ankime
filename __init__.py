import logging

from aqt import gui_hooks, mw
from aqt.qt import QAction, Qt, QTimer
from aqt.utils import showWarning

from .core import config, debuglog

_window = None


def open_player():
    global _window
    if _window is not None:
        _window.setWindowState(_window.windowState() & ~Qt.WindowState.WindowMinimized)
        _window.raise_()
        _window.activateWindow()
        return
    from .dialogs import first_run
    if first_run.offer(then=open_player):
        return  # a new install: set up first, the player opens after
    try:
        from .player.window import PlayerWindow
        _window = PlayerWindow(on_closed=_clear_window)
    except Exception as e:
        logging.getLogger(__name__).exception("the player could not start")
        showWarning(f"Ankime could not start:\n\n{e}\n\n"
                    "Tools > Ankime > Settings > Test setup checks what is missing.")
        _window = None
        return
    _window.show()


def _clear_window():
    global _window
    _window = None


def _close_player():
    """Before the profile closes: the player saves its state into the collection, and
    subtitles still being made from the speech are stopped (Anki would wait for them)."""
    if _window is not None:
        _window.close()
    from .subs import transcribe
    transcribe.stop_all()


def open_settings(*_):
    from .dialogs.settings import open_settings as show
    show()


def open_setup_wizard(*_):
    from .dialogs.first_run import run_wizard
    run_wizard(mw, open_player=open_player)


def _offer_setup():
    """A new install: the setup wizard, once the main window has settled."""
    from .dialogs import first_run
    QTimer.singleShot(0, lambda: first_run.offer(open_player=open_player))


def open_dictionaries(*_):
    from .dialogs.dictionaries import open_dictionaries as show
    show()


def _known_words():
    from .language import known_words
    return known_words


def _browser_menus(browser):
    _known_words().add_browser_menu(browser, lambda: _window)


# One Ankime entry in the Tools menu, everything under it.
menu = mw.form.menuTools.addMenu("Ankime")
player_action = QAction("Player...", mw)
player_action.triggered.connect(open_player)
menu.addAction(player_action)
settings_action = QAction("Settings...", mw)
settings_action.triggered.connect(open_settings)
menu.addAction(settings_action)
setup_action = QAction("Setup wizard...", mw)
setup_action.triggered.connect(open_setup_wizard)
menu.addAction(setup_action)
dictionaries_action = QAction("Dictionaries...", mw)
dictionaries_action.triggered.connect(open_dictionaries)
menu.addAction(dictionaries_action)
menu.addSeparator()
known_menu = menu.addMenu("Known words")
known_menu.addAction("Mark known from a word list...",
                     lambda: _known_words().import_word_list(mw, _window))
known_menu.addAction("Export known words...", lambda: _known_words().export_known_words(mw, _window))

gui_hooks.browser_menus_did_init.append(_browser_menus)

gui_hooks.profile_did_open.append(_offer_setup)
gui_hooks.profile_will_close.append(_close_player)

debuglog.install()
config.install()

# The Config button in Tools > Add-ons opens the form instead of the raw JSON editor.
mw.addonManager.setConfigAction(__name__, open_settings)
