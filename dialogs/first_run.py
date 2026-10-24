"""First run: the default settings put cards in a Basic note type, with no field for the audio,
the picture or the word, and nothing is set up to play the video or look words up, so most
of Ankime would do nothing. Once, on a new install, the setup wizard (setup_wizard.py) walks
through all of it; Tools > Ankime > Setup wizard runs it again. What it decides is worked out
here, without Qt."""
import html

from aqt import mw
from aqt.utils import showWarning, tooltip

from ..cards import notes, presets
from ..core import config, state

# Field names that usually hold each thing, for a note type no preset fits (lower case, most
# likely first). Basic's Front and Back are what the defaults point at.
GUESSES = {
    "sentence_field": ("sentence", "front", "japanese", "example", "example sentence"),
    "word_field": ("word", "expression", "vocab", "vocabulary", "target word"),
    "reading_field": ("reading", "word reading", "expression reading", "kana"),
    "definition_field": ("definition", "meaning", "glossary", "english", "back"),
    "audio_field": ("sentence audio", "audio", "sound"),
    "image_field": ("picture", "image", "screenshot"),
}

_wizard = None  # the wizard open, if one is: kept alive here, it isn't modal


def untouched(cfg):
    """Are the card settings still the defaults?"""
    keys = ("note_type", "sentence_field", "audio_field", "image_field", "word_field")
    return all(cfg[key] == config.DEFAULTS[key] for key in keys)


def guess_fields(field_names):
    """(preset, {config key: field}) for a note type with these fields: its preset's fields
    if it looks like one (preset None otherwise), else the fields named like GUESSES, each
    used once."""
    preset = presets.detect(field_names)
    if preset:
        return preset, presets.fields_for(preset, field_names)
    by_lower = {name.lower(): name for name in field_names}
    found, used = dict.fromkeys(presets.FIELD_KEYS, ""), set()
    for key, names in GUESSES.items():
        field = next((by_lower[n] for n in names if n in by_lower and by_lower[n] not in used),
                     "")
        found[key] = field
        used.add(field)
    return None, found


def mined_note_types(col):
    """[(note type, preset)] for the note types that look like one of the presets: what
    someone who already mines would pick, sorted by name."""
    found = []
    for name in sorted(m.name for m in col.models.all_names_and_ids()):
        model = col.models.by_name(name)
        preset = presets.detect([f["name"] for f in model["flds"]]) if model else None
        if preset:
            found.append((name, preset))
    return found


def known_word_sources(col, note_type):
    """[{"note_type", "word_field"}] of the other note types whose cards have a word field a
    preset knows, for the known words (vocab_sources)."""
    sources = []
    for name in sorted(m.name for m in col.models.all_names_and_ids()):
        model = col.models.by_name(name)
        word = presets.word_field([f["name"] for f in model["flds"]]) if model else ""
        if name != note_type and word:
            sources.append({"note_type": name, "word_field": word})
    return sources


# (config key, what it holds) of the fields the summary lists
SUMMARY_FIELDS = [("sentence_field", "sentence"), ("word_field", "word"),
                  ("reading_field", "reading"), ("definition_field", "definition"),
                  ("audio_field", "audio"), ("image_field", "picture")]


def summary(values, create=False):
    """What the wizard will save, as HTML list items. `create`: the note type is made then."""
    esc = html.escape
    if create:
        cards = (f"Cards go to the deck <b>{esc(values['deck'])}</b> as notes of a new "
                 f"<b>{esc(notes.NOTE_TYPE_NAME)}</b> note type, with a field for everything")
    else:
        fields = [f"{what} → {esc(values[key])}" for key, what in SUMMARY_FIELDS
                  if values.get(key)]
        cards = (f"Cards go to the deck <b>{esc(values['deck'])}</b> as "
                 f"<b>{esc(values['note_type'])}</b> notes ({', '.join(fields)})")
    if create or values.get("word_field"):
        known = (f"A word is known once its card's interval reaches "
                 f"{values['known_interval']} days")
    else:
        known = "Known words are only the ones you mark known (the note type has no word field)"
    others = [s["note_type"] for s in values.get("vocab_sources", ())]
    if others:
        known += f"; words of your {esc(', '.join(others))} cards count too"
    if values["dictionary"] == "local":
        lookup = "Words are looked up in Ankime's own dictionaries"
    else:
        lookup = f"Words are looked up in Yomitan, at {esc(values['yomitan_url'])}"
    tools = []
    for key, name in (("mpv_dll_dir", "libmpv"), ("ffmpeg_path", "ffmpeg")):
        if values[key] != config.DEFAULTS[key]:
            tools.append(f"{name}: {esc(values[key])}")
    items = [cards, known, lookup] + tools
    return "".join(f"<li>{item}</li>" for item in items)


def create_and_use(parent):
    """Create (or reuse) the Ankime note type and save the settings pointing at it. The
    settings saved, or None if it failed."""
    try:
        values = notes.create_note_type(mw.col)
    except Exception as e:
        showWarning(f"Couldn't create the note type:\n\n{e}", parent=parent)
        return None
    config.save(**values)
    state.save(note_type_offered=True)
    tooltip(f"Cards will be '{values['note_type']}' notes", parent=parent)
    return values


def run_wizard(parent=None, then=None, open_player=None):
    """Open the setup wizard (or bring forward the one open). then() once it closes,
    finished or not; with `open_player`, its last page offers to open the player."""
    global _wizard
    if mw.col is None:
        showWarning("Open a profile first.")
        return
    if _wizard is not None:
        _wizard.raise_()
        _wizard.activateWindow()
        return
    from .setup_wizard import SetupWizard
    _wizard = SetupWizard(parent or mw, open_player)

    def closed(_result):
        global _wizard
        _wizard.deleteLater()
        _wizard = None
        if then:
            then()

    _wizard.finished.connect(closed)
    _wizard.open()


def offer(then=None, open_player=None):
    """Once, on a new install (the card settings still the defaults): run the wizard. Was it
    run? Settings set up by hand already count as the setup done."""
    if state.get("note_type_offered") or mw.col is None:
        return False
    state.save(note_type_offered=True)
    if not untouched(config.get()):
        return False
    run_wizard(mw, then, open_player)
    return True
