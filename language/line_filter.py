"""The i+1 rules: which lines the line filter lets play, and when the player pauses by itself
on an i+1 line. Pure logic: the player window acts on what these return."""

# (config value, label)
LINE_FILTERS = [("off", "Play every line"),
                ("skip_known", "Skip lines I fully know"),
                ("only_i1", "Only play i+1 lines"),
                ("i1_jump", "Jump from i+1 line to i+1 line, then the next episode")]
I1_PAUSE = [("off", "Don't pause"),
            ("before", "Pause before an i+1 line's audio"),
            ("after", "Pause after an i+1 line's audio"),
            ("every", "Pause after every line")]

# Which lines get a card when extracting a whole episode: (mode, label)
EXTRACTIONS = [("all", "Every line"),
               ("i1", "i+1 lines"),
               ("new", "Lines with a new word")]

ARRIVAL = 0.6       # seconds: this close to its start, playback arrived at the line
PAUSE_EARLY = 0.05   # seconds: pause this much before the line's end, ticks are 0.1 s apart
JUMP_LEAD = 0.3      # seconds: "i1_jump" plays this much before an i+1 line...
JUMP_TAIL = 0.4      # ...and this much after it, so its audio isn't cut off


def wanted(kind, line_filter):
    """Does the line filter let a line of this kind ('known' | 'i+1' | 'harder' | None)
    play?"""
    if line_filter == "skip_known":
        return kind != "known"
    if line_filter in ("only_i1", "i1_jump"):
        return kind == "i+1"
    return True


def jump_target(spans, now):
    """For the "i1_jump" filter, on every tick while playing: `spans` are the (start, end) of
    the episode's i+1 lines, in video time and in order. None while playback is in one of
    them (or just around it), else where to go: the start of the next one, or "end" when
    there is none left (the next episode)."""
    for start, end in spans:
        if start - JUMP_LEAD <= now <= end + JUMP_TAIL:
            return None
        if start > now:
            return start
    return "end"


def extracted(kind, mode):
    """Does extracting the episode with `mode` (see EXTRACTIONS) give a line of this kind a
    card? "new": one new word or more (i+1 or harder)."""
    if mode == "i1":
        return kind == "i+1"
    if mode == "new":
        return kind in ("i+1", "harder")
    return True


def on_line_shown(kind, line_filter, paused):
    """What to do once a line's words are on the panel: "play" it (and arm the i+1 pause),
    "skip" to the next wanted line, or "stay" (an unwanted line seen while paused: you are
    browsing, leave it alone)."""
    if wanted(kind, line_filter):
        return "play"
    return "stay" if paused else "skip"


def pause_plan(kind, mode, start, end, now, paused, replay=False):
    """The automatic pause for a line: ("at", t) to pause once playback reaches t (after
    its audio), ("now", start) to pause at once and go back to its start (before its
    audio, so you can read it first), or None. "every" pauses after any line, whatever its
    kind (None too: not analysed yet); the other modes are for i+1 lines only. "before" only
    when playback arrived at the line, not when scrubbed into it, and not on a replay."""
    if mode == "every":
        return ("at", end) if end is not None else None
    if kind != "i+1" or mode == "off":
        return None
    if mode == "after" and end is not None:
        return "at", end
    if (mode == "before" and not replay and not paused and start is not None
            and now is not None and now - start < ARRIVAL):
        return "now", start
    return None


def reached(pause_at, now):
    """Has playback reached a pause set with ("at", pause_at)?"""
    return pause_at is not None and now is not None and now >= pause_at - PAUSE_EARLY


def next_filter(current):
    """The line filter after `current`, round the list."""
    values = [value for value, _ in LINE_FILTERS]
    if current not in values:
        return values[0]
    return values[(values.index(current) + 1) % len(values)]
