"""A time in a video, as the player and the cards show it: 03:25, or 1:02:05 past an hour."""


def fmt_time(seconds):
    seconds = int(max(seconds, 0))
    h, m, s = seconds // 3600, seconds // 60 % 60, seconds % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
