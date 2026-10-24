"""Tools > Ankime > Dictionaries: Ankime's own dictionaries (language/local_dict.py), so words
can be looked up without Yomitan's browser: download JMdict in one click, import any Yomitan
dictionary, remove one."""
import html
import tempfile
from pathlib import Path

from aqt import mw
from aqt.qt import (QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout, QLabel, QListWidget,
                    QPushButton, QVBoxLayout)
from aqt.utils import askUser, showWarning, tooltip

from ..core import config, downloads
from ..language import local_dict, yomitan
from .downloads import mb, run_with_progress

# Where to find more, for the dialog: (name, what it adds, url)
MORE = [("Jitendex", "a richer English dictionary, with examples", "https://jitendex.org"),
        ("Frequency lists (JPDB...)", "for the frequency field and the transcript's order",
         "https://github.com/Kuuuube/yomitan-dictionaries"),
        ("Pitch accents (Kanjium)", "for the pitch accent field",
         "https://github.com/Kuuuube/yomitan-dictionaries")]


def describe(d):
    counts = []
    if d["terms"]:
        counts.append(f"{d['terms']:,} definitions")
    if d["meta"]:
        counts.append(f"{d['meta']:,} frequencies / pitch accents")
    return f"{d['title']} — {', '.join(counts) or 'empty'}"


class DictionariesDialog(QDialog):
    """`on_changed(setting, value)` is told when the dictionary setting is switched here (a
    settings dialog showing it)."""

    def __init__(self, parent, on_changed=None):
        super().__init__(parent)
        self._on_changed = on_changed
        self.setWindowTitle("Ankime: Dictionaries")
        intro = QLabel("Ankime can look words up in dictionaries of its own, so the browser "
                       "with Yomitan doesn't have to be open. They are Yomitan dictionaries "
                       "(the .zip files Yomitan imports). JMdict, an English dictionary, is "
                       "one click away; frequency and pitch-accent lists fill the frequency "
                       "and pitch fields of word cards. Word audio still needs Yomitan.")
        intro.setWordWrap(True)
        self.list = QListWidget()
        self.list.setAccessibleName("Dictionaries imported")
        self.list.currentRowChanged.connect(self._selected)
        jmdict = QPushButton("Download JMdict (English)")
        jmdict.setToolTip("The latest JMdict from github.com/yomidevs/jmdict-yomitan, about "
                          "16 MB")
        jmdict.clicked.connect(self.download_jmdict)
        add = QPushButton("Import a dictionary...")
        add.setToolTip("A Yomitan dictionary .zip file")
        add.clicked.connect(self.import_files)
        self.remove_button = QPushButton("Remove")
        self.remove_button.clicked.connect(self.remove_selected)
        row = QHBoxLayout()
        for button in (jmdict, add, self.remove_button):
            row.addWidget(button)
        row.addStretch(1)
        more = QLabel("More dictionaries: " + " · ".join(
            f'<a href="{html.escape(url, quote=True)}">{html.escape(name)}</a> '
            f"({html.escape(what)})" for name, what, url in MORE))
        more.setWordWrap(True)
        more.setOpenExternalLinks(True)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.linkActivated.connect(lambda _link: self.use_local())
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        for widget in (intro, self.list):
            layout.addWidget(widget)
        layout.addLayout(row)
        for widget in (more, self.status, buttons):
            layout.addWidget(widget)
        self.resize(560, 420)
        self.refresh()

    def refresh(self):
        try:
            self._dicts = local_dict.dictionaries()
        except Exception as e:
            self._dicts = []
            self.status.setText(f"Couldn't read the dictionaries: {html.escape(str(e))}")
            return
        self.list.clear()
        for d in self._dicts:
            self.list.addItem(describe(d))
        self._selected(self.list.currentRow())
        using = yomitan.uses_local()
        if not self._dicts:
            text = "No dictionary yet."
        elif using:
            text = "Ankime looks words up in these, in this order (Settings > Lookup)."
        else:
            text = ("Words are looked up in Yomitan for now: <a href='use'>use these "
                    "instead</a> (Settings > Lookup > Dictionary).")
        self.status.setText(text)

    def _selected(self, row):
        self.remove_button.setEnabled(0 <= row < len(self._dicts))

    def use_local(self):
        config.save(dictionary="local")
        yomitan.BREAKER.retry_now()
        if self._on_changed:
            self._on_changed("dictionary", "local")
        tooltip("Words are now looked up in Ankime's dictionaries", parent=self)
        self.refresh()

    def _imported(self, titles):
        """After an import: offer to use the dictionaries, if Yomitan is used still."""
        yomitan.BREAKER.retry_now()
        self.refresh()
        if titles and not yomitan.uses_local() and askUser(
                f"Imported {', '.join(titles)}.\n\nLook words up in Ankime's dictionaries "
                "from now on, rather than in Yomitan? (Settings > Lookup > Dictionary "
                "switches back.)", parent=self):
            self.use_local()
        elif titles:
            tooltip(f"Imported {html.escape(', '.join(titles))}", parent=self)

    def import_files(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Import Yomitan dictionaries", "",
                                                "Yomitan dictionaries (*.zip)")
        if not paths:
            return

        def work(progress, cancelled):
            titles = []
            for i, path in enumerate(paths):
                name = Path(path).name
                titles.append(local_dict.import_zip(
                    path, lambda done, total, i=i, name=name: progress(
                        (i + done / max(total, 1)) / len(paths), f"Importing {name}..."),
                    cancelled))
            return titles

        run_with_progress(self, "Importing...", work, self._finished_import)

    def _finished_import(self, future):
        try:
            titles = future.result()
        except (local_dict.Cancelled, downloads.Cancelled):
            self.refresh()
            return
        except Exception as e:
            showWarning(f"Couldn't import the dictionary:\n\n{e}", parent=self)
            self.refresh()
            return
        self._imported(titles)

    def download_jmdict(self):
        download_jmdict(self, self._finished_import)

    def remove_selected(self):
        row = self.list.currentRow()
        if not 0 <= row < len(self._dicts):
            return
        title = self._dicts[row]["title"]
        if not askUser(f"Remove {title}?", parent=self):
            return

        def done(future):
            try:
                future.result()
            except Exception as e:
                showWarning(f"Couldn't remove {title}:\n\n{e}", parent=self)
            self.refresh()

        self.status.setText(f"Removing {html.escape(title)}...")
        mw.taskman.run_in_background(lambda: local_dict.remove(title), done)


def download_jmdict(parent, done):
    """Download JMdict and import it, behind a progress dialog; then done(future), whose
    result is [its title]."""
    def work(progress, cancelled):
        with tempfile.TemporaryDirectory(prefix="ankime-") as folder:
            path = Path(folder) / downloads.JMDICT_ASSET
            downloads.download_jmdict(
                path, lambda done, total: progress(
                    0.4 * done / total, f"Downloading JMdict: {mb(done)} of {mb(total)}"),
                cancelled)
            return [local_dict.import_zip(
                path, lambda done, total: progress(
                    0.4 + 0.6 * done / max(total, 1), "Importing JMdict..."), cancelled)]

    run_with_progress(parent, "Downloading JMdict...", work, done)


def open_dictionaries(parent=None, on_changed=None):
    if mw.col is None:
        showWarning("Open a profile first.")
        return
    DictionariesDialog(parent or mw, on_changed).exec()
