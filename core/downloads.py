"""Downloading what Ankime needs and doesn't bring: libmpv and ffmpeg on Windows (the
setup's most common stumbling block), and JMdict for Ankime's own dictionaries. Blocking:
call from a background thread.

Programs are pinned: one build each, by URL and SHA-256, checked before anything is
unpacked, so a compromised mirror or a man in the middle can't swap them (the hashes were
checked against the ones SourceForge and gyan.dev publish). A newer build means a new
version of Ankime. JMdict changes daily and its releases are kept only a few months, so
it is the latest release, checked against the SHA-256 GitHub gives for it; it is data, read
by the importer like any dictionary (local_dict.py)."""
import hashlib
import os
import platform
import shutil
import subprocess
import sys
import uuid
import zipfile
from pathlib import Path
from typing import NamedTuple

import requests

from .store import USER_FILES

TOOLS_DIR = USER_FILES / "tools"
TIMEOUT = 30           # seconds without a byte before giving up
CHUNK = 256 * 1024     # bytes
USER_AGENT = "Ankime (Anki add-on)"


class DownloadError(Exception):
    """A download or its unpacking failed: the message says why."""


class Cancelled(Exception):
    pass


class Tool(NamedTuple):
    name: str
    version: str
    source: str     # where it comes from, for the confirmation
    url: str
    sha256: str
    size: int       # bytes, of the download
    archive: str    # "7z" or "zip"
    member: str     # the file of the archive that is needed
    folder: str     # under TOOLS_DIR


TOOLS = {
    "libmpv": Tool(
        "libmpv", "2026-10-11 (shinchiro's build)", "SourceForge (mpv-player-windows)",
        "https://downloads.sourceforge.net/project/mpv-player-windows/libmpv/"
        "mpv-dev-x86_64-20261011-git-accb7b2821.7z",
        "cf63b4ce66e03e5e70acf2648cbc3177656c77c5b9583e16d83efc58801182b7",
        31547972, "7z", "libmpv-2.dll", "libmpv"),
    "ffmpeg": Tool(
        "ffmpeg", "9.0.2 essentials (Gyan Doshi's build)", "GitHub (GyanD/codexffmpeg)",
        "https://github.com/GyanD/codexffmpeg/releases/download/9.0.2/"
        "ffmpeg-9.0.2-essentials_build.zip",
        "60f467265b1e312373dbcd92200c2618a74850f98d3d078e94296bb3fa2047ba",
        114768076, "zip", "ffmpeg-9.0.2-essentials_build/bin/ffmpeg.exe", "ffmpeg"),
}

JMDICT_RELEASE = "https://api.github.com/repos/yomidevs/jmdict-yomitan/releases/latest"
JMDICT_ASSET = "JMdict_english.zip"
JMDICT_URLS = "https://github.com/yomidevs/jmdict-yomitan/releases/download/"
MAX_DICTIONARY = 200 * 1024 * 1024  # bytes; JMdict is about 16 MB


def can_install():
    """Can the tools be downloaded for this Anki? Windows on x86-64 (also emulated on ARM),
    which is what the builds are for. Elsewhere the package manager has them."""
    return (sys.platform == "win32" and sys.maxsize > 2 ** 32
            and platform.machine().lower() in ("amd64", "x86_64"))


