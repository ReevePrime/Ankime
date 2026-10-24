"""Card creation: writing notes to the Anki collection. Main thread only; the media files
come from media.py, cut in a background thread beforehand."""
import html
import logging
import re
from pathlib import Path

from aqt import gui_hooks, mw
from aqt.operations.note import add_note, update_note
from aqt.operations.note import remove_notes as _remove_notes
from aqt.utils import askUser, showWarning

from ..core import config
from ..core.model import SetupError
from ..core.timefmt import fmt_time
from ..language.sanitize import clean_html
from ..subs import subtitles
from ..subs.session import token_offsets
from .media import discard

log = logging.getLogger(__name__)

# card key (see yomitan.card_fields) -> config key naming the note field it goes to
WORD_FIELDS = {"word": "word_field", "reading": "reading_field",
               "definition": "definition_field", "pitch": "pitch_field",
               "frequency": "frequency_field", "word_audio": "word_audio_field"}


# --- sentence formatting (pure) ---------------------------------------------

def _bold_range(text, bold, at):
    """(start, end) in `text` of the word to bold: the one at index `at`, where the word was
    in the line (人 in 日本人は人が… is the second one), else the first one. None if none."""
    if bold and (at is None or text[at:at + len(bold)] != bold):
        at = text.find(bold)
    return (at, at + len(bold)) if bold and at >= 0 else None


def sentence_html(text, bold=None, at=None):
    """The subtitle text for a field: escaped, line breaks as <br>, and `bold` (the word
    being mined) in <b>, found as _bold_range finds it."""
    found = _bold_range(text, bold, at)
    if not found:
        return html.escape(text, quote=False).replace("\n", "<br>")
    parts = (text[:found[0]], text[found[0]:found[1]], text[found[1]:])
    before, word, after = (html.escape(p, quote=False) for p in parts)
    return f"{before}<b>{word}</b>{after}".replace("\n", "<br>")


def sentence_furigana(text, tokens, bold=None, at=None):
    """The sentence in Anki's furigana format, 日本語[にほんご]を 勉強[べんきょう]する, from
    the readings Yomitan gave its `tokens` (session.tokens_for; [] for none): each reading
    in brackets after its kanji, with a space before them so the reading covers only those
    (the {{furigana:}} filter takes the space away). Otherwise like sentence_html; a bolded
    word takes in the whole of a kanji group it cuts into."""
    ruby = {}  # start in `text` -> (end, field text) of each segment with a reading
    for token, start in zip(tokens, token_offsets(text, tokens)):
        if start is None or isinstance(token.furigana, str):
            continue
        for segment, reading in token.furigana:
            if reading and reading != segment and text.startswith(segment, start):
                ruby[start] = (start + len(segment), f"{html.escape(segment, quote=False)}"
                               f"[{html.escape(reading, quote=False)}]")
            start += len(segment)
    found = _bold_range(text, bold, at)
    if found:
        b0, b1 = found
        for start, (end, _) in ruby.items():
            if start < b0 < end:
                b0 = start
            if start < b1 < end:
                b1 = end
        found = b0, b1
    out, i = [], 0
    while i < len(text):
        if found and i == found[0]:
            out.append("<b>")
        if i in ruby:
            end, piece = ruby[i]
            if out and not out[-1].endswith(">"):  # after a tag, the reading can't reach back
                out.append(" ")
            out.append(piece)
        else:
            end = i + 1
            out.append("<br>" if text[i] == "\n" else html.escape(text[i], quote=False))
        if found and end == found[1]:
            out.append("</b>")
        i = end
    return "".join(out)


def show_name(video):
    """The show a video belongs to: its folder's name ("Show", or "Show/Season 1"), the same
    key the subtitle delay is remembered by. "" for a video given without its folder."""
    name = subtitles.delay_key(Path(video).parent)
    return "" if name in (".", "..") else name


def source_text(video, start):
    """Where a card comes from, for the source field: "Show · episode · 12:34"."""
    parts = [show_name(video), Path(video).stem, fmt_time(start)]
    return html.escape(" · ".join(p for p in parts if p), quote=False)


