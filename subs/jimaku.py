"""Jimaku (https://jimaku.cc): searching it for a show's Japanese subtitles and downloading
them. The API needs a key, from the account page on jimaku.cc (the jimaku_api_key setting).
Blocking HTTP: call from a background thread. The rest is pure: the search a video starts
with, its episode number, what of a download is subtitles, and where it is saved."""
import io
import re
import zipfile
from pathlib import Path
from urllib.parse import urlparse

import requests

from . import subtitles

API = "https://jimaku.cc/api"
HOST = "jimaku.cc"  # the key is only ever sent there
USER_AGENT = "Ankime (Anki add-on)"  # Jimaku asks clients to name themselves
TIMEOUT = 20         # seconds
MAX_DOWNLOAD = 50 * 1024 * 1024  # bytes; a season's archive is a few MB
# Unpacked, an archive can be far larger than downloaded (a "zip bomb"): an episode's
# subtitles are well under a megabyte, a season's a few
MAX_SUBTITLE = 20 * 1024 * 1024  # bytes, one file of an archive
MAX_UNPACKED = 200 * 1024 * 1024  # bytes, all of them
ARCHIVE_EXTS = (".zip",)  # .rar and .7z need tools Python doesn't have


class JimakuError(Exception):
    """Jimaku couldn't be reached or refused: the message says what to do."""


# --- the API (background thread) ---------------------------------------------

def _headers(url, key):
    headers = {"User-Agent": USER_AGENT}
    host = urlparse(url).hostname or ""
    if key and (host == HOST or host.endswith("." + HOST)):
        headers["Authorization"] = key
    return headers


def _request(url, key, **kwargs):
    if not key:
        raise JimakuError("No Jimaku API key. Get one from your account page on jimaku.cc "
                          "and enter it in Tools > Ankime > Settings > Player.")
    try:
        r = requests.get(url, headers=_headers(url, key), timeout=TIMEOUT, **kwargs)
    except requests.RequestException as e:
        raise JimakuError(f"Couldn't reach Jimaku: {e}") from e
    if r.status_code == 401:
        raise JimakuError("Jimaku refused the API key: check it in Tools > Ankime > Settings "
                          "> Player.")
    if r.status_code == 429:
        raise JimakuError("Too many requests to Jimaku: try again in a minute.")
    if not r.ok:
        try:
            message = r.json().get("error") or r.reason
        except ValueError:
            message = r.reason
        raise JimakuError(f"Jimaku: {message} ({r.status_code})")
    return r


def search(query, key):
    """[entry] of the anime matching `query`: dicts with "id", "name" (romaji),
    "english_name", "japanese_name"..."""
    return _request(f"{API}/entries/search", key,
                    params={"query": query, "anime": "true"}).json()


def files(entry_id, key, episode=None):
    """[file] of an entry, its subtitles and archives only: dicts with "name", "url",
    "size". With `episode`, the files Jimaku guesses are of that episode."""
    params = {"episode": episode} if episode else {}
    found = _request(f"{API}/entries/{int(entry_id)}/files", key, params=params).json()
    return [f for f in found if is_wanted(f.get("name", ""))]


def download(url, key):
    """The file's bytes. Raises JimakuError past MAX_DOWNLOAD."""
    r = _request(url, key, stream=True)
    data = bytearray()
    for chunk in r.iter_content(64 * 1024):
        data += chunk
        if len(data) > MAX_DOWNLOAD:
            r.close()
            raise JimakuError(f"That file is larger than {MAX_DOWNLOAD // 2**20} MB.")
    return bytes(data)


# --- files ---------------------------------------------------------------

def is_wanted(name):
    return name.lower().endswith(subtitles.SUB_EXTS + ARCHIVE_EXTS)


