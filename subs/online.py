"""Videos on the web: YouTube and every other site yt-dlp knows. libmpv plays a URL itself,
through yt-dlp, when it can find it on the PATH; what the player needs besides, yt-dlp is
asked for here: the video's title and subtitles, and the media files ffmpeg cuts card media
from. Everything here is slow (a call to yt-dlp, a few seconds): call it in the background."""
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.parse
from pathlib import Path
from typing import NamedTuple

from . import subtitles

YTDLP_NAMES = ("yt-dlp", "youtube-dl")
YTDLP_TIMEOUT = 180  # seconds; a site that doesn't answer (segmented subtitles can be slow)
# Sites' media URLs expire (YouTube's after six hours): asked for again after this long.
STREAMS_MAX_AGE = 3600
# For card media: a picture needs no more than 720p, and the audio is read on its own.
CARD_FORMAT = "bv*[height<=720]+ba/b[height<=720]/bv*+ba/b"
# What mpv plays (its own default takes the largest video, 4K and all).
PLAY_FORMAT = "bv*[height<=1080]+ba/b[height<=1080]/bv*+ba/b"
# TTML first: yt-dlp turns it into SRT itself, and YouTube's automatic captions come out as
# plain lines, where its VTT repeats each line in the next one as they scroll.
SUB_FORMAT = "ttml/srt/vtt/ass/best"

_URL = re.compile(r"https?://\S+$", re.I)


class OnlineError(Exception):
    """yt-dlp is missing, or failed; the message says why."""


class Stream(NamedTuple):
    """A media file on the web for ffmpeg: its URL, the HTTP headers its site wants, and a
    name for the files cut from it."""
    url: str
    headers: dict
    name: str


def is_url(path):
    return isinstance(path, str) and _URL.match(path.strip()) is not None


def ytdlp_path():
    """yt-dlp (or youtube-dl) on the PATH, or None."""
    return next((path for name in YTDLP_NAMES if (path := shutil.which(name))), None)


def display_name(url, title=None):
    """How the playlist shows a URL: its title once known, else the URL without its scheme."""
    if title:
        return title
    parts = urllib.parse.urlsplit(url)
    return parts.netloc + parts.path + (f"?{parts.query}" if parts.query else "")


def file_stem(text):
    """`text` (a title) as a file name: no characters Windows refuses, not too long."""
    stem = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", text).strip(" .")
    return stem[:80] or "video"


def _key(url):
    return hashlib.sha1(url.encode()).hexdigest()[:12]


