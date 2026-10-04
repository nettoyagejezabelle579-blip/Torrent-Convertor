"""Smoke tests for the desktop window. Skipped when Tk or a display is unavailable."""

from __future__ import annotations

import time
import zipfile

import pytest

tk = pytest.importorskip("tkinter")

from torrent_convertor import gui  # noqa: E402

from .conftest import LOCAL_ONLY, SAMPLE_FILES  # noqa: E402


@pytest.fixture
def root():
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"no display: {exc}")
    yield root
    try:
        root.destroy()
    except tk.TclError:
        pass  # the app already closed the window


@pytest.fixture
def errors(monkeypatch):
    shown = []
    monkeypatch.setattr(gui.messagebox, "showerror", lambda title, message: shown.append(message))
    return shown


def run_until_idle(root, app, timeout=60):
    deadline = time.monotonic() + timeout
    while app.worker is not None:
        assert time.monotonic() < deadline, "conversion did not finish"
        root.update()
        time.sleep(0.02)
    root.update()


def test_convert_from_window(root, seeder, torrent_file, output_dir, errors):
    app = gui.App(root, str(torrent_file), session_config=LOCAL_ONLY, peers=[seeder.address])
    app.output_dir.set(str(output_dir))
    app.start()
    assert app.start_button.instate(["disabled"])
    run_until_idle(root, app)

    assert errors == []
    assert app.status.get() == "Done"
    assert app.result == output_dir.resolve() / "MyData.zip"
    assert app.open_button.instate(["!disabled"])
    with zipfile.ZipFile(app.result) as archive:
        assert len(archive.namelist()) == len(SAMPLE_FILES)
    assert "Created" in app.log_box.get("1.0", "end")


def test_error_is_shown(root, tmp_path, errors):
    app = gui.App(root, str(tmp_path / "missing.torrent"), session_config=LOCAL_ONLY)
    app.output_dir.set(str(tmp_path / "out"))
    app.start()
    run_until_idle(root, app)

    assert app.status.get() == "Failed"
    assert errors and "not found" in errors[0]
    assert app.start_button.instate(["!disabled"])


def test_cancel_from_window(root, slow_seeder, torrent_file, output_dir, errors):
    app = gui.App(root, str(torrent_file), session_config=LOCAL_ONLY, peers=[slow_seeder.address])
    app.output_dir.set(str(output_dir))
    app.start()
    deadline = time.monotonic() + 30
    while not app.status.get().startswith("Downloading"):
        assert time.monotonic() < deadline
        root.update()
        time.sleep(0.02)
    app.request_cancel()
    run_until_idle(root, app)

    assert app.status.get() == "Cancelled"
    assert errors == []
    assert not list(output_dir.glob("*.zip"))
