"""Progress reporting shared by the command line and the desktop window."""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Callable, Optional


class Stage(enum.Enum):
    STARTING = "Starting"
    METADATA = "Getting torrent info"
    CHECKING = "Checking files"
    DOWNLOADING = "Downloading"
    ZIPPING = "Creating zip"
    CLEANING = "Cleaning up"
    DONE = "Done"


@dataclass(frozen=True)
class Progress:
    stage: Stage
    name: str = ""
    done: int = 0  # bytes
    total: int = 0  # bytes
    download_rate: int = 0  # bytes per second
    upload_rate: int = 0  # bytes per second
    peers: int = 0
    seeds: int = 0

    @property
    def fraction(self) -> float:
        if self.total <= 0:
            return 1.0 if self.stage is Stage.DONE else 0.0
        return min(self.done / self.total, 1.0)

    @property
    def eta(self) -> Optional[float]:
        """Estimated seconds until the download finishes, or None if unknown."""
        if self.stage is not Stage.DOWNLOADING or self.download_rate <= 0:
            return None
        return max(self.total - self.done, 0) / self.download_rate


ProgressCallback = Callable[[Progress], None]


def format_size(num_bytes: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(num_bytes) < 1024:
            return f"{num_bytes:.0f} {unit}" if unit == "B" else f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} TiB"


def format_rate(bytes_per_second: float) -> str:
    return f"{format_size(bytes_per_second)}/s"


def format_eta(seconds: Optional[float]) -> str:
    if seconds is None:
        return "--"
    seconds = int(round(seconds))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"