def show_tag(video):
    """ankime::<Show> (ankime::Show::Season_1 for a season folder), or "" without a folder.
    Tags can't hold spaces, and "::" is Anki's tag hierarchy."""
    name = show_name(video)
    if not name:
        return ""
    parts = [re.sub(r"\s+", "_", p.strip()).replace(":", "_") for p in name.split("/")]
    return "::".join(["ankime", *(p for p in parts if p)])


def _replace_names(fields, renamed):
    """Point the fields at media files that write_data stored under another name."""
    for old, new in renamed.items():
        for key, value in fields.items():
            fields[key] = value.replace(old, new)


# --- the collection ----------------------------------------------------------

def add_card(parent, text, image, audio, word=None, translation="", bold=None,
             origin=None, on_added=None, on_finished=None, bold_at=None, tokens=(),
             context=""):
    """Builds the note and adds it as an undoable collection operation. `word` (optional)
    is (fields, media) from yomitan.card_fields; `translation` is the target-language
    subtitle, stored in translation_field if set; `bold` is the word to highlight in the
    sentence, at index `bold_at` of `text` if given (see sentence_html); `origin` is (video,
    start) for the source field and the show tag; `tokens` are the sentence's, for its
    furigana, and `context` the line before it. The scratch `image` and `audio` files are
    deleted either way; `on_added(note_id)` runs once the note exists, then
    `on_finished(ok)` whether it was added or not."""
    def finish(ok):
        if on_finished:
            on_finished(ok)

    try:
        note, deck_id, written = _build_note(text, image, audio, word, translation, bold,
                                             origin, bold_at, tokens, context)
    except Exception as e:
        log.warning("couldn't build the note: %s", e)
        discard(image, audio)
        showWarning(f"Failed to add card:\n\n{e}", parent=parent)
        finish(False)
        return

    def added(_changes):
        discard(image, audio)
        if on_added:
            on_added(note.id)
        finish(True)

    def failed(error):
        log.warning("couldn't add the note: %s", error)
        discard(image, audio)
        _trash_media(written)  # no note uses them
        showWarning(f"Failed to add card:\n\n{error}", parent=parent)
        finish(False)

    add_note(parent=parent, note=note, target_deck_id=deck_id).success(added).failure(
        failed).run_in_background()


class _Review:
    """The card waiting in Anki's Add window (review_card): told when it is added from there,
    and its media taken back if the window is closed, or given another card, without
    adding it."""

    def __init__(self):
        self.window = None    # the Add window holding it, None if none does
        self.written = []     # media it put in the collection
        self.on_added = None  # fn(note id)
        self._hooked = False

    def waiting(self):
        return self.window is not None

    def start(self, window, written, on_added):
        if not self._hooked:
            gui_hooks.add_cards_did_add_note.append(self._added)
            self._hooked = True
        if window is not self.window:
            window.destroyed.connect(lambda *_: self._closed(window))
        self.window, self.written, self.on_added = window, list(written), on_added

    def drop(self, keep=()):
        """Forget the card, taking back its media but the files named in `keep` (the
        fields of the card replacing it, which may use the same word audio)."""
        _trash_media([n for n in self.written if not any(n in f for f in keep)])
        self.window, self.written, self.on_added = None, [], None

    def _added(self, note):
        """A note was added from the Add window: the first one since the card was put in it
        is that card, however it was edited (the window empties itself after adding)."""
        if self.window is None:
            return
        on_added = self.on_added
        self.window, self.written, self.on_added = None, [], None
        if on_added:
            on_added(note.id)

    def _closed(self, window):
        if window is self.window:
            self.drop()


_REVIEW = _Review()


