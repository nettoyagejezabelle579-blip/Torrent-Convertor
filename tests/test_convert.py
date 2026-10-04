"""End-to-end tests: download from a local seeder and check the resulting zip."""

from __future__ import annotations

import threading
import zipfile

import pytest

from torrent_convertor.converter import WORK_DIR_NAME, ConvertOptions, convert, inspect
from torrent_convertor.errors import Cancelled, ConvertorError, SourceError
from torrent_convertor.progress import Stage

from .conftest import SAMPLE_FILES


def zip_contents(path):
    with zipfile.ZipFile(path) as archive:
        assert archive.testzip() is None
        return {info.filename: archive.read(info) for info in archive.infolist()}


def expected(*names):
    names = names or SAMPLE_FILES
    return {f"MyData/{name}": SAMPLE_FILES[name] for name in names}


def test_torrent_file_to_zip(session, seeder, torrent_file, output_dir):
    stages = []
    zip_path = convert(
        session,
        str(torrent_file),
        ConvertOptions(output_dir=output_dir, peers=[seeder.address]),
        on_progress=lambda p: stages.append(p.stage),
    )

    assert zip_path == output_dir / "MyData.zip"
    assert zip_contents(zip_path) == expected()
    # The temporary download folder is cleaned up; only the zip remains.
    assert sorted(p.name for p in output_dir.iterdir()) == ["MyData.zip"]
    assert stages[0] is Stage.STARTING
    assert Stage.DOWNLOADING in stages and Stage.ZIPPING in stages
    assert stages[-1] is Stage.DONE


def test_magnet_link_to_zip(session, seeder, output_dir):
    zip_path = convert(
        session, seeder.magnet, ConvertOptions(output_dir=output_dir, peers=[seeder.address])
    )
    assert zip_contents(zip_path) == expected()
    assert sorted(p.name for p in output_dir.iterdir()) == ["MyData.zip"]


def test_auto_compression_stores_media_and_deflates_text(session, seeder, torrent_file, output_dir):
    zip_path = convert(
        session, str(torrent_file), ConvertOptions(output_dir=output_dir, peers=[seeder.address])
    )
    with zipfile.ZipFile(zip_path) as archive:
        assert archive.getinfo("MyData/video.mp4").compress_type == zipfile.ZIP_STORED
        assert archive.getinfo("MyData/sub/b.txt").compress_type == zipfile.ZIP_DEFLATED


@pytest.mark.parametrize("use_magnet", [False, True])
def test_include_only_some_files(session, seeder, torrent_file, output_dir, use_magnet):
    source = seeder.magnet if use_magnet else str(torrent_file)
    zip_path = convert(
        session,
        source,
        ConvertOptions(output_dir=output_dir, include=["*.txt"], peers=[seeder.address]),
    )
    assert zip_contents(zip_path) == expected("sub/b.txt", "empty.txt")


def test_exclude_files(session, seeder, torrent_file, output_dir):
    zip_path = convert(
        session,
        str(torrent_file),
        ConvertOptions(output_dir=output_dir, exclude=["*.bin", "sub/*"], peers=[seeder.address]),
    )
    assert zip_contents(zip_path) == expected("empty.txt", "video.mp4")


def test_no_matching_files(session, torrent_file, output_dir):
    with pytest.raises(ConvertorError, match="No files"):
        convert(session, str(torrent_file), ConvertOptions(output_dir=output_dir, include=["*.iso"]))


def test_keep_files(session, seeder, torrent_file, output_dir):
    zip_path = convert(
        session,
        str(torrent_file),
        ConvertOptions(output_dir=output_dir, keep_files=True, peers=[seeder.address]),
    )
    assert zip_contents(zip_path) == expected()
    for name, data in SAMPLE_FILES.items():
        assert (output_dir / "MyData" / name).read_bytes() == data


def test_custom_download_dir_keeps_files(session, seeder, torrent_file, output_dir, tmp_path):
    download_dir = tmp_path / "downloads"
    zip_path = convert(
        session,
        str(torrent_file),
        ConvertOptions(output_dir=output_dir, download_dir=download_dir, peers=[seeder.address]),
    )
    assert zip_contents(zip_path) == expected()
    for name, data in SAMPLE_FILES.items():
        assert (download_dir / "MyData" / name).read_bytes() == data
    assert sorted(p.name for p in output_dir.iterdir()) == ["MyData.zip"]


def test_files_already_on_disk_are_zipped_without_peers(
    session, torrent_file, content_dir, output_dir
):
    # The files are already in the download folder (e.g. downloaded earlier by another
    # client), so they are verified and zipped even though nobody is seeding, and left
    # where they were.
    zip_path = convert(
        session,
        str(torrent_file),
        ConvertOptions(output_dir=output_dir, download_dir=content_dir.parent),
    )
    assert zip_contents(zip_path) == expected()
    for name, data in SAMPLE_FILES.items():
        assert (content_dir / name).read_bytes() == data


def test_zip_name_does_not_overwrite(session, seeder, torrent_file, output_dir):
    output_dir.mkdir()
    (output_dir / "MyData.zip").write_bytes(b"something else")
    zip_path = convert(
        session, str(torrent_file), ConvertOptions(output_dir=output_dir, peers=[seeder.address])
    )
    assert zip_path.name == "MyData (1).zip"
    assert (output_dir / "MyData.zip").read_bytes() == b"something else"


def test_cancel_keeps_partial_download(session, slow_seeder, torrent_file, output_dir):
    cancel = threading.Event()

    cancelled_at = []

    def on_progress(progress):
        if progress.stage is Stage.DOWNLOADING and progress.done > 0 and not cancel.is_set():
            cancelled_at.append(progress.done)
            cancel.set()

    with pytest.raises(Cancelled):
        convert(
            session,
            str(torrent_file),
            ConvertOptions(output_dir=output_dir, peers=[slow_seeder.address]),
            on_progress=on_progress,
            cancel=cancel,
        )
    assert 0 < cancelled_at[0] < sum(len(data) for data in SAMPLE_FILES.values())
    assert not list(output_dir.glob("*.zip*"))
    # The partial data stays for the next run to resume from.
    assert any(p.is_file() for p in (output_dir / WORK_DIR_NAME).rglob("*"))

    # Running again in the same session finishes the job.
    slow_seeder.handle.set_upload_limit(0)
    zip_path = convert(
        session,
        str(torrent_file),
        ConvertOptions(output_dir=output_dir, peers=[slow_seeder.address]),
    )
    assert zip_contents(zip_path) == expected()


def test_inspect_torrent_file(session, torrent_file):
    metadata = inspect(session, str(torrent_file))
    assert metadata.name == "MyData"
    assert {f.path: f.size for f in metadata.files} == {
        f"MyData/{name}": len(data) for name, data in SAMPLE_FILES.items()
    }


def test_inspect_magnet(session, seeder):
    metadata = inspect(session, seeder.magnet, peers=[seeder.address])
    assert metadata.name == "MyData"
    assert len(metadata.files) == len(SAMPLE_FILES)


def test_missing_torrent_file(session, tmp_path):
    with pytest.raises(SourceError, match="not found"):
        convert(session, str(tmp_path / "nope.torrent"), ConvertOptions(output_dir=tmp_path))
