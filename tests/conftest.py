"""Test fixtures: a real libtorrent seeder on localhost, so downloads run end to end
without touching the internet."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

lt = pytest.importorskip("libtorrent")

from torrent_convertor.engine import Session, SessionConfig  # noqa: E402

PIECE_SIZE = 32 * 1024

# name -> content of the sample torrent "MyData"
SAMPLE_FILES = {
    "a.bin": os.urandom(300_000),
    "sub/b.txt": b"hello torrent\n" * 20_000,
    "empty.txt": b"",
    "video.mp4": os.urandom(100_000),
}

LOCAL_ONLY = SessionConfig(listen_interfaces="127.0.0.1:0", dht=False, lsd=False, upnp=False)


def make_torrent(content_dir: Path) -> bytes:
    if hasattr(lt, "list_files"):  # libtorrent 2.1
        creator = lt.create_torrent(lt.list_files(str(content_dir)), PIECE_SIZE)
    else:
        storage = lt.file_storage()
        lt.add_files(storage, str(content_dir))
        creator = lt.create_torrent(storage, PIECE_SIZE)
    lt.set_piece_hashes(creator, str(content_dir.parent))
    return lt.bencode(creator.generate())


class Seeder:
    """A libtorrent session that seeds one torrent on 127.0.0.1."""

    def __init__(self, torrent_data: bytes, save_path: Path, upload_limit: int = 0):
        self.session = lt.session(
            {
                "listen_interfaces": "127.0.0.1:0",
                "enable_dht": False,
                "enable_lsd": False,
                "enable_upnp": False,
                "enable_natpmp": False,
            }
        )
        params = lt.add_torrent_params()
        params.ti = lt.torrent_info(torrent_data)
        params.save_path = str(save_path)
        self.handle = self.session.add_torrent(params)
        # A per-torrent limit, because session-wide limits don't apply to local peers.
        self.handle.set_upload_limit(upload_limit)
        self.magnet = lt.make_magnet_uri(lt.torrent_info(torrent_data))
        deadline = time.monotonic() + 10
        while not self.handle.status().is_seeding:
            assert time.monotonic() < deadline, "seeder did not start"
            time.sleep(0.05)

    @property
    def address(self):
        return ("127.0.0.1", self.session.listen_port())

    @property
    def peer_arg(self) -> str:
        return f"127.0.0.1:{self.session.listen_port()}"


@pytest.fixture
def content_dir(tmp_path) -> Path:
    root = tmp_path / "seed" / "MyData"
    for name, data in SAMPLE_FILES.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return root


@pytest.fixture
def torrent_file(tmp_path, content_dir) -> Path:
    path = tmp_path / "MyData.torrent"
    path.write_bytes(make_torrent(content_dir))
    return path


@pytest.fixture
def seeder(torrent_file, content_dir):
    seeder = Seeder(torrent_file.read_bytes(), content_dir.parent)
    yield seeder
    seeder.session = None


@pytest.fixture
def slow_seeder(torrent_file, content_dir):
    seeder = Seeder(torrent_file.read_bytes(), content_dir.parent, upload_limit=50_000)
    yield seeder
    seeder.session = None


@pytest.fixture
def session():
    with Session(LOCAL_ONLY) as session:
        yield session


@pytest.fixture
def output_dir(tmp_path) -> Path:
    return tmp_path / "out"
