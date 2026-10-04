"""Packing downloaded files into a zip archive."""

from __future__ import annotations

import os
import threading
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

from .errors import ArchiveError, Cancelled
from .progress import Progress, ProgressCallback, Stage

COMPRESSION_METHODS = {
    "auto": zipfile.ZIP_DEFLATED,
    "deflate": zipfile.ZIP_DEFLATED,
    "store": zipfile.ZIP_STORED,
    "bzip2": zipfile.ZIP_BZIP2,
    "lzma": zipfile.ZIP_LZMA,
}
COMPRESSIONS = tuple(COMPRESSION_METHODS)

# Formats that are already compressed. In "auto" mode they are stored as-is,
# because deflating them again costs a lot of time and saves almost nothing.
ALREADY_COMPRESSED = frozenset(
    """
    7z aac apk avi avif bz2 cab cbr cbz deb dmg docx epub flac flv gif gz heic iso
    jar jpeg jpg lz lzma m4a m4b m4v mkv mov mp3 mp4 mpeg mpg odp ods odt ogg ogv opus
    png pptx rar rpm tgz txz webm webp wma wmv xlsx xz zip zst
    """.split()
)

CHUNK_SIZE = 1024 * 1024
REPORT_INTERVAL = 0.2  # seconds between progress reports


@dataclass(frozen=True)
class ZipEntry:
    source: Path  # file on disk
    arcname: str  # "/"-separated path inside the zip
    size: int  # expected size in bytes


def create_zip(
    entries: Sequence[ZipEntry],
    destination: Path,
    *,
    compression: str = "auto",
    level: Optional[int] = None,
    name: str = "",
    on_progress: Optional[ProgressCallback] = None,
    cancel: Optional[threading.Event] = None,
) -> Path:
    """Write ``entries`` to the zip file ``destination``.

    The archive is written to ``<destination>.part`` and renamed when complete,
    so an interrupted run never leaves a truncated file behind that looks finished.
    """
    if compression not in COMPRESSION_METHODS:
        raise ValueError(f"unknown compression {compression!r}")
    destination = Path(destination)
    partial = destination.with_name(destination.name + ".part")
    total = sum(entry.size for entry in entries)
    done = 0
    last_report = 0.0

    def report(force: bool = False) -> None:
        nonlocal last_report
        now = time.monotonic()
        if on_progress is not None and (force or now - last_report >= REPORT_INTERVAL):
            last_report = now
            on_progress(Progress(Stage.ZIPPING, name, done, total))

    try:
        with zipfile.ZipFile(partial, "w", allowZip64=True) as archive:
            for entry in entries:
                if cancel is not None and cancel.is_set():
                    raise Cancelled()
                info = _zip_info(entry, compression, level)
                with archive.open(info, "w") as dest:
                    if entry.size:
                        with open(entry.source, "rb") as src:
                            remaining = entry.size
                            while remaining:
                                if cancel is not None and cancel.is_set():
                                    raise Cancelled()
                                chunk = src.read(min(CHUNK_SIZE, remaining))
                                if not chunk:
                                    raise ArchiveError(f"{entry.source} is shorter than expected")
                                dest.write(chunk)
                                remaining -= len(chunk)
                                done += len(chunk)
                                report()
        os.replace(partial, destination)
    except BaseException as exc:
        try:
            partial.unlink()
        except OSError:
            pass
        if isinstance(exc, OSError):
            raise ArchiveError(f"Could not create {destination}: {exc}") from exc
        raise
    report(force=True)
    return destination


def _zip_info(entry: ZipEntry, compression: str, level: Optional[int]) -> zipfile.ZipInfo:
    try:
        actual_size = entry.source.stat().st_size
    except FileNotFoundError:
        if entry.size:
            raise ArchiveError(f"Downloaded file is missing: {entry.source}") from None
        # libtorrent does not always create empty files; they need no data anyway.
        info = zipfile.ZipInfo(entry.arcname, time.localtime()[:6])
        info.external_attr = 0o644 << 16
    else:
        if actual_size != entry.size:
            raise ArchiveError(
                f"Downloaded file is incomplete: {entry.source} "
                f"({actual_size} of {entry.size} bytes)"
            )
        info = zipfile.ZipInfo.from_file(entry.source, entry.arcname, strict_timestamps=False)

    method = COMPRESSION_METHODS[compression]
    if compression == "auto" and _is_compressed_format(entry.arcname):
        method = zipfile.ZIP_STORED
    info.compress_type = method
    if level is not None and method in (zipfile.ZIP_DEFLATED, zipfile.ZIP_BZIP2):
        # ZipFile.open() ignores the archive-wide level for ZipInfo objects.
        # (Python 3.13 renamed this to compress_level and kept the old name as an alias.)
        info._compresslevel = level
    return info


def _is_compressed_format(arcname: str) -> bool:
    _, dot, extension = arcname.rpartition(".")
    return bool(dot) and "/" not in extension and extension.lower() in ALREADY_COMPRESSED


def safe_filename(name: str, default: str = "torrent") -> str:
    """Turn a torrent name into a file name that is valid on Windows, macOS and Linux."""
    cleaned = "".join("_" if ch in '<>:"/\\|?*' or ord(ch) < 32 else ch for ch in name)
    cleaned = cleaned.strip().rstrip(". ")[:200].rstrip(". ")
    if not cleaned:
        return default
    reserved = {"CON", "PRN", "AUX", "NUL"} | {f"{p}{i}" for p in ("COM", "LPT") for i in range(1, 10)}
    if cleaned.split(".")[0].upper() in reserved:
        cleaned = f"_{cleaned}"
    return cleaned


def unique_path(path: Path) -> Path:
    """Return ``path``, or ``name (1).ext``, ``name (2).ext``... if it already exists."""
    candidate = path
    counter = 1
    while candidate.exists() or candidate.with_name(candidate.name + ".part").exists():
        candidate = path.with_name(f"{path.stem} ({counter}){path.suffix}")
        counter += 1
    return candidate
