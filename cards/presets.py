"""Field presets for the note types people mine into: which field each setting points at.
Field names are matched without regard to case, and a field the note type doesn't have is
left blank, so a preset also fits an older or edited copy of its note type. Pure logic."""

# config key -> field name. Fields a preset leaves out are blank: those note types have none,
# or one in another format (Lapis' PitchPosition wants pitch numbers, Ankime gives graphs).
PRESETS = {
    "Lapis": {
        "sentence_field": "Sentence", "sentence_furigana_field": "SentenceFurigana",
        "audio_field": "SentenceAudio", "image_field": "Picture", "word_field": "Expression",
        "reading_field": "ExpressionReading", "definition_field": "MainDefinition",
        "frequency_field": "FreqSort", "word_audio_field": "ExpressionAudio",
        "source_field": "MiscInfo",
    },
    "JP Mining Note": {
        "sentence_field": "Sentence", "sentence_furigana_field": "SentenceReading",
        "audio_field": "SentenceAudio", "image_field": "Picture", "word_field": "Word",
        "key_field": "Key", "reading_field": "WordReading",
        "definition_field": "PrimaryDefinition", "frequency_field": "FrequencySort",
        "word_audio_field": "WordAudio", "source_field": "AdditionalNotes",
    },
    "Kiku": {
        "sentence_field": "Sentence", "sentence_furigana_field": "SentenceFurigana",
        "audio_field": "SentenceAudio", "image_field": "Picture", "word_field": "Expression",
        "reading_field": "ExpressionReading", "definition_field": "MainDefinition",
        "frequency_field": "FreqSort", "word_audio_field": "ExpressionAudio",
        "translation_field": "SentenceTranslation", "source_field": "MiscInfo",
    },
    "Senren": {
        "sentence_field": "sentence", "sentence_furigana_field": "sentenceFurigana",
        "audio_field": "sentenceAudio", "image_field": "picture", "word_field": "word",
        "reading_field": "reading", "definition_field": "definition",
        "pitch_field": "pitchAccents", "frequency_field": "freqSort",
        "word_audio_field": "wordAudio", "translation_field": "sentenceTranslation",
        "source_field": "miscInfo",
    },
    "Kaishi 1.5k": {
        "sentence_field": "Sentence", "sentence_furigana_field": "Sentence Furigana",
        "audio_field": "Sentence Audio", "image_field": "Picture", "word_field": "Word",
        "reading_field": "Word Reading", "definition_field": "Word Meaning",
        "word_audio_field": "Word Audio", "translation_field": "Sentence Meaning",
        "source_field": "Notes",
    },
}

# every setting naming a field of the note type cards are added as
FIELD_KEYS = ("sentence_field", "sentence_furigana_field", "context_field", "audio_field",
              "image_field", "word_field", "key_field", "reading_field", "definition_field",
              "pitch_field", "frequency_field", "word_audio_field", "translation_field",
              "source_field")


def fields_for(preset, field_names):
    """{config key: field} for every key in FIELD_KEYS: the preset's field as the note type
    spells it, or "" where the preset has none or the note type lacks it."""
    by_lower = {name.lower(): name for name in field_names}
    wanted = PRESETS[preset]
    return {key: by_lower.get(wanted.get(key, "").lower(), "") for key in FIELD_KEYS}


def missing(preset, field_names):
    """The preset's fields the note type doesn't have."""
    have = {name.lower() for name in field_names}
    return [f for f in PRESETS[preset].values() if f.lower() not in have]


def detect(field_names):
    """The preset whose fields the note type has the most of, or None. Its sentence and word
    fields must be there, and most of the rest; on a tie, the preset that fills more (Kiku
    is Lapis plus a translation field)."""
    best, best_score = None, (0.0, 0)
    for preset, wanted in PRESETS.items():
        found = fields_for(preset, field_names)
        if not (found["sentence_field"] and found["word_field"]):
            continue
        n = sum(1 for key in wanted if found[key])
        if (n / len(wanted), n) > best_score:
            best, best_score = preset, (n / len(wanted), n)
    return best if best_score[0] >= 0.6 else None


def word_field(field_names):
    """The word field of the preset the note type looks like, or ""."""
    preset = detect(field_names)
    return fields_for(preset, field_names)["word_field"] if preset else ""