def review_card(parent, text, image, audio, word=None, translation="", bold=None,
                origin=None, on_added=None, on_finished=None, bold_at=None, tokens=(),
                context=""):
    """Like add_card, but the note opens in Anki's Add window, filled in, to edit (trim the
    sentence, keep one definition) and add from there: the review_before_adding setting.
    `on_added(note_id)` runs if it is added; `on_finished(ok)` once the window shows it."""
    def finish(ok):
        if on_finished:
            on_finished(ok)

    from aqt import dialogs
    window = getattr(dialogs, "_dialogs", {}).get("AddCards", [None, None])[1]
    note_in_window = window is not None and window.editor.note is not None and any(
        window.editor.note.fields)
    if (_REVIEW.waiting() or note_in_window) and not askUser(
            "Anki's Add window holds a card that wasn't added. Replace it with this one?",
            parent=parent):
        discard(image, audio)
        finish(False)
        return
    try:
        note, deck_id, written = _build_note(text, image, audio, word, translation, bold,
                                             origin, bold_at, tokens, context)
    except Exception as e:
        log.warning("couldn't build the note: %s", e)
        showWarning(f"Failed to add card:\n\n{e}", parent=parent)
        finish(False)
        return
    finally:
        discard(image, audio)  # in the collection now, if the note was built
    if _REVIEW.waiting():
        _REVIEW.drop(keep=note.fields)
    try:
        window = dialogs.open("AddCards", mw)
        window.set_note(note, deck_id)
    except Exception as e:
        log.warning("couldn't open the Add window: %s", e)
        _trash_media(written)
        showWarning(f"Couldn't open Anki's Add window:\n\n{e}", parent=parent)
        finish(False)
        return
    _REVIEW.start(window, written, on_added)
    window.activateWindow()
    window.raise_()
    finish(True)


def _check_field(cfg, note, config_key):
    field = cfg[config_key]
    if field and field not in note:
        raise SetupError(f"Note type '{cfg['note_type']}' has no field '{field}' "
                        f"(config: {config_key})")
    return field


def _trash_media(names):
    """Take back media files written to the collection for a note that wasn't added or
    changed after all."""
    if not names:
        return
    try:
        mw.col.media.trash_files(list(names))
    except Exception as e:
        log.warning("couldn't remove the unused media %s: %s", names, e)


def _sentence_fields(cfg, note, text, bold, bold_at, tokens, context):
    """{field: value} for the sentence, its furigana and its context, as configured."""
    bold = bold if cfg["bold_word"] else None
    fields = {_check_field(cfg, note, "sentence_field"): sentence_html(text, bold, bold_at)}
    if field := _check_field(cfg, note, "sentence_furigana_field"):
        fields[field] = sentence_furigana(text, tokens, bold, bold_at)
    if (field := _check_field(cfg, note, "context_field")) and context:
        fields[field] = sentence_html(context)
    return fields


def _build_note(text, image, audio, word, translation, bold, origin, bold_at=None, tokens=(),
                context=""):
    """(note, deck id, names of the media files it added to the collection). Every field is
    checked before any media is written, and media written before a failure is taken back:
    a note that isn't added leaves nothing behind."""
    cfg = config.get()
    model = mw.col.models.by_name(cfg["note_type"])
    if model is None:
        raise SetupError(f"Note type '{cfg['note_type']}' not found")
    deck_id = mw.col.decks.id_for_name(cfg["deck"])
    if deck_id is None:
        raise SetupError(f"Deck '{cfg['deck']}' not found")

    note = mw.col.new_note(model)
    for field, value in _sentence_fields(cfg, note, text, bold, bold_at, tokens,
                                         context).items():
        note[field] = value
    audio_field = _check_field(cfg, note, "audio_field")
    image_field = _check_field(cfg, note, "image_field")
    values, media, targets = {}, [], {}
    if word:
        values, media = word
        targets = {key: _check_field(cfg, note, config_key)
                   for key, config_key in WORD_FIELDS.items()}
        # dictionary HTML can hold anything, and card templates run script
        values = {key: clean_html(values.get(key, "")) for key, field in targets.items()
                  if field}
        # the word again, for note types keyed by it (JP Mining Note's Key)
        if key_field := _check_field(cfg, note, "key_field"):
            targets["key"] = key_field
            values["key"] = clean_html(word[0].get("word", ""))
    translation_field = _check_field(cfg, note, "translation_field")
    source_field = _check_field(cfg, note, "source_field")

    written = []  # media new to the collection, to take back if the note isn't added
    try:
        if audio_field:
            written.append(mw.col.media.add_file(str(audio)))
            note[audio_field] = f"[sound:{written[-1]}]"
        if image_field:
            written.append(mw.col.media.add_file(str(image)))
            note[image_field] = f'<img src="{written[-1]}">'
        renamed = {}
        for name, data in media:
            if any(name in v for v in values.values()):  # only what the fields refer to
                existed = mw.col.media.have(name)  # another card's word audio or image
                stored = mw.col.media.write_data(name, data)
                if stored != name:
                    renamed[name] = stored
                if stored != name or not existed:
                    written.append(stored)
    except Exception:
        _trash_media(written)
        raise
    _replace_names(values, renamed)
    for key, value in values.items():
        note[targets[key]] = value
    if translation_field and translation:
        note[translation_field] = sentence_html(translation)
    if source_field and origin:
        note[source_field] = source_text(*origin)
    note.tags = list(cfg["tags"])
    if cfg["show_tag"] and origin and (tag := show_tag(origin[0])):
        note.tags.append(tag)
    return note, deck_id, written


