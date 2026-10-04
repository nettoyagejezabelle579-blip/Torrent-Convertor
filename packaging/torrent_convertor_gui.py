"""Entry point for building a standalone desktop app with PyInstaller."""

import sys

from torrent_convertor.gui import main

if __name__ == "__main__":
    sys.exit(main())
