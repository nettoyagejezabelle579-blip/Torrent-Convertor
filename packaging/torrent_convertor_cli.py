"""Entry point for building a standalone command line executable with PyInstaller."""

import sys

from torrent_convertor.cli import main

if __name__ == "__main__":
    sys.exit(main())
