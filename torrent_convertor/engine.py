"""Downloading torrents with libtorrent, the engine also used by qBittorrent and Deluge."""

from __future__ import annotations

import logging
import socket
import threading
import time
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Callable, Iterable, List, Optional, Sequence, Tuple

import libtorrent as lt

from . import __version__
from .errors import Cancelled, DownloadError
from .progress import Progress, ProgressCallback, Stage

log = logging.getLogger(__name__)

POLL_INTERVAL = 0.25  # seconds between status checks
RECONNECT_INTERVAL = 10.0  # seconds between attempts to reach manually added peers
ALERT_TIMEOUT = 30.0  # seconds to wait for libtorrent to confirm flushes and removals
DEFAULT_PRIORITY = 4
DONT_DOWNLOAD = 0

DHT_BOOTSTRAP_NODES = ",".join(
    [
        "dht.libtorrent.org:25401",
        "router.bittorrent.com:6881",
        "router.utorrent.com:6881",
        "dht.transmissionbt.com:6881",
    ]
)

_CHECKING_STATES = (
    lt.torrent_status.checking_files,
    lt.torrent_status.checking_resume_data,
    lt.torrent_status.queued_for_checking,
    lt.torrent_status.allocating,
)

# Error alerts that happen all the time on a healthy download (dead trackers,
# unreachable peers, no IPv6, ...). They are only logged at debug level.
_ROUTINE_ERROR_ALERTS = frozenset(
    {
        "dht_error_alert",
        "i2p_alert",
        "listen_failed_alert",
        "lsd_error_alert",
        "peer_disconnected_alert",
        "peer_error_alert",
        "portmap_error_alert",
        "scrape_failed_alert",
        "socks5_alert",
        "tracker_error_alert",
        "tracker_warning_alert",
        "udp_error_alert",
        "url_seed_alert",
    }
)

_ALERT_MASK = int(lt.alert_category.error | lt.alert_category.status | lt.alert_category.storage)


@dataclass(frozen=True)
class TorrentFile:
    index: int  # position in the torrent's file list
    path: str  # "/"-separated path relative to the download folder
    size: int


@dataclass(frozen=True)
class TorrentMetadata:
    name: str
    files: Tuple[TorrentFile, ...]

    @property
    def total_size(self) -> int:
        return sum(f.size for f in self.files)


@dataclass
class SessionConfig:
    port: int = 6881  # 0 lets the operating system pick a free port
    dht: bool = True
    lsd: bool = True  # local service discovery
    upnp: bool = True  # UPnP and NAT-PMP port forwarding
    download_limit: int = 0  # bytes per second, 0 = unlimited
    upload_limit: int = 0  # bytes per second, 0 = unlimited
    listen_interfaces: Optional[str] = None  # overrides ``port``, e.g. "127.0.0.1:0"

    def to_settings(self) -> dict:
        interfaces = self.listen_interfaces or f"0.0.0.0:{self.port},[::]:{self.port}"
        return {
            "user_agent": f"TorrentConvertor/{__version__} libtorrent/{lt.__version__}",
            "listen_interfaces": interfaces,
            "enable_dht": self.dht,
            "dht_bootstrap_nodes": DHT_BOOTSTRAP_NODES,
            "enable_lsd": self.lsd,
            # Connect out over TCP: libtorrent's uTP was 3-7x slower in our measurements.
            # Peers can still connect to us over uTP, so no one becomes unreachable.
            "enable_outgoing_utp": False,
            # Don't keep the user waiting at the end for trackers to acknowledge we left.
            "stop_tracker_timeout": 1,
            "enable_upnp": self.upnp,
            "enable_natpmp": self.upnp,
            "download_rate_limit": self.download_limit,
            "upload_rate_limit": self.upload_limit,
            "alert_mask": _ALERT_MASK,
        }


def file_storage(info: "lt.torrent_info"):
    # libtorrent 2.1 renamed torrent_info.files() to layout().
    return info.layout() if hasattr(info, "layout") else info.files()


