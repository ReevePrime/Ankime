"""Test setup: checks everything the player depends on, says exactly what is missing, and
fixes what it can: downloading libmpv and ffmpeg (Windows), switching to Ankime's own
dictionaries when Yomitan isn't there."""
import ctypes
import ctypes.util
import html
import os
import subprocess
import sys
from pathlib import Path

from aqt import mw
from aqt.qt import QDialog, QDialogButtonBox, QLabel, Qt, QVBoxLayout
from aqt.utils import openLink

from ..cards import media, presets
from ..core import config, downloads
from ..core.model import SetupError
from ..language import local_dict, mecab, yomitan
from ..player import mpv_controller

OK, WARN, FAIL = "ok", "warn", "fail"
_ICONS = {OK: "✔", WARN: "⚠", FAIL: "✖"}
_COLORS = {OK: "#2b8a3e", WARN: "#c2410c", FAIL: "#c92a2a"}
# Where to get what a failed check is missing: check name -> (link text, url)
HELP = {
    "libmpv": ("Download libmpv (the mpv-dev-x86_64 package)",
               "https://sourceforge.net/projects/mpv-player-windows/files/libmpv/"),
    "ffmpeg": ("Download ffmpeg", "https://ffmpeg.org/download.html"),
    "Yomitan API": ("Set up yomitan-api", "https://github.com/yomidevs/yomitan-api"),
    "MeCab": ("Install ankimorphs-japanese-mecab", "https://ankiweb.net/shared/info/1974309724"),
}
# What a failed check can have done for it, here: check name -> (link text, action)
FIXES = {
    "libmpv": ("Download libmpv and set it up for me", "install:libmpv"),
    "ffmpeg": ("Download ffmpeg and set it up for me", "install:ffmpeg"),
    "Yomitan API": ("Or use Ankime's own dictionaries instead (no browser needed)", "local"),
    "Dictionaries": ("Download JMdict, or import a dictionary", "dictionaries"),
}


def status_html(status, message):
    """One check's result, its icon in its colour, for a rich-text label."""
    text = html.escape(str(message)).replace("\n", "<br>")
    return (f'<span style="color:{_COLORS[status]};font-size:15px">{_ICONS[status]}</span> '
            f"{text}")


# --- each check returns (status, message) ------------------------------------

def check_platform(cfg):
    try:
        output = mpv_controller.video_output(cfg["video_output"])
    except SetupError as e:
        return FAIL, str(e)
    return OK, f"Video output: {dict(mpv_controller.VIDEO_OUTPUTS)[output]}"


def check_collection(cfg, col):
    """Deck, note type and every configured field (main thread)."""
    problems = []
    if col.decks.id_for_name(cfg["deck"]) is None:
        problems.append(f"Deck '{cfg['deck']}' doesn't exist")
    model = col.models.by_name(cfg["note_type"])
    if model is None:
        return FAIL, f"Note type '{cfg['note_type']}' doesn't exist"
    names = {f["name"] for f in model["flds"]}
    for key in presets.FIELD_KEYS:
        field = cfg[key]
        if field and field not in names:
            problems.append(f"'{cfg['note_type']}' has no field '{field}' ({key})")
    for source in cfg["vocab_sources"]:
        other = col.models.by_name(source.get("note_type", "")) if isinstance(
            source, dict) else None
        if other is None:
            problems.append(f"Known words: note type {source.get('note_type')!r} doesn't "
                            "exist" if isinstance(source, dict) else
                            f"Known words: {source!r} isn't a note type and field")
        elif source.get("word_field") not in {f["name"] for f in other["flds"]}:
            problems.append(f"Known words: '{other['name']}' has no field "
                            f"'{source.get('word_field')}'")
    if problems:
        return FAIL, "\n".join(problems)
    where = f"Cards go to '{cfg['deck']}' as '{cfg['note_type']}'"
    if not cfg["word_field"]:
        return WARN, (f"{where}\nNo word field: known words can't be read from your cards, "
                      "only the ones you mark as known count")
    return OK, where


