"""Immersion log: time watched and cards mined per day, synced with the collection. Each
computer keeps its own totals (store.device_entries), and the log shows their sum, so
watching on two computers between syncs counts both. Cards per show are counted from the
ankime::Show tags instead, so they cover every card the collection still has. The
dialog showing the log is dialogs/immersion.py."""
import datetime

from .store import SyncedValue, device_entries, device_id, main_col

LOG_KEY = "ankime_immersion"   # one entry for every computer, before each kept its own
DEFAULT_ROLLOVER = 4           # hour Anki's next day starts at, unless set otherwise
LOG_PREFIX = "ankime_log"
STREAK_SECONDS = 60            # a day counts towards the streak from a minute watched
CHART_DAYS = 30
SHOW_TAG = "ankime::"          # notes.show_tag: ankime::Show, ankime::Show::Season_1


def fmt_duration(seconds):
    minutes = int(seconds) // 60
    return f"{minutes // 60}h {minutes % 60:02d}m" if minutes >= 60 else f"{minutes}m"


def study_day(now, rollover=DEFAULT_ROLLOVER):
    """"YYYY-MM-DD" of the day `now` (a datetime) belongs to the way Anki counts days: they
    start at `rollover` o'clock, so watching at 1 am counts for the evening before, like the
    reviews done then."""
    return (now - datetime.timedelta(hours=rollover)).date().isoformat()


def _rollover(col):
    """The hour the collection's next day starts at (Preferences > Review)."""
    try:
        hour = (col if col is not None else main_col()).get_config("rollover", DEFAULT_ROLLOVER)
        return int(hour) if 0 <= int(hour) <= 23 else DEFAULT_ROLLOVER
    except Exception:
        return DEFAULT_ROLLOVER


def _days(data):
    """{"YYYY-MM-DD": [seconds, cards]} from a stored value, dropping malformed entries."""
    if not isinstance(data, dict):
        return {}
    days = {}
    for d, v in data.items():
        try:
            days[d] = [float(v[0]), int(v[1])]
        except (TypeError, ValueError, IndexError, KeyError):
            pass
    return days


def streak(days, today):
    """Days in a row, ending today (or yesterday: today isn't over), with at least
    STREAK_SECONDS watched or a card mined. `days` is {"YYYY-MM-DD": (seconds, cards)}."""
    def active(d):
        watched, cards = days.get(d.isoformat(), (0, 0))
        return watched >= STREAK_SECONDS or cards > 0

    day = datetime.date.fromisoformat(today)
    if not active(day):
        day -= datetime.timedelta(days=1)
    n = 0
    while active(day):
        n += 1
        day -= datetime.timedelta(days=1)
    return n


def show_stats(rows, rollover=DEFAULT_ROLLOVER):
    """[(show, cards, last day mined)] from (note id, tags) rows, most cards first. Seasons
    count for their show; a note id is the time it was added, in milliseconds. Anki's tags
    ignore case, so "Show" and "show" are one show, shown as first spelled."""
    shows = {}
    for nid, tags in rows:
        names = {t.split("::")[1] for t in tags.split() if t.lower().startswith(SHOW_TAG)}
        for name in names - {""}:
            entry = shows.setdefault(name.lower(), [name.replace("_", " "), 0, 0])
            entry[1] += 1
            entry[2] = max(entry[2], nid)
    return sorted(((name, n, study_day(datetime.datetime.fromtimestamp(last / 1000), rollover))
                   for name, n, last in shows.values()), key=lambda s: (-s[1], s[0].lower()))


class ImmersionLog:
    """{"YYYY-MM-DD": [seconds watched, cards mined]}. Watch time piles up in memory and is
    written by flush(), so playback doesn't touch the collection every few seconds. Only
    this computer's totals are written; `days` adds the other computers' to them."""

    def __init__(self, col=None, today=None, device=None):
        self._col, self._device = col, device or device_id()
        self._store = SyncedValue(f"{LOG_PREFIX}::{self._device}", col)
        self._today = today or (lambda: study_day(datetime.datetime.now(), _rollover(col)))
        self._own = _days(self._store.load({}))
        self._others = {}  # the other computers' and the old shared entry's, summed
        self.reload_others()

    def reload_others(self):
        """Read the other computers' totals again: a sync may have brought newer ones."""
        self._others = {}
        sources = [SyncedValue(LOG_KEY, self._col).load({})]
        sources += [v for d, v in device_entries(LOG_PREFIX, self._col).items()
                    if d != self._device]
        for source in sources:
            for d, (w, c) in _days(source).items():
                total = self._others.setdefault(d, [0.0, 0])
                total[0] += w
                total[1] += c

    @property
    def days(self):
        merged = {d: list(v) for d, v in self._others.items()}
        for d, (w, c) in self._own.items():
            total = merged.setdefault(d, [0.0, 0])
            total[0] += w
            total[1] += c
        return merged

    def _day(self, day=None):
        return self._own.setdefault(day or self._today(), [0.0, 0])

    def add_watch(self, seconds):
        self._day()[0] += seconds

    def add_cards(self, n=1, day=None):
        """Count `n` cards (fewer, if negative: undone) on `day` ("YYYY-MM-DD", default
        today). Returns the day they counted for, so an undo can take them off that day."""
        day = day or self._today()
        totals = self._day(day)
        totals[1] = max(0, totals[1] + n)
        return day

    def flush(self):
        self._store.save({d: [round(w), c] for d, (w, c) in self._own.items()})

    def today(self):
        return tuple(self.days.get(self._today(), (0.0, 0)))

    def day(self):
        """Today's study day, "YYYY-MM-DD"."""
        return self._today()

    def rollover(self):
        """The hour the next study day starts at."""
        return _rollover(self._col)

    def streak(self):
        return streak(self.days, self._today())

    def recent(self, n=30):
        """[(day, seconds, cards)] for the last `n` days that have entries, newest first."""
        days = self.days
        return [(d, *days[d]) for d in sorted(days, reverse=True)[:n]]

    def shows(self):
        """show_stats for the collection's notes; a note mined for two shows counts for both."""
        col = self._col if self._col is not None else main_col()
        rows = col.db.all("select id, tags from notes where tags like ?", f"%{SHOW_TAG}%")
        return show_stats(rows, _rollover(col))

    def last_days(self, n=CHART_DAYS):
        """[(day, seconds)] for each of the last `n` days, oldest first, 0 for days off."""
        today = datetime.date.fromisoformat(self._today())
        days = self.days
        out = []
        for i in range(n - 1, -1, -1):
            d = (today - datetime.timedelta(days=i)).isoformat()
            out.append((d, days.get(d, (0.0, 0))[0]))
        return out
