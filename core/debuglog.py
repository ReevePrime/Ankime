"""A small debug log in user_files/ankime.log: errors that are handled quietly (a file that
couldn't be written, Yomitan or ffmpeg failing) leave a trace there to attach to a bug report.
Modules log through logging.getLogger(__name__); install() is called once, in Anki."""
import logging
from logging.handlers import RotatingFileHandler

from .store import USER_FILES

LOG_FILE = USER_FILES / "ankime.log"
_LOGGER = __name__.split(".")[0]   # the add-on's package: every module logs under it


def install():
    logger = logging.getLogger(_LOGGER)
    if any(isinstance(h, RotatingFileHandler) for h in logger.handlers):
        return  # the add-on was reloaded
    try:
        USER_FILES.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(LOG_FILE, maxBytes=512 * 1024, backupCount=1,
                                      encoding="utf-8")
    except OSError:
        return
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False  # not into Anki's own log / console


def uninstall():
    """Closes the log file: on Windows, Anki can't move user_files/ aside to update the
    add-on while it is open. Removed, not just closed: a closed handler reopens its file."""
    logger = logging.getLogger(_LOGGER)
    for h in [h for h in logger.handlers if isinstance(h, RotatingFileHandler)]:
        logger.removeHandler(h)
        h.close()
