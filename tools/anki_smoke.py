"""Smoke test in a real Anki: the tests stub Anki and Qt out, so they can't tell when Anki
renames or drops something Ankime calls. This builds the package (tools/build.py), installs
it in a throwaway profile under a numeric folder name, as AnkiWeb does, starts the Anki that
is installed in this Python (pip install "aqt[qt6]==<version>") without a window on screen,
and once the profile is open: checks the add-on loaded, walks through the setup wizard
(shown on a new install), opens the settings, creates the note type, adds a word card, reads
the known words back, removes the card, opens the browser (the add-on adds a menu there),
adds a card through Anki's Add window (review before adding), and installs the
package over itself, as an update does. The player isn't started: it needs libmpv and a screen.

    python tools/anki_smoke.py [--base DIR] [--show]

Exits 0 if every check passed. A dialog that would wait for a click fails the run instead."""
import argparse
import json
import logging
import os
import shutil
import sys
import tempfile
import threading
import traceback
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build  # noqa: E402

ADDON_ID = "1234567890"  # AnkiWeb installs add-ons in a folder named by their number
PROFILE = "smoke"
TIMEOUT = 180  # seconds for the whole run, Anki's start included

results = {"anki": None, "checks": [], "dialogs": [], "deprecated": [], "ok": False}


def log(message):
    print(f"[smoke] {message}", flush=True)


def finish(code):
    results["ok"] = code == 0
    log(json.dumps(results, ensure_ascii=False, indent=1))
    log("PASSED" if code == 0 else "FAILED")
    os._exit(code)  # Anki's own shutdown can wait on dialogs or threads: not worth it here


def install_addon(base):
    """The package as built for release, unzipped where Anki looks for add-ons."""
    build.main()
    version = json.loads((build.ROOT / "manifest.json").read_text(encoding="utf-8"))
    package = build.ROOT / "dist" / f"ankime-{version['human_version']}.ankiaddon"
    target = base / "addons21" / ADDON_ID
    shutil.rmtree(target, ignore_errors=True)
    with zipfile.ZipFile(package) as z:
        z.extractall(target)


def seed_profile(base):
    """A profile that opens without the first-run questions (language) or the update check."""
    from aqt.profiles import ProfileManager

    pm = ProfileManager(str(base))
    pm.setupMeta()
    pm.meta["defaultLang"] = "en_US"
    pm.meta["firstRun"] = False
    pm.meta["check_for_updates"] = False  # recent Ankis
    pm.meta["updates"] = False            # older ones
    pm.create(PROFILE)
    pm.load(PROFILE)
    pm.save()
    pm.db.close()


def refuse_dialogs():
    """Anki's message boxes and Ankime's own (through aqt.utils, which the add-on imports
    after this), and any other modal dialog: recorded and answered at once, never shown. Any
    of them is a failure."""
    from unittest import mock

    import aqt.addons
    import aqt.errors
    import aqt.utils
    from aqt.qt import QDialog, QMessageBox

    def note(kind, text):
        results["dialogs"].append(f"{kind}: {text[:2000]}")
        log(f"dialog {kind}: {text[:2000]}")

    def record(kind, answer=None):
        def fn(*args, **kwargs):
            note(kind, str(args[0]) if args else str(kwargs.get("text", "")))
            return answer() if callable(answer) else answer
        return fn

    # showText(run=False) hands back (dialog, buttons) for the caller to add buttons to
    for module in (aqt.utils, aqt.addons, aqt.errors):
        for name, answer in (("showWarning", None), ("showCritical", None), ("showInfo", None),
                             ("showText", lambda: (mock.MagicMock(), mock.MagicMock())),
                             ("askUser", False)):
            if hasattr(module, name):
                setattr(module, name, record(name, answer))

    def refuse_exec(dialog, *args):
        text = dialog.text() if isinstance(dialog, QMessageBox) else ""
        note(type(dialog).__name__, f"{dialog.windowTitle()} {text}".strip())
        return 0  # rejected

    QDialog.exec = refuse_exec
    QMessageBox.exec = refuse_exec