def subtitle_files(name, data):
    """[(name, bytes)] of the subtitles in a download: the file itself, or those in a zip
    archive. Names are the files' own, without the folders of the archive. Raises
    JimakuError for an archive that would unpack past MAX_SUBTITLE / MAX_UNPACKED."""
    if not name.lower().endswith(ARCHIVE_EXTS):
        return [(Path(name).name, data)]
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            wanted = [info for info in z.infolist()
                      if not info.is_dir() and info.filename.lower().endswith(subtitles.SUB_EXTS)
                      and not Path(info.filename).name.startswith(".")]  # macOS' ._ files
            if sum(info.file_size for info in wanted) > MAX_UNPACKED:
                raise JimakuError(f"{name} would unpack to more than "
                                  f"{MAX_UNPACKED // 2**20} MB of subtitles: not unpacked.")
            return [(Path(info.filename).name, _read_member(z, info)) for info in wanted]
    except zipfile.BadZipFile as e:
        raise JimakuError(f"{name} isn't a zip archive that can be read: {e}") from e


def _read_member(z, info):
    """A file of the archive, read no further than MAX_SUBTITLE whatever its header says
    (the sizes in a zip's directory are the archive's word, not a promise)."""
    too_big = JimakuError(f"{Path(info.filename).name} is larger than "
                          f"{MAX_SUBTITLE // 2**20} MB: not a subtitle file.")
    if info.file_size > MAX_SUBTITLE:
        raise too_big
    with z.open(info) as f:
        data = f.read(MAX_SUBTITLE + 1)
    if len(data) > MAX_SUBTITLE:
        raise too_big
    return data


# [Group], [1080p], (BD 1080p x265): never the episode
_BRACKETS = re.compile(r"\[[^\]]*\]|\([^)]*\)|【[^】]*】")
_EPISODE = [
    re.compile(r"\bS\d{1,2}\s*E(\d{1,4})", re.I),                    # S01E03
    re.compile(r"(?:\b(?:ep|episode|e)\s*\.?\s*|第\s*)(\d{1,4})", re.I),  # ep03, E03, 第3話
    re.compile(r"(?:^|\s)-\s*(\d{1,4})(?:v\d)?(?:\s|$)"),           # Show - 03, Show - 03v2
    re.compile(r"(\d{1,4})\s*話"),                                   # 3話
]
# a lone number, not part of "1080p", "x264", "S2", "2nd" or a year
_LONE_NUMBER = re.compile(r"(?<![\w.])(\d{1,3})(?:v\d)?(?![\w])")


def episode_number(name):
    """The episode number in a video's (or subtitle file's) name, or None:
    "[Group] Show - 03 [1080p].mkv", "Show.S01E03.mkv", "Show 第3話.mp4", "03.mkv"."""
    stem = _BRACKETS.sub(" ", Path(name).stem).replace("_", " ")
    for pattern in _EPISODE:
        m = pattern.search(stem)
        if m:
            return int(m.group(1))
    numbers = _LONE_NUMBER.findall(stem.replace(".", " "))
    return int(numbers[-1]) if numbers else None


_NOISE = re.compile(r"\b(season\s*\d+|s\d{1,2}|\d{3,4}p|bd|bluray|web(-?dl)?|hevc|x26[45]|"
                    r"flac|aac|dual[\s-]?audio|batch|complete)\b", re.I)


def search_query(video):
    """What to search Jimaku for: the show's folder name, cleaned of release tags
    ("[Group] Frieren S01 [1080p]" -> "Frieren"). "Show/Season 1" searches for "Show"."""
    show = subtitles.delay_key(Path(video).parent).split("/")[0]
    text = _NOISE.sub(" ", _BRACKETS.sub(" ", show).replace(".", " ").replace("_", " "))
    return " ".join(text.split()) or show


def best_match(names, episode):
    """Index of the file of `names` for `episode` (by the number in its name), else of the
    only file, else None: then the user chooses."""
    if len(names) == 1:
        return 0
    matches = [i for i, name in enumerate(names) if episode_number(name) == episode]
    return matches[0] if episode is not None and len(matches) == 1 else None


def save_path(video, name, lang):
    """Where a downloaded subtitle file goes: next to the video, named after it with the
    language tag (`ep01.ja.srt`), so it is found by name like any other. A file already
    there isn't overwritten: `ep01.ja.jimaku.srt`, then `ep01.ja.jimaku2.srt`..."""
    video = Path(video)
    ext = Path(name).suffix.lower()
    path = video.with_name(f"{video.stem}.{lang}{ext}")
    n = 1
    while path.exists():
        path = video.with_name(f"{video.stem}.{lang}.jimaku{n if n > 1 else ''}{ext}")
        n += 1
    return path
