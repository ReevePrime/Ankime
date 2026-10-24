"""What the player's Undo takes back: cards added (a batch of them at once), sentences added to
a card, words marked known. Pure logic: the player undoes each record; this keeps them in
order and says what the next Undo would do."""
from collections import deque
from typing import NamedTuple

UNDO_DEPTH = 20


class Record(NamedTuple):
    """kind "added": data is [note ids] (several for a batch), words the cards' words (or
    their lines, for sentence cards); "appended": data is (note id, the fields before, the
    fields after, the media files added), words the word whose card it was; "marked": data
    is {word: marked before}, words the words, `marked` what they were marked as."""
    kind: str
    data: object
    words: list
    marked: bool = True

    @property
    def label(self):
        """What undoing it does, for the Undo button: "remove the card for 食べる"."""
        if self.kind == "added":
            if len(self.data) == 1:
                return f"remove the card for {_short(self.words[0])}"
            return f"remove the batch of {len(self.data)} cards ({_some(self.words)})"
        if self.kind == "appended":
            return f"remove the sentence added to the card for {_short(self.words[0])}"
        if self.marked:
            return f"unmark {_some(self.words)} as known"
        return f"mark {_some(self.words)} known again"


def _short(text, limit=20):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _some(words, shown=3):
    """"a, b, c" or "a, b, c and 17 more"."""
    words = [_short(w) for w in words]
    head = "、".join(words[:shown])
    return head if len(words) <= shown else f"{head} and {len(words) - shown} more"


class UndoHistory:
    """Newest last, up to `depth` records; the oldest go first."""

    def __init__(self, depth=UNDO_DEPTH):
        self._records = deque(maxlen=depth)
        self._batch = None  # the record the cards of the batch being added go to

    def __bool__(self):
        return bool(self._records)

    def __iter__(self):
        return iter(self._records)

    def start_batch(self):
        """The cards added with batch=True from now on are one record, undone together."""
        self._batch = None

    def added(self, note_id, word, batch=False):
        """A card was added for `word` (its line, for a sentence card)."""
        if batch and self._batch is not None and any(r is self._batch for r in self._records):
            self._batch.data.append(note_id)
            self._batch.words.append(word)
            return
        record = Record("added", [note_id], [word])
        if batch:  # its first card, or its record was undone or dropped meanwhile
            self._batch = record
        self._records.append(record)

    def appended(self, note_id, old_fields, new_fields, media, word):
        """A sentence was added to the note `note_id`: its fields went from `old_fields` to
        `new_fields`, and `media` (file names) went into the collection."""
        self._records.append(Record("appended", (note_id, old_fields, new_fields, media),
                                    [word]))

    def marked(self, before, marked):
        """Words were marked known (or unmarked): `before` is {word: was it marked}."""
        self._records.append(Record("marked", dict(before), list(before), marked))

    def pop(self):
        """The newest record, taken off; None if there is none."""
        return self._records.pop() if self._records else None

    def label(self):
        """What the next Undo would do, or "" if there is nothing to undo."""
        return self._records[-1].label if self._records else ""