def record_deprecations():
    """Anki prints a line when something deprecated is used (it still works, for now): the
    ones Ankime's code causes are recorded, and fail the run, before Anki drops them."""
    import anki._legacy as legacy

    original = legacy.print_deprecation_warning

    def warn(msg, frame=1):
        path, line, _, _ = traceback.extract_stack(limit=frame + 2)[0]
        if f"{os.sep}{ADDON_ID}{os.sep}" in path:
            where = path.split(f"{os.sep}{ADDON_ID}{os.sep}", 1)[1]
            results["deprecated"].append(f"{where}:{line}: {msg}")
        original(msg, frame + 1)

    legacy.print_deprecation_warning = warn


def check(name, fn):
    """Run one check; a failure ends the run."""
    try:
        detail = fn()
    except Exception:
        results["checks"].append({"check": name, "ok": False, "error": traceback.format_exc()})
        finish(1)
    results["checks"].append({"check": name, "ok": True, **({"detail": detail} if detail else {})})
    log(f"ok: {name}{f' ({detail})' if detail else ''}")


def run_checks(mw):
    """After the profile opened. Adding and removing a card are collection operations that
    finish later, so the checks go on from their callbacks."""
    from aqt import dialogs
    from aqt.qt import QTimer

    from anki.buildinfo import version

    results["anki"] = version
    addon = sys.modules.get(ADDON_ID)

    def loaded():
        if addon is None or not hasattr(addon, "open_player"):
            raise AssertionError("the add-on wasn't imported (see the dialogs)")
        titles = [a.text() for a in mw.form.menuTools.actions()]
        if "Ankime" not in titles:
            raise AssertionError(f"no Ankime menu under Tools: {titles}")

    check("add-on loaded, Tools > Ankime", loaded)
    config = sys.modules[f"{ADDON_ID}.core.config"]
    check("config read", lambda: f"{len(config.get())} settings")

    def setup_wizard():
        """A new install: the setup wizard opened by itself. Walked through, it creates the
        note type and a new deck and saves the settings."""
        from importlib import import_module
        first_run = import_module(f"{ADDON_ID}.dialogs.first_run")
        wizard = first_run._wizard
        if wizard is None or not wizard.isVisible():
            raise AssertionError("the setup wizard didn't open on a new install")
        wizard.cards.create.setChecked(True)
        wizard.cards.deck.setCurrentText("Mining")
        titles = [wizard.currentPage().title()]
        while wizard.currentPage() is not wizard.done_page:
            if not wizard.currentPage().isComplete():
                raise AssertionError(f"can't go on from {titles[-1]}")
            wizard.next()
            titles.append(wizard.currentPage().title())
        if "Mining" not in wizard.done_page.summary.text():
            raise AssertionError(f"summary: {wizard.done_page.summary.text()}")
        wizard.done_page.open_player.setChecked(False)  # it needs libmpv
        wizard.accept()
        cfg = config.get()
        if (cfg["note_type"], cfg["deck"], cfg["word_field"]) != ("Ankime", "Mining", "Word"):
            raise AssertionError(f"saved {cfg['note_type']}, {cfg['deck']}, {cfg['word_field']}")
        if first_run._wizard is not None:
            raise AssertionError("the wizard is still open")
        return " > ".join(titles)

    check("setup wizard on a new install", setup_wizard)

    def note_type():
        from importlib import import_module
        notes = import_module(f"{ADDON_ID}.cards.notes")
        settings = notes.create_note_type(mw.col)
        config.save(**settings, deck="Default")
        return settings["note_type"]

    check("Ankime note type created", note_type)

    def settings_dialog():
        from importlib import import_module
        dialog = import_module(f"{ADDON_ID}.dialogs.settings").SettingsDialog(mw)
        dialog.show()
        dialog.sentence_words.setChecked(True)
        dialog.sentence_sources._add_row("Basic", "")
        collected = dialog._collect()
        dialog.close()
        if not collected["sentence_words"] or collected["sentence_sources"] != [
                {"note_type": "Basic", "sentence_field": "Front"}]:
            raise AssertionError("sentence cards not collected: "
                                 f"{collected['sentence_words']} {collected['sentence_sources']}")
        styles = {key: collected[key] for key in ("new_word_style", "learning_word_style",
                                                  "known_word_style")}
        if styles != {key: config.DEFAULTS[key] for key in styles}:
            raise AssertionError(f"word styles not collected as saved: {styles}")

    check("settings dialog opens", settings_dialog)

    def local_dictionary():
        """Ankime's own dictionaries in a real Anki: import one, look a word up through
        yomitan.py, and open the dialogs that manage and check them."""
        from importlib import import_module
        local_dict = import_module(f"{ADDON_ID}.language.local_dict")
        yomitan = import_module(f"{ADDON_ID}.language.yomitan")
        path = Path(mw.pm.base) / "smoke-dictionary.zip"
        gloss = [{"type": "structured-content", "content": {"tag": "ul", "content": [
            {"tag": "li", "content": "to eat"}]}}]
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("index.json", json.dumps({"title": "Smoke", "format": 3}))
            z.writestr("term_bank_1.json", json.dumps(
                [["食べる", "たべる", "v1", "v1", 1, gloss, 1, ""]], ensure_ascii=False))
        local_dict.import_zip(path)
        config.save(dictionary="local")
        try:
            tokens = yomitan.tokenize("食べた")
            if [t.base for t in tokens] != ["食べる"]:
                raise AssertionError(f"食べた split as {tokens}")
            if "to eat" not in yomitan.lookup("食べる")[0]["glossary-brief"]:
                raise AssertionError("no definition for 食べる")
            dialog = import_module(f"{ADDON_ID}.dialogs.dictionaries").DictionariesDialog(mw)
            dialog.show()
            if dialog.list.count() != 1:
                raise AssertionError(f"{dialog.list.count()} dictionaries listed")
            dialog.close()
            check_dialog = import_module(f"{ADDON_ID}.dialogs.setup_check").SetupCheckDialog(
                mw, config.get())
            check_dialog.show()
            check_dialog.close()
        finally:
            config.save(dictionary="yomitan")
            local_dict.remove("Smoke")
        return "imported, 食べた -> 食べる, dialogs open"

    check("Ankime's own dictionaries", local_dictionary)

    def parser():
        """Which parser splits the lines: MeCab, if another add-on brings it (not on CI)."""
        from importlib import import_module
        tokenizer = import_module(f"{ADDON_ID}.language.tokenizer")
        mecab = import_module(f"{ADDON_ID}.language.mecab")
        found = tokenizer.uses_mecab(config.get())
        if found is None:
            return "no MeCab: Yomitan's scanning parser"
        line = "仕事を探さないといけませんね"
        words = [surface for surface, _ in mecab.words(line, mecab.parse([line], found)[0])]
        if words != ["仕事", "を", "探さない", "と", "いけません", "ね"]:
            raise AssertionError(f"MeCab split {line} as {words}")
        return f"MeCab from {mecab.describe(found)}"

    check("parser found", parser)

    from importlib import import_module
    notes = import_module(f"{ADDON_ID}.cards.notes")
    knowledge = import_module(f"{ADDON_ID}.language.knowledge")
    scratch = Path(tempfile.mkdtemp(prefix="ankime-smoke-"))
    audio, image = scratch / "line.m4a", scratch / "line.webp"
    audio.write_bytes(b"not really audio")
    image.write_bytes(b"not really a picture")
    word = ({"word": "食べる", "reading": "たべる", "definition": "to eat"}, [])

    def added(note_id):
        def card_written():
            note = mw.col.get_note(note_id)
            if "<b>食べる</b>" not in note["Sentence"] or note["Word"] != "食べる":
                raise AssertionError(f"unexpected fields: {dict(note.items())}")
            if not note["Audio"].startswith("[sound:") or "<img" not in note["Picture"]:
                raise AssertionError(f"media missing: {note['Audio']!r} {note['Picture']!r}")
            return f"note {note_id}"

        check("word card added", card_written)

        def known_words():
            days, found, *_ = knowledge.Knowledge.read_cards(mw.col, config.get())
            if "食べる" not in days or found.get("食べる") != note_id:
                raise AssertionError(f"食べる not read back: {days} {found}")
            # a new card: its sentence is read, but not split (that waits for it to be known)
            sentences = knowledge.Knowledge.read_cards(
                mw.col, {**config.get(), "sentence_words": True})[3]
            if [words for _days, words in sentences.values()] != [None]:
                raise AssertionError(f"sentence card not read back: {sentences}")

        check("known words read from the cards", known_words)
        notes.remove_notes(mw, [note_id], on_done=removed)

    def removed():
        def gone():
            if mw.col.find_notes(f"nid:{note_ids[0]}"):
                raise AssertionError("the note is still there")

        check("card removed (undo)", gone)

        def deck():  # made by a collection op: there by now
            if mw.col.decks.id_for_name("Mining") is None:
                raise AssertionError("no deck 'Mining'")

        check("deck created by the setup wizard", deck)

        def browser():
            b = dialogs.open("Browser", mw)
            titles = [a.text() for a in b.form.menu_Notes.actions()]
            b.close()
            if not any(t.startswith("Ankime:") for t in titles):
                raise AssertionError(f"no Ankime action in the browser's Notes menu: {titles}")

        check("browser menu added", browser)
        check_panel_style(lambda: check_popup_size(lambda: check_review(lambda: (
            check("updates over itself", update),
            finish(1 if results["dialogs"] or results["deprecated"] else 0)))))

    def update():
        """Installing the package over the add-on, as an update from AnkiWeb does: Anki
        moves user_files/ aside first, which Windows refuses while the log or the dictionary
        database (opened here in two threads) is open. Ankime closes them before."""
        local_dict = import_module(f"{ADDON_ID}.language.local_dict")
        worker = threading.Thread(target=local_dict.dictionaries)
        worker.start()
        worker.join()
        local_dict.dictionaries()
        logging.getLogger(ADDON_ID).info("update")  # the log is open too
        version = json.loads((build.ROOT / "manifest.json").read_text(encoding="utf-8"))
        package = build.ROOT / "dist" / f"ankime-{version['human_version']}.ankiaddon"
        result = mw.addonManager.install(str(package), manifest={"package": ADDON_ID})
        if not hasattr(result, "name"):
            raise AssertionError(f"not installed: {result}")
        user_files = Path(mw.addonManager.addonsFolder(ADDON_ID)) / "user_files"
        if not (user_files / "dictionaries.sqlite").exists():
            raise AssertionError(f"user_files/ not kept: {list(user_files.glob('*'))}")
        local_dict.dictionaries()  # still works, reconnected
        return "user_files/ kept"

    def check_panel_style(then):
        """The subtitle panel, on its own (the player needs libmpv): a word style chosen in
        the settings shows on its words."""
        sub_panel = import_module(f"{ADDON_ID}.player.sub_panel")
        panel = sub_panel.SubtitlePanel(None, lambda *_: None, lambda: None, lambda: None)
        styles = {**sub_panel.word_styles(config.get()), "new": {
            "color": "#0072b2", "bold": True, "underline": "wavy", "size": 150}}
        css = "\n".join((sub_panel.word_css(styles), sub_panel.pitch_css({"odaka": "#00ff00"})))
        panel.set_style(28, css, "off", "off", "color")
        panel.set_sub('<span class="w new">猫</span><span class="w new p-odaka">橋</span>'
                      '<span class="w known p-heiban">箸</span>')
        # a pitch colour goes over the status's (a chosen one too), a default one included:
        # heiban's light or night-mode blue, as Anki's theme is
        wanted = ["rgb(0, 114, 178)", "wavy", "42px", "700", "rgb(0, 255, 0)"]
        heiban = ("rgb(24, 100, 171)", "rgb(77, 171, 247)")
        js = ("(() => { const s = getComputedStyle(document.querySelector('.w.new')); "
              "const color = sel => getComputedStyle(document.querySelector(sel)).color; "
              "return [s.color, s.textDecorationStyle, s.fontSize, s.fontWeight, "
              "color('.p-odaka'), color('.p-heiban')]; })()")

        def got(value):
            def styled():
                if value[:-1] != wanted or value[-1] not in heiban:
                    raise AssertionError(f"the words are styled {value}, not {wanted} and "
                                         f"one of {heiban}")

            check("subtitle panel shows the word styles and pitch accents", styled)
            panel.deleteLater()
            then()

        # the panel runs the calls made before its page loaded once it has: ask after those
        panel.loadFinished.connect(lambda _ok: QTimer.singleShot(
            200, lambda: panel.page().runJavaScript(js, got)))

    def check_popup_size(then):
        """The definition popup takes its text size from the settings (following the
        subtitle size here), times the player's scale (zoom, fullscreen), and grows with it."""
        from aqt.qt import QRect
        popup_module = import_module(f"{ADDON_ID}.player.popup")
        config.save(popup_font_size=popup_module.FOLLOW_SUBTITLES, subtitle_font_size=42)
        popup = popup_module.Popup(mw, lambda _cmd: None, scale=lambda: 4 / 3)
        popup.show_at(QRect(200, 200, 20, 20), "<b>猫</b> cat")
        js = "getComputedStyle(document.getElementById('pop')).fontSize"

        def got(value):
            def sized():  # 42 / 28 * 15 = 22.5 px, times 4/3: 30 px
                wanted = min(2 * popup_module.WIDTH,
                             round(popup._area().width() * popup_module.SCREEN_SHARE[0]))
                if value != "30px" or popup.width() != wanted:
                    raise AssertionError(f"popup text {value}, width {popup.width()}")

            check("definition popup follows its text size", sized)
            config.save(popup_font_size=config.DEFAULTS["popup_font_size"],
                        subtitle_font_size=config.DEFAULTS["subtitle_font_size"])
            popup.deleteLater()
            then()

        popup.web.loadFinished.connect(lambda _ok: QTimer.singleShot(
            300, lambda: popup.web.page().runJavaScript(js, got)))

    def check_review(then):
        """Review before adding: the card opens in Anki's Add window, filled in, and adding
        it there tells Ankime (here pressed by the test), which then removes it again."""
        audio2, image2 = scratch / "review.m4a", scratch / "review.webp"
        audio2.write_bytes(b"not really audio")
        image2.write_bytes(b"not really a picture")
        window = []

        def reviewed_added(note_id):
            def added_from_window():
                note = mw.col.get_note(note_id)
                if "<b>食べる</b>" not in note["Sentence"] or note["Word"] != "食べる":
                    raise AssertionError(f"unexpected fields: {dict(note.items())}")
                return f"note {note_id}"

            check("card added from Anki's Add window", added_from_window)

            def closed():
                window[0].close()
                then()

            notes.remove_notes(mw, [note_id], on_done=closed)

        def failed(ok):
            if not ok:
                results["checks"].append({"check": "Add window opened", "ok": False})
                finish(1)

        notes.review_card(mw, "ご飯を食べる", image2, audio2, word=word, bold="食べる",
                          on_added=reviewed_added, on_finished=failed)

        def filled():
            window.append(dialogs._dialogs["AddCards"][1])
            note = window[0].editor.note
            if "<b>食べる</b>" not in note["Sentence"] or "[sound:" not in note["Audio"]:
                raise AssertionError(f"the Add window holds {dict(note.items())}")
            if not mw.col.find_notes("ご飯を食べる") == []:
                raise AssertionError("the card was added before its review")

        check("Add window filled in", filled)
        QTimer.singleShot(1500, lambda: window[0].add_current_note())

    note_ids = []

    def on_added(note_id):
        note_ids.append(note_id)
        added(note_id)

    def on_finished(ok):
        if not ok:
            results["checks"].append({"check": "word card added", "ok": False})
            finish(1)

    notes.add_card(mw, "ご飯を食べる", image, audio, word=word, bold="食べる",
                   origin=(str(scratch / "Show" / "ep01.mkv"), 12.0),
                   on_added=on_added, on_finished=on_finished)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", help="Anki's folder for the run (default: a new temp one)")
    parser.add_argument("--show", action="store_true", help="show Anki's windows")
    args = parser.parse_args()
    base = Path(args.base or tempfile.mkdtemp(prefix="ankime-anki-")).resolve()
    base.mkdir(parents=True, exist_ok=True)
    log(f"Anki folder: {base}")

    # Anki runs once per user: under another name, it doesn't hand over to an Anki already open
    os.environ["LOGNAME"] = "ankime-smoke"
    if not args.show:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
    os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--no-sandbox --disable-gpu")
    install_addon(base)
    seed_profile(base)
    refuse_dialogs()
    record_deprecations()

    timer = threading.Timer(TIMEOUT, lambda: (results["checks"].append(
        {"check": f"finished within {TIMEOUT}s", "ok": False}), finish(2)))
    timer.daemon = True
    timer.start()

    import aqt
    from aqt import gui_hooks
    from aqt.qt import QTimer

    # once the main window has settled, not inside the hook that opens the profile
    gui_hooks.profile_did_open.append(lambda: QTimer.singleShot(500, lambda: run_checks(aqt.mw)))
    aqt._run(argv=["anki", "-b", str(base), "-p", PROFILE])
    results["checks"].append({"check": "Anki exited before the checks ran", "ok": False})
    finish(1)


if __name__ == "__main__":
    main()
