"""Downloading libmpv and ffmpeg for the user (Windows), with a progress dialog, and setting
them up: the settings then point at them. See core/downloads.py for what is downloaded and
how it is checked."""
from aqt import mw
from aqt.qt import QProgressDialog, Qt
from aqt.utils import askUser, showWarning, tooltip

from ..core import config, downloads

# the setting each tool's download sets
SETTINGS = {"libmpv": "mpv_dll_dir", "ffmpeg": "ffmpeg_path"}


def run_with_progress(parent, title, work, done):
    """work(progress, cancelled) in the background behind a progress dialog with Cancel,
    then done(future) on the main thread. work calls progress(fraction, text) from its
    thread, and should stop (raising) once cancelled() is True."""
    dialog = QProgressDialog(title, "Cancel", 0, 1000, parent)
    dialog.setWindowTitle("Ankime")
    dialog.setWindowModality(Qt.WindowModality.WindowModal)
    dialog.setMinimumDuration(0)
    dialog.setAutoClose(False)
    dialog.setAutoReset(False)
    state = {"cancelled": False, "finished": False}
    dialog.canceled.connect(lambda: state.update(cancelled=True))

    def show(fraction, text):
        if not state["finished"]:
            dialog.setValue(int(max(0.0, min(fraction, 1.0)) * 1000))
            if text:
                dialog.setLabelText(text)

    def progress(fraction, text=""):
        mw.taskman.run_on_main(lambda: show(fraction, text))

    def finished(future):
        state["finished"] = True
        dialog.hide()
        dialog.deleteLater()
        done(future)

    dialog.show()
    mw.taskman.run_in_background(lambda: work(progress, lambda: state["cancelled"]), finished)


def mb(n):
    return f"{n / 2**20:.0f} MB"


def install_tool(parent, name, on_installed=None):
    """Offer to download tool `name` ("libmpv", "ffmpeg"); once set up, its setting is saved
    and on_installed(setting, value) told (a settings dialog showing it)."""
    tool = downloads.TOOLS[name]
    if not askUser(f"Download {tool.name} {tool.version}, {mb(tool.size)}, from {tool.source} "
                   "into Ankime's folder, and use it?\n\nThe download is checked against the "
                   "SHA-256 checksum of that exact build before it is used.", parent=parent):
        return

    def work(progress, cancelled):
        def bytes_done(done, total):
            progress(done / total, f"Downloading {tool.name}: {mb(done)} of {mb(total)}")
        return downloads.install(name, bytes_done, cancelled)

    def done(future):
        try:
            path = str(future.result())
        except downloads.Cancelled:
            return
        except Exception as e:
            showWarning(f"Couldn't set up {tool.name}:\n\n{e}", parent=parent)
            return
        key = SETTINGS[name]
        config.save(**{key: path})
        note = " It is used the next time the player opens." if name == "libmpv" else ""
        tooltip(f"{tool.name} is set up.{note}", parent=parent, period=5000)
        if on_installed:
            on_installed(key, path)

    run_with_progress(parent, f"Downloading {tool.name}...", work, done)