def metadata_from_torrent_info(info: "lt.torrent_info") -> TorrentMetadata:
    storage = file_storage(info)
    # Padding files only align data inside the torrent; symlinks carry no data.
    skip = lt.file_storage.flag_pad_file | lt.file_storage.flag_symlink
    files = tuple(
        TorrentFile(index, PurePath(storage.file_path(index)).as_posix(), storage.file_size(index))
        for index in range(storage.num_files())
        if not storage.file_flags(index) & skip
    )
    return TorrentMetadata(info.name(), files)


def _hash_key(hashes) -> str:
    return str(hashes.v1) if hashes.has_v1() else str(hashes.v2)


class Session:
    """A libtorrent session. Use it as a context manager so it is shut down cleanly."""

    def __init__(self, config: Optional[SessionConfig] = None):
        self.config = config or SessionConfig()
        self._ses = lt.session(self.config.to_settings())
        self.last_error = ""

    def __enter__(self) -> "Session":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def close(self) -> None:
        # Dropping the last reference shuts libtorrent down (this tells trackers we left).
        self._ses = None

    def add(
        self,
        params: "lt.add_torrent_params",
        save_path: Path,
        *,
        select_files_later: bool = False,
        peers: Iterable[Tuple[str, int]] = (),
    ) -> "Download":
        """Start downloading into ``save_path``.

        With ``select_files_later`` nothing is downloaded until :meth:`Download.select`
        is called, which is needed to pick files from a magnet link before its file
        list is known.
        """
        self.last_error = ""
        params.save_path = str(save_path)
        # Not auto-managed, so libtorrent's queue never pauses or resumes it behind our back.
        flags = params.flags & ~(lt.torrent_flags.auto_managed | lt.torrent_flags.paused)
        if select_files_later:
            flags |= lt.torrent_flags.default_dont_download
        params.flags = flags
        try:
            handle = self._ses.add_torrent(params)
        except RuntimeError as exc:
            raise DownloadError(f"Could not start the download: {exc}") from None
        return Download(self, handle, peers)

    def process_alerts(self) -> list:
        alerts = self._ses.pop_alerts()
        for alert in alerts:
            self._log_alert(alert)
        return alerts

    def wait_for_alert(
        self, alert_types: tuple, match: Callable[[object], bool], timeout: float = ALERT_TIMEOUT
    ):
        """Process alerts until one of ``alert_types`` satisfying ``match`` arrives."""
        deadline = time.monotonic() + timeout
        while True:
            for alert in self.process_alerts():
                if isinstance(alert, alert_types) and match(alert):
                    return alert
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            self._ses.wait_for_alert(int(min(remaining, 0.5) * 1000))

    def _log_alert(self, alert) -> None:
        name = type(alert).__name__
        message = alert.message()
        if name == "metadata_received_alert":
            log.info("Received the torrent's file list.")
        elif int(alert.category()) & int(lt.alert_category.error):
            if name in _ROUTINE_ERROR_ALERTS:
                log.debug("%s", message)
            else:
                self.last_error = message
                log.warning("%s", message)
        else:
            log.debug("%s", message)


