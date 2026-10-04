"""Loading torrents from .torrent files, magnet links and URLs."""

from __future__ import annotations

import urllib.error
import urllib.request
from pathlib import Path

import libtorrent as lt

from . import __version__
from .errors import SourceError

# Real .torrent files are at most a few megabytes; this guards against
# accidentally reading something huge.
MAX_TORRENT_FILE_SIZE = 50 * 1024 * 1024
URL_TIMEOUT = 30  # seconds


def is_magnet(source: str) -> bool:
    return source.strip().lower().startswith("magnet:")


def is_url(source: str) -> bool:
    return source.strip().lower().startswith(("http://", "https://"))


def load_source(source: str) -> "lt.add_torrent_params":
    """Turn a .torrent path, magnet link or http(s) URL into libtorrent add parameters."""
    source = source.strip()
    if not source:
        raise SourceError("No torrent given.")
    if is_magnet(source):
        try:
            return lt.parse_magnet_uri(source)
        except RuntimeError as exc:
            raise SourceError(f"Invalid magnet link: {exc}") from None

    data = _download(source) if is_url(source) else _read_file(source)
    try:
        info = lt.torrent_info(data)
    except RuntimeError as exc:
        raise SourceError(f"Not a valid .torrent file: {source} ({exc})") from None
    params = lt.add_torrent_params()
    params.ti = info
    return params


def _read_file(source: str) -> bytes:
    path = Path(source).expanduser()
    if path.is_dir():
        raise SourceError(f"{source} is a folder, not a .torrent file.")
    try:
        if path.stat().st_size > MAX_TORRENT_FILE_SIZE:
            raise SourceError(f"{source} is too large to be a .torrent file.")
        # Read the bytes ourselves: libtorrent can mis-handle non-ASCII paths on Windows.
        return path.read_bytes()
    except FileNotFoundError:
        raise SourceError(f"Torrent file not found: {source}") from None
    except OSError as exc:
        raise SourceError(f"Could not read {source}: {exc.strerror or exc}") from None


def _download(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": f"TorrentConvertor/{__version__}"})
    try:
        with urllib.request.urlopen(request, timeout=URL_TIMEOUT) as response:
            data = response.read(MAX_TORRENT_FILE_SIZE + 1)
    except urllib.error.HTTPError as exc:
        raise SourceError(f"Could not download {url}: HTTP {exc.code} {exc.reason}") from None
    except urllib.error.URLError as exc:
        raise SourceError(f"Could not download {url}: {exc.reason}") from None
    except (OSError, ValueError) as exc:
        raise SourceError(f"Could not download {url}: {exc}") from None
    if len(data) > MAX_TORRENT_FILE_SIZE:
        raise SourceError(f"{url} is too large to be a .torrent file.")
    return data


def info_hash_hex(params: "lt.add_torrent_params") -> str:
    """A stable id for the torrent, used to name its temporary download folder."""
    hashes = params.ti.info_hashes() if params.ti is not None else params.info_hashes
    return str(hashes.v1) if hashes.has_v1() else str(hashes.v2)


def display_name(params: "lt.add_torrent_params") -> str:
    """The torrent's name, if known before its metadata has been downloaded."""
    return params.ti.name() if params.ti is not None else params.name
