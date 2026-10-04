# Torrent-Convertor

Turn a torrent into a ZIP file. Give it a `.torrent` file or a magnet link, and it
downloads the torrent's files and packs them into a single `.zip` file.

Downloading is done by [libtorrent](https://libtorrent.org), the same engine that
qBittorrent and Deluge use. It supports DHT, magnet links, peer exchange, local peer
discovery, UPnP/NAT-PMP port forwarding, encryption and uTP.

- Works with `.torrent` files, magnet links and `http(s)://` links to `.torrent` files
- Desktop window (like a tiny uTorrent) and a command line tool
- Shows live progress, speed, peers and time remaining
- Lets you pick which files to download (`--include "*.mkv"`, `--exclude "*sample*"`)
- Resumes interrupted downloads. Run the same conversion again and it continues where it stopped.
- Fast by default: it connects to peers over TCP, which was several times faster than
  uTP in our tests. Text and documents are compressed with a fast setting. Videos,
  music, images, archives and any other file that doesn't shrink are stored as they
  are, since re-compressing them only wastes time. Use `--compression deflate` for a
  smaller (but slower) zip.
- Never overwrites an existing zip (`Name.zip` becomes `Name (1).zip`), and never
  leaves a half-written zip behind.
- Cleans up after itself: only the zip is left at the end (unless you ask to keep the
  downloaded files).

## Install

You need **Python 3.10 to 3.13** (libtorrent doesn't publish packages for newer Python
versions yet).

```sh
pip install git+https://github.com/nettoyagejezabelle579-blip/Torrent-Convertor.git
```

This installs two commands: `torrent-convertor-gui` (the window) and
`torrent-convertor` (the command line tool).

**Without Python:** every push builds standalone apps for Windows, macOS and Linux.
Open the repository's **Actions** tab, click the latest successful **CI** run, and
download `torrent-convertor-Windows` (or `-macOS` / `-Linux`) under **Artifacts**.
These builds are unsigned, so Windows SmartScreen or macOS Gatekeeper may ask you to
confirm before they run.

## Desktop app

```sh
torrent-convertor-gui
```

1. Click **Open .torrent...**, or paste a magnet link into the **Torrent** box.
2. Choose where to save the zip.
3. Click **Convert to ZIP**.

When it's done, **Show zip in folder** opens the folder containing the zip.

## Command line

```sh
# A .torrent file -> MyTorrent.zip in the current folder
torrent-convertor MyTorrent.torrent

# A magnet link (keep the quotes), saving the zip in another folder
torrent-convertor "magnet:?xt=urn:btih:..." -o ~/Downloads

# See what's inside before downloading
torrent-convertor MyTorrent.torrent --list

# Only the .mkv files, but no samples
torrent-convertor MyTorrent.torrent --include "*.mkv" --exclude "*sample*"

# Keep the downloaded files next to the zip, no compression, limit speed to 2 MiB/s
torrent-convertor MyTorrent.torrent --keep-files --compression store --max-download 2048
```

You can pass several torrents at once; they are converted one after another. When a
conversion finishes, the zip's path is printed. Press **Ctrl+C** to stop. What was
already downloaded is kept, and running the same command again resumes from there.

| Option | What it does |
| --- | --- |
| `-o, --output-dir DIR` | Where to save the zip (default: current folder) |
| `-k, --keep-files` | Keep the downloaded files after zipping |
| `--download-dir DIR` | Download into this folder and keep the files there. Files it already has are reused. |
| `--list` | Only list the torrent's files |
| `-i, --include PATTERN` | Only download matching files (repeatable) |
| `-x, --exclude PATTERN` | Skip matching files (repeatable) |
| `-c, --compression` | `auto` (default, fast), `deflate` (smaller), `store`, `bzip2` or `lzma` |
| `--level 0-9` | Compression level for deflate/bzip2 |
| `--port N` | Port for incoming connections (default 6881, `0` = random) |
| `--max-download`, `--max-upload` | Speed limits in KiB/s |
| `--peer HOST:PORT` | Also connect to a specific peer |
| `--no-dht`, `--no-lsd`, `--no-upnp` | Turn off DHT / local peer discovery / port forwarding |
| `-q, --quiet` / `-v, --verbose` | Less / more output |

Patterns match the end of a file's path and ignore case. For a file
`Show/Season 1/ep1.mkv`, all of these match: `*.mkv`, `ep1.mkv`, `Season 1/*`, `Show/*`.

## How it works

1. The torrent is downloaded into a hidden work folder, `.torrent-convertor`, inside
   the output folder. Every piece is checked against the torrent's hashes.
2. The files are written into `Name.zip.part`, which is renamed to `Name.zip` once
   it's complete. Inside the zip, files keep the same folder layout the torrent has.
3. The work folder is deleted, so only the zip remains. With `--keep-files` the
   files are downloaded straight into the output folder and kept. With
   `--download-dir` they go into that folder and are always kept.

Because the files and the zip exist at the same time for a while, you need free disk
space for about **twice** the torrent's size.

Only download content that you have the right to download and share. Like any torrent
client, Torrent-Convertor uploads pieces to other peers while it downloads.

## Development

```sh
python -m venv .venv
.venv/bin/pip install -e ".[test]"     # Windows: .venv\Scripts\pip ...
.venv/bin/python -m pytest
```

The tests run real downloads between libtorrent sessions on `127.0.0.1`, so they don't
need an internet connection. The GUI tests are skipped when no display is available.
