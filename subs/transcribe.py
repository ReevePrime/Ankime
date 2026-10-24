"""Making subtitles from the speech of a video, for shows with none on Jimaku: faster-whisper
(a command-line build of it: Faster-Whisper-XXL, whisper-ctranslate2) or whisper.cpp
(whisper-cli), installed separately (the transcriber setting). The audio track playing is
taken out with ffmpeg first, as the 16 kHz WAV whisper.cpp needs; a dual-audio video is
then transcribed in the language heard. It takes minutes with a GPU, much longer without:
call from a background thread."""
import os
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from ..cards import media
from ..core import config

TIMEOUT = 4 * 3600  # seconds; a large model on a slow CPU takes longer than the episode

# "[00:01:02.500 --> 00:01:05.000]  text": both print each line as it is heard
_PROGRESS = re.compile(r"-->\s*(?:(\d+):)?(\d+):(\d+(?:[.,]\d+)?)")

_running = set()  # the transcribers' processes, stopped when the profile closes
_lock = threading.Lock()


class TranscribeError(Exception):
    """The transcriber is missing or failed; the message ends with the last lines it
    printed."""


def tool_kind(path):
    """"faster-whisper" or "whisper.cpp" by the program's name (faster-whisper-xxl.exe,
    whisper-ctranslate2, whisper-cli...), or None."""
    name = Path(path).name.lower()
    if "faster" in name or "ctranslate2" in name:
        return "faster-whisper"
    if "whisper-cli" in name or "whisper-cpp" in name or "whisper.cpp" in name:
        return "whisper.cpp"
    return None


def find_ggml_model(tool):
    """The largest multilingual ggml model next to whisper.cpp (its folder, models/ there,
    or the source tree's models/ above build/bin), or None. `.en` models only know
    English."""
    folder = Path(tool).resolve().parent
    found = []
    for where in (folder, folder / "models", folder.parent / "models",
                  folder.parent.parent / "models"):
        if where.is_dir():
            found += [f for f in where.glob("ggml-*.bin") if ".en" not in f.name.lower()]
    return max(found, key=lambda f: f.stat().st_size, default=None)


def command(tool, audio, out_dir, lang, model=""):
    """The command writing `audio`'s subtitles to `out_dir`/<audio's stem>.srt."""
    audio, out_dir = Path(audio), Path(out_dir)
    if tool_kind(tool) == "whisper.cpp":
        args = [tool, "-f", str(audio), "-l", lang, "-osrt",
                "-of", str(out_dir / audio.stem), "-t", str(min(8, os.cpu_count() or 4))]
        return args + ["-m", str(model)] if model else args
    args = [tool, str(audio), "--language", lang, "--output_dir", str(out_dir),
            "--output_format", "srt", "--vad_filter", "True"]  # no lines made up in music
    return args + ["--model", model] if model else args


def parse_progress(line):
    """Seconds into the audio of a line the transcriber printed, or None."""
    m = _PROGRESS.search(line)
    if not m:
        return None
    hours, minutes, seconds = m.groups()
    return int(hours or 0) * 3600 + int(minutes) * 60 + float(seconds.replace(",", "."))


def subtitle_path(video, lang):
    """Where the subtitles go: next to the video, `ep01.ja.whisper.srt`, found by name like
    any other but after a real `ep01.ja.srt`. Never over an existing file."""
    video = Path(video)
    path = video.with_name(f"{video.stem}.{lang}.whisper.srt")
    n = 2
    while path.exists():
        path = video.with_name(f"{video.stem}.{lang}.whisper{n}.srt")
        n += 1
    return path


def stop_all():
    """Stop the transcriptions still running (the profile is closing: Anki would wait for
    them before quitting)."""
    with _lock:
        procs = list(_running)
    for proc in procs:
        try:
            proc.kill()
        except OSError:
            pass


