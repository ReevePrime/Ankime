"""The immersion log's dialog: today against the daily goal, the streak, the last days' chart
and table, the known words over time, and cards per show. The log itself is
core/immersion.py; the known words' history is language/progress.py's."""
from aqt.qt import (QColor, QDialog, QDialogButtonBox, QHeaderView, QLabel, QPainter,
                    QPainterPath, QPen, QPointF, Qt, QTableWidget, QTableWidgetItem,
                    QTabWidget, QToolTip, QVBoxLayout, QWidget)

from ..core.immersion import STREAK_SECONDS, fmt_duration


class WatchChart(QWidget):
    """Minutes watched on each of the last days, one bar per day, with the daily goal as a
    line. The table below it holds the same numbers, for screen readers."""

    def __init__(self, days, goal_minutes):
        super().__init__()
        self._days, self._goal = days, goal_minutes
        self.setMinimumHeight(110)
        watched = sum(1 for _, s in days if s >= STREAK_SECONDS)
        self.setAccessibleName("Minutes watched per day")
        self.setAccessibleDescription(
            f"Bar chart of the last {len(days)} days: you watched on {watched} of them")
        self.setToolTip(f"Minutes watched on each of the last {len(days)} days"
                        + (f"; the line is your goal of {goal_minutes} minutes"
                           if goal_minutes else ""))

    def paintEvent(self, _event):
        p = QPainter(self)
        w, h = self.width(), self.height()
        top = max([s / 60 for _, s in self._days] + [self._goal, 1])
        n = len(self._days)
        bar = w / n
        accent = self.palette().highlight().color()
        for i, (_, seconds) in enumerate(self._days):
            bh = int((h - 4) * (seconds / 60) / top)
            if bh:
                p.fillRect(int(i * bar + 1), h - bh, max(int(bar) - 2, 1), bh, accent)
        if self._goal:
            y = h - int((h - 4) * self._goal / top)
            pen = p.pen()
            pen.setColor(QColor(self.palette().text().color()))
            pen.setWidth(1)
            p.setPen(pen)
            p.drawLine(0, y, w, y)
        p.end()


class KnownWordsChart(QWidget):
    """Words known at the end of each day, as a line from the first day one was to today.
    Hovering shows a day's count; the "Known words" tab holds the same numbers, for screen
    readers."""
    MARGIN = 6  # px around the line: its ends and the hover dot aren't clipped

    def __init__(self, history):
        super().__init__()
        self._history = history  # [("YYYY-MM-DD", known)], oldest first
        self._hover = None       # index of the day under the mouse
        self.setMinimumHeight(110)
        self.setMouseTracking(True)
        first, last = history[0], history[-1]
        self.setAccessibleName("Known words over time")
        self.setAccessibleDescription(
            f"Line chart: {first[1]} words known on {first[0]}, {last[1]} today")
        self.setToolTip("Words known at the end of each day: hover the line for a day's count")

    def _point(self, i):
        m, w, h = self.MARGIN, self.width(), self.height()
        n = len(self._history)
        top = max(max(k for _, k in self._history), 1)
        x = m + (w - 2 * m) * (i / (n - 1) if n > 1 else 1)
        return QPointF(x, h - m - (h - 2 * m) * self._history[i][1] / top)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        accent = self.palette().highlight().color()
        path = QPainterPath(self._point(0))
        for i in range(1, len(self._history)):
            path.lineTo(self._point(i))
        pen = QPen(accent, 2)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.drawPath(path)
        if self._hover is not None:
            at = self._point(self._hover)
            muted = QColor(self.palette().text().color())
            muted.setAlpha(90)
            p.setPen(QPen(muted, 1))
            p.drawLine(QPointF(at.x(), 0), QPointF(at.x(), self.height()))
            p.setPen(QPen(self.palette().base().color(), 2))  # a ring: the dot stands out
            p.setBrush(accent)
            p.drawEllipse(at, 4.5, 4.5)
        p.end()

    def mouseMoveEvent(self, event):
        n = len(self._history)
        qt6 = hasattr(event, "position")
        x = (event.position().x() if qt6 else event.pos().x()) - self.MARGIN
        i = min(max(round(x / max(self.width() - 2 * self.MARGIN, 1) * (n - 1)), 0), n - 1)
        if i != self._hover:
            self._hover = i
            day, known = self._history[i]
            where = event.globalPosition().toPoint() if qt6 else event.globalPos()
            QToolTip.showText(where, f"{day}: {known} known words", self)
            self.update()

    def leaveEvent(self, _event):
        self._hover = None
        QToolTip.hideText()
        self.update()


