"""Cutting card media and subtitle tracks out of videos with ffmpeg. Everything slow here runs
in a background thread: never touch mw.col in this module (notes.py does that)."""
import hashlib
import json
import logging
import os
import re
import stat
import subprocess
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..core import config
from ..core.model import SetupError

log = logging.getLogger(__name__)

# Per user where the temp folder is shared (/tmp on Linux): another user could otherwise
# create the folder first and plant symlinks where ffmpeg writes.
TEMP = Path(tempfile.gettempdir()) / (
    f"ankime-{os.getuid()}" if hasattr(os, "getuid") else "ankime")
OLD_TEMP = Path(tempfile.gettempdir()) / "anime_player"  # the folder's name before 1.0
SHARED_TEMP = Path(tempfile.gettempdir()) / "ankime"  # before it was per user (= TEMP on Windows)
TEMP_MAX_AGE = 24 * 3600  # seconds; leftovers from crashed sessions are swept after this

IMAGE_FORMATS = [("webp", "WebP (small)"), ("jpg", "JPEG")]
# An animated clip of the line in place of the still picture (the still one if it fails).
ANIMATED_FORMATS = [("off", "Off (still picture)"), ("webp", "Animated WebP"),
                    ("avif", "Animated AVIF (smaller, slower to make)")]
ANIMATED_FPS = 10
ANIMATED_MAX_SECONDS = 8  # a longer card is cut around its picture
AUDIO_FORMATS = [("m4a", "M4A (AAC)"), ("mp3", "MP3"), ("opus", "Opus (smallest)")]
AUDIO_CODECS = {"m4a": ["-c:a", "aac", "-b:a", "128k"],
                "mp3": ["-c:a", "libmp3lame", "-q:a", "4"],
                "opus": ["-c:a", "libopus", "-b:a", "64k"]}
# EBU R128 loudness: every card plays at the same volume, whatever the show's mix. The clip
# is measured first and then gets one fixed gain: loudnorm's own single pass adjusts as it
# goes, which makes clips of a few seconds pump.
TARGET_LUFS = -16.0
TARGET_PEAK = -1.5  # dBTP
MAX_BOOST = 20.0  # dB: a near-silent clip isn't raised to a roar of background noise
MEASURE = f"loudnorm=I={TARGET_LUFS:g}:TP={TARGET_PEAK:g}:LRA=11:print_format=json"
# Seconds ffmpeg gets before it is stopped (a video on a network drive that went to sleep):
# cutting a line's media is quick, extracting a subtitle track reads the whole file.
FFMPEG_TIMEOUT = 60
EXTRACT_TIMEOUT = 300
# Condensed audio: lines less than this many seconds apart are kept with the pause between
# them, rather than cut together.
CONDENSED_GAP = 1.0
MAX_INLINE_FILTER = 8000  # characters; a longer filter goes through a file


class FfmpegError(Exception):
    """ffmpeg ran but failed; the message ends with the last lines it printed."""


# --- temp files ----------------------------------------------------------------

def _is_private(folder):
    """Is `folder` a real folder (not a symlink) that is ours and that only we can write
    to? Always, where there are no Unix users."""
    if not hasattr(os, "getuid"):
        return True
    st = os.lstat(folder)
    return (stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid()
            and not st.st_mode & (stat.S_IWGRP | stat.S_IWOTH))


def _temp():
    """The scratch folder, created on first use."""
    try:
        TEMP.mkdir(mode=0o700, exist_ok=True)
    except FileExistsError:
        pass  # a file or symlink in the way: refused below
    if not _is_private(TEMP):
        raise SetupError(f"Unsafe temp folder {TEMP}: it isn't a folder only you can write "
                        "to. Delete it and try again.")
    return TEMP


def scratch_dir():
    """The scratch folder, for files that name themselves (yt-dlp's)."""
    return _temp()


def scratch_file(name):
    """A new path in the scratch folder for a file called `name` (only its last part is
    used: it may come from elsewhere)."""
    safe = Path(str(name).replace("\\", "/")).name or "file"
    return _temp() / f"{uuid.uuid4().hex[:8]}_{safe}"


def discard(*paths):
    """Delete scratch files; Anki has its own copy once they were added to the media folder."""
    for path in paths:
        try:
            Path(path).unlink()
        except OSError:
            pass