def _run(args, progress, duration):
    """Run the transcriber, calling progress(fraction) as it prints lines. Returns the
    last lines it printed; raises TranscribeError if it fails."""
    flags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
    name = Path(args[0]).name
    try:
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, creationflags=flags)
    except FileNotFoundError as e:
        raise TranscribeError(f"{args[0]} not found: check the transcriber in Tools > "
                              "Ankime > Settings > Player.") from e
    with _lock:
        _running.add(proc)
    timer = threading.Timer(TIMEOUT, proc.kill)
    timer.daemon = True
    timer.start()
    started, tail = time.monotonic(), []
    try:
        for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").rstrip()
            if line:
                tail = (tail + [line])[-3:]
            at = parse_progress(line)
            if progress and duration and at is not None:
                progress(min(at / duration, 1.0))
        code = proc.wait()
    finally:
        timer.cancel()
        with _lock:
            _running.discard(proc)
    if code:
        if time.monotonic() - started >= TIMEOUT:
            raise TranscribeError(f"{name} took more than {TIMEOUT // 3600} hours and was "
                                  "stopped.")
        tail_text = "\n".join(tail)
        raise TranscribeError(f"{name} failed:\n{tail_text}" if tail_text else
                              f"{name} was stopped.")


def transcribe(video, out, audio_stream=None, duration=None, progress=None, tool=None,
               model=None):
    """Write subtitles of the speech in `video` (its ffmpeg stream `audio_stream`, else the
    default audio) to `out` (.srt). progress(fraction) is called from this thread while it
    runs, if `duration` (seconds) is known. Returns `out`."""
    cfg = config.get()
    tool = tool or cfg["transcriber"]
    model = (cfg["transcriber_model"] if model is None else model).strip()
    if not tool:
        raise TranscribeError("No transcriber set: install faster-whisper or whisper.cpp "
                              "and set it in Tools > Ankime > Settings > Player.")
    kind = tool_kind(tool)
    if kind is None:
        raise TranscribeError(f"{Path(tool).name} isn't faster-whisper or whisper.cpp.")
    if kind == "whisper.cpp" and not model:
        model = find_ggml_model(tool)
        if model is None:
            raise TranscribeError("whisper.cpp needs a model: download a multilingual one "
                                  "(ggml-large-v3-turbo.bin, say) next to whisper-cli, or "
                                  "set its file as the transcriber model in Tools > Ankime "
                                  "> Settings > Player.")
    lang = "ja"  # Ankime is for Japanese only; whisper takes the ISO 639-1 code
    stream = ["-map", f"0:{audio_stream}"] if audio_stream is not None else []
    with tempfile.TemporaryDirectory(prefix="ankime-whisper-") as tmp:
        audio = Path(tmp) / "audio.wav"
        try:  # a whole episode: longer than a card's clip
            media.run_ffmpeg("-i", str(video), "-vn", "-sn", "-dn", *stream, "-ac", "1",
                             "-ar", "16000", "-c:a", "pcm_s16le", str(audio),
                             timeout=media.FFMPEG_TIMEOUT * 10)
        except subprocess.CalledProcessError as e:
            output = (e.stderr or b"").decode("utf-8", "replace").strip()
            tail = "\n".join(output.splitlines()[-3:])
            raise TranscribeError(f"ffmpeg couldn't read the audio:\n{tail}") from e
        except media.FfmpegError as e:
            raise TranscribeError(str(e)) from e
        except FileNotFoundError as e:
            raise TranscribeError("ffmpeg not found: check it in Tools > Ankime > Settings "
                                  "(Test setup can download it).") from e
        _run(command(tool, audio, tmp, lang, model), progress, duration)
        written = Path(tmp) / f"{audio.stem}.srt"
        if not written.is_file():
            written = next(Path(tmp).glob("*.srt"), None)
        if written is None:
            raise TranscribeError(f"{Path(tool).name} wrote no subtitles.")
        if not written.read_bytes().strip():
            raise TranscribeError(f"{Path(tool).name} heard no speech.")
        Path(out).write_bytes(written.read_bytes())
    return Path(out)
