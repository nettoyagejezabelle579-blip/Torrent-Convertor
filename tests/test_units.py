"""Tests for file selection, progress formatting and loading sources."""

from __future__ import annotations

import http.server
import threading

import pytest

from torrent_convertor.converter import select_files
from torrent_convertor.engine import TorrentFile
from torrent_convertor.errors import SourceError
from torrent_convertor.progress import Progress, Stage, format_eta, format_rate, format_size
from torrent_convertor.sources import display_name, info_hash_hex, load_source

FILES = [
    TorrentFile(0, "Show/Season 1/ep1.mkv", 10),
    TorrentFile(1, "Show/Season 1/ep1.srt", 1),
    TorrentFile(2, "Show/Season 2/ep1.MKV", 10),
    TorrentFile(3, "Show/Sample/sample.mkv", 2),
    TorrentFile(4, "Show/readme.txt", 1),
]


def paths(files):
    return [f.path for f in files]


@pytest.mark.parametrize(
    "include, exclude, expected",
    [
        ([], [], [0, 1, 2, 3, 4]),
        (["*.mkv"], [], [0, 2, 3]),
        (["*.mkv"], ["sample/*"], [0, 2]),
        (["Season 1/*"], [], [0, 1]),
        (["Show/Season 2/*"], [], [2]),
        (["readme.txt"], [], [4]),
        ([], ["*.srt", "*.txt"], [0, 2, 3]),
        (["*.iso"], [], []),
    ],
)
def test_select_files(include, exclude, expected):
    assert paths(select_files(FILES, include, exclude)) == [FILES[i].path for i in expected]


def test_format_size():
    assert format_size(0) == "0 B"
    assert format_size(1023) == "1023 B"
    assert format_size(1536) == "1.5 KiB"
    assert format_size(5 * 1024**3) == "5.0 GiB"
    assert format_size(3 * 1024**4) == "3.0 TiB"
    assert format_rate(2048) == "2.0 KiB/s"


def test_format_eta():
    assert format_eta(None) == "--"
    assert format_eta(42) == "42s"
    assert format_eta(252) == "4m 12s"
    assert format_eta(3600 + 120) == "1h 02m"
    assert format_eta(2 * 86400 + 3 * 3600) == "2d 3h"


def test_progress_fraction_and_eta():
    downloading = Progress(Stage.DOWNLOADING, done=25, total=100, download_rate=5)
    assert downloading.fraction == 0.25
    assert downloading.eta == 15
    assert Progress(Stage.DOWNLOADING, done=25, total=100).eta is None
    assert Progress(Stage.METADATA).fraction == 0.0
    assert Progress(Stage.DONE).fraction == 1.0


MAGNET = "magnet:?xt=urn:btih:d00c58f3746a3b889fc86de6988b0408bf8b1b4f&dn=Hello%20World"


def test_load_magnet():
    params = load_source(f"  {MAGNET}  ")
    assert params.ti is None
    assert display_name(params) == "Hello World"
    assert info_hash_hex(params) == "d00c58f3746a3b889fc86de6988b0408bf8b1b4f"


@pytest.mark.parametrize(
    "source, message",
    [
        ("magnet:?dn=no-hash", "Invalid magnet link"),
        ("magnet:?xt=urn:btih:nothex", "Invalid magnet link"),
        ("", "No torrent"),
    ],
)
def test_bad_sources(source, message):
    with pytest.raises(SourceError, match=message):
        load_source(source)


def test_not_a_torrent_file(tmp_path):
    path = tmp_path / "fake.torrent"
    path.write_text("this is not a torrent")
    with pytest.raises(SourceError, match="Not a valid .torrent file"):
        load_source(str(path))


def test_folder_is_not_a_torrent(tmp_path):
    with pytest.raises(SourceError, match="is a folder"):
        load_source(str(tmp_path))


def test_load_torrent_file(torrent_file):
    params = load_source(str(torrent_file))
    assert display_name(params) == "MyData"
    assert len(info_hash_hex(params)) == 40


@pytest.fixture
def web_server(tmp_path, torrent_file):
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "MyData.torrent").write_bytes(torrent_file.read_bytes())

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(tmp_path / "web"), **kwargs)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def test_load_from_url(web_server):
    params = load_source(f"{web_server}/MyData.torrent")
    assert display_name(params) == "MyData"


def test_url_not_found(web_server):
    with pytest.raises(SourceError, match="HTTP 404"):
        load_source(f"{web_server}/missing.torrent")
