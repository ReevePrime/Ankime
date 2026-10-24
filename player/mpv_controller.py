"""libmpv in the player window. mpv calls back on its own thread; every signal here is
emitted on Qt's main thread. How the video gets on screen is player/video.py's."""
import importlib
import locale
import logging
import os
import sys

from aqt import mw
from aqt.qt import QGuiApplication, QObject, QOffscreenSurface, QOpenGLContext, pyqtSignal

from ..core.model import SetupError
from ..subs import online

log = logging.getLogger(__name__)

LIBMPV_NAMES = (("libmpv.2.dylib", "libmpv.dylib") if sys.platform == "darwin"
                else ("libmpv-2.dll", "mpv-2.dll", "mpv-1.dll"))
# where Homebrew and MacPorts put libmpv.dylib: ctypes doesn't look there by itself
MAC_LIBRARY_DIRS = ("/opt/homebrew/lib", "/usr/local/lib", "/opt/local/lib")

# The only mpv default bindings kept: the mouse wheel changes the volume.
SAFE_BINDINGS = {"WHEEL_UP": "add volume 2", "WHEEL_DOWN": "add volume -2"}
# Keys on the video that aren't settings: mpv key -> player action. A hotkey on the same key
# wins.
FIXED_KEYS = {"MBTN_LEFT_DBL": "toggle_fullscreen", "ESC": "escape"}
MAX_VOLUME = 130  # mpv's default volume-max

# The video_output setting: how mpv gets the video on screen.
VIDEO_OUTPUTS = [("auto", "Automatic"),
                 ("opengl", "OpenGL (mpv draws into the player)"),
                 ("window", "Native window (the older way)")]


def window_problem():
    """Why mpv can't draw into a native window of its own (wid) here, or "" if it can: fine
    on Windows and X11, unsupported on Wayland, and unreliable on macOS."""
    if QGuiApplication.platformName() == "wayland":
        return "on Wayland, mpv can't be given a window to draw into"
    if sys.platform == "darwin":
        return "on macOS, mpv can't draw reliably into a window of its own"
    return ""


def opengl_problem():
    """Why Qt can't give mpv an OpenGL context here, or "" if it can: mpv needs OpenGL 2.1 or
    OpenGL ES 2.0 at least. A context is made and dropped (main thread)."""
    ctx = QOpenGLContext()
    if not ctx.create():
        return "OpenGL isn't available"
    surface = QOffscreenSurface()
    surface.setFormat(ctx.format())
    surface.create()
    try:
        if not ctx.makeCurrent(surface):
            return "OpenGL isn't available (no context could be made current)"
        fmt, es = ctx.format(), ctx.isOpenGLES()
        version = (fmt.majorVersion(), fmt.minorVersion())
        ctx.doneCurrent()
        if version < ((2, 0) if es else (2, 1)):
            return f"OpenGL {'ES ' if es else ''}{version[0]}.{version[1]} is too old"
        return ""
    finally:
        surface.destroy()


def video_output(setting):
    """"opengl" or "window": how mpv draws the video, from the video_output setting. "auto"
    takes OpenGL, or a native window where OpenGL isn't available and that works. Raises
    SetupError, saying why, if what is asked for can't work here."""
    if setting == "window":
        problem = window_problem()
        if problem:
            raise SetupError(f"The video can't be shown: {problem}. Set Video output to "
                             "Automatic in Tools > Ankime > Settings (Media tab).")
        return "window"
    problem = opengl_problem()
    if not problem:
        return "opengl"
    if setting == "auto" and not window_problem():
        return "window"
    raise SetupError(f"The video can't be shown: {problem}. Check the graphics drivers, or "
                     "try another video driver in Anki's preferences.")


def load_mpv(dll_dir):
    """Import the bundled python-mpv, pointing it at libmpv first. Raises a readable error
    if libmpv can't be found."""
    if dll_dir and os.path.isdir(dll_dir):
        if dll_dir not in os.environ["PATH"].split(os.pathsep):
            os.environ["PATH"] = dll_dir + os.pathsep + os.environ["PATH"]
        if hasattr(os, "add_dll_directory"):
            os.add_dll_directory(dll_dir)
    if sys.platform == "darwin":
        _add_mac_library_dirs([dll_dir] if dll_dir else [])
    locale.setlocale(locale.LC_NUMERIC, "C")  # mpv requires this
    try:
        return importlib.import_module("..vendor.mpv", __package__)
    except OSError as e:
        where = f" in {dll_dir}" if dll_dir else ""
        raise SetupError(f"libmpv not found{where} ({' / '.join(LIBMPV_NAMES)}). Set the "
                         f"libmpv folder in Tools > Ankime > Settings.\n\n{e}") from e


