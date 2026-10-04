from __future__ import annotations

import threading
import zipfile
from pathlib import Path

import pytest

from torrent_convertor.archive import ZipEntry, create_zip, safe_filename, unique_path
from torrent_convertor.errors import ArchiveError, Cancelled
from torrent_convertor.progress import Stage


def write(path: Path, data: bytes) -> ZipEntry:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return ZipEntry(path, path.relative_to(path.parents[1]).as_posix(), len(data))


@pytest.fixture
def entries(tmp_path):
    return [
        write(tmp_path / "src" / "notes.txt", b"some text " * 1000),
        write(tmp_path / "src" / "movie.MKV", bytes(range(256)) * 100),
    ]


def test_creates_zip_with_progress(tmp_path, entries):
    seen = []
    result = create_zip(entries, tmp_path / "out.zip", name="x", on_progress=seen.append)

    assert result == tmp_path / "out.zip"
    assert not (tmp_path / "out.zip.part").exists()
    with zipfile.ZipFile(result) as archive:
        assert archive.read("src/notes.txt") == b"some text " * 1000
        assert archive.read("src/movie.MKV") == bytes(range(256)) * 100
    assert seen[-1].stage is Stage.ZIPPING
    assert seen[-1].done == seen[-1].total == sum(e.size for e in entries)


@pytest.mark.parametrize(
    "compression, text_method, video_method",
    [
        ("auto", zipfile.ZIP_DEFLATED, zipfile.ZIP_STORED),
        ("deflate", zipfile.ZIP_DEFLATED, zipfile.ZIP_DEFLATED),
        ("store", zipfile.ZIP_STORED, zipfile.ZIP_STORED),
        ("bzip2", zipfile.ZIP_BZIP2, zipfile.ZIP_BZIP2),
        ("lzma", zipfile.ZIP_LZMA, zipfile.ZIP_LZMA),
    ],
)
def test_compression_methods(tmp_path, entries, compression, text_method, video_method):
    result = create_zip(entries, tmp_path / "out.zip", compression=compression)
    with zipfile.ZipFile(result) as archive:
        assert archive.testzip() is None
        assert archive.getinfo("src/notes.txt").compress_type == text_method
        assert archive.getinfo("src/movie.MKV").compress_type == video_method


def test_compression_level_is_applied(tmp_path, entries):
    fast = create_zip(entries[:1], tmp_path / "fast.zip", compression="deflate", level=0)
    best = create_zip(entries[:1], tmp_path / "best.zip", compression="deflate", level=9)
    assert best.stat().st_size < fast.stat().st_size


def test_missing_empty_file_is_added(tmp_path):
    entry = ZipEntry(tmp_path / "does-not-exist.txt", "t/empty.txt", 0)
    result = create_zip([entry], tmp_path / "out.zip")
    with zipfile.ZipFile(result) as archive:
        assert archive.read("t/empty.txt") == b""


def test_missing_file_with_data_fails(tmp_path):
    entry = ZipEntry(tmp_path / "missing.bin", "t/missing.bin", 10)
    with pytest.raises(ArchiveError, match="missing"):
        create_zip([entry], tmp_path / "out.zip")
    assert list(tmp_path.iterdir()) == []


def test_incomplete_file_fails(tmp_path):
    entry = write(tmp_path / "src" / "a.bin", b"12345")
    with pytest.raises(ArchiveError, match="incomplete"):
        create_zip([ZipEntry(entry.source, entry.arcname, 10)], tmp_path / "out.zip")
    assert not (tmp_path / "out.zip").exists()
    assert not (tmp_path / "out.zip.part").exists()


def test_cancel_removes_partial_zip(tmp_path, entries):
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(Cancelled):
        create_zip(entries, tmp_path / "out.zip", cancel=cancel)
    assert not (tmp_path / "out.zip").exists()
    assert not (tmp_path / "out.zip.part").exists()


def test_unwritable_destination(tmp_path, entries):
    with pytest.raises(ArchiveError, match="Could not create"):
        create_zip(entries, tmp_path / "no-such-folder" / "out.zip")


@pytest.mark.parametrize(
    "name, expected",
    [
        ("Ubuntu 24.04", "Ubuntu 24.04"),
        ('a<b>c:d"e/f\\g|h?i*j', "a_b_c_d_e_f_g_h_i_j"),
        ("trailing dots... ", "trailing dots"),
        ("CON", "_CON"),
        ("nul.txt", "_nul.txt"),
        ("", "torrent"),
        ("...", "torrent"),
        ("tab\there", "tab_here"),
    ],
)
def test_safe_filename(name, expected):
    assert safe_filename(name) == expected


def test_safe_filename_is_shortened():
    assert len(safe_filename("x" * 500)) == 200


def test_unique_path(tmp_path):
    target = tmp_path / "name.zip"
    assert unique_path(target) == target
    target.touch()
    assert unique_path(target) == tmp_path / "name (1).zip"
    (tmp_path / "name (1).zip.part").touch()  # another conversion is writing this one
    assert unique_path(target) == tmp_path / "name (2).zip"