class Download:
    """One torrent in a :class:`Session`."""

    def __init__(self, session: Session, handle, peers: Iterable[Tuple[str, int]] = ()):
        self._session = session
        self._handle = handle
        self._peers = _resolve_peers(peers)
        self._last_connect: Optional[float] = None
        self.removed = False

    def wait_for_metadata(
        self, on_progress: Optional[ProgressCallback], cancel: threading.Event
    ) -> TorrentMetadata:
        """Wait until the file list is known (immediately for .torrent files)."""
        while True:
            status = self._poll(cancel)
            if status.has_metadata:
                return metadata_from_torrent_info(self._handle.torrent_file())
            _emit(
                on_progress,
                Progress(
                    Stage.METADATA,
                    status.name,
                    download_rate=status.download_rate,
                    upload_rate=status.upload_rate,
                    peers=status.num_peers,
                    seeds=status.num_seeds,
                ),
            )
            cancel.wait(POLL_INTERVAL)

    def select(self, files: Sequence[TorrentFile]) -> None:
        """Download only ``files``."""
        wanted = {f.index for f in files}
        count = file_storage(self._handle.torrent_file()).num_files()
        self._handle.prioritize_files(
            [DEFAULT_PRIORITY if index in wanted else DONT_DOWNLOAD for index in range(count)]
        )

    def wait_until_complete(
        self,
        files: Sequence[TorrentFile],
        on_progress: Optional[ProgressCallback],
        cancel: threading.Event,
    ) -> None:
        """Wait until every file in ``files`` is downloaded and verified."""
        total = sum(f.size for f in files)
        while True:
            status = self._poll(cancel)
            checking = status.state in _CHECKING_STATES
            # Counts only pieces that passed the hash check.
            have = self._handle.file_progress(lt.torrent_handle.piece_granularity)
            done = sum(min(have[f.index], f.size) for f in files)
            if done >= total and not checking:
                break
            if checking:
                progress = Progress(Stage.CHECKING, status.name, int(status.progress * total), total)
            else:
                progress = Progress(
                    Stage.DOWNLOADING,
                    status.name,
                    done,
                    total,
                    download_rate=status.download_payload_rate,
                    upload_rate=status.upload_payload_rate,
                    peers=status.num_peers,
                    seeds=status.num_seeds,
                )
            _emit(on_progress, progress)
            cancel.wait(POLL_INTERVAL)
        _emit(on_progress, Progress(Stage.DOWNLOADING, status.name, total, total))

    def finish(self) -> None:
        """Stop transferring and wait until everything downloaded is written to disk.

        A piece counts as downloaded once its hash is verified, which can happen before
        libtorrent has finished writing it. libtorrent sends torrent_paused_alert only
        after all disk I/O of the paused torrent is complete and its files are closed.
        """
        self._handle.pause()
        alert = self._session.wait_for_alert(
            (lt.torrent_paused_alert,), lambda a: a.handle == self._handle
        )
        if alert is None:
            raise DownloadError("libtorrent did not finish writing the downloaded files to disk.")

    def remove(self, delete_files: bool = False) -> None:
        """Remove the torrent from the session, optionally deleting its downloaded files."""
        if self.removed:
            return
        self.removed = True
        try:
            key = _hash_key(self._handle.info_hashes())
        except RuntimeError:  # the handle is no longer valid
            return
        ses = self._session._ses
        if delete_files:
            ses.remove_torrent(self._handle, lt.session.delete_files)
            expected = (lt.torrent_deleted_alert, lt.torrent_delete_failed_alert)
        else:
            ses.remove_torrent(self._handle)
            expected = (lt.torrent_removed_alert,)
        alert = self._session.wait_for_alert(expected, lambda a: _hash_key(a.info_hashes) == key)
        if isinstance(alert, lt.torrent_delete_failed_alert):
            log.warning("Could not delete the downloaded files: %s", alert.message())
        elif alert is None:
            log.warning("libtorrent did not confirm that the torrent was removed")

    def _poll(self, cancel: threading.Event):
        if cancel.is_set():
            raise Cancelled()
        self._session.process_alerts()
        status = self._handle.status()
        if status.errc.value():
            detail = self._session.last_error or status.errc.message()
            raise DownloadError(f"Download failed: {detail}")
        if status.upload_mode:
            # We never request upload mode, so libtorrent entered it after a disk write failed.
            detail = self._session.last_error or "could not write to disk"
            raise DownloadError(f"Download failed: {detail}")
        self._connect_peers(status)
        return status

    def _connect_peers(self, status) -> None:
        if not self._peers or status.state in _CHECKING_STATES:
            return
        now = time.monotonic()
        if self._last_connect is not None and (
            status.num_peers or now - self._last_connect < RECONNECT_INTERVAL
        ):
            return
        self._last_connect = now
        for endpoint in self._peers:
            self._handle.connect_peer(endpoint)


def _resolve_peers(peers: Iterable[Tuple[str, int]]) -> List[Tuple[str, int]]:
    # libtorrent only accepts IP addresses here.
    endpoints = []
    for host, port in peers:
        try:
            address = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)[0][4][0]
        except (OSError, IndexError) as exc:
            log.warning("Ignoring peer %s:%s: %s", host, port, exc)
            continue
        endpoints.append((address, port))
    return endpoints


def _emit(on_progress: Optional[ProgressCallback], progress: Progress) -> None:
    if on_progress is not None:
        on_progress(progress)