def _add_mac_library_dirs(dirs):
    """ctypes finds libmpv.dylib on dyld's fallback path, read as it looks: put the chosen
    folder and Homebrew's on it."""
    current = [d for d in os.environ.get("DYLD_FALLBACK_LIBRARY_PATH", "").split(":") if d]
    wanted = [d for d in (*dirs, *MAC_LIBRARY_DIRS) if d not in current and os.path.isdir(d)]
    if wanted:
        # dyld's own defaults after them, as when the variable isn't set
        defaults = [os.path.expanduser("~/lib"), "/usr/local/lib", "/lib", "/usr/lib"]
        os.environ["DYLD_FALLBACK_LIBRARY_PATH"] = ":".join(
            dict.fromkeys(wanted + (current or defaults)))


class MpvController(QObject):
    # (text, start, end) of the source-language line, in video time (subtitle delay
    # included); text "" between lines
    subtitle_changed = pyqtSignal(str, object, object)
    translation_changed = pyqtSignal(str)
    pause_changed = pyqtSignal(bool)
    volume_changed = pyqtSignal(float, bool)  # volume (0-MAX_VOLUME), muted
    speed_changed = pyqtSignal(float)
    file_loaded = pyqtSignal()
    seeked = pyqtSignal()         # playback restarted after a seek or a load
    end_reached = pyqtSignal()
    key_pressed = pyqtSignal(str)  # the action bound to the key (see hotkeys)

    def __init__(self, parent, mpv_module, keys, wid=None):
        """`keys` is {action: mpv key name}; a press emits key_pressed(action). With `wid`,
        mpv draws into that native window; without, into a render context (player/video.py
        makes it)."""
        super().__init__(parent)
        self._alive = True
        self.mpv_module = mpv_module
        output = {"wid": str(int(wid))} if wid is not None else {"vo": "libmpv"}
        self.mpv = mpv_module.MPV(
            # mpv's own keys would act behind the player's back: q quits mpv, s saves
            # screenshots in Anki's folder, j / v / # switch the tracks the panel relies on
            input_default_bindings=False,
            input_vo_keyboard=True,
            osc=False,      # the Qt seek bar and buttons are the one control surface
            sub_auto="no",  # the player picks the subtitle files itself
            **output,
        )
        p = self.mpv
        for key, command in SAFE_BINDINGS.items():  # the hotkeys below take precedence
            p.keybind(key, command)
        p["sub-visibility"] = "no"  # the subtitle panel shows the text instead
        p["secondary-sub-visibility"] = "no"
        p["keep-open"] = "yes"      # hold the last frame so we can see the end
        try:
            # a file dropped on the video goes to the player window (the playlist), not
            # straight to mpv behind its back
            p["drag-and-drop"] = "no"
        except Exception:
            pass  # libmpv older than 0.37: no such option
        self._set_up_ytdl()

        p.observe_property("sub-text", self._sub_changed)
        p.observe_property("sub-start", self._sub_changed)  # same text, new line
        p.observe_property("sub-delay", self._sub_changed)  # same line, new video times
        p.observe_property("secondary-sub-text", lambda _n, text: self._main(
            self.translation_changed.emit, text or ""))
        p.observe_property("pause", lambda _n, paused: self._main(
            self.pause_changed.emit, bool(paused)))
        p.observe_property("volume", lambda _n, _v: self._volume_changed())
        p.observe_property("mute", lambda _n, _v: self._volume_changed())
        p.observe_property("speed", lambda _n, speed: speed and self._main(
            self.speed_changed.emit, float(speed)))
        p.observe_property("eof-reached", lambda _n, reached: reached and self._main(
            self.end_reached.emit))
        p.event_callback("file-loaded")(lambda _e: self._main(self.file_loaded.emit))
        p.event_callback("playback-restart")(lambda _e: self._main(self.seeked.emit))
        self._bindings = []
        self.set_keys(keys)

    def _set_up_ytdl(self):
        """URLs: mpv plays them through yt-dlp (its ytdl_hook), the one found on the PATH,
        at 1080p at most. The player fetches the subtitles itself (subs/online.py), so mpv
        isn't to add every language the site has: "none" matches none."""
        try:
            self.mpv["ytdl-format"] = online.PLAY_FORMAT
            self.mpv["ytdl-raw-options"] = "sub-langs=none"
            if ytdlp := online.ytdlp_path():
                # one key=value, not parsed as a list: a path may have a comma in it
                self.mpv.command("change-list", "script-opts", "append",
                                 f"ytdl_hook-ytdl_path={ytdlp}")
        except Exception as e:  # a libmpv built without yt-dlp support
            log.info("couldn't set mpv's yt-dlp options: %s", e)

    def press(self, key):
        """Press an mpv key (a mouse button, the wheel): what is bound to it happens, as when
        mpv sees it on a window of its own."""
        self.mpv.command("keypress", key)

    def set_keys(self, keys):
        """Replace the hotkeys: `keys` is {action: mpv key name}."""
        for binding in self._bindings:
            binding.unregister_mpv_key_bindings()
        pairs = list(keys.items()) + [(action, key) for key, action in FIXED_KEYS.items()
                                      if key not in keys.values()]
        self._bindings = [
            self.mpv.on_key_press(key)(
                lambda action=action: self._main(self.key_pressed.emit, action))
            for action, key in pairs]

    def _volume_changed(self):
        try:
            volume, muted = float(self.mpv.volume or 0), bool(self.mpv.mute)
        except Exception:
            return  # shutting down
        self._main(self.volume_changed.emit, volume, muted)

    def _main(self, fn, *args):
        """Run fn on the main thread, unless the player has been closed meanwhile."""
        mw.taskman.run_on_main(lambda: self._alive and fn(*args))

    def _sub_changed(self, _name, _value):
        try:
            p = self.mpv
            text, start, end = p.sub_text or "", p.sub_start, p.sub_end
            delay = p["sub-delay"] or 0.0
        except Exception:
            return  # shutting down
        # sub-start / sub-end are the file's times: the delay isn't in them
        if start is not None:
            start += delay
        if end is not None:
            end += delay
        self._main(self.subtitle_changed.emit, text, start, end)

    # --- state ---------------------------------------------------------------

    def _get(self, name, default=None):
        """A property (attribute access: mpv[name] would read the option of that name)."""
        try:
            return getattr(self.mpv, name.replace("-", "_"))
        except Exception:
            return default

    @property
    def path(self):
        return self._get("path")

    @property
    def time_pos(self):
        return self._get("time-pos")

    @property
    def duration(self):
        return self._get("duration")

    @property
    def paused(self):
        return bool(self._get("pause", True))

    @paused.setter
    def paused(self, value):
        self.mpv.pause = value

    @property
    def sub_delay(self):
        return self._get("sub-delay", 0.0) or 0.0

    @sub_delay.setter
    def sub_delay(self, delay):
        self.mpv["sub-delay"] = delay
        try:
            self.mpv["secondary-sub-delay"] = delay  # keep the translation in step
        except Exception:
            pass  # older libmpv

    @property
    def volume(self):
        return self._get("volume", 100.0) or 0.0

    @volume.setter
    def volume(self, value):
        self.mpv.volume = max(0.0, min(float(value), MAX_VOLUME))

    @property
    def muted(self):
        return bool(self._get("mute", False))

    @muted.setter
    def muted(self, value):
        self.mpv.mute = bool(value)

    @property
    def speed(self):
        return self._get("speed", 1.0) or 1.0

    @speed.setter
    def speed(self, value):
        self.mpv.speed = value

    @property
    def secondary_text(self):
        return self._get("secondary-sub-text") or ""

    @property
    def track_list(self):
        return self._get("track-list", [])

    # --- commands --------------------------------------------------------------

    def play(self, path, start=None):
        self.mpv["start"] = f"{start:.1f}" if start else "none"
        self.mpv.play(path)

    def seek(self, t):
        """Absolute, frame-exact seek, in seconds."""
        self.mpv.seek(t, reference="absolute", precision="exact")

    def seek_by(self, seconds):
        self.mpv.seek(seconds)

    def loop(self, start, end):
        """Repeat [start, end] until clear_loop()."""
        self.mpv["ab-loop-a"] = start
        self.mpv["ab-loop-b"] = end

    def clear_loop(self):
        self.mpv["ab-loop-a"] = "no"
        self.mpv["ab-loop-b"] = "no"

    def add_subtitle(self, path, select=False):
        """Add a subtitle file; it is only shown once selected (or with `select`)."""
        self.mpv.command("sub-add", str(path), "select" if select else "auto")

    def select(self, prop, track_id):
        """Set "sid", "secondary-sid" or "aid" (None = off)."""
        self.mpv[prop] = track_id if track_id is not None else "no"

    def terminate(self):
        self._alive = False
        self.mpv.terminate()