def check_libmpv(dll_dir):
    if dll_dir and not os.path.isdir(dll_dir):
        return FAIL, f"The libmpv folder '{dll_dir}' doesn't exist"
    dirs = [dll_dir] if dll_dir else []
    if sys.platform == "darwin":
        dirs += mpv_controller.MAC_LIBRARY_DIRS
    candidates = [Path(d) / n for d in dirs for n in mpv_controller.LIBMPV_NAMES]
    found = next((str(c) for c in candidates if c.is_file()), None)
    if found is None:
        found = next((p for n in ("mpv-2", "mpv", "libmpv") if (p := ctypes.util.find_library(n))),
                     None)
    if found is None:
        if sys.platform.startswith("linux"):
            return FAIL, ("libmpv not found. Install it with your package manager (e.g. "
                          "'sudo apt install libmpv2', 'sudo dnf install mpv-libs')")
        if sys.platform == "darwin":
            return FAIL, ("libmpv not found. Install mpv with Homebrew ('brew install mpv'), "
                          "or set the folder containing libmpv.dylib")
        return FAIL, ("libmpv not found. Download a libmpv build (the mpv-dev package, link "
                      f"below), unpack it and set the folder containing "
                      f"{mpv_controller.LIBMPV_NAMES[0]}")
    try:
        ctypes.CDLL(found)
    except OSError as e:
        return FAIL, f"{found} exists but can't be loaded (wrong architecture?): {e}"
    return OK, found


# The encoder each format needs, what happens without it, and whether that is a failure.
ENCODERS = {("image", "webp"): ("libwebp", "pictures will be JPEG", False),
            ("animated", "webp"): ("libwebp", "pictures will be still ones", False),
            ("animated", "avif"): ("libaom-av1", "pictures will be still ones", False),
            ("audio", "mp3"): ("libmp3lame", "cards can't be added", True),
            ("audio", "opus"): ("libopus", "cards can't be added", True)}


def check_ffmpeg(path, image_format, animated="off", audio="m4a"):
    try:
        out = media.run_ffmpeg("-hide_banner", "-version", ffmpeg=path).stdout
    except FileNotFoundError:
        return FAIL, f"ffmpeg not found at '{path}'"
    except (OSError, subprocess.CalledProcessError, media.FfmpegError) as e:
        return FAIL, f"ffmpeg at '{path}' doesn't run: {e}"
    version = out.decode("utf-8", "replace").splitlines()[0] if out else "ffmpeg"
    needed = [ENCODERS[k] for k in (("image", image_format), ("animated", animated),
                                    ("audio", audio)) if k in ENCODERS]
    if not needed:
        return OK, version
    encoders = media.run_ffmpeg("-hide_banner", "-encoders", ffmpeg=path).stdout
    missing = [(f"This build has no {name} encoder: {effect}", fatal)
               for name, effect, fatal in needed if name.encode() not in encoders]
    if not missing:
        return OK, version
    status = FAIL if any(fatal for _, fatal in missing) else WARN
    return status, "\n".join([version, *(line for line, _ in missing)])


def check_yomitan(url, scan_length):
    try:
        v = yomitan.version(base_url=url)
        tokens = yomitan.split_lines(
            ["日本語を勉強する"],
            lambda lines: yomitan._tokenize_lines(lines, base_url=url, scan_length=scan_length))[0]
    except yomitan.YomitanError as e:
        return FAIL, str(e)
    if not any(base for _, _, base, _ in tokens):
        return WARN, (f"Yomitan {v} answered but found no words: are dictionaries "
                      "installed and enabled in Yomitan?")
    return OK, f"Yomitan {v} at {url}"


def check_local_dictionaries():
    try:
        found = local_dict.dictionaries()
        if not found:
            return FAIL, local_dict.NO_DICTIONARY
        words = local_dict.tokenize_line("日本語を勉強する")
    except Exception as e:
        return FAIL, f"Ankime's dictionaries can't be read: {e}"
    titles = ", ".join(d["title"] for d in found)
    if not any(w[0].get("headwords") for w in words):
        return FAIL, (f"Imported: {titles}, but none has words (only frequencies or pitch "
                      "accents): import a dictionary such as JMdict too")
    note = "" if any(d["meta"] for d in found) else (
        "\nNo frequency list: the frequency field stays empty and the transcript can't sort "
        "by frequency (Tools > Ankime > Dictionaries says where to find one)")
    return (WARN if note else OK), f"Words are looked up in {titles}{note}"


def check_mecab(parser):
    if parser == "yomitan":
        return OK, "Not used: Yomitan's scanning parser splits the lines (Lookup > Parser)"
    found = mecab.find()
    if found is None:
        return WARN, ("Not found, so Yomitan's scanning parser splits the lines. It takes the "
                      "longest word it can, which can misread particles (と|いけません as "
                      "とい|け|ません). MeCab comes with other add-ons:")
    return OK, f"MeCab from {mecab.describe(found)} splits the lines"