def append_to_note(parent, note_id, text, image, audio, translation="", bold=None,
                   origin=None, on_done=None, on_finished=None, bold_at=None, tokens=(),
                   context=""):
    """Add another sentence (and its furigana, context, audio, translation and source) to
    an existing note
    instead of making a duplicate. `on_done(old_fields, new_fields, media)` gets what the
    changed fields held before and after, and the media files added, to undo it (see
    restore_fields); `on_finished(ok)` runs after, whether it worked or not."""
    cfg = config.get()

    def finish(ok):
        if on_finished:
            on_finished(ok)

    written = []  # the audio, once in the collection: taken back if the note isn't changed
    try:
        note = mw.col.get_note(note_id)
        changes = _sentence_fields(cfg, note, text, bold, bold_at, tokens, context)
        audio_field = _check_field(cfg, note, "audio_field")
        field = _check_field(cfg, note, "translation_field")
        if field and translation:
            changes[field] = sentence_html(translation)
        field = _check_field(cfg, note, "source_field")
        if field and origin:
            changes[field] = source_text(*origin)
        if audio_field:
            written.append(mw.col.media.add_file(str(audio)))
            changes[audio_field] = f"[sound:{written[-1]}]"
        old = {name: note[name] for name in changes}
        for name, value in changes.items():
            note[name] = f"{note[name]}<br>{value}" if note[name] else value
        new = {name: note[name] for name in changes}
    except Exception as e:
        log.warning("couldn't update note %s: %s", note_id, e)
        discard(image, audio)
        _trash_media(written)
        showWarning(f"Failed to update the card:\n\n{e}", parent=parent)
        finish(False)
        return
    discard(image)  # the note keeps its own picture

    def done(_changes):
        discard(audio)
        if on_done:
            on_done(old, new, list(written))
        finish(True)

    def failed(error):
        log.warning("couldn't update note %s: %s", note_id, error)
        discard(audio)
        _trash_media(written)
        showWarning(f"Failed to update the card:\n\n{error}", parent=parent)
        finish(False)

    update_note(parent=parent, note=note).success(done).failure(failed).run_in_background()


def unappend(current, old, new):
    """A field as it was before append_to_note turned `old` into `new`, keeping what was
    edited in it since: what was appended is taken off its end. None if the field no longer
    ends with it (the appended sentence was edited or moved)."""
    if current == new:
        return old
    added = new[len(old):]  # "<br>" + the sentence, or the sentence if the field was empty
    if not added or not current.endswith(added):
        return None
    return current[:len(current) - len(added)]


def restore_fields(parent, note_id, old_fields, new_fields, media=(), on_done=None):
    """Undo append_to_note: take the appended sentence off each field (see unappend), and
    the `media` it added out of the collection. Edits made to the note since are kept; if
    the appended part itself was edited, nothing is changed."""
    try:
        note = mw.col.get_note(note_id)
    except Exception as e:  # deleted meanwhile
        showWarning(f"Couldn't undo: the card is gone ({e})", parent=parent)
        return
    restored = {}
    for name, value in old_fields.items():
        if name not in note:
            continue
        restored[name] = unappend(note[name], value, new_fields[name])
        if restored[name] is None:
            showWarning(f"Couldn't undo: the card's {name} field was edited since the "
                        "sentence was added to it. Remove the sentence by hand (Edit last "
                        "card).", parent=parent)
            return
    for name, value in restored.items():
        note[name] = value

    def done(_changes):
        _trash_media(media)  # no note uses them any more
        if on_done:
            on_done()

    def failed(error):
        showWarning(f"Couldn't undo:\n\n{error}", parent=parent)

    update_note(parent=parent, note=note).success(done).failure(failed).run_in_background()


