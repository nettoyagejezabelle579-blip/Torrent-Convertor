from __future__ import annotations

import io
import zipfile

import pytest

from torrent_convertor import cli
from torrent_convertor.progress import Progress, Stage

from .conftest import SAMPLE_FILES

LOCAL_FLAGS = ["--port", "0", "--no-dht", "--no-lsd", "--no-upnp"]


def test_convert(seeder, torrent_file, output_dir, capsys):
    code = cli.main(
        [str(torrent_file), "-o", str(output_dir), "--peer", seeder.peer_arg, "-q", *LOCAL_FLAGS]
    )
    out, err = capsys.readouterr()
    assert code == 0, err
    assert out.strip() == str(output_dir.resolve() / "MyData.zip")
    with zipfile.ZipFile(output_dir / "MyData.zip") as archive:
        assert sorted(archive.namelist()) == sorted(f"MyData/{n}" for n in SAMPLE_FILES)


def test_list(torrent_file, capsys):
    code = cli.main([str(torrent_file), "--list", "--include", "*.txt", *LOCAL_FLAGS])
    out, _ = capsys.readouterr()
    assert code == 0
    assert out.splitlines()[0].startswith("MyData  (2 files")
    assert "MyData/sub/b.txt" in out and "MyData/a.bin" not in out


def test_list_magnet(seeder, capsys):
    code = cli.main([seeder.magnet, "--list", "--peer", seeder.peer_arg, "-q", *LOCAL_FLAGS])
    out, err = capsys.readouterr()
    assert code == 0, err
    assert "MyData/video.mp4" in out


def test_errors_are_reported_and_other_sources_continue(seeder, torrent_file, output_dir, capsys):
    code = cli.main(
        [
            "missing.torrent",
            str(torrent_file),
            "-o",
            str(output_dir),
            "--peer",
            seeder.peer_arg,
            "-q",
            *LOCAL_FLAGS,
        ]
    )
    out, err = capsys.readouterr()
    assert code == 1
    assert "error: Torrent file not found: missing.torrent" in err
    assert out.strip().endswith("MyData.zip")


@pytest.mark.parametrize(
    "value, expected",
    [("1.2.3.4:6881", ("1.2.3.4", 6881)), ("[::1]:51413", ("::1", 51413)), ("host:1", ("host", 1))],
)
def test_parse_peer(value, expected):
    assert cli.parse_peer(value) == expected


@pytest.mark.parametrize("value", ["1.2.3.4", ":6881", "host:abc", "host:0", "host:70000"])
def test_parse_peer_rejects(value):
    with pytest.raises(Exception):
        cli.parse_peer(value)


def test_bad_arguments_exit_with_usage_error(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["x.torrent", "--compression", "bzip2", "--level", "0"])
    assert exc.value.code == 2


def test_progress_line():
    line = cli.render_progress(
        Progress(
            Stage.DOWNLOADING, "MyData", 512 * 1024, 1024 * 1024, 2048, 1024, peers=3, seeds=2
        )
    )
    assert line == (
        "Downloading  50.0% [########--------] 512.0 KiB/1.0 MiB"
        "  2.0 KiB/s  ETA 4m 16s  peers 3 (2 seeds)  up 1.0 KiB/s"
    )
    assert len(line.split("  peers")[0]) < 80  # the essentials fit in a small terminal
    assert cli.render_progress(Progress(Stage.METADATA, "x", peers=4)) == (
        "Getting torrent info from peers (4 connected)  x"
    )


def test_progress_printer_redraws_one_line():
    class FakeTerminal(io.StringIO):
        def isatty(self):
            return True

    stream = FakeTerminal()
    printer = cli.ProgressPrinter(stream)
    printer.update(Progress(Stage.STARTING, "abc"))
    printer.update(Progress(Stage.CLEANING, "abc"))
    printer.clear()
    assert "\n" not in stream.getvalue()
    assert stream.getvalue().startswith("\rStarting  abc")
    assert stream.getvalue().endswith("\r")