def clean_temp():
    """Remove scratch files that an earlier session left behind, and the folders older
    versions used once they are empty. Never follows a symlink, and in a folder that isn't
    private (the shared one older versions used on Linux) only touches our own files."""
    cutoff = time.time() - TEMP_MAX_AGE
    old_folders = [f for f in (OLD_TEMP, SHARED_TEMP) if f != TEMP]
    for folder in (TEMP, *old_folders):
        try:
            if os.path.islink(folder):
                continue
            private = _is_private(folder)
            files = list(folder.iterdir())
        except OSError:
            continue  # not made yet / already gone
        for f in files:
            try:
                st = os.lstat(f)
                ours = private or st.st_uid == os.getuid()
                if ours and stat.S_ISREG(st.st_mode) and st.st_mtime < cutoff:
                    f.unlink()
            except OSError as e:
                log.info("couldn't delete %s: %s", f, e)
    for folder in old_folders:
        try:
            folder.rmdir()
        except OSError:
            pass


# --- ffmpeg ----------------------------------------------------------------------

def _input(source):
    """ffmpeg's options to read `source`: a file's path, or a subs.online.Stream (a media
    file on the web) with the HTTP headers its site wants."""
    if isinstance(source, (str, os.PathLike)):
        return ["-i", str(source)]
    headers = "".join(f"{key}: {value}\r\n" for key, value in source.headers.items())
    return [*(["-headers", headers] if headers else []), "-i", source.url]


def _stem(source):
    """A name for the files cut from `source`: a Stream's, or the video file's."""
    name = source.name if hasattr(source, "url") else Path(source).stem
    return re.sub(r'[\\/:*?"<>|\s]+', "_", name)