def remove_notes(parent, note_ids, on_done=None):
    """Undo add_card: several notes at once for a batch."""
    def failed(error):
        showWarning(f"Couldn't undo:\n\n{error}", parent=parent)

    _remove_notes(parent=parent, note_ids=list(note_ids)).success(
        lambda _: on_done and on_done()).failure(failed).run_in_background()


# --- the note type ------------------------------------------------------------

NOTE_TYPE_NAME = "Ankime"
# field name -> the setting that points at it
NOTE_TYPE_FIELDS = {
    "Sentence": "sentence_field", "Picture": "image_field", "Audio": "audio_field",
    "Word": "word_field", "Reading": "reading_field", "Definition": "definition_field",
    "Pitch": "pitch_field", "Frequency": "frequency_field", "Word Audio": "word_audio_field",
    "Translation": "translation_field", "Source": "source_field",
    "Sentence Furigana": "sentence_furigana_field", "Context": "context_field",
}
NOTE_TYPE_ADDED_LATER = {"Sentence Furigana", "Context"}  # an older Ankime lacks them
NOTE_TYPE_FRONT = """{{#Context}}<div class="context" lang="ja">{{Context}}</div>{{/Context}}
<div class="sentence" lang="ja">{{Sentence}}</div>"""
NOTE_TYPE_BACK = """{{#Context}}<div class="context" lang="ja">{{Context}}</div>{{/Context}}
<div class="sentence" lang="ja">{{#Sentence Furigana}}{{furigana:Sentence Furigana}}\
{{/Sentence Furigana}}{{^Sentence Furigana}}{{Sentence}}{{/Sentence Furigana}}</div>
<hr id="answer">
{{Audio}}
{{#Word}}<div class="word" lang="ja">{{Word}}{{#Reading}} 【{{Reading}}】{{/Reading}} \
{{Word Audio}}</div>{{/Word}}
{{#Pitch}}<div class="pitch">{{Pitch}}</div>{{/Pitch}}
{{#Frequency}}<div class="frequency">Frequency rank {{Frequency}}</div>{{/Frequency}}
{{#Definition}}<div class="definition">{{Definition}}</div>{{/Definition}}
<div class="picture">{{Picture}}</div>
{{#Translation}}<div class="translation">{{Translation}}</div>{{/Translation}}
{{#Source}}<div class="source">{{Source}}</div>{{/Source}}"""
NOTE_TYPE_CSS = """.card { font-family: "Yu Gothic UI", "Meiryo", "Hiragino Sans",
  "Noto Sans CJK JP", sans-serif; font-size: 22px; text-align: center; }
.sentence { font-size: 30px; }
.context { font-size: 18px; opacity: .6; margin-bottom: 6px; }
.word { font-size: 26px; margin-top: 10px; }
.definition { text-align: left; display: inline-block; max-width: 40em; }
.picture img { max-width: 100%; max-height: 50vh; margin-top: 10px; }
.translation, .source, .frequency { font-size: 16px; opacity: .7; margin-top: 8px; }"""


def create_note_type(col):
    """Add the Ankime note type (or reuse one by that name that has its fields; one made by
    an older version may lack NOTE_TYPE_ADDED_LATER, left blank) and return {setting: value}
    pointing the settings at it."""
    mm = col.models
    model = mm.by_name(NOTE_TYPE_NAME)
    if model is None or not set(NOTE_TYPE_FIELDS) - NOTE_TYPE_ADDED_LATER <= {
            f["name"] for f in model["flds"]}:
        name, n = NOTE_TYPE_NAME, 2
        while mm.by_name(name) is not None:
            name, n = f"{NOTE_TYPE_NAME} {n}", n + 1
        model = mm.new(name)
        for field in NOTE_TYPE_FIELDS:
            mm.add_field(model, mm.new_field(field))
        template = mm.new_template("Sentence")
        template["qfmt"], template["afmt"] = NOTE_TYPE_FRONT, NOTE_TYPE_BACK
        mm.add_template(model, template)
        model["css"] = NOTE_TYPE_CSS
        (mm.add_dict if hasattr(mm, "add_dict") else mm.add)(model)
        model = mm.by_name(name)
    names = {f["name"] for f in model["flds"]}
    return {"note_type": model["name"], "key_field": "",
            **{key: field if field in names else "" for field, key in NOTE_TYPE_FIELDS.items()}}
