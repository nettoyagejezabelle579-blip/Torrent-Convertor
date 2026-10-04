"""Desktop window for Torrent Convertor (built with Tkinter, which comes with Python)."""

from __future__ import annotations

import logging
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path
from typing import Optional, Sequence, Tuple

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from . import __version__
from .archive import COMPRESSIONS
from .errors import Cancelled, ConvertorError
from .progress import Progress, Stage, format_eta, format_rate, format_size

log = logging.getLogger(__name__)

POLL_MS = 100


class _QueueLogHandler(logging.Handler):
    def __init__(self, events: "queue.Queue"):
        super().__init__()
        self.events = events

    def emit(self, record: logging.LogRecord) -> None:
        self.events.put(("log", self.format(record)))


def default_output_dir() -> Path:
    downloads = Path.home() / "Downloads"
    return downloads if downloads.is_dir() else Path.home()


class App:
    def __init__(
        self,
        root: tk.Tk,
        source: str = "",
        *,
        session_config=None,
        peers: Sequence[Tuple[str, int]] = (),
    ):
        self.root = root
        self.session_config = session_config
        self.peers = list(peers)
        self.events: "queue.Queue" = queue.Queue()
        self.cancel = threading.Event()
        self.worker: Optional[threading.Thread] = None
        self.result: Optional[Path] = None
        self.closing = False
        self.destroyed = False

        self.source = tk.StringVar(value=source)
        self.output_dir = tk.StringVar(value=str(default_output_dir()))
        self.compression = tk.StringVar(value="auto")
        self.keep_files = tk.BooleanVar(value=False)
        self.name = tk.StringVar(value="-")
        self.status = tk.StringVar(value="Choose a .torrent file or paste a magnet link.")
        self.stats = tk.StringVar(value="")

        self.log_handler = _QueueLogHandler(self.events)
        self.log_handler.setFormatter(logging.Formatter("%(message)s"))
        package_logger = logging.getLogger("torrent_convertor")
        package_logger.addHandler(self.log_handler)
        if package_logger.getEffectiveLevel() > logging.INFO:
            package_logger.setLevel(logging.INFO)  # show progress messages in the log box

        root.title("Torrent Convertor")
        root.minsize(560, 420)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._build()
        root.after(POLL_MS, self._poll_events)

    def _build(self) -> None:
        frame = ttk.Frame(self.root, padding=12)
        frame.grid(sticky="nsew")
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        frame.columnconfigure(1, weight=1)

        ttk.Label(frame, text="Torrent:").grid(row=0, column=0, sticky="w")
        self.source_entry = ttk.Entry(frame, textvariable=self.source)
        self.source_entry.grid(row=0, column=1, sticky="ew", padx=6)
        self.browse_button = ttk.Button(frame, text="Open .torrent...", command=self.browse_torrent)
        self.browse_button.grid(row=0, column=2, sticky="ew")
        ttk.Label(
            frame, text="A .torrent file, a magnet link or a link to a .torrent file", foreground="gray"
        ).grid(row=1, column=1, columnspan=2, sticky="w", padx=6, pady=(0, 8))

        ttk.Label(frame, text="Save zip in:").grid(row=2, column=0, sticky="w")
        self.output_entry = ttk.Entry(frame, textvariable=self.output_dir)
        self.output_entry.grid(row=2, column=1, sticky="ew", padx=6)
        self.output_button = ttk.Button(frame, text="Browse...", command=self.browse_output)
        self.output_button.grid(row=2, column=2, sticky="ew")

        options = ttk.Frame(frame)
        options.grid(row=3, column=1, columnspan=2, sticky="w", padx=6, pady=8)
        ttk.Label(options, text="Compression:").pack(side="left")
        self.compression_box = ttk.Combobox(
            options, textvariable=self.compression, values=COMPRESSIONS, state="readonly", width=8
        )
        self.compression_box.pack(side="left", padx=(4, 16))
        self.keep_check = ttk.Checkbutton(
            options, text="Keep downloaded files", variable=self.keep_files
        )
        self.keep_check.pack(side="left")

        buttons = ttk.Frame(frame)
        buttons.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(0, 8))
        self.start_button = ttk.Button(buttons, text="Convert to ZIP", command=self.start)
        self.start_button.pack(side="left")
        self.cancel_button = ttk.Button(
            buttons, text="Cancel", command=self.request_cancel, state="disabled"
        )
        self.cancel_button.pack(side="left", padx=6)
        self.open_button = ttk.Button(
            buttons, text="Show zip in folder", command=self.open_output, state="disabled"
        )
        self.open_button.pack(side="right")

        ttk.Separator(frame).grid(row=5, column=0, columnspan=3, sticky="ew", pady=4)
        ttk.Label(frame, text="Name:").grid(row=6, column=0, sticky="w")
        ttk.Label(frame, textvariable=self.name).grid(row=6, column=1, columnspan=2, sticky="w", padx=6)
        ttk.Label(frame, text="Status:").grid(row=7, column=0, sticky="w")
        ttk.Label(frame, textvariable=self.status).grid(row=7, column=1, columnspan=2, sticky="w", padx=6)
        self.progress_bar = ttk.Progressbar(frame, maximum=1000)
        self.progress_bar.grid(row=8, column=0, columnspan=3, sticky="ew", pady=6)
        ttk.Label(frame, textvariable=self.stats).grid(row=9, column=0, columnspan=3, sticky="w")

        self.log_box = ScrolledText(frame, height=8, state="disabled", wrap="word")
        self.log_box.grid(row=10, column=0, columnspan=3, sticky="nsew", pady=(8, 0))
        frame.rowconfigure(10, weight=1)

    # -- user actions ------------------------------------------------------------

    def browse_torrent(self) -> None:
        path = filedialog.askopenfilename(
            title="Open torrent",
            filetypes=[("Torrent files", "*.torrent"), ("All files", "*.*")],
        )
        if path:
            self.source.set(path)

    def browse_output(self) -> None:
        path = filedialog.askdirectory(title="Save zip in", initialdir=self.output_dir.get())
        if path:
            self.output_dir.set(path)

    def start(self) -> None:
        if self.worker is not None:
            return
        source = self.source.get().strip()
        if not source:
            messagebox.showwarning("Torrent Convertor", "Choose a .torrent file or paste a magnet link.")
            return
        output_dir = self.output_dir.get().strip()
        if not output_dir:
            messagebox.showwarning("Torrent Convertor", "Choose a folder to save the zip in.")
            return

        self.cancel.clear()
        self.result = None
        self.name.set("-")
        self.stats.set("")
        self.progress_bar["value"] = 0
        self._set_running(True)
        self._log(f"Starting: {source}")
        self.worker = threading.Thread(
            target=self._work,
            args=(source, Path(output_dir), self.compression.get(), self.keep_files.get()),
            daemon=True,
        )
        self.worker.start()

    def request_cancel(self) -> None:
        if self.worker is not None:
            self.cancel.set()
            self.status.set("Cancelling...")
            self.cancel_button.state(["disabled"])

    def open_output(self) -> None:
        target = self.result.parent if self.result else Path(self.output_dir.get())
        try:
            if sys.platform.startswith("win"):
                if self.result:
                    subprocess.Popen(["explorer", "/select,", str(self.result)])
                else:
                    os.startfile(target)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", "-R", str(self.result)] if self.result else ["open", str(target)])
            else:
                subprocess.Popen(["xdg-open", str(target)])
        except OSError as exc:
            messagebox.showerror("Torrent Convertor", f"Could not open the folder: {exc}")

    def on_close(self) -> None:
        if self.worker is None:
            self._destroy()
            return
        if not messagebox.askyesno(
            "Torrent Convertor",
            "A conversion is still running. Stop it and quit?\n\n"
            "What was already downloaded is kept, so you can continue later.",
        ):
            return
        self.closing = True
        self.request_cancel()

    # -- background work ---------------------------------------------------------

    def _work(self, source: str, output_dir: Path, compression: str, keep_files: bool) -> None:
        try:
            from .converter import ConvertOptions, convert
            from .engine import Session
        except ImportError as exc:
            self.events.put(
                ("error", f"libtorrent could not be loaded ({exc}).\nInstall it with: pip install libtorrent")
            )
            return
        options = ConvertOptions(
            output_dir=output_dir,
            keep_files=keep_files,
            compression=compression,
            peers=self.peers,
        )
        try:
            with Session(self.session_config) as session:
                zip_path = convert(
                    session,
                    source,
                    options,
                    on_progress=lambda p: self.events.put(("progress", p)),
                    cancel=self.cancel,
                )
                # Report before the session shuts down, which can take a moment.
                self.events.put(("done", zip_path))
        except Cancelled:
            self.events.put(("cancelled", None))
        except (ConvertorError, OSError) as exc:
            self.events.put(("error", str(exc)))
        except Exception as exc:  # report anything unexpected instead of failing silently
            log.debug("Unexpected error", exc_info=True)
            self.events.put(("error", f"Unexpected error: {exc!r}"))

    def _poll_events(self) -> None:
        latest: Optional[Progress] = None
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "progress":
                    latest = value
                    continue
                if latest is not None:
                    self._show_progress(latest)
                    latest = None
                self._handle_event(kind, value)
                if self.destroyed:
                    return
        except queue.Empty:
            pass
        if self.destroyed:
            return
        if latest is not None:
            self._show_progress(latest)
        self.root.after(POLL_MS, self._poll_events)

    def _handle_event(self, kind: str, value) -> None:
        if kind == "log":
            self._log(value)
            return
        self.worker = None
        self._set_running(False)
        if kind == "done":
            self.result = value
            self.status.set("Done")
            self.stats.set(f"Saved {value}")
            self.progress_bar["value"] = 1000
            self.open_button.state(["!disabled"])
        elif kind == "cancelled":
            self.status.set("Cancelled")
            self._log("Cancelled. What was already downloaded is kept; convert again to continue.")
        elif kind == "error":
            self.status.set("Failed")
            self._log(f"Error: {value}")
            if not self.closing:
                messagebox.showerror("Torrent Convertor", value)
        if self.closing:
            self._destroy()

    def _show_progress(self, progress: Progress) -> None:
        if progress.name:
            self.name.set(progress.name)
        stage = progress.stage
        if stage in (Stage.CHECKING, Stage.DOWNLOADING, Stage.ZIPPING):
            self.status.set(f"{stage.value}  {progress.fraction * 100:.1f}%")
            self.progress_bar["value"] = int(progress.fraction * 1000)
        elif not self.cancel.is_set():
            self.status.set(stage.value)
        if stage is Stage.DOWNLOADING:
            self.stats.set(
                f"{format_size(progress.done)} of {format_size(progress.total)}    "
                f"Down {format_rate(progress.download_rate)}    "
                f"Up {format_rate(progress.upload_rate)}    "
                f"Peers {progress.peers} ({progress.seeds} seeds)    "
                f"ETA {format_eta(progress.eta)}"
            )
        elif stage is Stage.METADATA:
            self.stats.set(f"Asking peers for the file list... {progress.peers} peers connected")
        elif stage in (Stage.CHECKING, Stage.ZIPPING):
            self.stats.set(f"{format_size(progress.done)} of {format_size(progress.total)}")

    # -- helpers -----------------------------------------------------------------

    def _set_running(self, running: bool) -> None:
        idle = "disabled" if running else "!disabled"
        for widget in (self.start_button, self.browse_button, self.output_button, self.keep_check):
            widget.state([idle])
        for entry in (self.source_entry, self.output_entry):
            entry.state([idle])
        self.compression_box.state(["disabled"] if running else ["!disabled", "readonly"])
        self.cancel_button.state(["!disabled"] if running else ["disabled"])
        if running:
            self.open_button.state(["disabled"])

    def _log(self, message: str) -> None:
        self.log_box.configure(state="normal")
        self.log_box.insert("end", message + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _destroy(self) -> None:
        logging.getLogger("torrent_convertor").removeHandler(self.log_handler)
        self.destroyed = True
        self.root.destroy()


def main(argv: Optional[Sequence[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if sys.platform.startswith("win"):
        try:  # sharp text on high-DPI screens
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass
    root = tk.Tk()
    App(root, source=argv[0] if argv else "")
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
