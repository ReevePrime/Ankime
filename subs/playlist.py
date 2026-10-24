"""The playlist: videos played one after the other, and where you stopped in each."""
import os
import re
from pathlib import Path

from . import online

VIDEO_SUFFIXES = {".mkv", ".mp4", ".avi", ".webm", ".mov"}


def natural_key(path):
    """Sort 'ep2' before 'ep10'."""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", Path(path).name)]


def videos_in(folder):
    return sorted((str(p) for p in Path(folder).iterdir() if p.suffix.lower() in VIDEO_SUFFIXES),
                  key=natural_key)


class Playlist:
    def __init__(self):
        self.paths = []      # video paths
        self.index = -1      # position of the video playing (-1 = none yet)
        self.positions = {}  # video path -> seconds watched, to resume where you stopped
        self.subtitles = {}  # video path -> subtitle file chosen by hand for it
        self.titles = {}     # URL of a video on the web -> its title, once yt-dlp said

    @property
    def current(self):
        return self.paths[self.index] if 0 <= self.index < len(self.paths) else None

    def to_json(self):
        return {"paths": self.paths, "index": self.index,
                "positions": {v: t for v, t in self.positions.items() if v in self.paths},
                "subtitles": {v: s for v, s in self.subtitles.items() if v in self.paths},
                "titles": {v: t for v, t in self.titles.items() if v in self.paths}}

    def restore(self, data, exists=os.path.exists):
        """Load a saved playlist, dropping videos that have been moved or deleted while
        keeping the index on the same video (or the one that took its place). Videos on the
        web are kept."""
        try:
            saved, index = list(data["paths"]), int(data["index"])
            positions = dict(data.get("positions", {}))
            subs = dict(data.get("subtitles", {}))
            titles = dict(data.get("titles", {}))
        except (KeyError, TypeError, ValueError, AttributeError):
            return False
        kept = [p for p in saved if isinstance(p, str) and (online.is_url(p) or exists(p))]
        gone_before = sum(1 for p in saved[:max(index, 0)] if p not in kept)
        self.paths = kept
        self.index = min(max(index - gone_before, -1), len(kept) - 1)
        self.positions = {v: t for v, t in positions.items()
                          if v in kept and isinstance(t, (int, float))}
        self.subtitles = {v: s for v, s in subs.items() if v in kept and isinstance(s, str)}
        self.titles = {v: t for v, t in titles.items() if v in kept and isinstance(t, str)}
        return True

    def replace(self, paths):
        self.paths, self.index = list(paths), -1

    def append(self, paths):
        """True if the playlist was empty before."""
        was_empty = not self.paths
        self.paths.extend(paths)
        return was_empty

    def move(self, rows, delta):
        """Move the rows one step up (delta -1) or down (+1), as a block where they touch.
        Returns the rows' new positions."""
        selected = set(rows)
        for row in (sorted(rows) if delta < 0 else sorted(rows, reverse=True)):
            other = row + delta
            if not 0 <= other < len(self.paths) or other in selected:
                continue  # at the edge, or blocked by another selected row
            self.paths[row], self.paths[other] = self.paths[other], self.paths[row]
            if self.index == row:
                self.index = other
            elif self.index == other:
                self.index = row
            selected.discard(row)
            selected.add(other)
        return selected

    def sort(self, key):
        """Put the videos in order of key(path), those with the same key as they were; the
        video playing stays the one playing."""
        current = self.current
        self.paths.sort(key=key)
        if current is not None:
            self.index = self.paths.index(current)

    def remove(self, rows):
        for row in sorted(set(rows), reverse=True):
            del self.paths[row]
            if row <= self.index:
                self.index -= 1  # the video playing now stays; "next" still follows it

    def name(self, path):
        """How the playlist shows a video: a file's name, a web video's title (or URL)."""
        return (online.display_name(path, self.titles.get(path)) if online.is_url(path)
                else Path(path).name)

    def clear(self):
        self.paths, self.index = [], -1

    def remember(self, path, t, duration):
        """Note how far into `path` we are; forget it at the very start or once finished."""
        if t < 5 or (duration and t > duration - 10):
            self.positions.pop(path, None)
        else:
            self.positions[path] = round(t, 1)
