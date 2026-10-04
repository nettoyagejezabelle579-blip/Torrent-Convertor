"""Torrent to zip conversion: download the torrent, zip its files, clean up."""

from __future__ import annotations

import logging
import os
import tempfile
import threading
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from .archive import ZipEntry, create_zip, safe_filename, unique_path
from .engine import Session, TorrentFile, TorrentMetadata, metadata_from_torrent_info
from .errors import ConvertorError
from .progress import Progress, ProgressCallback, Stage, format_size
from .sources import display_name, info_hash_hex, load_source

log = logging.getLogger(__name__)

# Partial downloads are kept here (inside the output folder) so that running the
# same conversion again resumes instead of starting over.
WORK_DIR_NAME = ".torrent-convertor"


@dataclass
class ConvertOptions:
    output_dir: Path = Path(".")
    # Download into this folder and keep the files there. By default the files are
    # downloaded into a work folder inside output_dir and deleted after zipping.
    download_dir: Optional[Path] = None
    keep_files: bool = False  # keep the downloaded files next to the zip
    include: Sequence[str] = ()  # only download files matching one of these patterns
    exclude: Sequence[str] = ()  # never download files matching one of these patterns
    compression: str = "auto"
    level: Optional[int] = None
    peers: Sequence[Tuple[str, int]] = ()  # extra peers to connect to


def select_files(
    files: Sequence[TorrentFile], include: Sequence[str] = (), exclude: Sequence[str] = ()
) -> List[TorrentFile]:
    """Filter files with shell-style patterns (``*.mkv``, ``Season 1/*``).

    A pattern matches a file if it matches the end of its path inside the torrent:
    ``Show/Season 1/ep1.mkv`` is matched by ``*.mkv``, ``ep1.mkv``, ``Season 1/*`` and
    ``Show/*``. Matching ignores case.
    """

    def matches(file: TorrentFile, patterns: Sequence[str]) -> bool:
        parts = file.path.lower().split("/")
        tails = ["/".join(parts[i:]) for i in range(len(parts))]
        return any(fnmatchcase(tail, p.lower()) for p in patterns for tail in tails)

    return [
        f for f in files if (not include or matches(f, include)) and not matches(f, exclude)
    ]


def inspect(
    session: Session,
    source: str,
    *,
    peers: Sequence[Tuple[str, int]] = (),
    on_progress: Optional[ProgressCallback] = None,
    cancel: Optional[threading.Event] = None,
) -> TorrentMetadata:
    """Return the name and file list of a torrent without downloading its contents."""
    params = load_source(source)
    if params.ti is not None:
        return metadata_from_torrent_info(params.ti)
    # A magnet link only contains the info-hash; the file list has to come from peers.
    with tempfile.TemporaryDirectory(prefix="torrent-convertor-") as scratch:
        download = session.add(params, Path(scratch), select_files_later=True, peers=peers)
        try:
            return download.wait_for_metadata(on_progress, cancel or threading.Event())
        finally:
            download.remove()


def convert(
    session: Session,
    source: str,
    options: ConvertOptions,
    *,
    on_progress: Optional[ProgressCallback] = None,
    cancel: Optional[threading.Event] = None,
) -> Path:
    """Download ``source`` and pack its files into a zip. Returns the zip's path."""
    cancel = cancel or threading.Event()
    report = on_progress or (lambda progress: None)

    params = load_source(source)
    output_dir = Path(options.output_dir).expanduser().resolve()
    work_dir = None  # set when the files are downloaded into our own work folder
    if options.download_dir is not None:
        save_path = Path(options.download_dir).expanduser().resolve()
    elif options.keep_files:
        save_path = output_dir
    else:
        work_dir = output_dir / WORK_DIR_NAME
        save_path = work_dir / info_hash_hex(params)
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        save_path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ConvertorError(f"Could not create folder {exc.filename}: {exc.strerror}") from None

    report(Progress(Stage.STARTING, display_name(params)))
    selective = bool(options.include or options.exclude)
    download = session.add(params, save_path, select_files_later=selective, peers=options.peers)
    try:
        metadata = download.wait_for_metadata(report, cancel)
        files = select_files(metadata.files, options.include, options.exclude)
        if not files:
            if selective:
                raise ConvertorError("No files in the torrent match the include/exclude patterns.")
            raise ConvertorError("The torrent does not contain any files.")
        if selective:
            download.select(files)
        log.info(
            "Downloading %d file(s), %s, into %s",
            len(files),
            format_size(sum(f.size for f in files)),
            save_path,
        )
        download.wait_until_complete(files, report, cancel)
        download.finish()
        log.info("Download complete.")

        zip_path = unique_path(output_dir / f"{safe_filename(metadata.name)}.zip")
        create_zip(
            [ZipEntry(save_path / f.path, f.path, f.size) for f in files],
            zip_path,
            compression=options.compression,
            level=options.level,
            name=metadata.name,
            on_progress=report,
            cancel=cancel,
        )
        log.info("Created %s", zip_path)
        report(Progress(Stage.CLEANING, metadata.name))
        # Only delete files from our own work folder: a folder the user chose may hold
        # files they already had before.
        download.remove(delete_files=work_dir is not None)
    finally:
        # On failure or cancel, keep what was downloaded so the next run can resume.
        download.remove()

    if work_dir is not None:
        _remove_empty_dirs(save_path, work_dir)
    total = sum(f.size for f in files)
    report(Progress(Stage.DONE, metadata.name, total, total))
    return zip_path


def _remove_empty_dirs(*paths: Path) -> None:
    for path in paths:
        try:
            os.rmdir(path)
        except OSError:
            return
