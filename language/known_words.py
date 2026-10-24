"""Known words, from outside the player's word popup: mark the words of a word list known,
export the known words to a word list, and mark every new word of a line (the player's) or
of notes' sentences (the browser's) known. Each takes the player window, None if it isn't
open: while it is, they work on its Knowledge, so the player re-colours at once and Undo
takes the marks back; otherwise on a Knowledge of their own, read from the collection when
needed."""
import datetime
import logging
from pathlib import Path

from aqt.operations import QueryOp
from aqt.qt import QFileDialog
from aqt.utils import askUser, askUserDialog, showWarning, tooltip

from ..core import config, state
from ..subs import subtitles
from . import knowledge, tokenizer, word_lists, yomitan

log = logging.getLogger(__name__)

def _sample(words, shown=5):
    return "、".join(words[:shown]) + ("…" if len(words) > shown else "")


def _target(player):
    """(Knowledge, mark(words)): the player's, undoable, while it is open; else a new one,
    read afresh (the marks may have changed since: a sync, the player)."""
    if player is not None:
        return player.knowledge, player.mark_words_known
    know = knowledge.Knowledge()

    def mark(words):
        know.mark(words)
        tooltip(f"Marked {len(words)} word{'s' if len(words) != 1 else ''} as known")

    return know, mark


def _with_cards(parent, player, know, then):
    """Run then() once `know` has the cards read; the player's always has. A failure (the
    note type is missing...) is shown, and then() runs with the marks only."""
    if player is not None:
        then()
        return
    cfg = config.get()

    def done(result):
        know.apply(cfg["known_interval"], *result)
        then()

    def failed(error):
        log.warning("couldn't read the known words: %s", error)
        showWarning(f"Ankime could not read your cards:\n\n{error}\n\n"
                    "Only the words marked known count.", parent=parent)
        then()

    QueryOp(parent=parent, op=lambda col: knowledge.Knowledge.read_cards(col, cfg),
            success=done).failure(failed).with_progress("Reading your cards...").run_in_background()


# --- word lists ---------------------------------------------------------------

def import_word_list(parent, player=None):
    """Mark the known words of a word list known (see word_lists.import_list): JPDB's
    review history, a Migaku export or a plain list. A way to start from what you already
    know rather than from every word new."""
    path, _ = QFileDialog.getOpenFileName(
        parent, "Mark known from a word list", state.get("last_video_dir"),
        "Word lists (*.txt *.tsv *.csv *.json);;JPDB review history (*.json);;"
        "Migaku export (*.csv *.json);;All files (*)")
    if not path:
        return
    try:
        text = subtitles.decode(Path(path).read_bytes())
    except OSError as e:
        showWarning(f"Couldn't read {path}:\n\n{e}", parent=parent)
        return
    found = word_lists.import_list(text, config.get()["known_interval"])
    know, mark = _target(player)
    words = [w for w in found.words if not know.is_marked(w)]
    if not words:
        tooltip(f"No known Japanese words in that {found.source} that aren't marked known "
                "already")
        return
    skipped = (f"\n\n{found.skipped} words in it that you're still learning stay new."
               if found.skipped else "")
    undo = "Undo in the player takes them all back." if player is not None else (
        "Marks can be taken back one word at a time, from the player's word popup.")
    if askUser(f"From this {found.source}: mark {len(words)} words as known "
               f"({_sample(words)})?{skipped}\n\n{undo}", parent=parent):
        mark(words)


EXPORT_CHOICES = ["All known words", "Only the words I marked known", "Cancel"]