def fetch(url, dest, sha256, max_bytes, progress=None, cancelled=None, session=None):
    """Download `url` to `dest`, which only appears once complete and with the SHA-256
    `sha256`. Raises DownloadError past `max_bytes` or for another checksum, Cancelled
    when `cancelled()`. `progress(bytes done, bytes expected)`."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(f"{dest.name}.{uuid.uuid4().hex[:8]}.part")
    digest, done = hashlib.sha256(), 0
    try:
        try:
            r = (session or requests).get(url, stream=True, timeout=TIMEOUT,
                                          headers={"User-Agent": USER_AGENT})
            r.raise_for_status()
            with open(part, "wb") as f:
                for chunk in r.iter_content(CHUNK):
                    if cancelled and cancelled():
                        raise Cancelled()
                    done += len(chunk)
                    if done > max_bytes:
                        raise DownloadError(f"{url} is larger than expected "
                                            f"({max_bytes // 2**20} MB): not used.")
                    digest.update(chunk)
                    f.write(chunk)
                    if progress:
                        progress(done, max_bytes)
        except requests.RequestException as e:
            raise DownloadError(f"Couldn't download {url}: {e}") from e
        if digest.hexdigest() != sha256.lower():
            raise DownloadError(f"{url} isn't the file expected (its SHA-256 checksum "
                                "differs): not used. Try again later, or download it by "
                                "hand.")
        os.replace(part, dest)
    finally:
        if part.exists():
            part.unlink()
    return dest


def _system_tar():
    """Windows' own tar (bsdtar, since Windows 10 1803), which reads .7z: by its full path,
    never one found on the PATH."""
    tar = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "tar.exe"
    if not tar.is_file():
        raise DownloadError("Unpacking libmpv needs Windows' tar.exe (Windows 10 1803 or "
                            "later). Download and unpack libmpv by hand instead.")
    return tar


def _extract(tool, archive, out_dir):
    """The tool's file out of its archive into `out_dir`. Only that file is unpacked, by
    its name, so nothing can land outside `out_dir`."""
    out = out_dir / Path(tool.member).name
    if tool.archive == "zip":
        try:
            with zipfile.ZipFile(archive) as z, z.open(tool.member) as src, \
                    open(out, "wb") as dst:
                shutil.copyfileobj(src, dst, CHUNK)
        except (KeyError, zipfile.BadZipFile) as e:
            raise DownloadError(f"{tool.member} isn't in the archive: {e}") from e
    else:
        flags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
        try:
            subprocess.run([str(_system_tar()), "-xf", str(archive), "-C", str(out_dir),
                            tool.member], check=True, capture_output=True, timeout=600,
                           creationflags=flags)
        except (OSError, subprocess.SubprocessError) as e:
            raise DownloadError(f"Couldn't unpack {archive.name}: {e}") from e
    if not out.is_file():
        raise DownloadError(f"{tool.member} isn't in the archive")
    return out


def install(name, progress=None, cancelled=None, session=None):
    """Download and unpack tool `name` (of TOOLS) under TOOLS_DIR. Returns what its setting
    takes: the folder holding libmpv-2.dll, the path of ffmpeg.exe."""
    tool = TOOLS[name]
    folder = TOOLS_DIR / tool.folder
    staging = folder / f"new-{uuid.uuid4().hex[:8]}"
    staging.mkdir(parents=True)
    archive = staging / f"download.{tool.archive}"
    try:
        fetch(tool.url, archive, tool.sha256, tool.size, progress, cancelled, session)
        unpacked = _extract(tool, archive, staging)
        target = folder / unpacked.name
        try:
            os.replace(unpacked, target)
        except PermissionError as e:
            raise DownloadError(f"{target} is in use: close the player and try again.") from e
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return folder if name == "libmpv" else target


def latest_jmdict(session=None):
    """(url, sha256, size) of the latest JMdict (English) for Yomitan."""
    try:
        r = (session or requests).get(JMDICT_RELEASE, timeout=TIMEOUT,
                                      headers={"User-Agent": USER_AGENT,
                                               "Accept": "application/vnd.github+json"})
        r.raise_for_status()
        assets = r.json().get("assets") or []
    except (requests.RequestException, ValueError, AttributeError) as e:
        raise DownloadError(f"Couldn't find the latest JMdict: {e}") from e
    for asset in assets:
        if not isinstance(asset, dict) or asset.get("name") != JMDICT_ASSET:
            continue
        url, digest = str(asset.get("browser_download_url")), str(asset.get("digest") or "")
        size = asset.get("size")
        if (url.startswith(JMDICT_URLS) and digest.startswith("sha256:")
                and isinstance(size, int) and 0 < size <= MAX_DICTIONARY):
            return url, digest[len("sha256:"):], size
        break
    raise DownloadError("The latest JMdict release doesn't have the file expected: download "
                        "it from github.com/yomidevs/jmdict-yomitan and import it.")


def download_jmdict(dest, progress=None, cancelled=None, session=None):
    url, sha256, size = latest_jmdict(session)
    return fetch(url, dest, sha256, size, progress, cancelled, session)
