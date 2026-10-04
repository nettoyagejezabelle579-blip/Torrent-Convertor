"""Command line interface: ``torrent-convertor FILE.torrent``."""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
import time
from pathlib import Path
from typing import List, Optional, TextIO, Tuple

from . import __version__
from .archive import COMPRESSIONS
from .errors import Cancelled, ConvertorError
from .progress import Progress, Stage, format_eta, format_rate, format_size

BAR_WIDTH = 16


def parse_peer(value: str) -> Tuple[str, int]:
    """Parse ``HOST:PORT`` or ``[IPv6]:PORT``."""
    host, sep, port = value.rpartition(":")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if not sep or not host or not port.isdigit() or not 0 < int(port) < 65536:
        raise argparse.ArgumentTypeError(f"expected HOST:PORT, got {value!r}")
    return host, int(port)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="torrent-convertor",
        description="Download a torrent and pack its contents into a ZIP file.",
        epilog="Example: torrent-convertor ubuntu.torrent -o ~/Downloads",
    )
    parser.add_argument(
        "sources",
        nargs="+",
        metavar="TORRENT",
        help="a .torrent file, a magnet link (in quotes) or an http(s) link to a .torrent file",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        default=Path("."),
        metavar="DIR",
        help="folder to save the zip file in (default: current folder)",
    )
    parser.add_argument(
        "--download-dir",
        type=Path,
        metavar="DIR",
        help="download the torrent's files into DIR and keep them there "
        "(default: a temporary folder inside the output folder)",
    )
    parser.add_argument(
        "-k",
        "--keep-files",
        action="store_true",
        help="keep the downloaded files after creating the zip",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="only show the files in the torrent, don't download anything",
    )

    selection = parser.add_argument_group("choosing files")
    selection.add_argument(
        "-i",
        "--include",
        action="append",
        default=[],
        metavar="PATTERN",
        help="only download files matching PATTERN, e.g. '*.mkv' (can be repeated)",
    )
    selection.add_argument(
        "-x",
        "--exclude",
        action="append",
        default=[],
        metavar="PATTERN",
        help="skip files matching PATTERN, e.g. '*sample*' (can be repeated)",
    )

    archive = parser.add_argument_group("zip options")
    archive.add_argument(
        "-c",
        "--compression",
        choices=COMPRESSIONS,
        default="auto",
        help="'auto' (default) is fast: it compresses quickly and stores files that "
        "don't shrink, like videos, images and archives. 'deflate' makes smaller zips",
    )
    archive.add_argument(
        "--level",
        type=int,
        choices=range(10),
        metavar="0-9",
        help="compression level for deflate/bzip2 (higher = smaller but slower)",
    )

    network = parser.add_argument_group("network options")
    network.add_argument(
        "--port",
        type=int,
        default=6881,
        help="port for incoming connections (default: 6881, 0 = random)",
    )
    network.add_argument(
        "--max-download", type=float, default=0, metavar="KiB/s", help="download speed limit"
    )
    network.add_argument(
        "--max-upload", type=float, default=0, metavar="KiB/s", help="upload speed limit"
    )
    network.add_argument(
        "--peer",
        action="append",
        default=[],
        type=parse_peer,
        metavar="HOST:PORT",
        help="also connect to this peer (can be repeated)",
    )
    network.add_argument("--no-dht", action="store_true", help="disable DHT")
    network.add_argument("--no-lsd", action="store_true", help="disable local peer discovery")
    network.add_argument(
        "--no-upnp", action="store_true", help="disable UPnP/NAT-PMP port forwarding"
    )

    parser.add_argument("-q", "--quiet", action="store_true", help="don't show progress")
    parser.add_argument(
        "-v", "--verbose", action="count", default=0, help="show more details (-vv for debugging)"
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


class ProgressPrinter:
    """Shows a single, constantly updated progress line on a terminal."""

    def __init__(self, stream: TextIO, enabled: bool = True):
        self.stream = stream
        self.enabled = enabled
        self.interactive = stream.isatty()
        self._line_length = 0
        self._last_print = 0.0
        self._last_stage: Optional[Stage] = None

    def update(self, progress: Progress) -> None:
        if not self.enabled:
            return
        now = time.monotonic()
        interval = 0.25 if self.interactive else 10.0
        if progress.stage is self._last_stage and now - self._last_print < interval:
            return
        self._last_stage = progress.stage
        self._last_print = now
        line = render_progress(progress)
        if self.interactive:
            width = shutil.get_terminal_size().columns - 1
            line = line[:width]
            self.stream.write("\r" + line.ljust(self._line_length))
            self._line_length = len(line)
        else:
            self.stream.write(line + "\n")
        self.stream.flush()

    def clear(self) -> None:
        """Erase the progress line so that a message can be printed."""
        if self._line_length:
            self.stream.write("\r" + " " * self._line_length + "\r")
            self.stream.flush()
            self._line_length = 0
            self._last_print = 0.0  # redraw on the next update


# Short labels so that the important numbers fit in an 80 column terminal.
_LABELS = {Stage.CHECKING: "Checking", Stage.DOWNLOADING: "Downloading", Stage.ZIPPING: "Zipping"}


def render_progress(progress: Progress) -> str:
    stage = progress.stage
    name = progress.name if len(progress.name) <= 40 else progress.name[:37] + "..."
    if stage is Stage.METADATA:
        return f"{stage.value} from peers ({progress.peers} connected)  {name}"
    if stage in _LABELS:
        filled = int(progress.fraction * BAR_WIDTH)
        bar = "#" * filled + "-" * (BAR_WIDTH - filled)
        line = (
            f"{_LABELS[stage]:<11} {progress.fraction * 100:5.1f}% [{bar}] "
            f"{format_size(progress.done)}/{format_size(progress.total)}"
        )
        if stage is Stage.DOWNLOADING:
            line += (
                f"  {format_rate(progress.download_rate)}"
                f"  ETA {format_eta(progress.eta)}"
                f"  peers {progress.peers} ({progress.seeds} seeds)"
                f"  up {format_rate(progress.upload_rate)}"
            )
        return line
    return f"{stage.value}  {name}"


class _ConsoleLogHandler(logging.Handler):
    def __init__(self, printer: ProgressPrinter):
        super().__init__()
        self.printer = printer

    def emit(self, record: logging.LogRecord) -> None:
        self.printer.clear()
        self.printer.stream.write(self.format(record) + "\n")
        self.printer.stream.flush()


def print_listing(metadata, files) -> None:
    total = sum(f.size for f in files)
    print(f"{metadata.name}  ({len(files)} files, {format_size(total)})")
    for f in files:
        print(f"  {format_size(f.size):>11}  {f.path}")


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.compression == "bzip2" and args.level == 0:
        parser.error("bzip2 compression levels go from 1 to 9")

    printer = ProgressPrinter(sys.stderr, enabled=not args.quiet)
    handler = _ConsoleLogHandler(printer)
    handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    package_logger = logging.getLogger("torrent_convertor")
    previous_level = package_logger.level
    package_logger.addHandler(handler)
    package_logger.setLevel(
        logging.DEBUG if args.verbose > 1 else logging.INFO if args.verbose else logging.WARNING
    )
    try:
        return _run(args, printer)
    finally:
        package_logger.removeHandler(handler)
        package_logger.setLevel(previous_level)


def _run(args: argparse.Namespace, printer: ProgressPrinter) -> int:
    try:
        from .converter import ConvertOptions, convert, inspect, select_files
        from .engine import Session, SessionConfig
    except ImportError as exc:
        print(
            f"error: libtorrent could not be loaded ({exc}).\n"
            "Install it with:  pip install libtorrent",
            file=sys.stderr,
        )
        return 1

    config = SessionConfig(
        port=args.port,
        dht=not args.no_dht,
        lsd=not args.no_lsd,
        upnp=not args.no_upnp,
        download_limit=int(args.max_download * 1024),
        upload_limit=int(args.max_upload * 1024),
    )
    options = ConvertOptions(
        output_dir=args.output_dir,
        download_dir=args.download_dir,
        keep_files=args.keep_files,
        include=args.include,
        exclude=args.exclude,
        compression=args.compression,
        level=args.level,
        peers=args.peer,
    )
    failures = 0
    try:
        with Session(config) as session:
            for source in args.sources:
                try:
                    if args.list:
                        metadata = inspect(
                            session, source, peers=args.peer, on_progress=printer.update
                        )
                        printer.clear()
                        print_listing(
                            metadata, select_files(metadata.files, args.include, args.exclude)
                        )
                        continue
                    zip_path = convert(session, source, options, on_progress=printer.update)
                except (ConvertorError, OSError) as exc:
                    printer.clear()
                    print(f"error: {exc}", file=sys.stderr)
                    failures += 1
                    continue
                printer.clear()
                print(zip_path)
    except (KeyboardInterrupt, Cancelled):
        printer.clear()
        print(
            "Cancelled. What was already downloaded has been kept; "
            "run the same command again to continue.",
            file=sys.stderr,
        )
        return 130
    return 1 if failures else 0
