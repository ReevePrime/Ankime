"""Aligning a subtitle file to a video's audio with alass or ffsubsync, installed separately
(the sub_sync_tool setting). They listen to the whole episode, so it takes a while: call from
a background thread."""
import os
import subprocess
from pathlib import Path

from ..core import config

TIMEOUT = 600  # seconds; an episode takes alass a few seconds, ffsubsync up to a minute


class RetimeError(Exception):
    """The aligner is missing or failed; the message ends with the last lines it printed."""


def tool_kind(path):
    """"alass" or "ffsubsync" by the program's name (alass-cli.exe, ffsubsync...), or None."""
    name = Path(path).name.lower()
    if "alass" in name:
        return "alass"
    if "subsync" in name:
        return "ffsubsync"
    return None


def command(tool, video, sub, out):
    if tool_kind(tool) == "alass":
        return [tool, str(video), str(sub), str(out)]
    return [tool, str(video), "-i", str(sub), "-o", str(out)]


def _env(ffmpeg):
    """Both find ffmpeg on PATH; alass also by ALASS_FFMPEG_PATH. The ffmpeg of the
    settings goes first."""
    env = dict(os.environ)
    if ffmpeg and Path(ffmpeg).is_file():
        folder = str(Path(ffmpeg).parent)
        env["PATH"] = folder + os.pathsep + env.get("PATH", "")
        env["ALASS_FFMPEG_PATH"] = ffmpeg
        probe = Path(ffmpeg).with_name(Path(ffmpeg).name.replace("ffmpeg", "ffprobe"))
        if probe.is_file():
            env["ALASS_FFPROBE_PATH"] = str(probe)
    return env


def align(video, sub, out, tool=None):
    """Write `sub` shifted to match the speech in `video` to `out` (same format). Returns
    `out`."""
    cfg = config.get()
    tool = tool or cfg["sub_sync_tool"]
    if not tool:
        raise RetimeError("No subtitle aligner set: install alass or ffsubsync and set it in "
                          "Tools > Ankime > Settings > Player.")
    if tool_kind(tool) is None:
        raise RetimeError(f"{Path(tool).name} isn't alass or ffsubsync.")
    flags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
    try:
        subprocess.run(command(tool, video, sub, out), check=True, capture_output=True,
                       creationflags=flags, timeout=TIMEOUT, env=_env(cfg["ffmpeg_path"]))
    except FileNotFoundError as e:
        raise RetimeError(f"{tool} not found: check the subtitle aligner in Tools > Ankime > "
                          "Settings > Player.") from e
    except subprocess.TimeoutExpired as e:
        raise RetimeError(f"{Path(tool).name} took more than {TIMEOUT} seconds and was "
                          "stopped.") from e
    except subprocess.CalledProcessError as e:
        output = (e.stderr or e.stdout or b"").decode("utf-8", "replace").strip()
        tail = "\n".join(output.splitlines()[-3:])
        raise RetimeError(f"{Path(tool).name} failed:\n{tail}") from e
    if not Path(out).is_file():
        raise RetimeError(f"{Path(tool).name} wrote no file.")
    return Path(out)


def aligned_path(sub, video, lang):
    """Where the aligned copy of `sub` goes: next to the video, `ep01.ja.synced.srt` (a
    subtitle file extracted from the video lives in a temp folder). Never one that exists:
    the aligner would read and write the same file."""
    video, ext = Path(video), Path(sub).suffix.lower()
    path = video.with_name(f"{video.stem}.{lang}.synced{ext}")
    n = 2
    while path.exists():
        path = video.with_name(f"{video.stem}.{lang}.synced{n}{ext}")
        n += 1
    return path