def run_background_checks(cfg):
    """The slow checks (subprocesses, HTTP, loading libmpv): call via run_in_background."""
    return [("libmpv", *check_libmpv(cfg["mpv_dll_dir"])),
            ("ffmpeg", *check_ffmpeg(cfg["ffmpeg_path"], cfg["image_format"],
                                       cfg["animated_image"], cfg["audio_format"])),
            (("Dictionaries", *check_local_dictionaries()) if yomitan.uses_local(cfg) else
             ("Yomitan API", *check_yomitan(cfg["yomitan_url"], cfg["scan_length"]))),
            ("MeCab", *check_mecab(cfg["parser"]))]


class SetupCheckDialog(QDialog):
    """Runs the checks against `cfg` (which may be unsaved settings). A fix made here saves
    its setting and tells on_fixed(setting, value) (a settings dialog showing it), then the
    checks run again."""

    def __init__(self, parent, cfg, on_fixed=None):
        super().__init__(parent)
        self.setWindowTitle("Ankime: Test setup")
        self.cfg = dict(cfg)
        self._on_fixed = on_fixed
        self._running = False
        self.label = QLabel()
        self.label.setWordWrap(True)
        self.label.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        self.label.linkActivated.connect(self._link)
        self.label.setMinimumWidth(520)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        self.again = buttons.addButton("Check again", QDialogButtonBox.ButtonRole.ActionRole)
        self.again.clicked.connect(self.run)
        layout = QVBoxLayout(self)
        layout.addWidget(self.label)
        layout.addWidget(buttons)
        self.run()

    def run(self):
        if self._running:
            return
        self._running = True
        self.again.setEnabled(False)
        cfg = dict(self.cfg)
        self.results = [("System", *check_platform(cfg))]
        try:
            self.results.append(("Collection", *check_collection(cfg, mw.col)))
        except Exception as e:
            self.results.append(("Collection", FAIL, str(e)))
        self._show(pending=True)
        mw.taskman.run_in_background(lambda: run_background_checks(cfg), self._done)

    def _done(self, future):
        self._running = False
        self.again.setEnabled(True)
        try:
            self.results += future.result()
        except Exception as e:
            self.results.append(("Checks", FAIL, f"A check crashed: {e}"))
        self._show(pending=False)

    def _link(self, link):
        """A link of the results: a web page, or one of FIXES."""
        if not link.startswith("ankime:"):
            openLink(link)
            return
        action = link[len("ankime:"):]
        if action.startswith("install:"):
            from .downloads import install_tool
            install_tool(self, action[len("install:"):], self._fixed)
        elif action == "local":
            config.save(dictionary="local")
            yomitan.BREAKER.retry_now()
            self._fixed("dictionary", "local")
        elif action == "dictionaries":
            from .dictionaries import open_dictionaries
            open_dictionaries(self, self._fixed)
            self.run()

    def _fixed(self, key, value):
        self.cfg[key] = value
        if self._on_fixed:
            self._on_fixed(key, value)
        self.run()

    @staticmethod
    def _help(name, status):
        if status == OK:
            return ""
        links = []
        if name in HELP and not (name == "libmpv" and sys.platform != "win32"):
            links.append(HELP[name])  # libmpv elsewhere: the package manager or Homebrew
        if name in FIXES and (not FIXES[name][1].startswith("install:")
                              or downloads.can_install()):
            text, action = FIXES[name]
            links.insert(0, (text, f"ankime:{action}"))
        return "".join(f'<br><a href="{html.escape(url, quote=True)}">{html.escape(text)}</a>'
                       for text, url in links)

    def _show(self, pending):
        rows = [f'<tr><td style="color:{_COLORS[status]};font-size:16px;padding-right:8px">'
                f'{_ICONS[status]}</td><td><b>{name}</b> ({status_word})<br>'
                f'{html.escape(str(message)).replace(chr(10), "<br>")}{self._help(name, status)}'
                f'</td></tr>'
                for name, status, message in self.results
                for status_word in [{OK: "OK", WARN: "warning", FAIL: "failed"}[status]]]
        if pending:
            words = "the dictionaries" if yomitan.uses_local(self.cfg) else "Yomitan"
            rows.append(f"<tr><td></td><td><i>Checking libmpv, ffmpeg, {words} and "
                        "MeCab...</i></td></tr>")
        self.label.setText(f'<table cellspacing="6">{"".join(rows)}</table>')
