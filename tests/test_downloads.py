"""Downloading libmpv, ffmpeg and JMdict (core/downloads.py): nothing is used unless it is the
file expected, and nothing is left behind when it isn't. No network: a fake session."""
import hashlib
import io
import json
import re
import zipfile

import pytest
import requests

from ankime.core import downloads


class FakeResponse:
    def __init__(self, content=b"", status=200, payload=None):
        self.content, self.status, self.payload = content, status, payload

    def raise_for_status(self):
        if self.status >= 400:
            raise requests.HTTPError(f"{self.status}")

    def iter_content(self, size):
        for i in range(0, len(self.content), 3):
            yield self.content[i:i + 3]

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, responses):
        self.responses, self.urls = responses, []

    def get(self, url, **_kwargs):
        self.urls.append(url)
        return self.responses[url]


def sha(data):
    return hashlib.sha256(data).hexdigest()


def leftovers(folder):
    return [p.name for p in folder.rglob("*") if p.is_file()]


def test_fetch_keeps_the_file_only_when_it_is_the_one_expected(tmp_path):
    session = FakeSession({"https://x/f": FakeResponse(b"hello world")})
    seen = []
    dest = downloads.fetch("https://x/f", tmp_path / "f", sha(b"hello world"), 100,
                           progress=lambda done, total: seen.append(done), session=session)
    assert dest.read_bytes() == b"hello world" and seen[-1] == 11
    with pytest.raises(downloads.DownloadError, match="checksum"):
        downloads.fetch("https://x/f", tmp_path / "g", sha(b"other"), 100, session=session)
    assert leftovers(tmp_path) == ["f"]


def test_fetch_stops_past_the_expected_size_and_when_cancelled(tmp_path):
    session = FakeSession({"https://x/f": FakeResponse(b"0123456789")})
    with pytest.raises(downloads.DownloadError, match="larger than expected"):
        downloads.fetch("https://x/f", tmp_path / "f", sha(b"0123456789"), 5, session=session)
    with pytest.raises(downloads.Cancelled):
        downloads.fetch("https://x/f", tmp_path / "f", sha(b"0123456789"), 100,
                        cancelled=lambda: True, session=session)
    session.responses["https://x/f"] = FakeResponse(status=404)
    with pytest.raises(downloads.DownloadError, match="Couldn't download"):
        downloads.fetch("https://x/f", tmp_path / "f", "", 100, session=session)
    assert leftovers(tmp_path) == []


def _zip(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


def test_install_unpacks_only_the_tool_and_returns_its_setting(tmp_path, monkeypatch):
    archive = _zip({"build/bin/ffmpeg.exe": b"EXE", "build/bin/ffplay.exe": b"other",
                    "../../evil.exe": b"no"})
    tool = downloads.Tool("ffmpeg", "1", "test", "https://x/ffmpeg.zip", sha(archive),
                          len(archive), "zip", "build/bin/ffmpeg.exe", "ffmpeg")
    monkeypatch.setitem(downloads.TOOLS, "ffmpeg", tool)
    monkeypatch.setattr(downloads, "TOOLS_DIR", tmp_path / "tools")
    session = FakeSession({tool.url: FakeResponse(archive)})
    path = downloads.install("ffmpeg", session=session)
    assert path == tmp_path / "tools" / "ffmpeg" / "ffmpeg.exe"
    assert path.read_bytes() == b"EXE"
    assert leftovers(tmp_path) == ["ffmpeg.exe"]  # no archive, nothing else unpacked


def test_install_of_an_archive_without_the_tool_fails_cleanly(tmp_path, monkeypatch):
    archive = _zip({"something.txt": b"x"})
    tool = downloads.Tool("ffmpeg", "1", "test", "https://x/ffmpeg.zip", sha(archive),
                          len(archive), "zip", "build/bin/ffmpeg.exe", "ffmpeg")
    monkeypatch.setitem(downloads.TOOLS, "ffmpeg", tool)
    monkeypatch.setattr(downloads, "TOOLS_DIR", tmp_path / "tools")
    with pytest.raises(downloads.DownloadError, match="isn't in the archive"):
        downloads.install("ffmpeg", session=FakeSession({tool.url: FakeResponse(archive)}))
    assert leftovers(tmp_path) == []


def test_the_tools_are_pinned():
    for tool in downloads.TOOLS.values():
        assert tool.url.startswith("https://")
        assert re.fullmatch("[0-9a-f]{64}", tool.sha256)
        assert tool.size > 0 and tool.archive in ("7z", "zip")


def test_tools_are_offered_on_64_bit_windows_only(monkeypatch):
    monkeypatch.setattr(downloads.sys, "platform", "linux")
    assert not downloads.can_install()
    monkeypatch.setattr(downloads.sys, "platform", "win32")
    monkeypatch.setattr(downloads.platform, "machine", lambda: "AMD64")
    assert downloads.can_install() == (downloads.sys.maxsize > 2 ** 32)
    monkeypatch.setattr(downloads.platform, "machine", lambda: "x86")
    assert not downloads.can_install()


def _release(name="JMdict_english.zip", url=None, digest="sha256:" + "a" * 64, size=1000):
    url = url or downloads.JMDICT_URLS + "2026-10-23/" + name
    return FakeResponse(payload={"assets": [{"name": name, "browser_download_url": url,
                                             "digest": digest, "size": size}]})


def test_the_latest_jmdict_comes_with_its_checksum():
    session = FakeSession({downloads.JMDICT_RELEASE: _release()})
    url, digest, size = downloads.latest_jmdict(session)
    assert url.endswith("/JMdict_english.zip") and digest == "a" * 64 and size == 1000


@pytest.mark.parametrize("release", [
    _release(url="https://elsewhere.example/JMdict_english.zip"),
    _release(digest=""),
    _release(size=downloads.MAX_DICTIONARY + 1),
    _release(name="JMdict_french.zip"),
    FakeResponse(payload={"message": "rate limited"}),
])
def test_a_jmdict_release_that_isnt_as_expected_is_refused(release):
    with pytest.raises(downloads.DownloadError):
        downloads.latest_jmdict(FakeSession({downloads.JMDICT_RELEASE: release}))


def test_download_jmdict_checks_the_file(tmp_path):
    data = json.dumps({"title": "JMdict"}).encode()
    url = downloads.JMDICT_URLS + "x/JMdict_english.zip"
    session = FakeSession({downloads.JMDICT_RELEASE: _release(url=url, digest="sha256:" + sha(data),
                                                              size=len(data)),
                           url: FakeResponse(data)})
    assert downloads.download_jmdict(tmp_path / "j.zip", session=session).read_bytes() == data