def _run(args, timeout=YTDLP_TIMEOUT):
    """Run yt-dlp; (exit code, stdout, stderr), as text."""
    ytdlp = ytdlp_path()
    if not ytdlp:
        raise OnlineError("yt-dlp isn't installed (or isn't on the PATH): it is what reads "
                          "videos from YouTube and other sites. Install it from "
                          "github.com/yt-dlp/yt-dlp and restart Anki.")
    flags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
    try:
        done = subprocess.run([ytdlp, "--no-warnings", "--no-playlist", *args],
                              capture_output=True, creationflags=flags, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise OnlineError(f"yt-dlp took more than {timeout} seconds and was stopped") from e
    except OSError as e:
        raise OnlineError(f"Couldn't run yt-dlp: {e}") from e
    return (done.returncode, done.stdout.decode("utf-8", "replace"),
            done.stderr.decode("utf-8", "replace"))


def _failure(stderr):
    lines = [line for line in stderr.strip().splitlines() if line.strip()]
    tail = "\n".join(lines[-2:]) or "no message"
    hint = ("\n\nAn outdated yt-dlp is the usual cause: update it (yt-dlp -U)."
            if "ERROR" in tail else "")
    return OnlineError(f"yt-dlp failed:\n{tail}{hint}")


def _lang_pattern(lang):
    """yt-dlp's --sub-langs for a language: every spelling of it, with a region or variant
    after it (ja, ja-JP, ja-orig)."""
    return ",".join(f"{code}(-.*)?" for code in sorted(subtitles.lang_codes(lang)))


def _ffmpeg_location(ffmpeg):
    """yt-dlp's --ffmpeg-location for the ffmpeg setting: a path; "ffmpeg" alone is on the
    PATH, where yt-dlp finds it anyway."""
    return ["--ffmpeg-location", ffmpeg] if ffmpeg and os.path.dirname(ffmpeg) else []


def _pick(files, lang):
    """The subtitle file of `files` for `lang`: named <key>.<lang>[-variant].<ext> by
    yt-dlp. The language as is (ja) before a variant (ja-orig) and a region (ja-JP)."""
    codes = subtitles.lang_codes(lang)
    found = []
    for f in files:
        tag = f.suffixes[-2][1:].lower() if len(f.suffixes) >= 2 else ""
        if tag.split("-")[0] in codes:
            found.append((tag not in codes, len(tag), f))
    return min(found)[2] if found else None


def fetch_subtitles(url, folder, source_lang, target_lang=None, ffmpeg=None):
    """(title, source file, target file) of the video at `url`: its subtitles in
    `source_lang` (and `target_lang`, if given) are saved in `folder` as SRT, the site's own
    ones, else its automatic captions. A file is None if the site has none in that language;
    the title is None if yt-dlp didn't say. Raises OnlineError if yt-dlp failed and saved
    nothing."""
    folder = Path(folder)
    stem = _key(url)
    for old in folder.glob(f"{stem}.*"):  # the last time this video was opened
        try:
            old.unlink()
        except OSError:
            pass
    langs = ",".join(_lang_pattern(lang) for lang in (source_lang, target_lang) if lang)
    title_file = folder / f"{stem}.title.txt"
    code, _, err = _run(["--skip-download", "--write-subs", "--write-auto-subs",
                         "--sub-langs", langs, "--sub-format", SUB_FORMAT,
                         "--convert-subs", "srt", *_ffmpeg_location(ffmpeg),
                         "--print-to-file", "%(title)s", str(title_file),
                         "-o", str(folder / f"{stem}.%(ext)s"), "--", url])
    # a language that failed (a site refusing too many requests) doesn't lose the others
    files = sorted(f for f in folder.glob(f"{stem}.*")
                   if f.suffix.lower() in subtitles.SUB_EXTS)
    try:
        title = title_file.read_text(encoding="utf-8").strip() or None
    except OSError:
        title = None
    if code and not files and title is None:
        raise _failure(err)
    source = _pick(files, source_lang)
    target = _pick([f for f in files if f != source], target_lang) if target_lang else None
    return title, source, target


_streams = {}  # url -> (time asked, (video Stream, audio Stream))
_streams_lock = threading.Lock()


def _stream(fmt, name):
    return Stream(fmt["url"], dict(fmt.get("http_headers") or {}), name)


def streams(url, title=None):
    """(video, audio): the Streams of the video at `url` to cut card media from, at most
    720p; the same Stream twice where the site has them in one file. Kept for a while: the
    URLs work for some hours, and each card would otherwise wait for yt-dlp."""
    with _streams_lock:
        asked, found = _streams.get(url, (0, None))
        if found and time.time() - asked < STREAMS_MAX_AGE:
            return found
    code, out, err = _run(["-J", "-f", CARD_FORMAT, "--", url])
    try:
        info = json.loads(out)
    except ValueError:
        raise _failure(err) from None
    if code and not isinstance(info, dict):
        raise _failure(err)
    name = file_stem(title or info.get("title") or _key(url))
    formats = info.get("requested_formats") or [info]
    formats = [f for f in formats if f.get("url")]

    def having(kind):
        return next((f for f in formats if (f.get(kind) or "none") != "none"), None)

    video, audio = having("vcodec"), having("acodec")
    if not (video or audio):
        raise OnlineError("yt-dlp found no media file to cut cards from")
    found = (_stream(video or audio, name), _stream(audio or video, name))
    with _streams_lock:
        _streams[url] = (time.time(), found)
    return found


def media_sources(video, title=None):
    """(picture source, audio source) for card media from `video`: a file's path twice, or
    a URL's Streams."""
    return streams(video, title) if is_url(video) else (video, video)