def run_ffmpeg(*args, ffmpeg=None, timeout=FFMPEG_TIMEOUT):
    """Run ffmpeg; it is stopped after `timeout` seconds, raising FfmpegError."""
    flags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
    try:
        return subprocess.run([ffmpeg or config.get()["ffmpeg_path"], "-y", *args],
                              check=True, capture_output=True, creationflags=flags,
                              timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise FfmpegError(f"ffmpeg took more than {timeout} seconds and was stopped (is the "
                          "video on a drive that is asleep or gone?)") from e


def image_args(fmt, max_height):
    """ffmpeg output options for the screenshot."""
    args = ["-frames:v", "1"]
    if max_height:
        args += ["-vf", f"scale=-2:'min({int(max_height)},ih)'"]  # never upscale
    if fmt == "webp":
        args += ["-c:v", "libwebp", "-quality", "80"]
    else:
        args += ["-q:v", "3"]
    return args


def animated_args(fmt, max_height):
    """ffmpeg output options for the animated picture (an endless loop, no sound)."""
    scale = f"scale=-2:'min({int(max_height)},ih)'," if max_height else ""
    args = ["-an", "-sn", "-dn", "-vf", f"{scale}fps={ANIMATED_FPS}"]
    if fmt == "avif":
        return args + ["-c:v", "libaom-av1", "-cpu-used", "8", "-row-mt", "1", "-crf", "40",
                       "-b:v", "0", "-pix_fmt", "yuv420p", "-loop", "0"]
    return args + ["-c:v", "libwebp", "-quality", "60", "-compression_level", "4",
                   "-loop", "0"]


def clip_span(start, end, at, longest=ANIMATED_MAX_SECONDS):
    """(start, duration) of the animated picture: the line, or the `longest` seconds of it
    around `at` (the still picture's time)."""
    if end - start <= longest:
        return start, max(end - start, 0.1)
    return min(max(start, at - longest / 2), end - longest), longest


def audio_format(cfg):
    fmt = cfg["audio_format"]
    return fmt if fmt in AUDIO_CODECS else "m4a"


def _map(audio_stream):
    # no video, subtitle or data streams: without a -map, ffmpeg would copy a text subtitle
    # track into the .m4a as mov_text
    args = ["-vn", "-sn", "-dn"]
    if audio_stream is not None:
        args += ["-map", f"0:{audio_stream}"]
    return args


def audio_args(audio_stream, gain_db=None, fmt="m4a"):
    args = _map(audio_stream)
    if gain_db:
        args += ["-af", f"volume={gain_db:.2f}dB"]
    return args + AUDIO_CODECS[fmt]


def parse_loudness(stderr):
    """(integrated loudness in LUFS, true peak in dBTP) from loudnorm's JSON report, or None
    if there is none or the clip is silent."""
    text = stderr.decode("utf-8", "replace") if isinstance(stderr, bytes) else stderr
    begin, end = text.rfind("{"), text.rfind("}")
    try:
        report = json.loads(text[begin:end + 1])
        loudness, peak = float(report["input_i"]), float(report["input_tp"])
    except (ValueError, KeyError, TypeError):
        return None
    if loudness != loudness or loudness == float("-inf"):  # nan / silence
        return None
    return loudness, peak


def normalize_gain(measured):
    """dB to add for TARGET_LUFS without the peak going over TARGET_PEAK; None to leave the
    clip as it is."""
    if measured is None:
        return None
    loudness, peak = measured
    gain = min(TARGET_LUFS - loudness, TARGET_PEAK - peak, MAX_BOOST)
    return gain if abs(gain) >= 0.1 else None


def _extract_audio(video, a_start, duration, audio_stream, normalize, out, fmt):
    cut = ["-ss", f"{a_start:.3f}", *_input(video), "-t", f"{duration:.3f}"]
    gain = None
    if normalize:
        measured = run_ffmpeg("-hide_banner", "-nostats", *cut, *_map(audio_stream),
                              "-af", MEASURE, "-f", "null", "-")
        gain = normalize_gain(parse_loudness(measured.stderr))
    run_ffmpeg(*cut, *audio_args(audio_stream, gain, fmt), str(out))


def _extract_image(video, at, stem, cfg, start, end):
    """The animated picture of start..end if one is set (and this ffmpeg can make it), else
    the still picture at `at`."""
    fmt = cfg["animated_image"]
    if fmt in ("webp", "avif"):
        image = _temp() / f"{stem}.{fmt}"
        clip_start, duration = clip_span(start, end, at)
        try:
            run_ffmpeg("-ss", f"{clip_start:.3f}", *_input(video), "-t", f"{duration:.3f}",
                       *animated_args(fmt, cfg["image_height"]), str(image))
            return image
        except subprocess.CalledProcessError as e:  # a build without libaom / libwebp
            tail = (e.stderr or b"").decode("utf-8", "replace").strip().splitlines()[-1:]
            log.warning("animated %s picture failed, taking a still one: %s", fmt, tail)
            discard(image)
    return _extract_still(video, at, stem, cfg)


def _extract_still(video, at, stem, cfg):
    fmt, height = cfg["image_format"], cfg["image_height"]
    image = _temp() / f"{stem}.{fmt}"
    try:
        run_ffmpeg("-ss", f"{at:.3f}", *_input(video), *image_args(fmt, height), str(image))
    except subprocess.CalledProcessError:
        if fmt == "jpg":
            raise
        image = _temp() / f"{stem}.jpg"  # an ffmpeg build without libwebp
        run_ffmpeg("-ss", f"{at:.3f}", *_input(video), *image_args("jpg", height), str(image))
    return image


def picture_time(start, end, paused_at=None, slack=0.5):
    """When the card's picture is taken: the frame on screen if playback is paused within
    the line (or just around it: you paused on purpose), else the middle of the line."""
    if paused_at is not None and start - slack <= paused_at <= end + slack:
        return max(0.0, paused_at)
    return (start + end) / 2


def extract_media(video, start, end, audio_stream=None, picture_at=None, audio_source=None):
    """Returns (image_path, audio_path). Slow: call via run_in_background. The screenshot and
    the audio are cut in parallel. `audio_stream` is the ffmpeg stream index of the audio
    track playing (None = ffmpeg's pick); `picture_at` is when to take the picture (None =
    the middle of the line). `video` is a file's path or a Stream on the web; the audio is
    cut from `audio_source` if given (a site's audio Stream), else from `video`."""
    cfg = config.get()
    pad = cfg["audio_padding"]
    # unique: the same line can be mined twice at once (a hotkey pressed twice, a batch)
    stem = f"{_stem(video)}_{start:.2f}_{uuid.uuid4().hex[:8]}"
    fmt = audio_format(cfg)
    audio = _temp() / f"{stem}.{fmt}"
    a_start, a_end = max(0, start - pad), end + pad
    at = (start + end) / 2 if picture_at is None else picture_at
    with ThreadPoolExecutor(2) as pool:
        image_job = pool.submit(_extract_image, video, at, stem, cfg, start, end)
        audio_job = pool.submit(_extract_audio, audio_source or video, a_start,
                                a_end - a_start, audio_stream,
                                cfg["normalize_audio"], audio, fmt)
    try:
        image = image_job.result()
        audio_job.result()
    except Exception as e:
        discard(audio, *(TEMP / f"{stem}.{ext}" for ext in ("webp", "jpg", "avif")))
        if isinstance(e, subprocess.CalledProcessError) and e.stderr:
            tail = e.stderr.decode("utf-8", "replace").strip().splitlines()[-3:]
            raise FfmpegError("ffmpeg failed:\n" + "\n".join(tail)) from e
        raise
    return image, audio


def condensed_spans(times, pad, gap=CONDENSED_GAP):
    """The [(start, end)] of audio that condensed audio keeps for lines at `times` [(start,
    end)]: each line padded by `pad`, and lines that overlap or are less than `gap` apart
    kept as one stretch, with the pause between them."""
    spans = []
    for start, end in sorted(times):
        start, end = max(0.0, start - pad), end + pad
        if spans and start <= spans[-1][1] + gap:
            spans[-1][1] = max(spans[-1][1], end)
        else:
            spans.append([start, end])
    return [(start, end) for start, end in spans]


def condensed_filter(spans, normalize):
    """The ffmpeg audio filter keeping only `spans` of the track, one after the other."""
    keep = "+".join(f"between(t,{start:.3f},{end:.3f})" for start, end in spans)
    # times from the track's own start (a .ts file's don't start at 0)
    chain = f"asetpts=PTS-STARTPTS,aselect='{keep}',asetpts=N/SR/TB"
    if normalize:  # over minutes of audio, loudnorm's single pass doesn't pump
        chain += f",loudnorm=I={TARGET_LUFS:g}:TP={TARGET_PEAK:g}:LRA=11"
    return chain


def condensed_audio(video, times, out, audio_stream=None):
    """Write the audio of the lines at `times` [(start, end)], video time, one after the
    other to `out` (.m4a, .mp3 or .opus): the episode's condensed audio. `video` is a
    file's path or a Stream on the web. Returns how many
    seconds long it is. Slow: call via run_in_background."""
    cfg = config.get()
    out = Path(out)
    fmt = out.suffix[1:].lower()
    spans = condensed_spans(times, cfg["audio_padding"])
    if fmt not in AUDIO_CODECS or not spans:
        raise ValueError(f"Can't make condensed audio as {out.name}")
    script = None
    chain = condensed_filter(spans, cfg["normalize_audio"])
    if len(chain) < MAX_INLINE_FILTER:
        filter_args = ["-af", chain]
    else:  # a film's worth of lines: too long for a command line on Windows
        script = scratch_file("condensed.txt")
        script.write_text(chain, encoding="utf-8")
        filter_args = ["-filter_script:a", str(script)]
    part = out.with_name(f"{out.stem}.{uuid.uuid4().hex[:6]}.part{out.suffix}")
    try:
        # loudnorm works at 192 kHz; "-ar", not an aresample filter, which libopus refuses
        run_ffmpeg(*_input(video), *_map(audio_stream), *filter_args, "-ar", "48000",
                   *AUDIO_CODECS[fmt], str(part), timeout=EXTRACT_TIMEOUT)
        os.replace(part, out)
    except subprocess.CalledProcessError as e:
        tail = (e.stderr or b"").decode("utf-8", "replace").strip().splitlines()[-3:]
        raise FfmpegError("ffmpeg failed:\n" + "\n".join(tail)) from e
    finally:
        discard(part, *([script] if script else []))
    return sum(end - start for start, end in spans)


def extract_subtitles(video, stream_index):
    """Extract an embedded subtitle track to an .ass file (text-based tracks only), reusing
    the one extracted earlier while the video is unchanged. Slow: call via
    run_in_background."""
    st = os.stat(video)
    key = hashlib.md5(f"{video}|{st.st_size}|{st.st_mtime_ns}".encode()).hexdigest()[:10]
    out = _temp() / f"{Path(video).stem}_{key}_s{stream_index}.ass".replace(" ", "_")
    if out.is_file():
        out.touch()  # not swept as a leftover while it is in use
        return out
    part = out.with_name(f"{out.stem}.{uuid.uuid4().hex[:6]}.part.ass")  # ffmpeg goes by the suffix
    try:
        run_ffmpeg("-i", video, "-map", f"0:{stream_index}", str(part),
                   timeout=EXTRACT_TIMEOUT)
        os.replace(part, out)
    finally:
        discard(part)
    return out