def export_known_words(parent, player=None):
    """Save the known words to a word list, one per line, in the format chosen as the file
    type (word_lists.EXPORT_FORMATS: Ankime's, JPDB's, Migaku's): a backup of the marks,
    or the words to take to another tool. Import reads each back."""
    know, _ = _target(player)
    choice = askUserDialog(
        "Which words?\n\nAll known words: those marked known and those whose cards have "
        "reached the known interval (for another tool).\nOnly the words I marked known: "
        "the ones not in your cards (a backup of the marks).",
        EXPORT_CHOICES, parent=parent).run()
    if choice not in EXPORT_CHOICES[:2]:
        return
    marked_only = choice == EXPORT_CHOICES[1]

    def save():
        words = know.known_words(marked_only)
        if not words:
            tooltip("No known words to export")
            return
        today = datetime.date.today().isoformat()
        name = f"ankime-{'marked' if marked_only else 'known'}-words-{today}.txt"
        path, selected = QFileDialog.getSaveFileName(
            parent, "Export known words", str(Path(state.get("last_video_dir") or "") / name),
            ";;".join(map(word_lists.export_filter, word_lists.EXPORT_FORMATS)))
        if not path:
            return
        fmt = word_lists.format_for_filter(selected)
        what = "words marked known" if marked_only else "known words"
        try:
            Path(path).write_text(
                word_lists.export_list(words, fmt, f"Ankime {what}, {today}: {len(words)}"),
                encoding="utf-8", newline="\n")
        except OSError as e:
            showWarning(f"Couldn't save {path}:\n\n{e}", parent=parent)
            return
        tooltip(f"Exported {len(words)} {what}")

    if marked_only:
        save()
    else:
        _with_cards(parent, player, know, save)


# --- the new words of lines ----------------------------------------------------

def confirm_and_mark(parent, words, mark, where):
    """Ask, then mark `words` (new words of `where`) known."""
    if not words:
        tooltip(f"No new words in {where}")
        return
    if askUser(f"Mark the {len(words)} new word{'s' if len(words) != 1 else ''} of "
               f"{where} as known?\n\n{_sample(words, 20)}", parent=parent):
        mark(words)


def note_sentences(col, note_ids, field):
    """The plain text of `field` of each note that has it, and how many notes don't."""
    texts, missing = [], 0
    for nid in note_ids:
        note = col.get_note(nid)
        if field not in note:
            missing += 1
        elif text := knowledge.sentence_text(note[field]):
            texts.append(text)
    return texts, missing


def mark_notes_known(browser, player=None):
    """Mark every new word of the selected notes' sentences known. The sentences are split
    into words by Yomitan, as the player's lines are."""
    note_ids = list(browser.selected_notes())
    if not note_ids:
        tooltip("Select some notes first", parent=browser)
        return
    cfg = config.get()
    field = cfg["sentence_field"]
    know, mark = _target(player)

    def op(col):
        texts, missing = note_sentences(col, note_ids, field)
        return texts, missing, tokenizer.tokenize_many(texts) if texts else []

    def done(result):
        texts, missing, tokens = result
        if missing and not texts:
            showWarning(f"None of the selected notes has a '{field}' field (Ankime's "
                        "sentence field, see its settings).", parent=browser)
            return
        if not texts:
            tooltip("The selected notes' sentences are empty", parent=browser)
            return
        words = list(dict.fromkeys(w for line in tokens for w in know.new_words(line)))
        where = "this sentence" if len(texts) == 1 else f"these {len(texts)} sentences"
        if missing:
            where += f" ({missing} notes have no '{field}' field)"
        confirm_and_mark(browser, words, mark, where)

    def failed(error):
        if not isinstance(error, yomitan.YomitanError):
            log.error("couldn't mark the notes' words known", exc_info=error)
        showWarning(f"Ankime could not split the sentences into words:\n\n{error}",
                    parent=browser)

    _with_cards(browser, player, know, lambda: QueryOp(parent=browser, op=op, success=done)
                .failure(failed).with_progress("Asking Yomitan...").run_in_background())


def add_browser_menu(browser, get_player):
    """Notes > Ankime: mark the sentences' new words known (in the right-click menu too).
    `get_player()` is the player window if it is open when the action is chosen."""
    menu = browser.form.menu_Notes
    menu.addSeparator()
    action = menu.addAction("Ankime: Mark every new word of the sentence known",
                            lambda: mark_notes_known(browser, get_player()))
    action.setToolTip("The new words of the selected notes' sentence field, as Yomitan "
                      "splits them (names don't count)")