def known_rows(history):
    """[(day, known, change)] for the days the count changed, newest first."""
    rows, before = [], 0
    for day, known in history:
        if known != before:
            rows.append((day, known, known - before))
        before = known
    return rows[::-1]


def known_summary(history, days=30):
    """"N known words (+k in the last `days` days)", or "" without a history."""
    if not history:
        return ""
    now = history[-1][1]
    then = history[-days - 1][1] if len(history) > days else 0
    return f"Known words: {now} ({now - then:+d} in the last {days} days)."


class LogDialog(QDialog):
    def __init__(self, parent, log, goal_minutes=0, known=None):
        """`known` is the known words' history (progress.known_history), None if it
        couldn't be read."""
        super().__init__(parent)
        self.setWindowTitle("Ankime Immersion Log")
        rows = log.recent(60)
        days = log.days
        total_watch = sum(w for w, _ in days.values())
        total_cards = sum(c for _, c in days.values())
        watched, mined = log.today()
        today = f"Today: {fmt_duration(watched)} watched"
        if goal_minutes:
            done = "goal reached!" if watched >= goal_minutes * 60 else (
                f"{fmt_duration(goal_minutes * 60 - watched)} to go")
            today += f" of your {fmt_duration(goal_minutes * 60)} goal ({done})"
        n = log.streak()
        summary = QLabel(f"{today}, {mined} cards mined.\n"
                         f"Streak: {n} day{'s' if n != 1 else ''} in a row.\n"
                         f"All time: {fmt_duration(total_watch)} watched, {total_cards} cards "
                         f"over {len(days)} days."
                         + (f"\n{known_summary(known)}" if known else ""))
        table = QTableWidget(len(rows), 3)
        table.setHorizontalHeaderLabels(["Day", "Watched", "Cards"])
        table.verticalHeader().hide()
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        for r, (day, watch, cards) in enumerate(rows):
            for c, value in enumerate((day, fmt_duration(watch), str(cards))):
                table.setItem(r, c, QTableWidgetItem(value))
        tabs = QTabWidget()
        tabs.addTab(table, "Days")
        if known:
            tabs.addTab(self._known_tab(known), "Known words")
        tabs.addTab(self._shows_table(log.shows()), "Shows")
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(summary)
        layout.addWidget(WatchChart(log.last_days(), goal_minutes))
        layout.addWidget(tabs, 1)
        layout.addWidget(buttons)
        self.resize(420, 560)

    @staticmethod
    def _known_tab(history):
        """The known words over time: the chart, and the days the count changed."""
        rows = known_rows(history)
        table = QTableWidget(len(rows), 3)
        table.setHorizontalHeaderLabels(["Day", "Known words", "Change"])
        table.verticalHeader().hide()
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        for r, (day, known, change) in enumerate(rows):
            for c, value in enumerate((day, str(known), f"{change:+d}")):
                table.setItem(r, c, QTableWidgetItem(value))
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.addWidget(KnownWordsChart(history))
        layout.addWidget(table, 1)
        tab.setToolTip("Words known by their cards' interval (reviews before today: their "
                       "interval then) or marked known")
        return tab

    @staticmethod
    def _shows_table(shows):
        table = QTableWidget(len(shows), 3)
        table.setHorizontalHeaderLabels(["Show", "Cards", "Last card"])
        table.setToolTip("Cards in your collection tagged with each show (ankime::Show)")
        table.verticalHeader().hide()
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for r, (name, cards, last) in enumerate(shows):
            for c, value in enumerate((name, str(cards), last)):
                table.setItem(r, c, QTableWidgetItem(value))
        return table
